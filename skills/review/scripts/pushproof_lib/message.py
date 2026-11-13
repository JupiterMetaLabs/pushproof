"""Paste-ready PR review message (never posted by pushproof)."""
from .core import *  # noqa: F401,F403
from .github import pr_live_head


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
