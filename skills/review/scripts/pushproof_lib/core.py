"""Shared constants and helpers: git, review state, line ranges, JSON."""
import argparse
import datetime
import hashlib
import json
import os
import re
import shlex
import signal
import subprocess
import sys


STATE = ".pushproof"
MAX_DIFF_LINES_DEFAULT = 4000
SEVERITIES = ["critical", "high", "medium", "low"]
PENALTY = {"critical": 3.0, "high": 1.5, "medium": 0.5, "low": 0.1}
EVIDENCE_TAIL = 1  # slack lines after the cited range (evidence must START on `line`)
CONTEXT_RADIUS = 3  # lines either side hashed to detect "the cited code changed"
PASSING = ("PASS", "PASS_WITH_NOTES")
HOOK_MARKER = "# pushproof pre-push gate"
# PR mode is read-only on GitHub: these are the only gh subcommands pushproof may run.
GH_READ_ONLY = {("pr", "view")}
REVIEW_ACTION = {
    "PASS": "Approve",
    "PASS_WITH_NOTES": "Approve with comments",
    "BLOCK": "Request changes",
    "INCOMPLETE": "No decision: the review is incomplete",
}
PR_URL = re.compile(r"^https?://([^/]+)/([^/]+)/([^/]+)/pull/(\d+)")
PROTECTED_STATE = ("config.json", "verdicts.json", "feedback.json", "report.json", "report.md")


# ---------------------------------------------------------------- git helpers
def run_git(args, check=True):
    r = subprocess.run(["git", *args], capture_output=True, text=True)
    if check and r.returncode != 0:
        sys.exit(f"error: git {' '.join(args)} failed: {r.stderr.strip()}")
    return r


def git(*args):
    return run_git(list(args)).stdout


def repo_root():
    r = run_git(["rev-parse", "--show-toplevel"], check=False)
    if r.returncode != 0:
        sys.exit("error: not inside a git repository.")
    return r.stdout.strip()


def ref_exists(ref):
    return run_git(["rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"], check=False).returncode == 0


def detect_base():
    r = run_git(["symbolic-ref", "-q", "--short", "refs/remotes/origin/HEAD"], check=False)
    if r.returncode == 0 and r.stdout.strip() and ref_exists(r.stdout.strip()):
        return r.stdout.strip()
    for cand in ("origin/main", "origin/master", "origin/develop", "main", "master", "develop"):
        if ref_exists(cand):
            return cand
    sys.exit("error: could not detect a base branch; use --base <ref>.")


def file_lines(rev, path):
    r = run_git(["show", f"{rev}:{path}"], check=False)
    if r.returncode != 0:
        return None
    return r.stdout.splitlines()


# -------------------------------------------------------------- state helpers
def state_dir(root):
    d = os.path.join(root, STATE)
    os.makedirs(d, exist_ok=True)
    excl = run_git(["rev-parse", "--git-path", "info/exclude"]).stdout.strip()
    excl = excl if os.path.isabs(excl) else os.path.join(root, excl)
    os.makedirs(os.path.dirname(excl), exist_ok=True)
    existing = open(excl).read() if os.path.exists(excl) else ""
    if f"{STATE}/" not in existing.split("\n"):
        with open(excl, "a") as f:
            f.write(("" if existing.endswith("\n") or not existing else "\n") + f"{STATE}/\n")
    return d


def load_manifest(sd):
    p = os.path.join(sd, "manifest.json")
    if not os.path.exists(p):
        sys.exit("error: no round in progress; run `pushproof.py scope` first.")
    return json.load(open(p))


def log_event(sd, ev):
    ev["ts"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    with open(os.path.join(sd, "reads.jsonl"), "a") as f:
        f.write(json.dumps(ev) + "\n")


def load_events(sd):
    p = os.path.join(sd, "reads.jsonl")
    if not os.path.exists(p):
        return []
    return [json.loads(l) for l in open(p) if l.strip()]


# ------------------------------------------------------------- range helpers
def merge_ranges(rs):
    out = []
    for s, e in sorted(rs):
        if out and s <= out[-1][1] + 1:
            out[-1][1] = max(out[-1][1], e)
        else:
            out.append([s, e])
    return out


def uncovered(need, have):
    """Return parts of ranges in `need` not inside union of `have`."""
    have = merge_ranges(have)
    gaps = []
    for s, e in need:
        cur = s
        for hs, he in have:
            if he < cur:
                continue
            if hs > e:
                break
            if hs > cur:
                gaps.append([cur, hs - 1])
            cur = max(cur, he + 1)
            if cur > e:
                break
        if cur <= e:
            gaps.append([cur, e])
    return gaps


def fmt_ranges(rs):
    return ", ".join(f"{s}" if s == e else f"{s}-{e}" for s, e in rs)


def norm(s):
    return re.sub(r"\s+", " ", s).strip()


def load_json(path, default):
    if not os.path.exists(path):
        return default
    try:
        return json.load(open(path))
    except (json.JSONDecodeError, OSError):
        return default


def save_json(path, data):
    tmp = path + ".tmp"
    json.dump(data, open(tmp, "w"), indent=2)
    os.replace(tmp, path)
