#!/usr/bin/env python3
"""Pure trusted merge-control classifier.

Only an explicit first non-empty control line can become authority. Historical
quotes/fences and untrusted actors remain prose. The caller supplies GitHub's
author_association from the issue_comment event; no username allowlist is copied
across repositories.
"""
from __future__ import annotations

import argparse
import re

TRUSTED_ASSOCIATIONS = {"OWNER", "MEMBER", "COLLABORATOR"}
STOP_RE = re.compile(
    r"^(?:@[A-Za-z0-9_-]+\s+)?STOP-BEFORE-(MERGE|PROMOTE|DEPLOY)\b",
    re.IGNORECASE,
)
STRUCTURED = {
    "CONTROL-ACTION: MERGE-HOLD": "MERGE-HOLD",
    "MERGE-HOLD: TRUE": "MERGE-HOLD",
}


def first_control(body: str, association: str) -> str | None:
    if association.upper() not in TRUSTED_ASSOCIATIONS:
        return None
    first = next((line for line in body.splitlines() if line.strip()), "")
    raw = first.strip()
    if not raw or raw.startswith(">") or raw.startswith("```"):
        return None
    normalized = raw.lstrip("#*- ").strip().strip("`")
    stop = STOP_RE.match(normalized)
    if stop:
        return f"STOP-BEFORE-{stop.group(1).upper()}"
    return STRUCTURED.get(normalized.upper())


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--association", required=True)
    parser.add_argument("--body", required=True)
    args = parser.parse_args()
    control = first_control(args.body, args.association)
    if control:
        print(control)
        return 0
    return 3


if __name__ == "__main__":
    raise SystemExit(main())
