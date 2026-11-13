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
import signal
import sys

sys.dont_write_bytecode = True  # never write __pycache__ into the installed plugin

from pushproof_lib.core import MAX_DIFF_LINES_DEFAULT  # noqa: E402
from pushproof_lib.github import gh  # noqa: E402,F401  (re-exported for tests)
from pushproof_lib.review import cmd_scope, cmd_diff, cmd_read, cmd_coverage  # noqa: E402
from pushproof_lib.check import cmd_check  # noqa: E402
from pushproof_lib.feedback import cmd_feedback  # noqa: E402
from pushproof_lib.message import cmd_message  # noqa: E402
from pushproof_lib.gate import cmd_gate, cmd_hook, cmd_prepush  # noqa: E402


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
