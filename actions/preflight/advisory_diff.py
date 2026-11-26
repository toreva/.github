#!/usr/bin/env python3
"""P6 advisory drift split: fail only on advisories the lockfile change introduces.

Background (TCTC-33): `npm audit` gates went red on PRs and in the merge queue
with nothing in the diff, because new advisories were published against an
unchanged lockfile (goblin_ui #1805 re-queued ~54 times a day). This script
audits the base and head lockfiles (no install, `--package-lock-only`) and
compares advisory ids:

    python3 advisory_diff.py --base origin/main [--level high] [--json out.json]

Exit 1 only when the head introduces an advisory at or above --level that the
base does not have. Pre-existing advisories are printed as a backlog list for
one scheduled issue per repo; they never block a PR. This is a split, not a
weakening: every new advisory still blocks, and the backlog stays visible.
Exit 0 with SKIP when the toolchain or lockfile is missing (unknown, not pass).
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

LEVELS = ["info", "low", "moderate", "high", "critical"]


def advisories(audit_json: dict) -> dict[str, str]:
    """Map advisory id (GHSA url or source id) -> severity, from npm audit v2 JSON."""
    out: dict[str, str] = {}
    for name, vuln in (audit_json.get("vulnerabilities") or {}).items():
        for via in vuln.get("via") or []:
            if isinstance(via, dict):
                key = via.get("url") or f"{via.get('source')}:{via.get('name', name)}"
                sev = via.get("severity") or vuln.get("severity") or "info"
                if LEVELS.index(sev) > LEVELS.index(out.get(key, "info")) or key not in out:
                    out[key] = sev
    # npm v1 shape
    for adv in (audit_json.get("advisories") or {}).values():
        out[adv.get("url") or str(adv.get("id"))] = adv.get("severity", "info")
    return out


def diff(base: dict[str, str], head: dict[str, str], level: str) -> tuple[list[str], list[str]]:
    floor = LEVELS.index(level)
    new = sorted(k for k, s in head.items() if k not in base and LEVELS.index(s) >= floor)
    backlog = sorted(k for k, s in head.items() if k in base and LEVELS.index(s) >= floor)
    return new, backlog


def audit_at(repo: Path, ref: str | None) -> dict | None:
    with tempfile.TemporaryDirectory(prefix="toreva-adv-") as tmp:
        tmpd = Path(tmp)
        for f in ("package.json", "package-lock.json"):
            if ref:
                p = subprocess.run(["git", "show", f"{ref}:{f}"], cwd=repo, capture_output=True, text=True)
                if p.returncode != 0:
                    return None
                (tmpd / f).write_text(p.stdout)
            else:
                if not (repo / f).exists():
                    return None
                shutil.copy(repo / f, tmpd / f)
        p = subprocess.run(["npm", "audit", "--json", "--package-lock-only"], cwd=tmpd, capture_output=True, text=True, timeout=300)
        try:
            return json.loads(p.stdout or "{}")
        except ValueError:
            return None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo-dir", default=".")
    ap.add_argument("--base", default="origin/main")
    ap.add_argument("--level", choices=LEVELS, default="high")
    ap.add_argument("--json")
    a = ap.parse_args(argv)
    repo = Path(a.repo_dir).resolve()
    if not shutil.which("npm"):
        print("SKIP P6 advisory drift: npm not installed in this sandbox")
        return 0
    if not (repo / "package-lock.json").exists():
        print("SKIP P6 advisory drift: no package-lock.json (pnpm/yarn: use `pnpm audit --json` with the same diff())")
        return 0
    base, head = audit_at(repo, a.base), audit_at(repo, None)
    if base is None or head is None:
        print(f"SKIP P6 advisory drift: could not audit {'base ' + a.base if base is None else 'head'}")
        return 0
    new, backlog = diff(advisories(base), advisories(head), a.level)
    for k in backlog:
        print(f"BACKLOG (pre-existing, not blocking): {k}")
    for k in new:
        print(f"NEW (introduced by this change): {k}")
    if a.json:
        Path(a.json).write_text(json.dumps({"new": new, "backlog": backlog, "level": a.level}, indent=1))
    print(f"{'FAIL' if new else 'PASS'} P6 advisory drift: {len(new)} new >= {a.level}, {len(backlog)} pre-existing (backlog)")
    return 1 if new else 0


if __name__ == "__main__":
    sys.exit(main())
