#!/usr/bin/env python3
"""Regressions for the TCTC-33 queue re-entry breaker and hold-label refusal.

Offline: every provider read is faked. Run:
    python3 actions/merge-transaction/test_queue_reentry.py -v
"""
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import provider  # noqa: E402

HEAD = "a" * 40
PR = provider.parse_pr_url("https://github.com/toreva/app.toreva.com/pull/1046")


def run(id_, pr, conclusion, created, sha="q", status="completed"):
    return {"id": id_, "head_branch": f"gh-readonly-queue/main/pr-{pr}-{'b' * 40}", "head_sha": sha,
            "status": status, "conclusion": conclusion, "created_at": created}


class PureDecision(unittest.TestCase):
    def test_same_head_after_failed_queue_run_is_refused(self):
        runs = [run(2, 1046, "failure", "2026-10-10T10:00:00Z", "q2"), run(1, 1046, "success", "2026-10-09T10:00:00Z")]
        b = provider.queue_reentry_blocker(1046, HEAD, runs, {"q2": ["base", HEAD]}, None)
        self.assertTrue(b and b.startswith("queue_reentry_refused:last_merge_group_run=2:failed_on_same_head"))

    def test_head_predating_failure_is_refused(self):
        runs = [run(2, 1046, "failure", "2026-10-10T10:00:00Z", "q2")]
        b = provider.queue_reentry_blocker(1046, HEAD, runs, {"q2": ["base"]}, "2026-10-10T09:59:59Z")
        self.assertTrue(b and "head_predates_failure" in b)

    def test_new_head_after_failure_is_allowed(self):
        runs = [run(2, 1046, "failure", "2026-10-10T10:00:00Z", "q2")]
        self.assertIsNone(provider.queue_reentry_blocker(1046, HEAD, runs, {"q2": ["base", "old"]}, "2026-10-11T00:00:00Z"))

    def test_latest_success_or_no_history_is_allowed(self):
        runs = [run(3, 1046, "success", "2026-10-11T10:00:00Z"), run(2, 1046, "failure", "2026-10-10T10:00:00Z")]
        self.assertIsNone(provider.queue_reentry_blocker(1046, HEAD, runs, {}, None))
        self.assertIsNone(provider.queue_reentry_blocker(1046, HEAD, [], {}, None))

    def test_other_prs_and_in_progress_runs_are_ignored(self):
        runs = [run(9, 999, "failure", "2026-10-12T10:00:00Z"), run(8, 1046, None, "2026-10-12T11:00:00Z", status="in_progress")]
        self.assertIsNone(provider.queue_reentry_blocker(1046, HEAD, runs, {}, None))

    def test_unprovable_head_change_is_a_refusal_not_an_arm(self):
        runs = [run(2, 1046, "failure", "2026-10-10T10:00:00Z", "q2")]
        b = provider.queue_reentry_blocker(1046, HEAD, runs, {}, None)
        self.assertTrue(b and b.startswith("queue_reentry_unproven"))


class ProviderWiring(unittest.TestCase):
    def snapshot(self, labels):
        return {"headRefOid": HEAD, "state": "OPEN", "merged": False, "isDraft": False, "title": "t",
                "labels": {"nodes": [{"name": l} for l in labels]}}

    def test_hold_label_blocks_arm_before_core_mutation(self):
        with mock.patch.object(provider, "query_pr", return_value=self.snapshot(["merge-hold"])), \
             mock.patch.object(provider, "admit"), mock.patch.object(provider._provenance, "admit"), \
             mock.patch.object(provider._core, "arm") as core_arm, \
             mock.patch.object(provider._core, "run_gh") as gh:
            with self.assertRaises(provider.TxnError) as ctx:
                provider.arm(PR, HEAD, "", "[SUPERSEDED")
            self.assertIn("hold_label_present:merge-hold", str(ctx.exception))
            core_arm.assert_not_called()
            gh.assert_not_called()

    def test_failed_queue_on_same_head_blocks_arm(self):
        def fake_gh(args, **_):
            path = args[1]
            if "actions/runs" in path:
                return json.dumps({"workflow_runs": [run(5, 1046, "failure", "2026-10-10T10:00:00Z", "qs")]})
            if path.endswith("/commits/qs"):
                return json.dumps({"parents": [{"sha": "base"}, {"sha": HEAD}]})
            if path.endswith(f"/commits/{HEAD}"):
                return json.dumps({"commit": {"committer": {"date": "2026-10-09T00:00:00Z"}}})
            raise AssertionError(f"unexpected read {args}")
        with mock.patch.object(provider, "query_pr", return_value=self.snapshot(["walk18-acceptance"])), \
             mock.patch.object(provider._core, "run_gh", side_effect=fake_gh), \
             mock.patch.object(provider, "admit"), mock.patch.object(provider._provenance, "admit"), \
             mock.patch.object(provider._core, "arm") as core_arm:
            with self.assertRaises(provider.TxnError) as ctx:
                provider.arm(PR, HEAD, "", "[SUPERSEDED")
            self.assertIn("queue_reentry_refused", str(ctx.exception))
            core_arm.assert_not_called()

    def test_clean_history_reaches_existing_admissions(self):
        with mock.patch.object(provider, "query_pr", return_value=self.snapshot([])), \
             mock.patch.object(provider._core, "run_gh", return_value=json.dumps({"workflow_runs": []})), \
             mock.patch.object(provider, "admit") as admit, \
             mock.patch.object(provider._provenance, "admit") as prov, \
             mock.patch.object(provider._core, "arm") as core_arm:
            provider.arm(PR, HEAD, "", "[SUPERSEDED")
            admit.assert_called_once()
            prov.assert_called_once()
            core_arm.assert_called_once()

    def test_unreadable_history_refuses(self):
        with mock.patch.object(provider, "query_pr", return_value=self.snapshot([])), \
             mock.patch.object(provider._core, "run_gh", side_effect=provider.TxnError("github_transport_failed:403")), \
             mock.patch.object(provider, "admit"), mock.patch.object(provider._provenance, "admit"), \
             mock.patch.object(provider._core, "arm") as core_arm:
            with self.assertRaises(provider.TxnError):
                provider.arm(PR, HEAD, "", "[SUPERSEDED")
            core_arm.assert_not_called()

    def test_hold_revoke_retire_paths_are_untouched(self):
        # Containment must stay available: hold/revoke/retire never consult the breaker.
        with mock.patch.object(provider, "assert_no_queue_reentry") as breaker, \
             mock.patch.object(provider, "revoke", return_value=(HEAD, {})) as rv:
            provider.main(["revoke", PR.url, "--expected-head", HEAD])
            rv.assert_called_once()
            breaker.assert_not_called()


if __name__ == "__main__":
    unittest.main()
