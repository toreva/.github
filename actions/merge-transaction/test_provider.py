#!/usr/bin/env python3
from __future__ import annotations

import json
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

STUB = '''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
p=Path(os.environ["PROVIDER_LOG"])
with p.open("a") as f: f.write(json.dumps({"script":Path(__file__).name,"args":sys.argv[1:]})+"\\n")
code=int(os.environ.get("EXIT_"+Path(__file__).stem.upper(),"0"))
raise SystemExit(code)
'''

class ProviderEntrypointTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix="provider-entry-")
        root=Path(self.tmp.name)
        shutil.copy2(SOURCE, root / "provider.py")
        (root / "fallback_provenance.py").write_text(STUB)
        (root / "merge_transaction.py").write_text(STUB)
        self.log=root / "log.jsonl"; self.log.write_text("")
        self.env=os.environ.copy(); self.env["PROVIDER_LOG"]=str(self.log)
        self.script=root / "provider.py"

    def tearDown(self): self.tmp.cleanup()

    def run_case(self, operation, **env):
        case_env=self.env.copy(); case_env.update(env)
        return subprocess.run(
            [sys.executable,str(self.script),operation,PR,"--expected-head",HEAD],
            env=case_env,text=True,capture_output=True,check=False,
        )

    def calls(self):
        return [json.loads(line) for line in self.log.read_text().splitlines() if line]

    def test_arm_runs_provenance_then_transaction(self):
        r=self.run_case("arm")
        self.assertEqual(r.returncode,0,r.stderr)
        self.assertEqual([x["script"] for x in self.calls()],["fallback_provenance.py","merge_transaction.py"])

    def test_provenance_refusal_prevents_transaction(self):
        r=self.run_case("arm",EXIT_FALLBACK_PROVENANCE="4")
        self.assertEqual(r.returncode,4)
        self.assertEqual([x["script"] for x in self.calls()],["fallback_provenance.py"])

    def test_revoke_bypasses_provenance(self):
        r=self.run_case("revoke",EXIT_FALLBACK_PROVENANCE="4")
        self.assertEqual(r.returncode,0,r.stderr)
        self.assertEqual([x["script"] for x in self.calls()],["merge_transaction.py"])

    def test_retire_bypasses_provenance(self):
        r=self.run_case("retire",EXIT_FALLBACK_PROVENANCE="4")
        self.assertEqual(r.returncode,0,r.stderr)
        self.assertEqual([x["script"] for x in self.calls()],["merge_transaction.py"])

if __name__ == "__main__": unittest.main(verbosity=2)
