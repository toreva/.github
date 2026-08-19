#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SOURCE = Path(__file__).with_name("provider.py")
HEAD = "a" * 40
PR = "https://github.com/example/service/pull/42"

CORE = '''#!/usr/bin/env python3
import json, os
class TxnError(RuntimeError): pass
class PullRequestRef:
    def __init__(self, url):
        self.url=url; self.slug="example/service"; self.number=42

def _log(name):
    with open(os.environ["PROVIDER_LOG"], "a") as fh: fh.write(name+"\\n")
def parse_pr_url(url): _log("parse"); return PullRequestRef(url)
def query_pr(pr):
    _log("query")
    labels=[{"name":"merge-hold"}] if os.environ.get("MERGE_HOLD_SET") == "1" else []
    return {"headRefOid":"a"*40,"state":"OPEN","merged":False,"labels":{"nodes":labels},"mergeQueueEntry":None,"autoMergeRequest":None}
def exact_head(snapshot, expected):
    head=snapshot.get("headRefOid")
    if head != expected: raise TxnError("head_moved")
    return head
def assert_unmerged(snapshot, phase):
    if snapshot.get("merged") or snapshot.get("state") == "MERGED": raise TxnError("merged:"+phase)
def label_names(snapshot): return {n["name"] for n in snapshot.get("labels",{}).get("nodes",[]) if n.get("name")}
def revoke(pr, expected): _log("revoke"); return expected, {"pr_state":"OPEN","dequeued":"true","auto_merge_disabled":"true"}
def retire(pr, expected): _log("retire")
def arm(pr, expected, required_label, reject_title_prefix): _log("core_arm")
def emit(*args, **kwargs): _log("emit")
def run_gh(args):
    joined=" ".join(args)
    if len(args) >= 2 and args[0] == "label" and args[1] == "create":
        _log("label_create")
        return ""
    if "/issues/42/labels" in joined:
        _log("label_apply")
        os.environ["MERGE_HOLD_SET"]="1"
        return json.dumps([{"name":"merge-hold"}])
    _log("provider_read")
    return json.dumps({"head":{"sha":"a"*40},"body":os.environ.get("PR_BODY","")})
def load_json(raw, code): return json.loads(raw)
'''
PROVENANCE = '''#!/usr/bin/env python3
import os
class AdmissionError(RuntimeError): pass
def admit(url, expected):
    with open(os.environ["PROVIDER_LOG"], "a") as fh: fh.write("provenance\\n")
    if os.environ.get("PROVENANCE_REFUSE") == "1": raise AdmissionError("mixed")
'''


class ProviderFacadeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="provider-facade-")
        root = Path(self.tmp.name)
        shutil.copy2(SOURCE, root / "provider.py")
        (root / "merge_transaction.py").write_text(CORE)
        (root / "fallback_provenance.py").write_text(PROVENANCE)
        self.log = root / "log.txt"
        self.log.write_text("")
        self.old_log = os.environ.get("PROVIDER_LOG")
        self.old_refuse = os.environ.get("PROVENANCE_REFUSE")
        self.old_body = os.environ.get("PR_BODY")
        self.old_hold = os.environ.get("MERGE_HOLD_SET")
        os.environ["PROVIDER_LOG"] = str(self.log)
        os.environ.pop("PROVENANCE_REFUSE", None)
        os.environ.pop("PR_BODY", None)
        os.environ.pop("MERGE_HOLD_SET", None)
        self.root = root

    def tearDown(self):
        if self.old_log is None:
            os.environ.pop("PROVIDER_LOG", None)
        else:
            os.environ["PROVIDER_LOG"] = self.old_log
        if self.old_refuse is None:
            os.environ.pop("PROVENANCE_REFUSE", None)
        else:
            os.environ["PROVENANCE_REFUSE"] = self.old_refuse
        if self.old_body is None:
            os.environ.pop("PR_BODY", None)
        else:
            os.environ["PR_BODY"] = self.old_body
        if self.old_hold is None:
            os.environ.pop("MERGE_HOLD_SET", None)
        else:
            os.environ["MERGE_HOLD_SET"] = self.old_hold
        self.tmp.cleanup()

    def module(self):
        spec = importlib.util.spec_from_file_location("provider_fixture", self.root / "provider.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)
        return mod

    def calls(self):
        return self.log.read_text().splitlines()

    def test_import_api_arm_runs_terminal_separation_and_provenance_before_core(self):
        mod = self.module()
        pr = mod.parse_pr_url(PR)
        self.log.write_text("")
        mod.arm(pr, HEAD, "", "[SUPERSEDED")
        self.assertEqual(self.calls(), ["provider_read", "provenance", "core_arm"])

    def test_import_api_provenance_refusal_prevents_core_arm(self):
        mod = self.module()
        pr = mod.parse_pr_url(PR)
        self.log.write_text("")
        os.environ["PROVENANCE_REFUSE"] = "1"
        with self.assertRaises(mod.TxnError):
            mod.arm(pr, HEAD, "", "[SUPERSEDED")
        self.assertEqual(self.calls(), ["provider_read", "provenance"])

    def test_arm_refuses_conditional_issue_autoclose_before_provenance_or_merge(self):
        mod = self.module()
        pr = mod.parse_pr_url(PR)
        self.log.write_text("")
        os.environ["PR_BODY"] = "Closes #2642 when merged and hot-activated."
        with self.assertRaisesRegex(mod.TxnError, "issue_autoclose_keyword_forbidden"):
            mod.arm(pr, HEAD, "", "[SUPERSEDED")
        self.assertEqual(self.calls(), ["provider_read"])

    def test_arm_refuses_cross_repo_autoclose_reference(self):
        mod = self.module()
        pr = mod.parse_pr_url(PR)
        self.log.write_text("")
        os.environ["PR_BODY"] = "Fixes goblin-agent/coordinator#2642 after runtime proof."
        with self.assertRaisesRegex(mod.TxnError, "issue_autoclose_keyword_forbidden"):
            mod.arm(pr, HEAD, "", "[SUPERSEDED")
        self.assertEqual(self.calls(), ["provider_read"])

    def test_non_terminal_close_language_does_not_false_positive(self):
        mod = self.module()
        pr = mod.parse_pr_url(PR)
        self.log.write_text("")
        os.environ["PR_BODY"] = "This closes the source composition gap. Refs #2642 for runtime proof."
        mod.arm(pr, HEAD, "", "[SUPERSEDED")
        self.assertEqual(self.calls(), ["provider_read", "provenance", "core_arm"])

    def test_hold_persists_label_before_revoking_and_reobserves_both(self):
        mod = self.module()
        pr = mod.parse_pr_url(PR)
        self.log.write_text("")
        head, fields = mod.hold(pr, HEAD)
        self.assertEqual(head, HEAD)
        self.assertEqual(fields["merge_hold_label"], "merge-hold")
        self.assertEqual(
            self.calls(),
            ["query", "label_create", "label_apply", "query", "revoke", "query"],
        )

    def test_revoke_and_retire_remain_admission_independent(self):
        mod = self.module()
        pr = mod.parse_pr_url(PR)
        self.log.write_text("")
        os.environ["PROVENANCE_REFUSE"] = "1"
        os.environ["PR_BODY"] = "Closes #2642"
        mod.revoke(pr, HEAD)
        mod.retire(pr, HEAD)
        self.assertEqual(self.calls(), ["revoke", "retire"])

    def test_cli_arm_uses_same_facade_arm(self):
        env = os.environ.copy()
        env["PROVIDER_LOG"] = str(self.log)
        self.log.write_text("")
        r = subprocess.run(
            [sys.executable, str(self.root / "provider.py"), "arm", PR, "--expected-head", HEAD],
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.calls(), ["parse", "provider_read", "provenance", "core_arm"])

    def test_cli_hold_uses_same_persist_then_revoke_transaction(self):
        env = os.environ.copy()
        env["PROVIDER_LOG"] = str(self.log)
        env.pop("MERGE_HOLD_SET", None)
        self.log.write_text("")
        r = subprocess.run(
            [sys.executable, str(self.root / "provider.py"), "hold", PR, "--expected-head", HEAD],
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(
            self.calls(),
            ["parse", "query", "label_create", "label_apply", "query", "revoke", "query", "emit"],
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
