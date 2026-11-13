"""Recorded disputes against findings."""
from .core import *  # noqa: F401,F403
from .github import current_head
from .check import context_hash


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
