# Agent instructions: toreva-preflight (paste into each repo's AGENTS.md / CLAUDE.md and the onboarding template)

## Before every `git push`
1. `git fetch origin`. Then run `python3 <toreva/.github>/actions/preflight/toreva_preflight.py --base origin/main` from the repo root, inside the cloud sandbox. Never run it on a founder machine.
2. **Exit 1 means do not push.** Fix every FAIL in the same change. That includes the co-changes a structure test names: allowlists, baselines, workflow wiring and required strings.
3. If the diff touches `package-lock.json`, also run `advisory_diff.py --base origin/main`. Only new advisories block. List the backlog in the PR body; don't fix it in an unrelated PR.
4. Do not use CI as your test runner. A second red run on the same branch is an escape and is counted against the preflight.

## Before arming auto-merge or enqueueing a PR
1. Run `toreva_preflight.py --pr <PR URL> --only P1,P2`.
2. **P1 FAIL (hold label):** do not arm or enqueue. Only the hold's owner releases a hold. Never remove a hold label yourself.
3. **P2 FAIL (last merge-queue run failed on this head):** do not re-queue. Fix the cause and push a new head, or update the branch, first.
4. SKIP means unknown. Do not arm on an unknown P1 or P2.

## Bots and scheduled workflows
- Never commit state, memory or monitoring output to a PR branch. Write it to an artifact, an issue or a store.
- If a commit is unavoidable, include `[skip ci]` and make sure the path is covered by `paths-ignore`.

## Always
- Pin `runs-on` to a versioned image (for example `ubuntu-24.04`), not `*-latest`.
- These steps add refusals only. They never lift a hold, skip Sentinel, Audit, PO or independent review, or self-certify a review.
