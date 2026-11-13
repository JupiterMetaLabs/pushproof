"""Review round: scope, diff, read and coverage."""
from .core import *  # noqa: F401,F403
from .github import resolve_pr


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
