#!/usr/bin/env python3
"""Canonical public entrypoint for Toreva's shared merge provider.

`merge_transaction.py` remains the mature provider-state engine. This facade is
both CLI- and import-compatible with that engine, and owns the cross-cutting
admissions every live landing path must share:

* daemon fallback provenance must be coherent before autonomous arm;
* a source vehicle must not also mutate issue terminal state via GitHub closing
  keywords; and
* a live STOP control can persist a merge hold before revoking queue/auto-merge;
* arm refuses a PR that carries any hold label, and refuses to re-arm/re-queue a
  PR whose latest merge-queue run failed on the same head (TCTC-33 queue
  re-entry breaker: 2,127 of 2,410 historical merge-queue failures were repeats).

`admit` exposes the terminal-separation check without provider mutation so repos
whose actual landing authority is GitHub merge queue can reuse the same rule.
Revoke/retire/hold deliberately bypass arm admissions so rollback and emergency
containment stay available.
"""
from __future__ import annotations

import argparse
import importlib.util
import re
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

ROOT = Path(__file__).resolve().parent
HOLD_LABEL = "merge-hold"
HOLD_LABEL_COLOR = "B60205"
HOLD_LABEL_DESCRIPTION = "Machine-enforced merge hold; explicit release required"
# Same convention as goblin_infra scripts/fleet-arm-gate.py: any label whose name
# contains "hold" is a hold (merge-hold, legal-hold, ...). Refusal only.
ANY_HOLD_LABEL_RE = re.compile(r"hold", re.IGNORECASE)
MERGE_QUEUE_REF_RE = re.compile(r"^gh-readonly-queue/[^/]+/pr-(\d+)-")

# GitHub recognizes these keyword/reference shapes and closes linked issues when
# the PR reaches the default branch. Source merge is not operational completion,
# so every machine landing path must keep the two state transitions separate.
ISSUE_AUTOCLOSE_RE = re.compile(
    r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+"
    r"(?:#\d+|[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+#\d+|"
    r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/issues/\d+)\b",
    re.IGNORECASE,
)


def _load_sibling(name: str, filename: str) -> ModuleType:
    path = ROOT / filename
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"shared_provider_dependency_unloadable:{filename}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_core = _load_sibling("toreva_merge_transaction_core", "merge_transaction.py")
_provenance = _load_sibling("toreva_merge_fallback_provenance", "fallback_provenance.py")

# Preserve the mature module API so existing import-based consumers can switch
# from the internal engine to this public facade without a calling-convention fork.
TxnError = _core.TxnError
PullRequestRef = _core.PullRequestRef
parse_pr_url = _core.parse_pr_url
query_pr = _core.query_pr
revoke = _core.revoke
retire = _core.retire
emit = _core.emit


def assert_issue_terminal_separation(pr: Any, expected: str) -> None:
    """Refuse GitHub auto-close side effects before a source landing.

    Re-read REST provider truth rather than trusting the workflow event body.
    The head compare-and-swap is repeated so a moved head cannot borrow an earlier
    admissible body read.
    """
    raw = _core.run_gh(["api", f"repos/{pr.slug}/pulls/{pr.number}"])
    payload = _core.load_json(raw, "pr_terminal_separation_discovery_failed")
    if not isinstance(payload, dict):
        raise TxnError("pr_terminal_separation_discovery_failed:unexpected_shape")

    head = payload.get("head")
    live_head = head.get("sha") if isinstance(head, dict) else None
    if live_head != expected:
        raise TxnError(f"head_moved:expected={expected}:actual={live_head or 'EMPTY'}")

    body = payload.get("body")
    if body is None:
        return
    if not isinstance(body, str):
        raise TxnError("pr_terminal_separation_discovery_failed:body_not_string")
    if ISSUE_AUTOCLOSE_RE.search(body):
        raise TxnError("issue_autoclose_keyword_forbidden:source_merge_is_not_terminal")


def admit(pr: Any, expected: str) -> None:
    """Run shared non-mutating source/terminal admission on an exact live head."""
    assert_issue_terminal_separation(pr, expected)


def _persist_hold_label(pr: Any) -> None:
    """Provision and apply the fleet-standard merge-hold label idempotently."""
    _core.run_gh(
        [
            "label",
            "create",
            HOLD_LABEL,
            "--repo",
            pr.slug,
            "--color",
            HOLD_LABEL_COLOR,
            "--description",
            HOLD_LABEL_DESCRIPTION,
            "--force",
        ]
    )
    raw = _core.run_gh(
        [
            "api",
            "--method",
            "POST",
            f"repos/{pr.slug}/issues/{pr.number}/labels",
            "-f",
            f"labels[]={HOLD_LABEL}",
        ]
    )
    payload = _core.load_json(raw, "merge_hold_label_write_failed")
    if not isinstance(payload, list):
        raise TxnError("merge_hold_label_write_failed:unexpected_shape")


def hold(pr: Any, expected: str) -> tuple[str, dict[str, Any]]:
    """Persist a merge hold, then revoke all live merge authorization.

    The durable label is written before dequeue/disable. If provider revocation
    later fails, the existing fleet hold reconciler can still observe the label
    and retry. Every phase is exact-head checked so a STOP for one source cannot
    silently attach authority to a moved PR head.
    """
    before = query_pr(pr)
    head = _core.exact_head(before, expected)
    _core.assert_unmerged(before, "before_hold")

    _persist_hold_label(pr)

    labeled = query_pr(pr)
    _core.exact_head(labeled, head)
    _core.assert_unmerged(labeled, "after_hold_label")
    if HOLD_LABEL not in _core.label_names(labeled):
        raise TxnError("merge_hold_label_unverified")

    head, fields = revoke(pr, head)
    final = query_pr(pr)
    _core.exact_head(final, head)
    _core.assert_unmerged(final, "after_hold_revoke")
    if HOLD_LABEL not in _core.label_names(final):
        raise TxnError("merge_hold_label_lost")
    if isinstance(final.get("mergeQueueEntry"), dict):
        raise TxnError("merge_hold_queue_revocation_unverified")
    if isinstance(final.get("autoMergeRequest"), dict):
        raise TxnError("merge_hold_auto_merge_revocation_unverified")
    return head, {**fields, "merge_hold_label": HOLD_LABEL}


def assert_no_hold_label(pr: Any, expected: str) -> None:
    """Refuse arm while any hold label is live on the exact head (refusal only)."""
    snapshot = query_pr(pr)
    _core.exact_head(snapshot, expected)
    held = sorted(name for name in _core.label_names(snapshot) if ANY_HOLD_LABEL_RE.search(name))
    if held:
        raise TxnError(f"hold_label_present:{','.join(held)}")


def queue_reentry_blocker(
    number: int,
    head: str,
    runs: list[dict[str, Any]],
    queue_commit_parents: dict[str, list[str]],
    head_committed_at: str | None,
) -> str | None:
    """Pure decision for the queue re-entry breaker.

    Returns a blocker code, or None when arming may proceed. Only the latest
    *completed* merge_group run for this PR matters. It blocks when that run
    concluded failure and the head is unchanged since: proven by the queue
    commit's parents containing the head, or by the head commit predating the
    failed run. When neither can be shown the answer is a refusal, because
    unknown is not zero.
    """
    mine = []
    for run in runs:
        if not isinstance(run, dict):
            continue
        match = MERGE_QUEUE_REF_RE.match(str(run.get("head_branch") or ""))
        if match and int(match.group(1)) == number and run.get("status") == "completed":
            mine.append(run)
    if not mine:
        return None
    last = max(mine, key=lambda run: str(run.get("created_at") or ""))
    if last.get("conclusion") != "failure":
        return None
    run_id = last.get("id")
    created = str(last.get("created_at") or "")
    parents = queue_commit_parents.get(str(last.get("head_sha") or ""))
    if parents is not None and head in parents:
        return f"queue_reentry_refused:last_merge_group_run={run_id}:failed_on_same_head={head}"
    if head_committed_at and created:
        if head_committed_at <= created:
            return f"queue_reentry_refused:last_merge_group_run={run_id}:head_predates_failure={head}"
        return None
    return f"queue_reentry_unproven:last_merge_group_run={run_id}:head={head}"


def assert_no_queue_reentry(pr: Any, expected: str) -> None:
    """Refuse re-arming a PR whose latest merge-queue run failed on this head.

    Reads are provider truth via the same governed gh transport. A read failure
    raises TxnError (refusal); it never arms on an unknown history.
    """
    raw = _core.run_gh(["api", f"repos/{pr.slug}/actions/runs?event=merge_group&per_page=100"])
    payload = _core.load_json(raw, "queue_reentry_history_unreadable")
    runs = payload.get("workflow_runs") if isinstance(payload, dict) else None
    if not isinstance(runs, list):
        raise TxnError("queue_reentry_history_unreadable:unexpected_shape")
    parents: dict[str, list[str]] = {}
    mine = [
        run for run in runs
        if isinstance(run, dict)
        and (m := MERGE_QUEUE_REF_RE.match(str(run.get("head_branch") or "")))
        and int(m.group(1)) == pr.number
        and run.get("status") == "completed"
    ]
    if not mine:
        return
    last = max(mine, key=lambda run: str(run.get("created_at") or ""))
    if last.get("conclusion") != "failure":
        return
    queue_sha = str(last.get("head_sha") or "")
    if queue_sha:
        commit = _core.load_json(_core.run_gh(["api", f"repos/{pr.slug}/commits/{queue_sha}"]), "queue_commit_unreadable")
        if isinstance(commit, dict):
            parents[queue_sha] = [p.get("sha") for p in commit.get("parents") or [] if isinstance(p, dict)]
    head_commit = _core.load_json(_core.run_gh(["api", f"repos/{pr.slug}/commits/{expected}"]), "head_commit_unreadable")
    committed = None
    if isinstance(head_commit, dict):
        committed = ((head_commit.get("commit") or {}).get("committer") or {}).get("date")
    blocker = queue_reentry_blocker(pr.number, expected, runs, parents, committed)
    if blocker:
        raise TxnError(blocker)


def arm(pr: Any, expected: str, required_label: str, reject_title_prefix: str) -> None:
    admit(pr, expected)
    try:
        _provenance.admit(pr.url, expected)
    except _provenance.AdmissionError as exc:
        raise TxnError(f"fallback_provenance_refused:{exc}") from exc
    # TCTC-33 refusal-only gates: after the existing admissions, before any arm mutation.
    assert_no_hold_label(pr, expected)
    assert_no_queue_reentry(pr, expected)
    _core.arm(pr, expected, required_label, reject_title_prefix)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("operation", choices=["hold", "revoke", "retire", "admit", "arm"])
    parser.add_argument("pr_url")
    parser.add_argument("--expected-head", required=True)
    parser.add_argument("--required-label", default="")
    parser.add_argument("--reject-title-prefix", default="[SUPERSEDED")
    args = parser.parse_args(argv)
    try:
        pr = parse_pr_url(args.pr_url)
        if args.operation == "hold":
            head, fields = hold(pr, args.expected_head)
            emit("hold", "held", pr, head, **fields)
        elif args.operation == "revoke":
            head, fields = revoke(pr, args.expected_head)
            emit("revoke", "revoked", pr, head, **fields)
        elif args.operation == "retire":
            retire(pr, args.expected_head)
        elif args.operation == "admit":
            admit(pr, args.expected_head)
            emit("admit", "admitted", pr, args.expected_head)
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
