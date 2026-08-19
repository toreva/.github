#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

PATH = Path(__file__).with_name("control.py")
spec = importlib.util.spec_from_file_location("trusted_merge_control", PATH)
assert spec and spec.loader
mod = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = mod
spec.loader.exec_module(mod)

# Exact production incident form.
assert mod.first_control(
    "@claude STOP-BEFORE-PROMOTE — rollback anchor is not actual serving traffic.",
    "OWNER",
) == "STOP-BEFORE-PROMOTE"

# Productive variants remain explicit and bounded.
assert mod.first_control("STOP-BEFORE-MERGE — hold it", "MEMBER") == "STOP-BEFORE-MERGE"
assert mod.first_control("  Merge-Hold: true  ", "COLLABORATOR") == "MERGE-HOLD"
assert mod.first_control("Control-Action: MERGE-HOLD", "OWNER") == "MERGE-HOLD"

# Trust and prose controls.
assert mod.first_control("STOP-BEFORE-MERGE", "NONE") is None
assert mod.first_control("> @claude STOP-BEFORE-PROMOTE — historical quote", "OWNER") is None
assert mod.first_control("```\nSTOP-BEFORE-MERGE\n```", "OWNER") is None
assert mod.first_control("Postmortem says STOP-BEFORE-MERGE was ignored", "OWNER") is None
assert mod.first_control("Please STOP-BEFORE-MERGE when ready", "OWNER") is None
assert mod.first_control("ordinary comment\nSTOP-BEFORE-MERGE", "OWNER") is None

print("PASS: trusted STOP classifier fires on the exact incident and ignores quoted/untrusted prose")
