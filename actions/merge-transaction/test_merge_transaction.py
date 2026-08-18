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
BASE = "c" * 40
TEST_MERGE = "d" * 40
BASE_TREE = "e" * 40
MERGE_TREE = "f" * 40
OLD_BASE = "9" * 40

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

if len(args) >= 2 and args[0] == "api" and args[1].startswith("repos/example/service/commits/") and args[1].endswith("/pulls"):
  associated=[{"number":42,"state":"open","merged_at":None,"head":{"sha":state["head"]}}]
  if state.get("duplicate_live"): associated.append({"number":99,"state":"open","merged_at":None,"head":{"sha":state["head"]}})
  if state.get("consumed_head"): associated.append({"number":99,"state":"closed","merged_at":"2026-08-17T00:00:00Z","head":{"sha":state["head"]}})
  print(json.dumps(associated)); raise SystemExit(0)

if args[:2] == ["api","repos/example/service/pulls/42"]:
  reads = int(state.get("projection_reads", 0)) + 1
  state["projection_reads"] = reads; save()
  mergeable = state.get("mergeable", True)
  if state.get("mergeability_unknown_once") and reads == 1: mergeable = None
  payload = {
    "number":42,"state":"open","merged":False,
    "head":{"sha":state["head"],"label":state.get("head_label","example:feature")},
    "base":{"ref":"main","sha":state.get("base", "c"*40)},
    "mergeable":mergeable,
    "merge_commit_sha":state.get("test_merge", "d"*40) if mergeable is True else None,
  }
  print(json.dumps(payload)); raise SystemExit(0)

if len(args) >= 2 and args[0] == "api" and args[1].startswith("repos/example/service/compare/"):
  base_sha = state.get("base", "c"*40)
  if state.get("stale_base"):
    print(json.dumps({"status":"diverged","behind_by":1,"ahead_by":1,"merge_base_commit":{"sha":"9"*40}}))
  else:
    print(json.dumps({"status":"ahead","behind_by":0,"ahead_by":1,"merge_base_commit":{"sha":base_sha}}))
  raise SystemExit(0)

if args[:4] == ["api","--method","GET","repos/example/service/pulls"]:
  history=[]
  if state.get("branch_consumed"):
    history.append({"number":99,"state":"closed","merged_at":"2026-08-17T00:00:00Z","head":{"sha":state["head"]}})
  print(json.dumps(history)); raise SystemExit(0)

if len(args) >= 2 and args[0] == "api" and args[1].startswith("repos/example/service/git/commits/"):
  sha=args[1].rsplit("/",1)[-1]
  tree = state.get("base_tree", "e"*40) if sha == state.get("base", "c"*40) else state.get("merge_tree", "f"*40)
  print(json.dumps({"sha":sha,"tree":{"sha":tree}})); raise SystemExit(0)

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
        state={
            "head":HEAD,"base":BASE,"test_merge":TEST_MERGE,
            "base_tree":BASE_TREE,"merge_tree":MERGE_TREE,
            "open":True,"merged":False,"draft":False,"queued":False,"auto":False,
            "labels":["automerge"],"title":"Safe PR",
        }
        state.update(overrides)
        self.state.write_text(json.dumps(state))
        args=[sys.executable,str(SCRIPT),operation,PR_URL,"--expected-head",HEAD]
        if operation=="arm": args += ["--required-label","automerge"]
        return subprocess.run(args, env=self.env, text=True, capture_output=True, check=False)

    def final(self): return json.loads(self.state.read_text())
    def calls(self): return [json.loads(x) for x in self.log.read_text().splitlines() if x]
    def arm_calls(self): return [x for x in self.calls() if x[:2] == ["pr","merge"]]

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

    def test_arm_rejects_stale_base_before_provider_mutation(self):
        r=self.run_case("arm", stale_base=True)
        self.assertEqual(r.returncode,4,r.stderr)
        self.assertIn("stale_base",r.stderr)
        self.assertEqual(self.arm_calls(),[])

    def test_arm_rejects_duplicate_live_head_before_provider_mutation(self):
        r=self.run_case("arm", duplicate_live=True); self.assertEqual(r.returncode,4); self.assertIn("duplicate_live_head_owner",r.stderr); self.assertEqual(self.arm_calls(),[])

    def test_arm_rejects_consumed_head_from_commit_association(self):
        r=self.run_case("arm", consumed_head=True); self.assertEqual(r.returncode,4); self.assertIn("consumed_head_replay",r.stderr); self.assertEqual(self.arm_calls(),[])

    def test_arm_rejects_consumed_head_from_recreated_branch_history(self):
        r=self.run_case("arm", branch_consumed=True); self.assertEqual(r.returncode,4); self.assertIn("consumed_head_replay",r.stderr); self.assertEqual(self.arm_calls(),[])

    def test_arm_rejects_squash_replay_by_prospective_tree_even_when_old_file_diff_would_be_nonempty(self):
        r=self.run_case("arm", merge_tree=BASE_TREE)
        self.assertEqual(r.returncode,4,r.stderr)
        self.assertIn("zero_effect_merge",r.stderr)
        self.assertEqual(self.arm_calls(),[])
        self.assertFalse(any("/files?" in " ".join(call) for call in self.calls()))

    def test_arm_waits_for_github_mergeability_computation(self):
        r=self.run_case("arm", mergeability_unknown_once=True); self.assertEqual(r.returncode,0,r.stderr); self.assertEqual(self.final()["projection_reads"],2)

    def test_arm_fails_closed_when_pr_is_not_mergeable(self):
        r=self.run_case("arm", mergeable=False); self.assertEqual(r.returncode,4); self.assertIn("pr_not_mergeable",r.stderr); self.assertEqual(self.arm_calls(),[])

    def test_merge_race_during_revoke_is_typed_failure(self):
        r=self.run_case("revoke", queued=True, merge_on_dequeue=True); self.assertEqual(r.returncode,4); self.assertIn("already_or_raced_merged",r.stderr)


if __name__ == "__main__": unittest.main(verbosity=2)
