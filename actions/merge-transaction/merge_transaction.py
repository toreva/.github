#!/usr/bin/env python3
"""Provider-level GitHub merge transaction primitive.

Policy stays in the caller. This module owns only GitHub provider truth:
- revoke: exact-head observe -> dequeue -> disable auto-merge -> verify absent
- retire: revoke -> close -> verify CLOSED/unmerged/unarmed
- arm: live exact-head/state re-read -> reject stale-base/replay/zero-effect ->
  optional label/title checks -> arm native auto-merge with compare-and-swap head ->
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


def _rest_pr_for_projection(pr: PullRequestRef, head: str, attempts: int = 4) -> dict[str, Any]:
    """Read GitHub's prospective test merge, waiting only for mergeability computation.

    GitHub computes `merge_commit_sha` for the repository state that would result
    if this PR merged into its current base. That state, not the merge-base file
    list, is the authoritative no-op predicate for squash/rebase history.
    """
    for attempt in range(1, attempts + 1):
        raw = run_gh(["api", f"repos/{pr.slug}/pulls/{pr.number}"])
        payload = load_json(raw, "pr_projection_discovery_failed")
        if not isinstance(payload, dict):
            raise TxnError("pr_projection_discovery_failed:unexpected_shape")
        provider_head = payload.get("head")
        provider_head_sha = provider_head.get("sha") if isinstance(provider_head, dict) else None
        if provider_head_sha != head:
            raise TxnError(f"head_moved:expected={head}:actual={provider_head_sha or 'EMPTY'}")
        mergeable = payload.get("mergeable")
        if mergeable is None:
            if attempt < attempts:
                time.sleep(0.25 * attempt)
                continue
            raise TxnError("mergeability_unresolved")
        if mergeable is not True:
            raise TxnError("pr_not_mergeable")
        merge_sha = payload.get("merge_commit_sha")
        base = payload.get("base")
        base_sha = base.get("sha") if isinstance(base, dict) else None
        if not isinstance(merge_sha, str) or not merge_sha:
            raise TxnError("test_merge_oid_missing")
        if not isinstance(base_sha, str) or not base_sha:
            raise TxnError("base_oid_missing")
        return payload
    raise TxnError("mergeability_unresolved")


def _assert_live_base_ancestry(pr: PullRequestRef, head: str, projection: dict[str, Any]) -> None:
    """Require the exact head to contain GitHub's current base before authorization.

    A workflow run can start after the base moved while still testing an older
    event/merge ref. Provider admission therefore proves live base ancestry
    directly instead of inferring freshness from run timestamps.
    """
    base = projection.get("base")
    base_sha = base.get("sha") if isinstance(base, dict) else None
    base_ref = base.get("ref") if isinstance(base, dict) else None
    if not isinstance(base_sha, str) or not base_sha:
        raise TxnError("base_oid_missing")

    raw = run_gh(["api", f"repos/{pr.slug}/compare/{base_sha}...{head}"])
    comparison = load_json(raw, "live_base_compare_failed")
    if not isinstance(comparison, dict):
        raise TxnError("live_base_compare_failed:unexpected_shape")

    behind = comparison.get("behind_by")
    merge_base = comparison.get("merge_base_commit")
    merge_base_sha = merge_base.get("sha") if isinstance(merge_base, dict) else None
    if behind != 0 or merge_base_sha != base_sha:
        raise TxnError(
            "stale_base:"
            f"base_ref={base_ref or 'unknown'}:"
            f"base={base_sha}:head={head}:"
            f"behind={behind if isinstance(behind, int) else 'unknown'}:"
            f"merge_base={merge_base_sha or 'missing'}"
        )


def _commit_tree(pr: PullRequestRef, commit_sha: str, code: str) -> str:
    raw = run_gh(["api", f"repos/{pr.slug}/git/commits/{commit_sha}"])
    payload = load_json(raw, code)
    if not isinstance(payload, dict):
        raise TxnError(f"{code}:unexpected_shape")
    tree = payload.get("tree")
    tree_sha = tree.get("sha") if isinstance(tree, dict) else None
    if not isinstance(tree_sha, str) or not tree_sha:
        raise TxnError(f"{code}:tree_oid_missing")
    return tree_sha


def _assert_branch_head_not_previously_consumed(pr: PullRequestRef, head: str, projection: dict[str, Any]) -> None:
    """Close the common branch-reuse seam that commit association cannot guarantee.

    Commit association remains useful, but squash merges do not make the source
    commit an ancestor of default-branch history. A second provider index over the
    current head label catches a deleted/recreated branch replay such as #2635 ->
    #2636, while still comparing the immutable source SHA before refusing.
    """
    head_info = projection.get("head")
    head_label = head_info.get("label") if isinstance(head_info, dict) else None
    if not isinstance(head_label, str) or not head_label:
        return
    raw = run_gh([
        "api", "--method", "GET", f"repos/{pr.slug}/pulls",
        "-f", "state=closed", "-f", f"head={head_label}", "-f", "per_page=100",
    ])
    history = load_json(raw, "head_branch_history_failed")
    if not isinstance(history, list):
        raise TxnError("head_branch_history_failed:unexpected_shape")
    for candidate in history:
        if not isinstance(candidate, dict) or candidate.get("number") == pr.number:
            continue
        candidate_head = candidate.get("head")
        candidate_sha = candidate_head.get("sha") if isinstance(candidate_head, dict) else None
        merged_at = candidate.get("merged_at")
        if candidate_sha == head and isinstance(merged_at, str) and merged_at:
            raise TxnError(f"consumed_head_replay:pr={candidate.get('number')}:head={head}")


def assert_arm_provider_admission(pr: PullRequestRef, head: str) -> None:
    """Refuse stale-base, provider-observed replay, or a zero-effect merge."""
    # Defense in depth: GitHub's commit association is useful but is not treated
    # as the sole historical ledger because its results depend on repository
    # reachability and merge topology.
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

    projection = _rest_pr_for_projection(pr, head)
    _assert_live_base_ancestry(pr, head, projection)
    _assert_branch_head_not_previously_consumed(pr, head, projection)

    base = projection.get("base") or {}
    base_sha = base.get("sha") if isinstance(base, dict) else None
    merge_sha = projection.get("merge_commit_sha")
    assert isinstance(base_sha, str) and isinstance(merge_sha, str)
    base_tree = _commit_tree(pr, base_sha, "base_tree_discovery_failed")
    merge_tree = _commit_tree(pr, merge_sha, "test_merge_tree_discovery_failed")
    if base_tree == merge_tree:
        raise TxnError(f"zero_effect_merge:base={base_sha}:tree={base_tree}")


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
