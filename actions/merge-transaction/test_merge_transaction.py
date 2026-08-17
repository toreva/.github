#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).with_name("merge_transaction.py")
PR_URL = "https://github.com/example/service/pull/42"
HEAD = "a" * 40
OTHER = "b" * 40

GH_STUB = r'''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
state_path = Path(os.environ["GH_STATE"])
log_path = Path(os.environ["GH_LOG"])
state = json.loads(state_path.read_text())
args = sys.argv[1:]
with log_path.open("a") as fh: fh.write(json.dumps(args) + "\n")

def save(): state_path.write_text(json.dumps(state))
def pr():
  return {
    "id":"PR_1","number":42,"state":"OPEN" if state.get("open", True) else "CLOSED",
    "merged":bool(state.get("merged")),"isDraft":bool(state.get("draft")),
    "title":state.get("title","Safe PR"),"headRefOid":state["head"],
    "labels":{"nodes":[{"name":x} for x in state.get("labels",[])]},
    "autoMergeRequest":{"enabledAt":"now","mergeMethod":"SQUASH"} if state.get("auto") else None,
    "mergeQueueEntry":{"id":"MQE_1","state":"AWAITING_CHECKS","headCommit":{"oid":state["head"]}} if state.get("queued") else None,
  }

if args[:2] == ["api","graphql"]:
  query = next((x[len("query="):] for x in args if x.startswith("query=")), "")
  if "DequeueMergeTxn" in query:
    if not state.get("dequeue_noop"): state["queued"] = False
    if state.get("merge_on_dequeue"): state["merged"] = True
    save(); print(json.dumps({"data":{"dequeuePullRequest":{"mergeQueueEntry":None}}})); raise SystemExit(0)
  if "DisableMergeTxn" in query:
    if not state.get("disable_noop"): state["auto"] = False
    save(); print(json.dumps({"data":{"disablePullRequestAutoMerge":{"pullRequest":pr()}}})); raise SystemExit(0)
  print(json.dumps({"data":{"repository":{"pullRequest":pr()}}})); raise SystemExit(0)

if args[:4] == ["api","--method","PATCH","repos/example/service/pulls/42"]:
  state["open"] = False; save(); print(json.dumps({"state":"closed"})); raise SystemExit(0)

if args[:2] == ["pr","merge"]:
  if state.get("merge_immediately"): state["merged"] = True
  else: state["auto"] = True
  save(); print("armed"); raise SystemExit(0)

print("unsupported: " + " ".join(args), file=sys.stderr); raise SystemExit(2)
'''


class MergeTransactionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="merge-txn-")
        root = Path(self.tmp.name)
        self.bin = root / "bin"; self.bin.mkdir()
        gh = self.bin / "gh"; gh.write_text(GH_STUB); gh.chmod(0o755)
        self.state = root / "state.json"; self.log = root / "log.jsonl"; self.log.write_text("")
        self.env = os.environ.copy(); self.env.update({"PATH":f"{self.bin}:{self.env.get('PATH','')}","GH_STATE":str(self.state),"GH_LOG":str(self.log)})

    def tearDown(self): self.tmp.cleanup()

    def run_case(self, operation, **overrides):
        state={"head":HEAD,"open":True,"merged":False,"draft":False,"queued":False,"auto":False,"labels":["automerge"],"title":"Safe PR"}; state.update(overrides)
        self.state.write_text(json.dumps(state))
        args=[sys.executable,str(SCRIPT),operation,PR_URL,"--expected-head",HEAD]
        if operation=="arm": args += ["--required-label","automerge"]
        return subprocess.run(args, env=self.env, text=True, capture_output=True, check=False)

    def final(self): return json.loads(self.state.read_text())

    def test_revoke_removes_queue_and_auto_merge(self):
        r=self.run_case("revoke", queued=True, auto=True); self.assertEqual(r.returncode,0,r.stderr); self.assertFalse(self.final()["queued"]); self.assertFalse(self.final()["auto"])

    def test_retire_revokes_before_close(self):
        r=self.run_case("retire", queued=True, auto=True); self.assertEqual(r.returncode,0,r.stderr); self.assertFalse(self.final()["open"]); self.assertFalse(self.final()["merged"])

    def test_head_move_refuses_without_mutation(self):
        r=self.run_case("revoke", head=OTHER, queued=True, auto=True); self.assertEqual(r.returncode,4); self.assertIn("head_moved",r.stderr); self.assertTrue(self.final()["queued"])

    def test_revoke_fails_if_provider_does_not_remove_queue(self):
        r=self.run_case("revoke", queued=True, dequeue_noop=True); self.assertEqual(r.returncode,4); self.assertIn("merge_queue_revocation_unverified",r.stderr)

    def test_arm_revalidates_label_and_exact_head(self):
        r=self.run_case("arm", labels=[]); self.assertEqual(r.returncode,4); self.assertIn("required_label_missing",r.stderr); self.assertFalse(self.final()["auto"])

    def test_arm_records_live_authorization(self):
        r=self.run_case("arm"); self.assertEqual(r.returncode,0,r.stderr); self.assertTrue(self.final()["auto"]); self.assertIn("Merge-Transaction-Status: armed",r.stdout)

    def test_arm_rejects_superseded_title(self):
        r=self.run_case("arm", title="[SUPERSEDED] old vehicle"); self.assertEqual(r.returncode,4); self.assertIn("title_rejected",r.stderr)

    def test_merge_race_during_revoke_is_typed_failure(self):
        r=self.run_case("revoke", queued=True, merge_on_dequeue=True); self.assertEqual(r.returncode,4); self.assertIn("already_or_raced_merged",r.stderr)


if __name__ == "__main__": unittest.main(verbosity=2)
