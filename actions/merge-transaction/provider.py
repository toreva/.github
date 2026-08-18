#!/usr/bin/env python3
"""Canonical public entrypoint for Toreva's shared merge provider.

`merge_transaction.py` remains the mature provider-state engine. This facade is
both CLI- and import-compatible with that engine, and adds the one cross-cutting
admission that every live arm caller must share: daemon fallback provenance.
Revoke/retire deliberately bypass provenance so rollback stays available.
"""
from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

ROOT = Path(__file__).resolve().parent


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


def arm(pr: Any, expected: str, required_label: str, reject_title_prefix: str) -> None:
    try:
        _provenance.admit(pr.url, expected)
    except _provenance.AdmissionError as exc:
        raise TxnError(f"fallback_provenance_refused:{exc}") from exc
    _core.arm(pr, expected, required_label, reject_title_prefix)


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
