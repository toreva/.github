#!/usr/bin/env python3
"""Offline regressions for toreva-preflight (no network, no installs).

Fixtures are real error lines from the TCTC-33 sample of failed runs.
Run: python3 actions/preflight/test_preflight.py -v
"""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import advisory_diff as adv  # noqa: E402
import digest  # noqa: E402
import toreva_preflight as tp  # noqa: E402

SIGS = digest.load_signatures()

LOGS = {
    "S01-held-pr-in-queue": "2026-10-18T20:58:17.8413208Z ##[error]source PR #2206 carries merge-hold",
    "S04-runner-image-drift": "Error: ERROR: Playwright does not support chromium on ubuntu26.04-x64",
    "S05-structure-invariant": "FAILED-SCRIPT: [44/128] bash scripts/test-daemon-isolated-checkout-admission.sh (exit 1)",
    "S06-registry-allowlist": "✗ CI-022 violation: tests/scripts/test-serving-index-provenance.py is not reached by any workflow (wire it into CI, or add it to the baseline with a reason)",
    "S07-type-contract": "##[error]src/training-consent/chain.ts(97,7): error TS18047: 'acceptedNotice' is possibly 'null'.",
    "S08-unit-regression": "##[error]AssertionError: expected 409 to be 200 // Object.is equality",
    "S09-clean-env-assumption": "ModuleNotFoundError: No module named 'numpy'",
    "S10-advisory-drift": "25 vulnerabilities (14 moderate, 9 high, 2 critical)",
    "S12-deploy-admission-guardrail": 'ADMISSION_RECEIPT: {"admitted":false,"verdict":"PENDING"}',
    "S14-post-deploy-acceptance": "##[error]deployed_not_accepted: healthy serving retained; composed acceptance remains unverified",
}


class Signatures(unittest.TestCase):
    def test_yaml_and_json_catalogs_agree(self):
        import json as j
        jd = j.loads((HERE / "signatures.json").read_text())
        self.assertEqual([s["id"] for s in jd["signatures"]], [s["id"] for s in SIGS])

    def test_each_seed_fixture_hits_its_signature(self):
        for sid, line in LOGS.items():
            ids = [s["id"] for s in digest.match(line, SIGS)]
            self.assertIn(sid, ids, f"{sid} missed: {line}")

    def test_cascade_alone_is_secondary_not_root(self):
        v, ids = digest.verdict(digest.match("required prerequisite unit ended failure; refusing merge authority", SIGS))
        self.assertEqual(v, "SECONDARY_ONLY")
        v, ids = digest.verdict(digest.match(
            "AssertionError: x\nrequired prerequisite unit ended failure; refusing merge authority", SIGS))
        self.assertEqual(v, "ESCAPE")
        self.assertIn("S08-unit-regression", ids)

    def test_signal_is_not_an_escape(self):
        self.assertEqual(digest.verdict(digest.match(LOGS["S14-post-deploy-acceptance"], SIGS))[0], "SIGNAL")

    def test_unknown_failure_is_novel_with_reviewable_candidate(self):
        log = "2026-11-26T01:02:03Z ##[error]Quantum flux capacitor 7f3a9c1 desynchronised at /opt/x/y.py line 42"
        v, _ = digest.verdict(digest.match(log, SIGS))
        self.assertEqual(v, "NOVEL")
        c = digest.candidate(log, "ci/flux")
        self.assertTrue(c["id"].startswith("NEW-"))
        self.assertIn("<preventable|signal|secondary>", c["status"])  # a human decides
        import re
        self.assertRegex("Quantum flux capacitor deadbeef1 desynchronised at /a/b/c.py line 9", c["match"]["log"].replace("\\#\\#\\[error\\]", ""))
        re.compile(c["match"]["log"])


class QueueReentry(unittest.TestCase):
    HEAD = "a" * 40
    RUNS = [
        {"id": 3, "head_branch": "gh-readonly-queue/main/pr-1046-bbbb", "head_sha": "q3", "status": "completed",
         "conclusion": "failure", "created_at": "2026-10-10T10:00:00Z", "html_url": "u3"},
        {"id": 2, "head_branch": "gh-readonly-queue/main/pr-1046-cccc", "head_sha": "q2", "status": "completed",
         "conclusion": "failure", "created_at": "2026-10-10T09:00:00Z"},
        {"id": 9, "head_branch": "gh-readonly-queue/main/pr-999-dddd", "head_sha": "q9", "status": "completed",
         "conclusion": "success", "created_at": "2026-10-11T09:00:00Z"},
    ]

    def test_refuses_requeue_on_same_head_after_failure(self):
        s, d = tp.queue_reentry_verdict(1046, self.HEAD, self.RUNS, {"q3": ["base", self.HEAD]})
        self.assertEqual(s, tp.FAIL, d)

    def test_refuses_when_head_predates_failed_run(self):
        s, _ = tp.queue_reentry_verdict(1046, self.HEAD, self.RUNS, {"q3": ["base"]}, "2026-10-09T00:00:00Z")
        self.assertEqual(s, tp.FAIL)

    def test_allows_new_head(self):
        s, _ = tp.queue_reentry_verdict(1046, self.HEAD, self.RUNS, {"q3": ["base", "old"]}, "2026-10-11T00:00:00Z")
        self.assertEqual(s, tp.PASS)

    def test_unknown_is_skip_not_pass(self):
        s, _ = tp.queue_reentry_verdict(1046, self.HEAD, self.RUNS, {}, None)
        self.assertEqual(s, tp.SKIP)

    def test_other_prs_and_success_do_not_block(self):
        self.assertEqual(tp.queue_reentry_verdict(999, self.HEAD, self.RUNS)[0], tp.PASS)
        self.assertEqual(tp.queue_reentry_verdict(5, self.HEAD, self.RUNS)[0], tp.PASS)


class Holds(unittest.TestCase):
    def test_hold_labels(self):
        self.assertEqual(tp.hold_labels(["merge-hold", "bug", "Legal-HOLD"]), ["Legal-HOLD", "merge-hold"])
        self.assertEqual(tp.hold_labels(["walk18-acceptance"]), [])


class WorkflowLint(unittest.TestCase):
    def test_unpinned_runner(self):
        hits = tp.lint_workflow_text("jobs:\n  a:\n    runs-on: ubuntu-latest\n  b:\n    runs-on: ubuntu-24.04\n")
        self.assertEqual(len(hits), 1)
        self.assertIn("line 3", hits[0])


class Advisory(unittest.TestCase):
    def test_only_new_advisories_block(self):
        base = {"vulnerabilities": {"zod": {"severity": "high", "via": [{"url": "GHSA-old", "severity": "high"}]}}}
        head = {"vulnerabilities": {"zod": {"severity": "high", "via": [{"url": "GHSA-old", "severity": "high"}]},
                                    "vite": {"severity": "critical", "via": [{"url": "GHSA-new", "severity": "critical"}]},
                                    "x": {"severity": "low", "via": [{"url": "GHSA-low", "severity": "low"}]}}}
        new, backlog = adv.diff(adv.advisories(base), adv.advisories(head), "high")
        self.assertEqual(new, ["GHSA-new"])
        self.assertEqual(backlog, ["GHSA-old"])

    def test_unchanged_lockfile_never_blocks(self):
        a = {"vulnerabilities": {"zod": {"severity": "critical", "via": [{"url": "GHSA-1", "severity": "critical"}]}}}
        self.assertEqual(adv.diff(adv.advisories(a), adv.advisories(a), "low")[0], [])


class EndToEnd(unittest.TestCase):
    """Run the CLI against a throwaway repo: structure script failure must FAIL."""

    def test_structure_script_failure_blocks_push(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = Path(tmp)
            run = lambda *c: subprocess.run(c, cwd=r, check=True, capture_output=True)
            run("git", "init", "-q", "-b", "main")
            run("git", "config", "user.email", "t@t")
            run("git", "config", "user.name", "t")
            (r / "scripts").mkdir()
            (r / "scripts/test-allowlist.sh").write_text("grep -q new.ts allowlist.txt || { echo 'FAIL: new.ts not in allowlist'; exit 1; }\n")
            (r / "allowlist.txt").write_text("old.ts\n")
            run("git", "add", "-A")
            run("git", "commit", "-qm", "base")
            run("git", "checkout", "-qb", "feature")
            (r / "new.ts").write_text("export const x = 1\n")
            run("git", "add", "-A")
            run("git", "commit", "-qm", "add new.ts without allowlist co-change")
            out = Path(tmp) / "report.json"
            rc = tp.run(["--repo-dir", str(r), "--base", "main", "--only", "P3,P4,P5", "--json", str(out)])
            self.assertEqual(rc, 1)
            rep = json.loads(out.read_text())
            self.assertIn("new.ts", rep["changed_files"])
            self.assertTrue(any(x["rule"].startswith("P5 structure") and x["status"] == "FAIL" for x in rep["results"]))
            (r / "allowlist.txt").write_text("old.ts\nnew.ts\n")
            run("git", "commit", "-qam", "co-change")
            self.assertEqual(tp.run(["--repo-dir", str(r), "--base", "main", "--only", "P3,P4,P5"]), 0)


if __name__ == "__main__":
    unittest.main()
