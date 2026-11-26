#!/usr/bin/env python3
"""Failure digest: classify failed runs against signatures.yaml (feedback loop).

Runs outside GitHub Actions (cloud agent / sandbox), read-only, so it costs no
Actions minutes:

    python3 digest.py match --log failed-job-tail.txt            # one log
    python3 digest.py daily --orgs toreva,goblin-agent --since 2026-11-26T00:00:00Z --out digest/

For every failed run it fetches the failed jobs' log tails (4 KB is enough for
every seed signature), then:
  * preventable signature hit -> ESCAPE for that rule (preflight not run, or rule gap)
  * signal signature hit      -> real runtime signal, routed as-is
  * secondary hit only        -> cascade symptom; root cause is another job
  * no hit                    -> NOVEL: a candidate signature is drafted into
                                 novel-candidates.yaml for HUMAN / independent
                                 review. Nothing is self-certified or auto-merged.
Zero-job runs and merge-queue re-entries are classified from run metadata.
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
QUEUE_REF_RE = re.compile(r"^gh-readonly-queue/[^/]+/pr-(\d+)-")
TS_RE = re.compile(r"^\d{4}-\d\d-\d\dT[\d:.]+Z\s*", re.M)
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def load_signatures(path: Path | None = None) -> list[dict]:
    path = path or HERE / "signatures.yaml"
    try:
        import yaml  # type: ignore
        data = yaml.safe_load(path.read_text())
    except ImportError:
        data = json.loads((path.with_suffix(".json")).read_text())
    sigs = data["signatures"]
    for s in sigs:
        m = s.get("match") or {}
        pat = m.get("log") or m.get("log_or_message")
        s["_re"] = re.compile(pat) if pat else None
    return sigs


def clean(text: str) -> str:
    return ANSI_RE.sub("", TS_RE.sub("", text))


def match(log: str, sigs: list[dict]) -> list[dict]:
    log = clean(log)
    return [s for s in sigs if s.get("_re") is not None and s["_re"].search(log)]


def verdict(hits: list[dict]) -> tuple[str, list[str]]:
    """Pick the root classification from all hits (secondary never wins)."""
    roots = [h for h in hits if h.get("status") != "secondary"]
    if not hits:
        return "NOVEL", []
    if not roots:
        return "SECONDARY_ONLY", [h["id"] for h in hits]
    if any(h.get("status") == "preventable" for h in roots):
        return "ESCAPE", [h["id"] for h in roots if h.get("status") == "preventable"]
    return "SIGNAL", [h["id"] for h in roots]


ERR_LINE_RE = re.compile(r"(##\[error\]|error|Error|FAIL|failed|AssertionError|Traceback|refus)", re.I)
NOISE_RE = re.compile(r"Process completed with exit code|ELIFECYCLE|Node\.js 20|Node 20 is being deprecated|^=== |^Run \d+: \d+ failed of|conclusion: failure|failed with exit code 128")


def candidate(log: str, job_name: str = "") -> dict:
    """Draft a signature from the most specific error line (for human review)."""
    lines = [l.strip() for l in clean(log).splitlines() if l.strip()]
    errs = [l for l in lines if ERR_LINE_RE.search(l) and not NOISE_RE.search(l)]
    line = (errs[-1] if errs else (lines[-1] if lines else ""))[:300]
    # Tokenise first, then escape, so placeholders never get mangled by re.escape.
    tok = re.sub(r"(?:/[\w.\-]+){2,}", "\x00P", line)          # paths
    tok = re.sub(r"\b[0-9a-f]{7,40}\b", "\x00H", tok)            # shas
    tok = re.sub(r"\d+", "\x00N", tok)                             # numbers
    norm = re.escape(tok).replace("\x00P", r"\S+").replace("\x00H", r"[0-9a-f]+").replace("\x00N", r"\d+")
    return {"id": "NEW-<name-me>", "rule": "<P-rule or none>", "status": "<preventable|signal|secondary>",
            "match": {"log": norm}, "job": job_name, "evidence_line": line}


# --------------------------------------------------------------- GitHub REST
def gh_get(path: str, token: str | None, raw: bool = False):
    url = path if path.startswith("http") else f"https://api.github.com/{path.lstrip('/')}"
    req = urllib.request.Request(url)
    req.add_header("Accept", "application/vnd.github+json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=60) as resp:
        body = resp.read()
    return body.decode(errors="replace") if raw else json.loads(body.decode())


def daily(orgs: list[str], since: str, out: Path, token: str | None, max_runs: int, sigs: list[dict]) -> int:
    out.mkdir(parents=True, exist_ok=True)
    rows, novel = [], []
    seen_queue_fail: dict[tuple[str, int], int] = collections.Counter()
    for org in orgs:
        page, repos = 1, []
        while True:
            batch = gh_get(f"orgs/{org}/repos?per_page=100&page={page}", token)
            repos += [r["full_name"] for r in batch if not r.get("archived")]
            if len(batch) < 100:
                break
            page += 1
        for slug in repos:
            try:
                runs = gh_get(f"repos/{slug}/actions/runs?status=failure&created=>={since}&per_page=100", token).get("workflow_runs", [])
            except urllib.error.HTTPError:
                continue
            for r in sorted(runs, key=lambda x: x["created_at"]):
                if len(rows) >= max_runs:
                    break
                row = {"repo": slug, "run": r["html_url"], "workflow": r["name"], "event": r["event"], "created": r["created_at"]}
                m = QUEUE_REF_RE.match(r.get("head_branch") or "")
                if r["event"] == "merge_group" and m:
                    k = (slug, int(m.group(1)))
                    seen_queue_fail[k] += 1
                    if seen_queue_fail[k] > 1:
                        row.update(verdict="ESCAPE", ids=["S02-queue-reentry"])
                        rows.append(row)
                        continue
                jobs = gh_get(f"repos/{slug}/actions/runs/{r['id']}/jobs?filter=latest&per_page=100", token).get("jobs", [])
                failed = [j for j in jobs if j.get("conclusion") == "failure"]
                if not jobs:
                    row.update(verdict="ESCAPE", ids=["S03-bot-state-commit/zero-job"], note="run had no jobs")
                    rows.append(row)
                    continue
                text = ""
                for j in failed[:3]:
                    try:
                        text += f"\n=== {j['name']}\n" + gh_get(f"repos/{slug}/actions/jobs/{j['id']}/logs", token, raw=True)[-4096:]
                    except urllib.error.HTTPError as exc:
                        text += f"\n=== {j['name']} (log unavailable: {exc.code})\n"
                v, ids = verdict(match(text, sigs))
                row.update(verdict=v, ids=ids, jobs=[j["name"] for j in failed])
                if v == "NOVEL":
                    c = candidate(text, failed[0]["name"] if failed else "")
                    c["example"] = r["html_url"]
                    novel.append(c)
                rows.append(row)
    counts = collections.Counter(r["verdict"] for r in rows)
    by_rule = collections.Counter(i for r in rows if r["verdict"] == "ESCAPE" for i in r["ids"])
    (out / "digest.json").write_text(json.dumps(rows, indent=1))
    (out / "novel-candidates.json").write_text(json.dumps(novel, indent=1))
    md = [f"# Failed-run digest since {since}", "", f"Runs classified: {len(rows)} (cap {max_runs})", ""]
    md += [f"- {k}: {v}" for k, v in counts.most_common()]
    md += ["", "## Escapes by signature (preflight should have stopped these)", ""] + [f"- {k}: {v}" for k, v in by_rule.most_common()]
    md += ["", f"## Novel failures: {len(novel)} candidate signature(s) in novel-candidates.json, awaiting human review", ""]
    md += [f"- {c['example']}: `{c['evidence_line'][:160]}`" for c in novel[:30]]
    (out / "digest.md").write_text("\n".join(md) + "\n")
    print("\n".join(md))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    m = sub.add_parser("match")
    m.add_argument("--log", required=True)
    m.add_argument("--job", default="")
    d = sub.add_parser("daily")
    d.add_argument("--orgs", required=True)
    d.add_argument("--since", required=True, help="ISO time, e.g. 2026-11-26T00:00:00Z")
    d.add_argument("--out", default="digest")
    d.add_argument("--max-runs", type=int, default=500, help="API budget guard")
    ap.add_argument("--signatures", type=Path)
    a = ap.parse_args(argv)
    sigs = load_signatures(a.signatures)
    if a.cmd == "match":
        text = Path(a.log).read_text(errors="replace")
        v, ids = verdict(match(text, sigs))
        res = {"verdict": v, "ids": ids}
        if v == "NOVEL":
            res["candidate"] = candidate(text, a.job)
        print(json.dumps(res, indent=1))
        return 0
    return daily([o.strip() for o in a.orgs.split(",")], a.since, Path(a.out),
                 os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN"), a.max_runs, sigs)


if __name__ == "__main__":
    sys.exit(main())
