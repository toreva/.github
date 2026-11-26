# toreva-preflight

This tool predicts a red GitHub Actions run before it happens. Every failure that still gets through becomes a reviewed rule. Source: TCTC-33 failed-run audit (46,097 failed runs, 26 Nov 2026), PREVENTION.md.

**Where it runs:** in a cloud sandbox (cloud agent or box), never on a founder Mac, and never inside GitHub Actions. It costs **zero Actions minutes**. It is read-only towards GitHub: it never pushes, arms, labels, re-runs or deletes anything.

## Use it (agents: mandatory, see AGENT_INSTRUCTIONS.md)

```bash
# once per sandbox
git clone --depth 1 https://github.com/toreva/.github /tmp/toreva-dotgithub
PF=/tmp/toreva-dotgithub/actions/preflight

# before every push (from the repo root, after `git fetch origin`)
python3 $PF/toreva_preflight.py --base origin/main

# before arming auto-merge or enqueueing (needs GITHUB_TOKEN/GH_TOKEN with read access)
python3 $PF/toreva_preflight.py --pr https://github.com/OWNER/REPO/pull/N --only P1,P2

# when the diff touches package-lock.json
python3 $PF/advisory_diff.py --base origin/main
```

**Exit codes:**
- `0`: no FAIL, so you may push or arm.
- `1`: at least one FAIL. Fix it first.
- `2`: usage error.

`SKIP` means *unknown* (a tool was missing or a read failed). It is never "pass". Report SKIPs and don't treat them as green.

## Rules

| Rule | What it checks | Historical failures addressed (TCTC-33) |
|---|---|---|
| P5 fast tier | A clean worktree (no untracked files or caches), then: frozen install, typecheck/lint, structure and registry scripts (`scripts/test-*`, `iac/lints/ci-preflight/CI-*.sh`), tests for touched paths (vitest/jest related, pytest by module) | ~63% of class D (sampled: 16 of 19 real failures reproducible) |
| P2 queue re-entry | The PR's latest completed `merge_group` run failed **and** the head hasn't changed since | 2,127 runs, 16.2% of D (exact) |
| P1 hold-aware | The PR carries any label containing `hold` (same convention as `fleet-arm-gate.py`) | most of the 1,805 bundle5 queue failures (5 of 6 sampled were `merge-hold`) |
| P3 bot-commit hygiene | A bot commit of state files to a branch without `[skip ci]` | 2,492 runs (exact, Jul–Sep loop) |
| P4 workflow lint | `runs-on: *-latest` (WARN), plus `actionlint` when installed | class F 1,062, plus runner drift seen on goblin_ui #1805 |
| P6 advisory drift (`advisory_diff.py`) | Fails only on advisories **introduced by the lockfile diff**. Pre-existing ones are listed as backlog. | goblin_ui #1805 queue loop; 2 of 19 sampled |

**Per-repo override:** `.toreva/preflight.json`.
- `{"commands": {"typecheck": "pnpm -w typecheck", "unit": "pnpm vitest run --changed origin/main"}}` replaces detection with the repo's own CI commands.
- `structure_globs` changes which structure scripts run.

No rule here weakens or bypasses a hold, Sentinel, Audit, PO, independent-review or founder requirement. Every rule only *adds* refusals earlier in the flow.

## Feedback loop (`digest.py`, `signatures.yaml`)

```bash
python3 $PF/digest.py match --log failed-job-tail.txt
python3 $PF/digest.py daily --orgs toreva,goblin-agent --since 2026-11-26T00:00:00Z --out digest/ --max-runs 500
```

1. For each new failed run, `digest.py` reads the failed jobs' log tails (4 KB each) and matches them against `signatures.yaml`.
2. Each match gets one of four verdicts:
   - **ESCAPE**: a preventable signature hit. The preflight should have stopped it, either because it wasn't run (adoption gap) or because the rule missed (rule gap).
   - **SIGNAL**: a real runtime signal, such as post-deploy acceptance or a readiness timeout. It stays loud.
   - **SECONDARY_ONLY**: a cascade symptom, such as bundle5's "required prerequisite … refusing merge authority".
   - **NOVEL**: no match. A candidate signature with a normalised regex is written to `novel-candidates.json`.
3. **Novel candidates are reviewed by a human or an independent reviewer** before they enter `signatures.yaml`. Nothing is self-certified. Deterministic ones then become new P5 entries, so every repo, agent and machine picks them up from this one file.
4. Track these weekly: failed runs by class, escapes by rule, novel accepted, and first-push-green rate on agent branches.

**Running the daily digest:** run it from a cloud agent or sandbox with a read-only token. `--max-runs` caps API use. Do **not** schedule it as an Actions workflow; that would spend the minutes it is meant to save.

## Tests

`python3 actions/preflight/test_preflight.py -v` runs offline, with no network and no installs. The fixtures are real error lines from the audit sample.
