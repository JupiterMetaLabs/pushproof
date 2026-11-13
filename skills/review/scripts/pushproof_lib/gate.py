"""Push/merge gate: settings, Claude Code PreToolUse hook and git pre-push hook."""
from .core import *  # noqa: F401,F403

ENTRY_SCRIPT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "pushproof.py")


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
        me = ENTRY_SCRIPT
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
