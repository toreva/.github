#!/usr/bin/env python3
"""Single executable entrypoint for Toreva's shared merge transaction.

The provider-state core remains in merge_transaction.py. This entrypoint adds
only composition: before `arm`, run the fallback PR provenance admission; then
execute the existing provider transaction unchanged. `revoke` and `retire`
never depend on provenance so rollback remains available for a bad vehicle.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PROVENANCE = ROOT / "fallback_provenance.py"
TRANSACTION = ROOT / "merge_transaction.py"


def value_after(args: list[str], flag: str) -> str:
    try:
        index = args.index(flag)
        return args[index + 1]
    except (ValueError, IndexError) as exc:
        raise SystemExit(f"shared merge provider: missing {flag}") from exc


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) < 2:
        print("shared merge provider: expected <operation> <pr-url>", file=sys.stderr)
        return 2

    operation, pr_url = args[0], args[1]
    if operation == "arm":
        expected_head = value_after(args, "--expected-head")
        result = subprocess.run(
            [sys.executable, str(PROVENANCE), pr_url, "--expected-head", expected_head],
            check=False,
        )
        if result.returncode != 0:
            return result.returncode

    os.execv(sys.executable, [sys.executable, str(TRANSACTION), *args])
    return 127


if __name__ == "__main__":
    raise SystemExit(main())
