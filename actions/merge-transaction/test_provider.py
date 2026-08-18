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
import os
class TxnError(RuntimeError): pass
class PullRequestRef:
    def __init__(self, url): self.url=url

def _log(name):
    with open(os.environ["PROVIDER_LOG"], "a") as fh: fh.write(name+"\\n")
def parse_pr_url(url): _log("parse"); return PullRequestRef(url)
def query_pr(pr): _log("query"); return {"headRefOid":"a"*40}
def revoke(pr, expected): _log("revoke"); return expected, {"pr_state":"OPEN"}
def retire(pr, expected): _log("retire")
def arm(pr, expected, required_label, reject_title_prefix): _log("core_arm")
def emit(*args, **kwargs): _log("emit")
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
        self.tmp=tempfile.TemporaryDirectory(prefix="provider-facade-")
        root=Path(self.tmp.name)
        shutil.copy2(SOURCE, root / "provider.py")
        (root / "merge_transaction.py").write_text(CORE)
        (root / "fallback_provenance.py").write_text(PROVENANCE)
        self.log=root / "log.txt"; self.log.write_text("")
        self.old_log=os.environ.get("PROVIDER_LOG"); self.old_refuse=os.environ.get("PROVENANCE_REFUSE")
        os.environ["PROVIDER_LOG"]=str(self.log); os.environ.pop("PROVENANCE_REFUSE",None)
        self.root=root

    def tearDown(self):
        if self.old_log is None: os.environ.pop("PROVIDER_LOG",None)
        else: os.environ["PROVIDER_LOG"]=self.old_log
        if self.old_refuse is None: os.environ.pop("PROVENANCE_REFUSE",None)
        else: os.environ["PROVENANCE_REFUSE"]=self.old_refuse
        self.tmp.cleanup()

    def module(self):
        spec=importlib.util.spec_from_file_location("provider_fixture", self.root/"provider.py")
        mod=importlib.util.module_from_spec(spec); sys.modules[spec.name]=mod; spec.loader.exec_module(mod); return mod

    def calls(self): return self.log.read_text().splitlines()

    def test_import_api_arm_runs_provenance_before_core(self):
        mod=self.module(); pr=mod.parse_pr_url(PR); self.log.write_text("")
        mod.arm(pr,HEAD,"","[SUPERSEDED")
        self.assertEqual(self.calls(),["provenance","core_arm"])

    def test_import_api_refusal_prevents_core_arm(self):
        mod=self.module(); pr=mod.parse_pr_url(PR); self.log.write_text(""); os.environ["PROVENANCE_REFUSE"]="1"
        with self.assertRaises(mod.TxnError): mod.arm(pr,HEAD,"","[SUPERSEDED")
        self.assertEqual(self.calls(),["provenance"])

    def test_revoke_and_retire_remain_provenance_independent(self):
        mod=self.module(); pr=mod.parse_pr_url(PR); self.log.write_text(""); os.environ["PROVENANCE_REFUSE"]="1"
        mod.revoke(pr,HEAD); mod.retire(pr,HEAD)
        self.assertEqual(self.calls(),["revoke","retire"])

    def test_cli_arm_uses_same_facade_arm(self):
        env=os.environ.copy(); env["PROVIDER_LOG"]=str(self.log); self.log.write_text("")
        r=subprocess.run([sys.executable,str(self.root/"provider.py"),"arm",PR,"--expected-head",HEAD],env=env,text=True,capture_output=True,check=False)
        self.assertEqual(r.returncode,0,r.stderr)
        self.assertEqual(self.calls(),["parse","provenance","core_arm"])

if __name__ == "__main__": unittest.main(verbosity=2)
