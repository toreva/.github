#!/usr/bin/env python3
"""Provider-level GitHub merge transaction primitive.

Policy stays in the caller. This module owns only GitHub provider truth:
- revoke: exact-head observe -> dequeue -> disable auto-merge -> verify absent
- retire: revoke -> close -> verify CLOSED/unmerged/unarmed
- arm: live exact-head/state re-read -> reject replay/zero-delta -> optional
  label/title checks -> arm native auto-merge with compare-and-swap head ->
  verify merged or armed on same head

Exit 0 means the requested provider state was re-observed. Exit 4 is a typed
fail-closed refusal. No operation force-pushes, changes branch protection, or
interprets product/business authority.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Any

PR_URL_RE = re.compile(r"^https://github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+)/pull/(?P<number>[1-9][0-9]*)/?$")
QUERY = """
query MergeTxn($owner: String!, $repo: String!, $number: Int!) {
  repository(owner: $owner, name: $repo) {
    pullRequest(number: $number) {
      id number state merged isDraft title headRefOid
      labels(first: 100) { nodes { name } }
      autoMergeRequest { enabledAt mergeMethod }
      mergeQueueEntry { id state enqueuedAt headCommit { oid } }
    }
  }
}
"""
DEQUEUE = """
mutation DequeueMergeTxn($pullRequestId: ID!) {
  dequeuePullRequest(input: { id: $pullRequestId }) {
    mergeQueueEntry { id state }
  }
}
"""
DISABLE = """
mutation DisableMergeTxn($pullRequestId: ID!) {
  disablePullRequestAutoMerge(input: { pullRequestId: $pullRequestId }) {
    pullRequest { id number state merged headRefOid }
  }
}
"""


class TxnError(RuntimeError):
    pass


@dataclass(frozen=True)
class PullRequestRef:
    owner: str
    repo: str
    number: int
    url: str

    @property
    def slug(self) -> str:
        return f"{self.owner}/{self.repo}"


def parse_pr_url(value: str) -> PullRequestRef:
    match = PR_URL_RE.match(value.strip())
    if not match:
        raise TxnError("invalid_pr_url")
    return PullRequestRef(match.group("owner"), match.group("repo"), int(match.group("number")), value.rstrip("/"))


def run_gh(args: list[str], attempts: int = 3) -> str:
    last = ""
    for attempt in range(1, attempts + 1):
        result = subprocess.run(["gh", *args], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
        if result.returncode == 0:
            return result.stdout
        last = (result.stderr or result.stdout or "gh_failed").strip()[:400]
        if attempt < attempts:
            time.sleep(0.2 * attempt)
    raise TxnError(f"github_transport_failed:{last}")


def load_json(raw: str, code: str) -> Any:
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise TxnError(f"{code}:invalid_json") from exc


def query_pr(pr: PullRequestRef) -> dict[str, Any]:
    raw = run_gh([
        "api", "graphql", "-f", f"query={QUERY}",
        "-F", f"owner={pr.owner}", "-F", f"repo={pr.repo}", "-F", f"number={pr.number}",
    ])
    payload = load_json(raw, "github_graphql_failed")
    if payload.get("errors"):
        raise TxnError(f"github_graphql_failed:{payload['errors'][0].get('message', 'graphql_error')}")
    value = payload.get("data", {}).get("repository", {}).get("pullRequest")
    if not isinstance(value, dict):
        raise TxnError("pull_request_not_found")
    return value


def mutate(mutation: str, pr_id: str) -> None:
    raw = run_gh(["api", "graphql", "-f", f"query={mutation}", "-F", f"pullRequestId={pr_id}"])
    payload = load_json(raw, "github_graphql_mutation_failed")
    if payload.get("errors"):
        raise TxnError(f"github_graphql_mutation_failed:{payload['errors'][0].get('message', 'graphql_error')}")


def exact_head(snapshot: dict[str, Any], expected: str) -> str:
    head = snapshot.get("headRefOid")
    if not isinstance(head, str) or not head:
        raise TxnError("head_oid_missing")
    if head != expected:
        raise TxnError(f"head_moved:expected={expected}:actual={head}")
    return head


def assert_unmerged(snapshot: dict[str, Any], phase: str) -> None:
    if snapshot.get("merged") is True or snapshot.get("state") == "MERGED":
        raise TxnError(f"already_or_raced_merged:{phase}")


def label_names(snapshot: dict[str, Any]) -> set[str]:
    nodes = snapshot.get("labels", {}).get("nodes", [])
    return {str(node.get("name")) for node in nodes if isinstance(node, dict) and node.get("name")}


def assert_arm_provider_admission(pr: PullRequestRef, head: str) -> None:
    """Refuse provider-observed replay or empty work before merge authorization.

    GitHub itself is the ledger. This makes the invariant available to every
    repository consuming the shared action, including PRs opened outside any
    daemon-specific landing path.
    """
    raw = run_gh(["api", f"repos/{pr.slug}/commits/{head}/pulls"])
    associated = load_json(raw, "head_owner_discovery_failed")
    if not isinstance(associated, list):
        raise TxnError("head_owner_discovery_failed:unexpected_shape")
    for candidate in associated:
        if not isinstance(candidate, dict) or candidate.get("number") == pr.number:
            continue
        candidate_head = candidate.get("head")
        candidate_sha = candidate_head.get("sha") if isinstance(candidate_head, dict) else None
        if candidate_sha != head:
            continue
        merged_at = candidate.get("merged_at")
        if isinstance(merged_at, str) and merged_at:
            raise TxnError(f"consumed_head_replay:pr={candidate.get('number')}:head={head}")
        if str(candidate.get("state", "")).lower() == "open":
            raise TxnError(f"duplicate_live_head_owner:pr={candidate.get('number')}:head={head}")

    files_raw = run_gh(["api", f"repos/{pr.slug}/pulls/{pr.number}/files?per_page=1"])
    files = load_json(files_raw, "pr_delta_discovery_failed")
    if not isinstance(files, list):
        raise TxnError("pr_delta_discovery_failed:unexpected_shape")
    if not files:
        raise TxnError("zero_delta_pr")


def emit(operation: str, status: str, pr: PullRequestRef, head: str, **fields: Any) -> None:
    prefix = "Merge-Transaction"
    print(f"{prefix}-Operation: {operation}")
    print(f"{prefix}-Status: {status}")
    print(f"{prefix}-PR: {pr.url}")
    print(f"{prefix}-Head: {head}")
    for key, value in fields.items():
        print(f"{prefix}-{key.replace('_', '-').title()}: {value}")


def revoke(pr: PullRequestRef, expected: str) -> tuple[str, dict[str, Any]]:
    before = query_pr(pr)
    head = exact_head(before, expected)
    assert_unmerged(before, "before_revoke")
    pr_id = before.get("id")
    if not isinstance(pr_id, str) or not pr_id:
        raise TxnError("pull_request_node_id_missing")

    dequeued = False
    disabled = False
    if isinstance(before.get("mergeQueueEntry"), dict):
        mutate(DEQUEUE, pr_id)
        dequeued = True

    middle = query_pr(pr)
    exact_head(middle, head)
    assert_unmerged(middle, "after_dequeue")
    if isinstance(middle.get("mergeQueueEntry"), dict):
        raise TxnError("merge_queue_revocation_unverified")

    if isinstance(middle.get("autoMergeRequest"), dict):
        mutate(DISABLE, pr_id)
        disabled = True

    final = query_pr(pr)
    exact_head(final, head)
    assert_unmerged(final, "after_disable")
    if isinstance(final.get("mergeQueueEntry"), dict):
        raise TxnError("merge_queue_revocation_unverified")
    if isinstance(final.get("autoMergeRequest"), dict):
        raise TxnError("auto_merge_revocation_unverified")
    return head, {"dequeued": str(dequeued).lower(), "auto_merge_disabled": str(disabled).lower(), "pr_state": final.get("state")}


def retire(pr: PullRequestRef, expected: str) -> None:
    head, actions = revoke(pr, expected)
    before_close = query_pr(pr)
    exact_head(before_close, head)
    assert_unmerged(before_close, "before_close")
    if isinstance(before_close.get("mergeQueueEntry"), dict) or isinstance(before_close.get("autoMergeRequest"), dict):
        raise TxnError("merge_authorization_reappeared_before_close")
    state = before_close.get("state")
    if state == "OPEN":
        raw = run_gh(["api", "--method", "PATCH", f"repos/{pr.slug}/pulls/{pr.number}", "-f", "state=closed"])
        payload = load_json(raw, "github_close_failed")
        if str(payload.get("state", "")).lower() != "closed":
            raise TxnError("close_mutation_unverified")
    elif state != "CLOSED":
        raise TxnError(f"unexpected_state_before_close:{state}")

    final = query_pr(pr)
    exact_head(final, head)
    assert_unmerged(final, "after_close")
    if final.get("state") != "CLOSED" or isinstance(final.get("mergeQueueEntry"), dict) or isinstance(final.get("autoMergeRequest"), dict):
        raise TxnError("retirement_state_unverified")
    emit("retire", "retired", pr, head, **actions, final_state="CLOSED")


def arm(pr: PullRequestRef, expected: str, required_label: str, reject_title_prefix: str) -> None:
    before = query_pr(pr)
    head = exact_head(before, expected)
    assert_unmerged(before, "before_arm")
    if before.get("state") != "OPEN":
        raise TxnError(f"pr_not_open:{before.get('state')}")
    if before.get("isDraft") is True:
        raise TxnError("pr_is_draft")
    title = str(before.get("title") or "")
    if reject_title_prefix and title.startswith(reject_title_prefix):
        raise TxnError(f"title_rejected:{reject_title_prefix}")
    if required_label and required_label not in label_names(before):
        raise TxnError(f"required_label_missing:{required_label}")

    assert_arm_provider_admission(pr, head)

    args = ["pr", "merge", pr.url, "--auto", "--squash", "--delete-branch", "--match-head-commit", head]
    run_gh(args)

    final = query_pr(pr)
    exact_head(final, head)
    if final.get("merged") is True or final.get("state") == "MERGED":
        emit("arm", "merged", pr, head, pr_state=final.get("state"))
        return
    if final.get("state") != "OPEN":
        raise TxnError(f"unexpected_state_after_arm:{final.get('state')}")
    if not isinstance(final.get("mergeQueueEntry"), dict) and not isinstance(final.get("autoMergeRequest"), dict):
        raise TxnError("merge_authorization_not_observed_after_arm")
    emit(
        "arm", "armed", pr, head,
        queue=str(isinstance(final.get("mergeQueueEntry"), dict)).lower(),
        auto_merge=str(isinstance(final.get("autoMergeRequest"), dict)).lower(),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("operation", choices=["revoke", "retire", "arm"])
    parser.add_argument("pr_url")
    parser.add_argument("--expected-head", required=True)
    parser.add_argument("--required-label", default="")
    parser.add_argument("--reject-title-prefix", default="[SUPERSEDED")
    args = parser.parse_args(argv)
    try:
        pr = parse_pr_url(args.pr_url)
        if args.operation == "revoke":
            head, fields = revoke(pr, args.expected_head)
            emit("revoke", "revoked", pr, head, **fields)
        elif args.operation == "retire":
            retire(pr, args.expected_head)
        else:
            arm(pr, args.expected_head, args.required_label, args.reject_title_prefix)
        return 0
    except TxnError as exc:
        print(f"Merge-Transaction-Operation: {args.operation}", file=sys.stderr)
        print("Merge-Transaction-Status: blocked", file=sys.stderr)
        print(f"Merge-Transaction-Blocker: {exc}", file=sys.stderr)
        return 4


if __name__ == "__main__":
    raise SystemExit(main())
