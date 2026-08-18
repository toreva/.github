#!/usr/bin/env python3
"""Fail closed when an auto-recovered PR claims work from the wrong dispatch.

This is a narrow provenance admission primitive for Toreva's shared merge
transaction. It only applies to the `agent-daemon.sh` fallback PR shape that is
created when a runner left an ahead branch with no open PR. Normal human/agent
PRs are untouched.

For a fallback PR, the PR body declares the dispatch filename and originating
agent. Every commit in that PR must carry matching machine-readable provenance:
`Source dispatch:` and/or the existing `Dispatched-By` / `Spawned-By` trailer.
Multiple commits are allowed when they belong to the same work identity. A
mixed, missing, or misattributed commit refuses merge authorization.

The check is intentionally provider-side. Revoke/retire remain available even
for an invalid PR; only `arm` calls this admission before merge authority is
created.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from typing import Any

FALLBACK_MARKER = (
    "Auto-PR opened by agent-daemon.sh because the runner already committed/pushed "
    "work but left no open PR for the ahead branch."
)
PR_URL_RE = re.compile(
    r"^https://github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+)/pull/(?P<number>[1-9][0-9]*)/?$"
)
DISPATCH_RE = re.compile(r"(?im)^Dispatch:\s*`?([^`\s]+)`?\s*$")
ORIGIN_RE = re.compile(r"(?im)^Original ask from:\s*`?([^`\s]+)`?\s*$")
ACTOR_RE = re.compile(r"(?im)^(?:Dispatched-By|Spawned-By):\s*(\S.*?)\s*$")
SOURCE_DISPATCH_RE = re.compile(r"(?im)^Source dispatch:\s*`?([^`\s]+)`?\s*$")


class AdmissionError(RuntimeError):
    pass


@dataclass(frozen=True)
class PullRequestRef:
    owner: str
    repo: str
    number: int

    @property
    def slug(self) -> str:
        return f"{self.owner}/{self.repo}"


def parse_pr_url(value: str) -> PullRequestRef:
    match = PR_URL_RE.match(value.strip())
    if not match:
        raise AdmissionError("invalid_pr_url")
    return PullRequestRef(match.group("owner"), match.group("repo"), int(match.group("number")))


def run_gh(args: list[str]) -> str:
    result = subprocess.run(
        ["gh", *args], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "gh_failed").strip()[:400]
        raise AdmissionError(f"github_transport_failed:{detail}")
    return result.stdout


def load_json(raw: str, code: str) -> Any:
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise AdmissionError(f"{code}:invalid_json") from exc


def canonical_actor(value: str) -> str:
    actor = value.strip().lower()
    if actor.endswith("-agent"):
        actor = actor[:-6]
    return actor


def basename(value: str) -> str:
    return value.replace("\\", "/").rsplit("/", 1)[-1].strip()


def require_one(pattern: re.Pattern[str], text: str, code: str) -> str:
    match = pattern.search(text)
    if not match:
        raise AdmissionError(code)
    return match.group(1).strip()


def commit_message(item: dict[str, Any]) -> str:
    commit = item.get("commit")
    message = commit.get("message") if isinstance(commit, dict) else None
    if not isinstance(message, str) or not message.strip():
        raise AdmissionError("commit_message_missing")
    return message


def admit(pr_url: str, expected_head: str) -> None:
    pr = parse_pr_url(pr_url)
    payload = load_json(run_gh(["api", f"repos/{pr.slug}/pulls/{pr.number}"]), "pr_read_failed")
    if not isinstance(payload, dict):
        raise AdmissionError("pr_read_failed:unexpected_shape")

    head = payload.get("head")
    head_sha = head.get("sha") if isinstance(head, dict) else None
    if head_sha != expected_head:
        raise AdmissionError(f"head_moved:expected={expected_head}:actual={head_sha or 'EMPTY'}")

    body = payload.get("body") or ""
    if not isinstance(body, str):
        body = ""
    if FALLBACK_MARKER not in body:
        print("Merge-Provenance-Status: not_applicable")
        return

    dispatch = basename(require_one(DISPATCH_RE, body, "fallback_dispatch_identity_missing"))
    origin = canonical_actor(require_one(ORIGIN_RE, body, "fallback_origin_identity_missing"))
    if not dispatch or not origin:
        raise AdmissionError("fallback_identity_empty")

    count = payload.get("commits")
    if not isinstance(count, int) or count < 1:
        raise AdmissionError("fallback_commit_count_invalid")
    if count > 100:
        raise AdmissionError(f"fallback_commit_count_unbounded:{count}")

    commits = load_json(
        run_gh(["api", f"repos/{pr.slug}/pulls/{pr.number}/commits?per_page=100"]),
        "fallback_commits_read_failed",
    )
    if not isinstance(commits, list) or len(commits) != count:
        raise AdmissionError(
            f"fallback_commit_observation_incomplete:expected={count}:observed={len(commits) if isinstance(commits, list) else 'invalid'}"
        )

    violations: list[str] = []
    for item in commits:
        if not isinstance(item, dict):
            violations.append("unknown:commit_shape_invalid")
            continue
        sha = str(item.get("sha") or "unknown")
        message = commit_message(item)
        actors = {canonical_actor(value) for value in ACTOR_RE.findall(message) if value.strip()}
        sources = {basename(value) for value in SOURCE_DISPATCH_RE.findall(message) if value.strip()}

        if not actors and not sources:
            violations.append(f"{sha}:provenance_missing")
            continue
        wrong_actors = sorted(actor for actor in actors if actor != origin)
        if wrong_actors:
            violations.append(f"{sha}:actor={','.join(wrong_actors)}:expected={origin}")
        wrong_sources = sorted(source for source in sources if source != dispatch)
        if wrong_sources:
            violations.append(f"{sha}:dispatch={','.join(wrong_sources)}:expected={dispatch}")

    if violations:
        raise AdmissionError("fallback_mixed_or_misattributed_provenance:" + ";".join(violations))

    print("Merge-Provenance-Status: admitted")
    print(f"Merge-Provenance-Dispatch: {dispatch}")
    print(f"Merge-Provenance-Origin: {origin}")
    print(f"Merge-Provenance-Commits: {count}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("pr_url")
    parser.add_argument("--expected-head", required=True)
    args = parser.parse_args(argv)
    try:
        admit(args.pr_url, args.expected_head)
        return 0
    except AdmissionError as exc:
        print("Merge-Provenance-Status: blocked", file=sys.stderr)
        print(f"Merge-Provenance-Reason: {exc}", file=sys.stderr)
        return 4


if __name__ == "__main__":
    raise SystemExit(main())
