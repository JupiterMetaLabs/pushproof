"""Findings validation, disputes, scoring, verdict and report."""
from .core import *  # noqa: F401,F403
from .github import current_head
from .review import compute_coverage
from .message import render_pr_message


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
