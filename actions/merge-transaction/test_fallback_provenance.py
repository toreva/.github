#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).with_name("fallback_provenance.py")
PR_URL = "https://github.com/example/service/pull/42"
HEAD = "a" * 40

GH_STUB = r'''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
state = json.loads(Path(os.environ["GH_STATE"]).read_text())
args = sys.argv[1:]
if args[:2] == ["api", "repos/example/service/pulls/42"]:
  print(json.dumps({
    "body": state.get("body", ""),
    "commits": len(state.get("commits", [])),
    "head": {"sha": state.get("head", "a"*40)},
  })); raise SystemExit(0)
if args[:2] == ["api", "repos/example/service/pulls/42/commits?per_page=100"]:
  print(json.dumps([
    {"sha": c.get("sha", str(i+1)*40), "commit": {"message": c["message"]}}
    for i, c in enumerate(state.get("commits", []))
  ])); raise SystemExit(0)
print("unsupported: " + " ".join(args), file=sys.stderr); raise SystemExit(2)
'''

MARKER = "Auto-PR opened by agent-daemon.sh because the runner already committed/pushed work but left no open PR for the ahead branch."


def body(dispatch="2026-08-19-iac-red-ci.md", origin="iac"):
    return f"## Summary\n\n{MARKER}\n\nDispatch: `{dispatch}`\nOriginal ask from: {origin}\nAhead of trunk: 2 commit(s)\n"


class FallbackProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="merge-prov-")
        root = Path(self.tmp.name)
        bindir = root / "bin"; bindir.mkdir()
        gh = bindir / "gh"; gh.write_text(GH_STUB); gh.chmod(0o755)
        self.state = root / "state.json"
        self.env = os.environ.copy()
        self.env.update({"PATH": f"{bindir}:{self.env.get('PATH','')}", "GH_STATE": str(self.state)})

    def tearDown(self):
        self.tmp.cleanup()

    def run_case(self, *, body_text="", commits=None, head=HEAD):
        self.state.write_text(json.dumps({"body": body_text, "commits": commits or [], "head": head}))
        return subprocess.run(
            [sys.executable, str(SCRIPT), PR_URL, "--expected-head", HEAD],
            env=self.env, text=True, capture_output=True, check=False,
        )

    def test_non_fallback_pr_is_untouched(self):
        r = self.run_case(body_text="ordinary PR", commits=[{"message":"no provenance"}])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("not_applicable", r.stdout)

    def test_single_matching_actor_is_admitted(self):
        r = self.run_case(body_text=body(), commits=[{"message":"fix thing\n\nDispatched-By: iac"}])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Merge-Provenance-Status: admitted", r.stdout)

    def test_multiple_commits_same_identity_are_admitted(self):
        dispatch = "2026-08-19-iac-red-ci.md"
        r = self.run_case(body_text=body(dispatch), commits=[
            {"message":f"fix one\n\nDispatched-By: iac\nSource dispatch: {dispatch}"},
            {"message":"test one\n\nSpawned-By: iac-agent"},
        ])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Merge-Provenance-Commits: 2", r.stdout)

    def test_current_incident_shape_is_blocked(self):
        r = self.run_case(body_text=body(), commits=[
            {"message":"No runner available is a WAIT\n\nDispatched-By: founder"},
            {"message":"fix: propagate canonical husky hooks fleet-wide\n\nDispatched-By: po"},
        ])
        self.assertEqual(r.returncode, 4)
        self.assertIn("fallback_mixed_or_misattributed_provenance", r.stderr)
        self.assertIn("expected=iac", r.stderr)

    def test_source_dispatch_mismatch_is_blocked_even_if_actor_matches(self):
        r = self.run_case(body_text=body(), commits=[
            {"message":"fix thing\n\nDispatched-By: iac\nSource dispatch: 2026-08-18-other.md"},
        ])
        self.assertEqual(r.returncode, 4)
        self.assertIn("dispatch=2026-08-18-other.md", r.stderr)

    def test_missing_commit_provenance_is_blocked(self):
        r = self.run_case(body_text=body(), commits=[{"message":"fix thing"}])
        self.assertEqual(r.returncode, 4)
        self.assertIn("provenance_missing", r.stderr)

    def test_head_move_is_blocked_before_commit_admission(self):
        r = self.run_case(body_text=body(), commits=[{"message":"Dispatched-By: iac"}], head="b"*40)
        self.assertEqual(r.returncode, 4)
        self.assertIn("head_moved", r.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
