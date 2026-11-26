#!/usr/bin/env python3
"""toreva-preflight: predict a red CI run before it exists.

Run from the root of a repository checkout, in a cloud sandbox (never on a
founder machine), before `git push` and again before arming auto-merge:

    python3 toreva_preflight.py                    # pre-push, fast tier
    python3 toreva_preflight.py --pr <PR URL>      # also hold + queue re-entry checks
    python3 toreva_preflight.py --json report.json

It never pushes, arms, labels, re-runs or mutates anything. Exit 0 = no FAIL
(push allowed), 1 = at least one FAIL (do not push / do not arm), 2 = usage.
A check that cannot run reports SKIP with the reason: SKIP is "unknown", never
"pass". Rules (TCTC-33 PREVENTION.md, historical share of class-D failures):

  P5 fast tier ............ clean-env frozen install, typecheck, structure and
                            lint scripts, tests for touched paths   (~63%, sampled)
  P2 queue re-entry ....... last merge_group run for this PR failed and the head
                            has not changed                         (16.2%, exact)
  P1 hold-aware ........... PR carries a hold label                  (overlaps P2)
  P3 bot-commit hygiene ... bot commit of state files to a PR branch (8.9%, exact)
  P4 workflow lint ........ unpinned runner images, actionlint
  P6 advisory drift ....... see advisory_diff.py (new advisories only)
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
PASS, FAIL, SKIP, WARN = "PASS", "FAIL", "SKIP", "WARN"

# Same convention as goblin_infra scripts/fleet-arm-gate.py: any label whose
# name contains "hold" is a hold. The shared provider's own label is merge-hold.
HOLD_RE = re.compile(r"hold", re.IGNORECASE)
QUEUE_REF_RE = re.compile(r"^gh-readonly-queue/[^/]+/pr-(\d+)-")
BOT_STATE_MSG_RE = re.compile(r"\[automated by [^\]]+\]|update memory state from monitoring", re.IGNORECASE)
STATE_PATH_GLOBS = ("MEMORY.md", "memory/*", "data/state/*", "state/*", "*.state.json", ".fleet-gate/*")
UNPINNED_RUNNER_RE = re.compile(r"runs-on:\s*['\"]?(ubuntu|windows|macos)-latest\b")


class Result:
    def __init__(self, rule: str, status: str, detail: str, fix: str = ""):
        self.rule, self.status, self.detail, self.fix = rule, status, detail, fix

    def line(self) -> str:
        tail = f"  -> {self.fix}" if self.fix and self.status in (FAIL, WARN) else ""
        return f"{self.status:<4} {self.rule}: {self.detail}{tail}"

    def as_dict(self) -> dict:
        return {"rule": self.rule, "status": self.status, "detail": self.detail, "fix": self.fix}


# ---------------------------------------------------------------- utilities
def sh(cmd: list[str], cwd: Path, timeout: int = 900, env: dict | None = None) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout,
                           env={**os.environ, **(env or {})})
        return p.returncode, (p.stdout + p.stderr)
    except FileNotFoundError:
        return 127, f"not found: {cmd[0]}"
    except subprocess.TimeoutExpired:
        return 124, f"timeout after {timeout}s: {' '.join(cmd)}"


def tail(text: str, n: int = 12) -> str:
    lines = [l for l in text.strip().splitlines() if l.strip()]
    return " | ".join(lines[-n:])[:900]


def changed_files(repo: Path, base: str) -> list[str] | None:
    rc, out = sh(["git", "merge-base", base, "HEAD"], repo, 60)
    if rc != 0:
        return None
    mb = out.strip().splitlines()[-1]
    rc, out = sh(["git", "diff", "--name-only", f"{mb}...HEAD"], repo, 60)
    if rc != 0:
        return None
    rc2, wt = sh(["git", "diff", "--name-only", "HEAD"], repo, 60)  # uncommitted work counts too
    files = set(out.split()) | (set(wt.split()) if rc2 == 0 else set())
    return sorted(f for f in files if f)


def load_config(repo: Path) -> dict:
    """Optional per-repo overrides in .toreva/preflight.json (commands win over detection)."""
    p = repo / ".toreva" / "preflight.json"
    if p.exists():
        try:
            return json.loads(p.read_text())
        except ValueError as exc:
            raise SystemExit(f"invalid {p}: {exc}")
    return {}


# ------------------------------------------------------- P1 / P2 (pure logic)
def hold_labels(labels: list[str]) -> list[str]:
    return sorted(l for l in labels if HOLD_RE.search(l))


def queue_reentry_verdict(pr_number: int, head_sha: str, runs: list[dict],
                          head_parents_of: dict[str, list[str]] | None = None,
                          head_committed_at: str | None = None) -> tuple[str, str]:
    """Decide P2 from merge_group runs (newest first, GitHub REST shape).

    Returns (status, detail). FAIL when the latest *completed* merge_group run
    for this PR concluded failure and the PR head is unchanged since that run.
    "Unchanged" is proven when the queue commit's parents include the head SHA;
    failing that, when the head commit predates the failed run. If neither can
    be shown the verdict is SKIP (unknown), never PASS.
    """
    mine = [r for r in runs if (m := QUEUE_REF_RE.match(r.get("head_branch") or "")) and int(m.group(1)) == pr_number]
    done = [r for r in mine if r.get("status") == "completed"]
    if not done:
        return PASS, f"no completed merge-queue run for PR #{pr_number}"
    last = max(done, key=lambda r: r.get("created_at") or "")
    if last.get("conclusion") != "failure":
        return PASS, f"last merge-queue run {last.get('id')} concluded {last.get('conclusion')}"
    parents = (head_parents_of or {}).get(last.get("head_sha") or "")
    if parents is not None and head_sha in parents:
        return FAIL, (f"last merge-queue run {last.get('html_url') or last.get('id')} failed on this exact head "
                      f"{head_sha[:12]}; re-queueing replays a known failure")
    if parents is not None and head_sha not in parents and head_committed_at and head_committed_at > (last.get("created_at") or ""):
        return PASS, f"head {head_sha[:12]} is newer than the failed queue run {last.get('id')}"
    if head_committed_at and head_committed_at <= (last.get("created_at") or ""):
        return FAIL, (f"last merge-queue run {last.get('html_url') or last.get('id')} failed and head {head_sha[:12]} "
                      f"was committed before it ({head_committed_at} <= {last.get('created_at')})")
    return SKIP, f"could not prove whether head changed since failed queue run {last.get('id')}"


# --------------------------------------------------------------- GitHub REST
class GitHub:
    def __init__(self, token: str | None):
        self.token = token

    def get(self, path: str) -> object:
        req = urllib.request.Request(f"https://api.github.com/{path.lstrip('/')}")
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("X-GitHub-Api-Version", "2022-11-28")
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        last = None
        for attempt in range(3):  # retry reads only
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    return json.loads(resp.read().decode())
            except (urllib.error.URLError, TimeoutError, ValueError) as exc:
                last = exc
                time.sleep(0.5 * (attempt + 1))
        raise RuntimeError(f"GET {path} failed: {last}")


def parse_pr_url(url: str) -> tuple[str, int]:
    m = re.match(r"https://github\.com/([^/]+/[^/]+)/pull/(\d+)", url or "")
    if not m:
        raise SystemExit(f"--pr must be a full PR URL, got {url!r}")
    return m.group(1), int(m.group(2))


def check_pr(pr_url: str, gh: GitHub) -> list[Result]:
    slug, number = parse_pr_url(pr_url)
    try:
        pr = gh.get(f"repos/{slug}/pulls/{number}")
    except RuntimeError as exc:
        return [Result("P1 hold-aware", SKIP, f"PR read failed: {exc}"), Result("P2 queue re-entry", SKIP, "PR read failed")]
    out = []
    labels = [l.get("name", "") for l in pr.get("labels", [])]
    held = hold_labels(labels)
    out.append(Result("P1 hold-aware", FAIL if held else PASS,
                      f"hold label(s) present: {', '.join(held)}" if held else "no hold label",
                      "do not arm or enqueue; a hold is released only by its owner"))
    head = pr["head"]["sha"]
    try:
        runs = gh.get(f"repos/{slug}/actions/runs?event=merge_group&per_page=100").get("workflow_runs", [])
        mine = [r for r in runs if (m := QUEUE_REF_RE.match(r.get("head_branch") or "")) and int(m.group(1)) == number]
        parents = {}
        if mine:
            last = max(mine, key=lambda r: r.get("created_at") or "")
            c = gh.get(f"repos/{slug}/commits/{last['head_sha']}")
            parents[last["head_sha"]] = [p["sha"] for p in c.get("parents", [])]
        hc = gh.get(f"repos/{slug}/commits/{head}")
        committed = ((hc.get("commit") or {}).get("committer") or {}).get("date")
        status, detail = queue_reentry_verdict(number, head, runs, parents, committed)
    except RuntimeError as exc:
        status, detail = SKIP, f"merge-queue history read failed: {exc}"
    out.append(Result("P2 queue re-entry", status, detail,
                      "fix the failure and push a new head (or update the branch) before re-queueing"))
    return out


# ----------------------------------------------------------- P3 / P4 checks
def check_bot_commit(repo: Path, files: list[str]) -> Result:
    rc, out = sh(["git", "log", "-1", "--format=%an%x1f%ae%x1f%B"], repo, 30)
    if rc != 0:
        return Result("P3 bot-commit hygiene", SKIP, "git log failed")
    name, email, body = (out.split("\x1f") + ["", "", ""])[:3]
    is_bot = "[bot]" in name or "[bot]" in email or bool(BOT_STATE_MSG_RE.search(body))
    state_only = bool(files) and all(any(fnmatch.fnmatch(f, g) for g in STATE_PATH_GLOBS) for f in files)
    if is_bot and (state_only or BOT_STATE_MSG_RE.search(body)) and "[skip ci]" not in body:
        return Result("P3 bot-commit hygiene", FAIL, f"bot commit of state files by {name.strip()} without [skip ci]",
                      "write state to an artifact/issue/store, never to a PR branch; if unavoidable add [skip ci]")
    return Result("P3 bot-commit hygiene", PASS, "not a bot state commit")


def lint_workflow_text(text: str) -> list[str]:
    return [f"line {i}: {l.strip()}" for i, l in enumerate(text.splitlines(), 1) if UNPINNED_RUNNER_RE.search(l)]


def check_workflows(repo: Path, files: list[str]) -> list[Result]:
    wf = [f for f in files if f.startswith(".github/workflows/") and f.endswith((".yml", ".yaml")) and (repo / f).exists()]
    if not wf:
        return [Result("P4 workflow lint", PASS, "no workflow files changed")]
    res = []
    hits = []
    for f in wf:
        hits += [f"{f} {h}" for h in lint_workflow_text((repo / f).read_text(errors="replace"))]
    res.append(Result("P4 runner pin", WARN if hits else PASS,
                      f"unpinned runner image: {'; '.join(hits[:5])}" if hits else "runner images pinned",
                      "pin runs-on to a versioned image such as ubuntu-24.04 (a runner upgrade broke goblin_ui #1805)"))
    if shutil.which("actionlint"):
        rc, out = sh(["actionlint", *wf], repo, 120)
        res.append(Result("P4 actionlint", PASS if rc == 0 else FAIL, "clean" if rc == 0 else tail(out), "fix the reported workflow errors"))
    else:
        res.append(Result("P4 actionlint", SKIP, "actionlint not installed in this sandbox"))
    return res


# ------------------------------------------------------------ P5 fast tier
def clean_worktree(repo: Path) -> tuple[Path | None, str]:
    """A fresh checkout of HEAD plus uncommitted tracked changes, without
    untracked files, node_modules or caches, so author-machine state cannot
    mask a missing dependency (sampled failure: numpy, jest-dom setup)."""
    tmp = Path(tempfile.mkdtemp(prefix="toreva-preflight-"))
    dest = tmp / "wt"
    rc, out = sh(["git", "worktree", "add", "--detach", str(dest), "HEAD"], repo, 300)
    if rc != 0:
        return None, tail(out)
    rc, diff = sh(["git", "diff", "HEAD", "--binary"], repo, 120)
    if rc == 0 and diff.strip():
        p = subprocess.run(["git", "apply", "--whitespace=nowarn", "-"], cwd=dest, input=diff, text=True, capture_output=True)
        if p.returncode != 0:
            return None, "could not carry uncommitted changes into the clean worktree"
    return dest, ""


def drop_worktree(repo: Path, dest: Path | None) -> None:
    if dest is not None:
        sh(["git", "worktree", "remove", "--force", str(dest)], repo, 120)
        shutil.rmtree(dest.parent, ignore_errors=True)


def node_pm(wt: Path) -> str | None:
    for lock, pm in (("pnpm-lock.yaml", "pnpm"), ("yarn.lock", "yarn"), ("package-lock.json", "npm"), ("bun.lockb", "bun")):
        if (wt / lock).exists():
            return pm
    return "npm" if (wt / "package.json").exists() else None


FROZEN = {"pnpm": ["pnpm", "install", "--frozen-lockfile"], "yarn": ["yarn", "install", "--frozen-lockfile"],
          "npm": ["npm", "ci"], "bun": ["bun", "install", "--frozen-lockfile"]}


def related_tests(files: list[str], wt: Path) -> list[str]:
    """Python tests whose name mentions a changed module stem (cheap heuristic)."""
    stems = {Path(f).stem.replace("-", "_") for f in files if f.endswith(".py")}
    out = []
    for t in wt.rglob("test_*.py"):
        if "node_modules" in t.parts or ".venv" in t.parts:
            continue
        if str(t.relative_to(wt)) in files or any(s and s in t.stem for s in stems):
            out.append(str(t.relative_to(wt)))
    return sorted(set(out))


def structure_scripts(wt: Path, cfg: dict) -> list[list[str]]:
    globs = cfg.get("structure_globs") or ["scripts/test-*.sh", "scripts/test-*.py", "iac/lints/ci-preflight/CI-*.sh"]
    cmds = []
    for g in globs:
        for p in sorted(wt.glob(g)):
            cmds.append(["bash", str(p.relative_to(wt))] if p.suffix == ".sh" else [sys.executable, str(p.relative_to(wt))])
    return cmds


def fast_tier(repo: Path, files: list[str], cfg: dict, tier: str, skip_install: bool) -> list[Result]:
    res: list[Result] = []
    wt, err = clean_worktree(repo)
    if wt is None:
        return [Result("P5 clean env", SKIP, f"could not create clean worktree: {err}")]
    try:
        if cfg.get("commands"):  # repo-declared commands win: they are the CI commands
            for name, cmd in cfg["commands"].items():
                rc, out = sh(["bash", "-lc", cmd], wt, int(cfg.get("timeout", 900)))
                res.append(Result(f"P5 {name}", PASS if rc == 0 else FAIL, "ok" if rc == 0 else tail(out), f"run `{cmd}` and fix before pushing"))
            return res
        pm = node_pm(wt)
        if pm:
            pkg = json.loads((wt / "package.json").read_text())
            scripts = pkg.get("scripts", {})
            if not skip_install:
                if shutil.which(pm):
                    rc, out = sh(FROZEN[pm], wt, 1200)
                    res.append(Result("P5 frozen install", PASS if rc == 0 else FAIL, f"{' '.join(FROZEN[pm])}" if rc == 0 else tail(out),
                                      "commit the lockfile that matches package.json; declare every import as a dependency"))
                    if rc != 0:
                        return res
                else:
                    res.append(Result("P5 frozen install", SKIP, f"{pm} not installed in this sandbox"))
            run = [pm, "run"] if pm != "yarn" else ["yarn"]
            for name in ("typecheck", "lint", "check:structure", "test:structure"):
                if name in scripts:
                    rc, out = sh([*run, name], wt, 900)
                    res.append(Result(f"P5 {name}", PASS if rc == 0 else FAIL, "ok" if rc == 0 else tail(out), f"`{pm} run {name}` must pass before push"))
            if "typecheck" not in scripts and (wt / "tsconfig.json").exists() and (wt / "node_modules/.bin/tsc").exists():
                rc, out = sh(["node_modules/.bin/tsc", "--noEmit"], wt, 900)
                res.append(Result("P5 tsc --NoEmit", PASS if rc == 0 else FAIL, "ok" if rc == 0 else tail(out), "fix type errors in every consumer of the changed type"))
            src = [f for f in files if f.endswith((".ts", ".tsx", ".js", ".jsx", ".mjs"))]
            if tier == "full" and "test" in scripts:
                rc, out = sh([*run, "test"], wt, 1800)
                res.append(Result("P5 tests (full)", PASS if rc == 0 else FAIL, "ok" if rc == 0 else tail(out)))
            elif src and (wt / "node_modules/.bin/vitest").exists():
                rc, out = sh(["node_modules/.bin/vitest", "related", "--run", *src], wt, 1200)
                res.append(Result("P5 tests for touched paths", PASS if rc == 0 else FAIL, "vitest related" if rc == 0 else tail(out)))
            elif src and (wt / "node_modules/.bin/jest").exists():
                rc, out = sh(["node_modules/.bin/jest", "--findRelatedTests", *src, "--passWithNoTests"], wt, 1200)
                res.append(Result("P5 tests for touched paths", PASS if rc == 0 else FAIL, "jest related" if rc == 0 else tail(out)))
            elif src:
                res.append(Result("P5 tests for touched paths", SKIP, "no vitest/jest found; use --tier full or .toreva/preflight.json"))
        reqs = [p for p in ("requirements.txt", "requirements-dev.txt", "requirements-test.txt") if (wt / p).exists()]
        if reqs or (wt / "pyproject.toml").exists():
            venv = wt / ".preflight-venv"
            py = venv / "bin" / "python"
            if not skip_install:
                rc, out = sh([sys.executable, "-m", "venv", str(venv)], wt, 300)
                for r in reqs:
                    if rc == 0:
                        rc, out = sh([str(py), "-m", "pip", "install", "-q", "-r", r], wt, 1200)
                res.append(Result("P5 clean python env", PASS if rc == 0 else FAIL, "fresh venv from requirements" if rc == 0 else tail(out),
                                  "declare every import in requirements (sampled failure: numpy missing)"))
            pyexe = str(py) if py.exists() else sys.executable
            tests = related_tests(files, wt)
            if tier == "full":
                rc, out = sh([pyexe, "-m", "pytest", "-q"], wt, 1800)
                res.append(Result("P5 pytest (full)", PASS if rc == 0 else FAIL, "ok" if rc == 0 else tail(out)))
            elif tests:
                rc, out = sh([pyexe, "-m", "pytest", "-q", *tests], wt, 1200)
                if rc == 127 or "No module named pytest" in out:
                    rc, out = 0, ""
                    for t in tests:
                        r2, o2 = sh([pyexe, t], wt, 600)
                        if r2 != 0:
                            rc, out = r2, o2
                            break
                res.append(Result("P5 tests for touched paths", PASS if rc == 0 else FAIL, f"{len(tests)} related test file(s)" if rc == 0 else tail(out)))
        scripts_ = structure_scripts(wt, cfg)
        if scripts_:
            failed = []
            for cmd in scripts_:
                rc, out = sh(cmd, wt, int(cfg.get("structure_timeout", 300)))
                if rc != 0:
                    failed.append(f"{' '.join(cmd)}: {tail(out, 4)}")
            res.append(Result("P5 structure/registry scripts", FAIL if failed else PASS,
                              f"{len(failed)}/{len(scripts_)} failed: " + " || ".join(failed[:3]) if failed else f"{len(scripts_)} script(s) ok",
                              "a co-change is missing (allowlist, baseline, workflow wiring, required string); update it in the same PR"))
        if not res:
            res.append(Result("P5 fast tier", SKIP, "no recognised toolchain; add .toreva/preflight.json with the repo's CI commands"))
        return res
    finally:
        drop_worktree(repo, wt)


# --------------------------------------------------------------------- main
def run(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo-dir", default=".")
    ap.add_argument("--base", default="origin/main", help="ref the PR will merge into")
    ap.add_argument("--pr", help="PR URL: also run P1 hold-aware and P2 queue re-entry (needs GITHUB_TOKEN or GH_TOKEN for private repos)")
    ap.add_argument("--tier", choices=["fast", "full"], default="fast")
    ap.add_argument("--skip-install", action="store_true", help="reuse existing deps (weaker: may hide missing dependencies)")
    ap.add_argument("--only", help="comma list of rule prefixes to run, e.g. P1,P2")
    ap.add_argument("--json", help="write a machine-readable report here")
    a = ap.parse_args(argv)
    repo = Path(a.repo_dir).resolve()
    if not (repo / ".git").exists():
        print("not a git checkout", file=sys.stderr)
        return 2
    only = [x.strip() for x in a.only.split(",")] if a.only else None
    want = lambda p: only is None or p in only
    cfg = load_config(repo)
    files = changed_files(repo, a.base)
    results: list[Result] = []
    if files is None:
        results.append(Result("diff", SKIP, f"could not diff against {a.base}; run `git fetch origin` first"))
        files = []
    if want("P3"):
        results.append(check_bot_commit(repo, files))
    if want("P4"):
        results += check_workflows(repo, files)
    if want("P5"):
        results += fast_tier(repo, files, cfg, a.tier, a.skip_install)
    if a.pr and (want("P1") or want("P2")):
        results += [r for r in check_pr(a.pr, GitHub(os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN"))) if want(r.rule[:2])]
    for r in results:
        print(r.line())
    fails = [r for r in results if r.status == FAIL]
    print(f"toreva-preflight: {len(fails)} FAIL, {sum(r.status == SKIP for r in results)} SKIP (unknown), "
          f"{sum(r.status == PASS for r in results)} PASS -> {'DO NOT PUSH / DO NOT ARM' if fails else 'ok to push'}")
    if a.json:
        Path(a.json).write_text(json.dumps({"repo": str(repo), "base": a.base, "pr": a.pr, "changed_files": files,
                                            "results": [r.as_dict() for r in results], "fail": bool(fails)}, indent=1))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(run())
