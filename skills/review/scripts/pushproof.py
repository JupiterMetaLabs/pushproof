#!/usr/bin/env python3
"""pushproof helper (stdlib only).

Subcommands:
  scope     Fix the round: resolve base, list changed files and changed lines.
  diff      Print the branch diff (logged as viewed, per file).
  read      Print a file at the scoped HEAD (or base) with line numbers (logged).
  coverage  Show which in-scope files / changed lines are not yet covered.
  check     Validate findings, apply recorded disputes, score and verdict; write report.
  feedback  Record, list or remove a dispute against a finding.
  gate      Turn the push gate on/off, show status, install/uninstall a git pre-push hook.
  message   (PR mode) Print the verdict and a paste-ready review message. Never posts to GitHub.
  hook      (internal) Claude Code PreToolUse hook entry point; reads JSON on stdin.
  prepush   (internal) git pre-push hook entry point.
"""
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


# ------------------------------------------------------------------ gh / PRs
def gh(args, timeout=60):
    if tuple(args[:2]) not in GH_READ_ONLY:
        return None, f"refused: pushproof is read-only on GitHub and does not run `gh {' '.join(args[:2])}`"
    try:
        r = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        return None, "GitHub CLI `gh` is not installed"
    except subprocess.TimeoutExpired:
        return None, f"`gh {' '.join(args[:3])}` timed out"
    if r.returncode != 0:
        return None, (r.stderr.strip() or r.stdout.strip() or f"gh exited {r.returncode}")
    return r.stdout, None


def gh_json(args, timeout=60):
    out, err = gh(args, timeout)
    if err:
        return None, err
    try:
        return json.loads(out), None
    except json.JSONDecodeError:
        return None, "gh returned non-JSON output"


def pr_slug(pr):
    return f"{pr['host']}/{pr['owner']}/{pr['repo']}"


def pr_live_head(pr):
    data, err = gh_json(["pr", "view", str(pr["number"]), "--repo", pr_slug(pr), "--json", "headRefOid,state"])
    if err:
        return None, err
    return data["headRefOid"], None


def remote_for(owner, repo):
    want = f"{owner}/{repo}".lower()
    for name in git("remote").split():
        url = git("remote", "get-url", name).strip().lower()
        url = url[:-4] if url.endswith(".git") else url
        if url.endswith("/" + want) or url.endswith(":" + want):
            return name
    return None


def resolve_pr(arg):
    data, err = gh_json(["pr", "view", str(arg), "--json",
                         "number,url,title,state,baseRefName,headRefName,headRefOid,isCrossRepository"])
    if err:
        sys.exit(f"error: could not load PR {arg}: {err}")
    m = PR_URL.match(data["url"])
    if not m:
        sys.exit(f"error: unexpected PR url {data['url']}")
    host, owner, repo, num = m.groups()
    if data["state"] != "OPEN":
        print(f"WARNING: PR #{num} is {data['state']}.")
    remote = remote_for(owner, repo)
    if not remote:
        sys.exit(f"error: no git remote points at {owner}/{repo}; add one, e.g. "
                 f"`git remote add upstream https://{host}/{owner}/{repo}.git`")
    base_ref = f"refs/pushproof/pr/{num}/base"
    head_ref = f"refs/pushproof/pr/{num}/head"
    r = run_git(["fetch", "--quiet", "--no-tags", remote,
                 f"+refs/heads/{data['baseRefName']}:{base_ref}",
                 f"+refs/pull/{num}/head:{head_ref}"], check=False)
    if r.returncode != 0:
        sys.exit(f"error: fetching PR #{num} from {remote} failed: {r.stderr.strip()}")
    fetched = git("rev-parse", head_ref).strip()
    if fetched != data["headRefOid"]:
        sys.exit(f"error: PR #{num} head changed during fetch ({data['headRefOid'][:10]} -> "
                 f"{fetched[:10]}); run scope again.")
    return {"number": int(num), "url": data["url"], "title": data["title"], "host": host,
            "owner": owner, "repo": repo, "remote": remote, "base_name": data["baseRefName"],
            "head_name": data["headRefName"], "fork": data.get("isCrossRepository", False),
            "base_ref": base_ref}, fetched


def current_head(m):
    """(sha, error) for what the round is supposed to track: PR head or local HEAD."""
    if m.get("pr"):
        return pr_live_head(m["pr"])
    return git("rev-parse", "HEAD").strip(), None


# ---------------------------------------------------------------------- scope
HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def changed_new_lines(mb, head, path, old_path):
    args = ["diff", "-U0", "--no-color", "-M", mb, head, "--"]
    args += [old_path, path] if old_path and old_path != path else [path]
    ranges = []
    for line in git(*args).splitlines():
        m = HUNK.match(line)
        if m:
            start = int(m.group(1))
            count = int(m.group(2)) if m.group(2) is not None else 1
            if count == 0:  # pure deletion: anchor on surrounding line
                ranges.append([max(start, 1), max(start, 1)])
            else:
                ranges.append([start, start + count - 1])
    return merge_ranges(ranges)


def cmd_scope(a):
    root = repo_root()
    os.chdir(root)
    sd = state_dir(root)
    pr = None
    if a.pr:
        if a.base:
            sys.exit("error: --base cannot be combined with --pr (the PR defines its base).")
        pr, head = resolve_pr(a.pr)
        base = pr["base_ref"]
        branch = f"PR #{pr['number']} ({pr['head_name']} -> {pr['base_name']})"
        dirty = False
    else:
        base = a.base or detect_base()
        if not ref_exists(base):
            sys.exit(f"error: base ref '{base}' not found.")
        head = git("rev-parse", "HEAD").strip()
        branch = git("rev-parse", "--abbrev-ref", "HEAD").strip()
        if branch == "HEAD":
            branch = "(detached HEAD)"
        dirty = bool(git("status", "--porcelain", "--untracked-files=no").strip())
    mb = run_git(["merge-base", base, head], check=False).stdout.strip()
    if not mb:
        sys.exit(f"error: {head[:10]} and {base} share no history.")

    binaries = set()
    toks = git("diff", "--numstat", "-z", "-M", "--no-color", mb, head).split("\0")
    i = 0
    while i < len(toks):
        if not toks[i]:
            i += 1
            continue
        added, deleted, path = toks[i].split("\t", 2)
        if path == "":  # rename/copy: old and new paths follow as separate tokens
            path = toks[i + 2]
            i += 3
        else:
            i += 1
        if added == "-" and deleted == "-":
            binaries.add(path)

    files = []
    for l in git("diff", "--name-status", "-M", "--no-color", mb, head).splitlines():
        parts = l.split("\t")
        status = parts[0][0]
        if status == "R" or status == "C":
            old, path = parts[1], parts[2]
        else:
            old, path = None, parts[1]
        binary = path in binaries
        changed = [] if (status == "D" or binary) else changed_new_lines(mb, head, path, old)
        files.append({"path": path, "old_path": old, "status": status,
                      "binary": binary, "changed": changed})

    patch = git("diff", "-M", "--no-color", mb, head)
    diff_lines = patch.count("\n")
    for name in ("reads.jsonl", "findings.json", "report.md", "report.json"):
        p = os.path.join(sd, name)
        if os.path.exists(p):
            os.remove(p)
    open(os.path.join(sd, "diff.patch"), "w").write(patch)
    manifest = {
        "created": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "base": base, "merge_base": mb, "head": head, "branch": branch,
        "dirty": dirty, "diff_lines": diff_lines,
        "max_diff_lines": a.max_diff_lines,
        "oversize": diff_lines > a.max_diff_lines,
        "files": files, "pr": pr,
    }
    json.dump(manifest, open(os.path.join(sd, "manifest.json"), "w"), indent=2)

    print(f"Round fixed: {branch} @ {head[:10]} vs {base} (merge-base {mb[:10]})")
    print(f"{len(files)} file(s) in scope, {diff_lines} diff lines")
    if not files:
        print("Nothing to review: branch has no changes against its base.")
    for f in files:
        extra = " [binary]" if f["binary"] else ""
        rn = f" (from {f['old_path']})" if f["old_path"] else ""
        ch = f" changed: {fmt_ranges(f['changed'])}" if f["changed"] else ""
        print(f"  {f['status']} {f['path']}{rn}{extra}{ch}")
    if dirty:
        print("WARNING: uncommitted changes exist; only committed HEAD is reviewed.")
    if manifest["oversize"]:
        print(f"WARNING: diff exceeds {a.max_diff_lines} lines; verdict will be INCOMPLETE "
              "unless check is run with --allow-large.")


# ----------------------------------------------------------------- diff/read
def cmd_diff(a):
    root = repo_root()
    os.chdir(root)
    sd = state_dir(root)
    m = load_manifest(sd)
    scope = {f["path"]: f for f in m["files"]}
    targets = a.paths or list(scope)
    for p in targets:
        f = scope.get(p)
        if not f:
            print(f"(not in scope: {p})", file=sys.stderr)
            continue
        args = ["diff", "-M", "--no-color", m["merge_base"], m["head"], "--"]
        args += [f["old_path"], p] if f["old_path"] else [p]
        sys.stdout.write(git(*args))
        log_event(sd, {"kind": "diff", "path": p})


def parse_lines_arg(s, n):
    if not s:
        return 1, n
    m = re.fullmatch(r"(\d+)(?::(\d+))?", s)
    if not m:
        sys.exit("error: --lines must be START or START:END")
    start = int(m.group(1))
    end = int(m.group(2)) if m.group(2) else start
    return max(1, start), min(n, end)


def cmd_read(a):
    root = repo_root()
    os.chdir(root)
    sd = state_dir(root)
    m = load_manifest(sd)
    rev_name = "base" if a.base else "head"
    rev = m["merge_base"] if a.base else m["head"]
    lines = file_lines(rev, a.path)
    if lines is None:
        sys.exit(f"error: {a.path} does not exist at {rev_name} ({rev[:10]}).")
    if not lines:
        print(f"({a.path} is empty)")
        log_event(sd, {"kind": "read", "path": a.path, "rev": rev_name, "start": 1, "end": 1})
        return
    s, e = parse_lines_arg(a.lines, len(lines))
    if s > e:
        sys.exit(f"error: range outside file ({len(lines)} lines).")
    print(f"== {a.path} @ {rev_name} lines {s}-{e} of {len(lines)} ==")
    for i in range(s, e + 1):
        print(f"{i:>6}\t{lines[i - 1]}")
    log_event(sd, {"kind": "read", "path": a.path, "rev": rev_name, "start": s, "end": e})


# ------------------------------------------------------------------ coverage
def compute_coverage(m, events):
    diffed = {e["path"] for e in events if e["kind"] == "diff"}
    reads = {}
    for e in events:
        if e["kind"] == "read" and e["rev"] == "head":
            reads.setdefault(e["path"], []).append([e["start"], e["end"]])
    gaps = []
    for f in m["files"]:
        p = f["path"]
        missing = []
        if p not in diffed:
            missing.append("diff not viewed")
        if f["status"] != "D" and not f["binary"]:
            g = uncovered(f["changed"], reads.get(p, []))
            if g:
                missing.append(f"changed lines not read: {fmt_ranges(g)}")
        if missing:
            gaps.append({"path": p, "missing": missing})
    return gaps, reads


def cmd_coverage(a):
    root = repo_root()
    os.chdir(root)
    sd = state_dir(root)
    m = load_manifest(sd)
    gaps, _ = compute_coverage(m, load_events(sd))
    done = len(m["files"]) - len(gaps)
    print(f"Coverage: {done}/{len(m['files'])} files complete")
    for g in gaps:
        print(f"  {g['path']}: {'; '.join(g['missing'])}")


# --------------------------------------------------------------------- check
def norm(s):
    return re.sub(r"\s+", " ", s).strip()


def finding_id(f):
    key = f"{f.get('file')}|{norm(str(f.get('evidence', '')))}"
    return "PP-" + hashlib.sha1(key.encode()).hexdigest()[:6]


def context_hash(m, f):
    rev = m["head"] if f.get("rev", "head") == "head" else m["merge_base"]
    lines = file_lines(rev, f["file"]) or []
    end = f.get("end_line", f["line"])
    lo, hi = max(1, f["line"] - CONTEXT_RADIUS), min(len(lines), end + CONTEXT_RADIUS)
    blob = "\n".join(norm(l) for l in lines[lo - 1:hi])
    return hashlib.sha256(f"{f['file']}\n{blob}".encode()).hexdigest()[:16]


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


def validate_finding(f, m, reads_all):
    req = ["severity", "confidence", "title", "file", "line", "evidence", "impact", "fix", "verify"]
    miss = [k for k in req if f.get(k) in (None, "")]
    if miss:
        return f"missing fields: {', '.join(miss)}"
    if f["severity"] not in SEVERITIES:
        return f"severity must be one of {SEVERITIES}"
    c = f["confidence"]
    if not isinstance(c, int) or c % 5 != 0 or c > 100:
        return "confidence must be an integer multiple of 5, max 100"
    if c < 50:
        return "confidence below 50 is a hypothesis, not a finding; move it to 'hypotheses'"
    if c < 100 and not f.get("to_confirm"):
        return "confidence below 100 requires 'to_confirm'"
    rev_name = f.get("rev", "head")
    if rev_name not in ("head", "base"):
        return "rev must be 'head' or 'base'"
    rev = m["head"] if rev_name == "head" else m["merge_base"]
    lines = file_lines(rev, f["file"])
    if lines is None:
        return f"file does not exist at {rev_name}"
    line, end = f["line"], f.get("end_line", f["line"])
    if not isinstance(line, int) or not isinstance(end, int) or line < 1 or end < line or end > len(lines):
        return f"line range {line}-{end} invalid (file has {len(lines)} lines)"
    ev_lines = [l for l in str(f["evidence"]).splitlines() if l.strip()]
    if not ev_lines:
        return "evidence is empty"
    if norm(ev_lines[0]) not in norm(lines[line - 1]):
        return f"evidence does not start on cited line {f['file']}:{line}"
    hi = min(len(lines), max(end, line + len(ev_lines) - 1) + EVIDENCE_TAIL)
    if norm(f["evidence"]) not in norm("\n".join(lines[line - 1:hi])):
        return f"evidence text not found at {f['file']}:{line}-{end}"
    read_ranges = reads_all.get((f["file"], rev_name), [])
    if uncovered([[line, end]], read_ranges):
        return f"cited lines {line}-{end} were never read at {rev_name}"
    return None


def cmd_check(a):
    root = repo_root()
    os.chdir(root)
    sd = state_dir(root)
    m = load_manifest(sd)
    events = load_events(sd)
    incomplete = []

    cur, err = current_head(m)
    if err:
        incomplete.append(f"could not confirm the PR head on GitHub: {err}")
    elif cur != m["head"]:
        what = "PR head" if m.get("pr") else "branch"
        incomplete.append(f"{what} moved since scope ({m['head'][:10]} -> {cur[:10]}); re-run scope")
    if m["oversize"] and not a.allow_large:
        incomplete.append(f"diff is {m['diff_lines']} lines (> {m['max_diff_lines']}); "
                          "split the branch or re-run check with --allow-large")

    gaps, _ = compute_coverage(m, events)
    for g in gaps:
        incomplete.append(f"not covered: {g['path']} ({'; '.join(g['missing'])})")

    reads_all = {}
    for e in events:
        if e["kind"] == "read":
            reads_all.setdefault((e["path"], e["rev"]), []).append([e["start"], e["end"]])

    fpath = a.findings or os.path.join(sd, "findings.json")
    data = {"findings": [], "hypotheses": [], "summary": ""}
    if not os.path.exists(fpath):
        incomplete.append(f"findings file missing: {os.path.relpath(fpath, root)}")
    else:
        try:
            data = json.load(open(fpath))
        except json.JSONDecodeError as ex:
            incomplete.append(f"findings file is not valid JSON: {ex}")
            data = {"findings": [], "hypotheses": [], "summary": ""}

    accepted, rejected, seen = [], [], {}
    for f in data.get("findings", []):
        err = validate_finding(f, m, reads_all)
        fid = finding_id(f)
        seen[fid] = seen.get(fid, 0) + 1
        f["id"] = fid if seen[fid] == 1 else f"{fid}-{seen[fid]}"
        (rejected if err else accepted).append({**f, **({"rejected": err} if err else {})})
    if rejected:
        incomplete.append(f"{len(rejected)} finding(s) failed validation; fix and re-run check")

    disputes = load_json(os.path.join(sd, "feedback.json"), {})
    explained, scored = [], []
    for f in accepted:
        d = disputes.get(f["id"])
        if d:
            same = d.get("context_hash") == context_hash(m, f)
            if not same:
                f["dispute_note"] = ("code changed since the explanation was recorded; "
                                     "the earlier explanation no longer applies")
            elif d["decision"] == "accepted":
                explained.append({**f, "intended": d["intended"], "reason": d["reason"]})
                continue
            else:
                f["dispute_note"] = f"explanation considered and rejected: {d['reason']}"
        scored.append(f)
    accepted = scored

    accepted.sort(key=lambda f: (SEVERITIES.index(f["severity"]), -f["confidence"], f["file"], f["line"]))
    counts = {s: sum(1 for f in accepted if f["severity"] == s) for s in SEVERITIES}
    score = max(0.0, 5.0 - sum(PENALTY[s] * n for s, n in counts.items()))
    if counts["critical"]:
        score = min(score, 1.0)
    score = round(score, 1)

    if incomplete:
        verdict = "INCOMPLETE"
    elif counts["critical"] or counts["high"]:
        verdict = "BLOCK"
    elif counts["medium"] or counts["low"]:
        verdict = "PASS_WITH_NOTES"
    else:
        verdict = "PASS"

    report = {
        "verdict": verdict, "score": score, "counts": counts,
        "branch": m["branch"], "head": m["head"], "pr": m.get("pr"), "base": m["base"], "merge_base": m["merge_base"],
        "files_in_scope": len(m["files"]), "dirty": m["dirty"],
        "incomplete_reasons": incomplete, "findings": accepted, "rejected": rejected,
        "explained": explained,
        "hypotheses": data.get("hypotheses", []), "summary": data.get("summary", ""),
    }
    json.dump(report, open(os.path.join(sd, "report.json"), "w"), indent=2)
    md = render(report)
    open(os.path.join(sd, "report.md"), "w").write(md)
    if m.get("pr"):
        open(os.path.join(sd, "pr-message.md"), "w").write(render_pr_message(report))
    if cur == m["head"]:
        hist_path = os.path.join(sd, "verdicts.json")
        hist = load_json(hist_path, {})
        hist[m["head"]] = {"verdict": verdict, "score": score, "base": m["base"],
                           "pr": (m.get("pr") or {}).get("number"),
                           "ts": datetime.datetime.now(datetime.timezone.utc).isoformat()}
        save_json(hist_path, hist)
    print(md)
    sys.exit({"PASS": 0, "PASS_WITH_NOTES": 0, "BLOCK": 1, "INCOMPLETE": 2}[verdict])


def render(r):
    o = [f"# pushproof review: {r['verdict']}  (score {r['score']}/5)", "",
         f"`{r['branch']}` @ `{r['head'][:10]}` vs `{r['base']}` (merge-base `{r['merge_base'][:10]}`), "
         f"{r['files_in_scope']} file(s) in scope.", ""]
    c = r["counts"]
    o.append(f"Findings: {c['critical']} critical, {c['high']} high, {c['medium']} medium, {c['low']} low.")
    if r["dirty"]:
        o.append("\nNote: uncommitted changes were present and were not reviewed.")
    if r["summary"]:
        o += ["", r["summary"]]
    if r["incomplete_reasons"]:
        o += ["", "## Why INCOMPLETE"] + [f"- {x}" for x in r["incomplete_reasons"]]
    for f in r["findings"]:
        loc = f"{f['file']}:{f['line']}" + (f"-{f['end_line']}" if f.get("end_line", f["line"]) != f["line"] else "")
        o += ["", f"## [{f['severity'].upper()}] {f['id']} ({f['confidence']}%): {f['title']}",
              f"Location: `{loc}`" + (" (base)" if f.get("rev") == "base" else ""),
              "", "Evidence:", "```", f["evidence"], "```",
              f"Impact: {f['impact']}", f"Fix: {f['fix']}", f"Verify: {f['verify']}"]
        if f.get("to_confirm"):
            o.append(f"To confirm: {f['to_confirm']}")
        if f.get("dispute_note"):
            o.append(f"Dispute: {f['dispute_note']}")
    if r.get("explained"):
        o += ["", "## Accepted explanations (not scored; re-checked if the code changes)"]
        for f in r["explained"]:
            o.append(f"- {f['id']} `{f['file']}:{f['line']}` {f['title']}: intended: {f['intended']} "
                     f"(accepted because: {f['reason']})")
    if r["rejected"]:
        o += ["", "## Rejected findings (must be fixed)"]
        o += [f"- {f['id']} `{f.get('file')}:{f.get('line')}` {f.get('title')}: {f['rejected']}" for f in r["rejected"]]
    if r["hypotheses"]:
        o += ["", "## Open hypotheses (<50%, not scored)"]
        for h in r["hypotheses"]:
            o.append(f"- {h.get('claim', h) if isinstance(h, dict) else h}"
                     + (f" | test: {h['test']}" if isinstance(h, dict) and h.get("test") else ""))
    return "\n".join(o) + "\n"



# ------------------------------------------------------------------ feedback
def cmd_feedback(a):
    root = repo_root()
    os.chdir(root)
    sd = state_dir(root)
    fb_path = os.path.join(sd, "feedback.json")
    fb = load_json(fb_path, {})
    if a.list:
        if not fb:
            print("No recorded disputes.")
        for k, d in sorted(fb.items()):
            print(f"{k} [{d['decision']}] {d['file']}: {d['title']}\n    intended: {d['intended']}\n"
                  f"    reason: {d['reason']}")
        return
    if not a.id:
        sys.exit("error: give a finding id, or --list")
    if a.remove:
        if a.id not in fb:
            sys.exit(f"error: no dispute recorded for {a.id}")
        del fb[a.id]
        save_json(fb_path, fb)
        print(f"Removed dispute {a.id}.")
        return
    if not (a.intended and a.decision and a.reason):
        sys.exit("error: recording a dispute needs --intended, --decision and --reason")
    m = load_manifest(sd)
    report = load_json(os.path.join(sd, "report.json"), None)
    if not report:
        sys.exit("error: no report; run `pushproof.py check` first.")
    cur, err = current_head(m)
    if err or report["head"] != cur or report["head"] != m["head"]:
        sys.exit("error: report is not for the current head; re-run scope and check first.")
    pool = report.get("findings", []) + report.get("explained", [])
    f = next((x for x in pool if x["id"] == a.id), None)
    if not f:
        sys.exit(f"error: {a.id} is not a valid finding in the current report.")
    fb[a.id] = {"file": f["file"], "title": f["title"], "evidence": f["evidence"],
                "intended": a.intended, "decision": a.decision, "reason": a.reason,
                "context_hash": context_hash(m, f), "head": report["head"],
                "ts": datetime.datetime.now(datetime.timezone.utc).isoformat()}
    save_json(fb_path, fb)
    print(f"Recorded {a.decision} dispute for {a.id}. Re-run `pushproof.py check` to apply it.")


# ---------------------------------------------------------------------- gate
def gate_enabled(root):
    return bool(load_json(os.path.join(root, STATE, "config.json"), {}).get("gate"))


def verdict_for(root, sha):
    return load_json(os.path.join(root, STATE, "verdicts.json"), {}).get(sha)


def hooks_dir(root):
    d = run_git(["rev-parse", "--git-path", "hooks"]).stdout.strip()
    return d if os.path.isabs(d) else os.path.join(root, d)


def cmd_gate(a):
    root = repo_root()
    os.chdir(root)
    sd = state_dir(root)
    cfg_path = os.path.join(sd, "config.json")
    cfg = load_json(cfg_path, {})
    hook_path = os.path.join(hooks_dir(root), "pre-push")
    ours = os.path.exists(hook_path) and HOOK_MARKER in open(hook_path).read()
    if a.action in ("on", "off"):
        cfg["gate"] = a.action == "on"
        save_json(cfg_path, cfg)
        print(f"Push gate {'ON' if cfg['gate'] else 'OFF'} for {root}.")
    elif a.action == "install-git-hook":
        if os.path.exists(hook_path) and not ours:
            sys.exit(f"error: {hook_path} already exists and is not ours; not overwriting. "
                     "Chain it manually: add a line running `python3 <pushproof.py> prepush \"$@\"`.")
        os.makedirs(os.path.dirname(hook_path), exist_ok=True)
        me = os.path.abspath(__file__)
        open(hook_path, "w").write(
            f"#!/bin/sh\n{HOOK_MARKER}\nBR='{me}'\n"
            'if [ ! -f "$BR" ]; then\n'
            '  echo "pushproof: helper not found at $BR (plugin moved or updated?)." >&2\n'
            '  echo "Re-run: python3 <path-to-pushproof.py> gate install-git-hook, or delete this hook." >&2\n'
            "  exit 1\nfi\n"
            'exec python3 "$BR" prepush "$@"\n')
        os.chmod(hook_path, 0o755)
        cfg["gate"] = True
        save_json(cfg_path, cfg)
        print(f"Installed git pre-push hook at {hook_path}; push gate ON.")
    elif a.action == "uninstall-git-hook":
        if not ours:
            sys.exit("error: no pushproof pre-push hook installed.")
        os.remove(hook_path)
        print(f"Removed {hook_path}. Gate setting unchanged ({'ON' if cfg.get('gate') else 'OFF'}).")
    head = run_git(["rev-parse", "--verify", "--quiet", "HEAD"], check=False).stdout.strip()
    v = verdict_for(root, head) if head else None
    print(f"Gate: {'ON' if gate_enabled(root) else 'OFF'} | git pre-push hook: "
          f"{'installed' if (os.path.exists(hook_path) and HOOK_MARKER in open(hook_path).read()) else 'not installed'}"
          + (f" | HEAD {head[:10]}: {v['verdict'] + ' (' + str(v['score']) + '/5)' if v else 'not reviewed'}"
             if head else " | no HEAD commit"))


def gate_reason(root, sha):
    """None if push of `sha` is allowed, else a human-readable reason."""
    v = verdict_for(root, sha)
    if not v:
        return f"commit {sha[:10]} has no pushproof verdict"
    if v["verdict"] not in PASSING:
        return f"last verdict for {sha[:10]} is {v['verdict']} ({v['score']}/5)"
    return None


SHELL_OPS = set("();<>|&")


PREFIX_WORDS = ("sudo", "env", "command", "exec", "time", "nohup")


def segments(command):
    """Split a shell command into simple commands: list of token lists starting at the
    command word (env assignments and wrapper words stripped). None if unparsable."""
    try:
        lex = shlex.shlex(command, posix=True, punctuation_chars=True)
        lex.whitespace_split = True
        toks = list(lex)
    except ValueError:
        return None
    segs, cur = [], []
    for t in toks + [";"]:
        if t and all(c in SHELL_OPS for c in t):
            i = 0
            while i < len(cur) and (re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", cur[i])
                                    or cur[i] in PREFIX_WORDS):
                i += 1
            if cur[i:]:
                segs.append(cur[i:])
            cur = []
        else:
            cur.append(t)
    return segs


def push_dirs(command, cwd):
    """Best-effort: directories in which `command` would run `git push`."""
    segs = segments(command)
    if segs is None:
        return [cwd] if re.search(r"\bgit\b.*\bpush\b", command) else []
    out, here = [], cwd
    for seg in segs:
        i = 0
        if seg[i] == "cd" and i + 1 < len(seg):
            here = os.path.normpath(os.path.join(here, os.path.expanduser(seg[i + 1])))
            continue
        if os.path.basename(seg[i]) != "git":
            continue
        d, j = here, i + 1
        while j < len(seg) and seg[j].startswith("-"):
            if seg[j] == "-C" and j + 1 < len(seg):
                d = os.path.normpath(os.path.join(d, os.path.expanduser(seg[j + 1])))
                j += 2
            elif seg[j] in ("-c", "--git-dir", "--work-tree", "--namespace") and j + 1 < len(seg):
                j += 2
            else:
                j += 1
        if j < len(seg) and seg[j] == "push":
            out.append(d)
    return out


def toplevel(d):
    if not os.path.isdir(d):
        return None
    r = subprocess.run(["git", "-C", d, "rev-parse", "--show-toplevel"], capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else None


def cmd_hook(a):
    try:
        ev = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        sys.exit(0)
    tool, ti = ev.get("tool_name", ""), ev.get("tool_input", {}) or {}
    cwd = ev.get("cwd") or os.getcwd()
    if tool in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
        fp = ti.get("file_path") or ti.get("notebook_path") or ""
        parts = os.path.normpath(fp).split(os.sep)
        if len(parts) >= 2 and parts[-2] == STATE and parts[-1] in PROTECTED_STATE:
            print(f"pushproof: {parts[-1]} is written only by pushproof.py; edit blocked.", file=sys.stderr)
            sys.exit(2)
        sys.exit(0)
    if tool != "Bash":
        sys.exit(0)
    cmd = ti.get("command", "") or ""
    if re.search(r"\bpushproof\.py\b.*\bgate\b.*\b(off|uninstall-git-hook)\b", cmd):
        print("pushproof: the push gate can only be turned off by the user. Ask the user to run "
              "`python3 <pushproof.py> gate off` in their own terminal.", file=sys.stderr)
        sys.exit(2)
    blocked = []
    root_here = toplevel(cwd)
    if root_here and gate_enabled(root_here):
        for sha in merge_target(cmd):
            why = merge_gate_reason(root_here, sha)
            if why:
                blocked.append(f"{root_here} (gh pr merge): {why}")
    for d in push_dirs(cmd, cwd):
        root = toplevel(d)
        if not root or not gate_enabled(root):
            continue
        head = subprocess.run(["git", "-C", root, "rev-parse", "HEAD"], capture_output=True,
                              text=True).stdout.strip()
        why = gate_reason(root, head)
        if why:
            blocked.append(f"{root}: {why}")
    if blocked:
        print("pushproof gate: command blocked.\n  " + "\n  ".join(blocked) +
              "\nRun the pushproof skill on this branch and fix findings until the verdict is PASS "
              "or PASS_WITH_NOTES, then push. Do not try to bypass the gate; if the user wants to push "
              "anyway, they can run the push themselves or turn the gate off.", file=sys.stderr)
        sys.exit(2)
    sys.exit(0)


def cmd_prepush(a):
    root = repo_root()
    if not gate_enabled(root):
        sys.exit(0)
    zero = re.compile(r"^0+$")
    problems = []
    for line in sys.stdin:
        parts = line.split()
        if len(parts) != 4:
            continue
        local_ref, local_sha, remote_ref, remote_sha = parts
        if zero.match(local_sha) or local_sha == remote_sha:
            continue
        sha = run_git(["rev-parse", f"{local_sha}^{{commit}}"], check=False).stdout.strip() or local_sha
        why = gate_reason(root, sha)
        if why:
            problems.append(f"{local_ref} -> {remote_ref}: {why}")
    if problems:
        print("pushproof push gate: push blocked.\n  " + "\n  ".join(problems) +
              "\nReview the branch first, or disable with: python3 <pushproof.py> gate off", file=sys.stderr)
        sys.exit(1)
    sys.exit(0)


# ------------------------------------------------------------------- message
def render_pr_message(r):
    """Paste-ready PR review message. pushproof never posts it; the user does."""
    pr, c, v = r["pr"], r["counts"], r["verdict"]
    scored = "no score" if v == "INCOMPLETE" else f"{r['score']}/5"
    o = [f"**pushproof review: {v} ({scored})**",
         f"PR #{pr['number']} ({pr['head_name']} -> {pr['base_name']}) at `{r['head'][:10]}`",
         "", f"**Suggested review action:** {REVIEW_ACTION[v]}", ""]
    if r.get("summary"):
        o += [r["summary"], ""]
    if v == "INCOMPLETE":
        o += ["The review did not finish, so it gives no verdict on this code:"]
        o += [f"- {x}" for x in r["incomplete_reasons"]]
        return "\n".join(o) + "\n"
    o.append(f"Findings: {c['critical']} critical, {c['high']} high, {c['medium']} medium, {c['low']} low. "
             f"Every changed line in the {r['files_in_scope']} file(s) in scope was read.")
    for i, f in enumerate(r["findings"], 1):
        loc = f"{f['file']}:{f['line']}" + (f"-{f['end_line']}" if f.get("end_line", f["line"]) != f["line"] else "")
        o += ["", f"{i}. **[{f['severity'].upper()}] {f['title']}** (`{loc}`, {f['confidence']}% confidence)",
              f"   - Impact: {f['impact']}", f"   - Fix: {f['fix']}", f"   - Verify: {f['verify']}"]
    if r.get("explained"):
        o += ["", "Explained as intended (not counted):"]
        o += [f"- `{f['file']}:{f['line']}` {f['title']}: {f['intended']}" for f in r["explained"]]
    if r.get("hypotheses"):
        o += ["", "Open questions (below 50% confidence, not counted):"]
        for h in r["hypotheses"]:
            o.append(f"- {h.get('claim', h) if isinstance(h, dict) else h}")
    return "\n".join(o) + "\n"


def cmd_message(a):
    root = repo_root()
    os.chdir(root)
    sd = state_dir(root)
    m = load_manifest(sd)
    r = load_json(os.path.join(sd, "report.json"), None)
    if not r or not r.get("pr"):
        sys.exit("error: no PR review; run `scope --pr <n>` and `check` first.")
    if r["head"] != m["head"]:
        sys.exit("error: report and round disagree; re-run check.")
    live, err = pr_live_head(r["pr"])
    note = ""
    if err:
        note = f"\nWARNING: could not confirm the PR head on GitHub ({err}); the PR may have changed."
    elif live != r["head"]:
        note = (f"\nWARNING: PR #{r['pr']['number']} moved ({r['head'][:10]} -> {live[:10]}) since this "
                "review. This message describes the old head; re-run scope and check.")
    scored = "no score" if r["verdict"] == "INCOMPLETE" else f"{r['score']}/5"
    print(f"Verdict: {r['verdict']} ({scored}). Suggested review action: {REVIEW_ACTION[r['verdict']]}.")
    print(f"Message saved to {os.path.join(STATE, 'pr-message.md')}; pushproof never posts it.{note}")
    print("-" * 60)
    print(render_pr_message(r), end="")


def merge_target(command):
    """Best-effort: for each `gh pr merge` in `command`, return its --match-head-commit (or '')."""
    segs = segments(command)
    if segs is None:
        return [""] if re.search(r"\bgh\b.*\bpr\b.*\bmerge\b", command) else []
    out = []
    for seg in segs:
        if os.path.basename(seg[0]) != "gh":
            continue
        rest = seg[1:]
        pos = [x for x in rest if not x.startswith("-")]
        if len(pos) >= 2 and pos[0] == "pr" and pos[1] == "merge":
            if any(x in ("--disable-auto",) for x in rest):
                continue
            sha = ""
            for j, x in enumerate(rest):
                if x == "--match-head-commit" and j + 1 < len(rest):
                    sha = rest[j + 1]
                elif x.startswith("--match-head-commit="):
                    sha = x.split("=", 1)[1]
            out.append(sha)
    return out


def merge_gate_reason(root, sha):
    if not sha:
        return ("`gh pr merge` must pin the reviewed commit with --match-head-commit <sha>, "
                "so GitHub refuses the merge if the PR changed after review")
    hist = load_json(os.path.join(root, STATE, "verdicts.json"), {})
    full = [k for k in hist if k.startswith(sha.lower())] if len(sha) >= 7 else []
    if len(full) != 1:
        return f"commit {sha[:10]} has no pushproof verdict"
    v = hist[full[0]]
    if v["verdict"] not in PASSING:
        return f"last verdict for {sha[:10]} is {v['verdict']} ({v['score']}/5)"
    return None


# ---------------------------------------------------------------------- main
def main():
    if hasattr(signal, "SIGPIPE"):
        signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    p = argparse.ArgumentParser(prog="pushproof.py", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("scope")
    s.add_argument("--base")
    s.add_argument("--pr", help="review a GitHub PR (number, URL or branch) instead of local HEAD")
    s.add_argument("--max-diff-lines", type=int, default=MAX_DIFF_LINES_DEFAULT)
    s.set_defaults(fn=cmd_scope)
    d = sub.add_parser("diff")
    d.add_argument("paths", nargs="*")
    d.set_defaults(fn=cmd_diff)
    r = sub.add_parser("read")
    r.add_argument("path")
    r.add_argument("--lines")
    r.add_argument("--base", action="store_true")
    r.set_defaults(fn=cmd_read)
    c = sub.add_parser("coverage")
    c.set_defaults(fn=cmd_coverage)
    k = sub.add_parser("check")
    k.add_argument("--findings")
    k.add_argument("--allow-large", action="store_true")
    k.set_defaults(fn=cmd_check)
    fb = sub.add_parser("feedback")
    fb.add_argument("id", nargs="?")
    fb.add_argument("--intended", help="the author's explanation of why the code is intended")
    fb.add_argument("--decision", choices=["accepted", "rejected"])
    fb.add_argument("--reason", help="why the explanation does or does not hold against the code")
    fb.add_argument("--list", action="store_true")
    fb.add_argument("--remove", action="store_true")
    fb.set_defaults(fn=cmd_feedback)
    g = sub.add_parser("gate")
    g.add_argument("action", nargs="?", default="status",
                   choices=["status", "on", "off", "install-git-hook", "uninstall-git-hook"])
    g.set_defaults(fn=cmd_gate)
    ms = sub.add_parser("message")
    ms.set_defaults(fn=cmd_message)
    h = sub.add_parser("hook")
    h.set_defaults(fn=cmd_hook)
    pp = sub.add_parser("prepush")
    pp.add_argument("remote", nargs="?")
    pp.add_argument("url", nargs="?")
    pp.set_defaults(fn=cmd_prepush)
    a = p.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
