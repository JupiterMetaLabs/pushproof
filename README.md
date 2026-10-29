# pushproof

Code review you can prove happened. pushproof has Claude review every change on a git branch, or
in a GitHub pull request, against its base before you push or merge. It logs every line Claude
reads, rejects any finding whose citation does not match the code, and returns `INCOMPLETE`
instead of a quiet pass when anything in scope was not read.

## Install

```
/plugin marketplace add saishibu/pushproof
/plugin install pushproof@pushproof
```

Requires `git` and Python 3 (standard library only). PR features also need the GitHub CLI `gh`,
signed in with push access to the repository.

## Use

Ask Claude in a repository:

- "review my branch before I push" or `/pushproof:review`
- "review PR #12" / "can I merge this PR?"
- "PP-1a2b3c is intended because ..." to dispute a finding
- "turn on the push gate" / "install the git push hook"

## What a review produces

- A verdict: `PASS`, `PASS_WITH_NOTES`, `BLOCK` or `INCOMPLETE`.
- A score out of 5: start at 5.0, subtract 3.0 per critical, 1.5 per high, 0.5 per medium and
  0.1 per low finding. Any critical caps the score at 1.0. Accepted disputes are not scored.
- Findings ordered by severity, each with file:line, quoted evidence, impact, fix, a
  verification step and a numeric confidence (multiples of 5, 50-100).
- `.pushproof/report.md` and `report.json` in the repository.

## How coverage is proven

1. `scope` fixes the round: base, merge-base, head commit, changed files and changed line ranges.
2. Claude opens in-scope code only through the helper's `diff` and `read` commands, which log
   every view.
3. `check` returns `INCOMPLETE` unless every file's diff was viewed and every changed line was
   read, and rejects any finding whose evidence does not start on the cited line or whose lines
   were never read.

## Disputes

Tell Claude why a finding is intended. Claude checks the explanation against the code and
records it as accepted or rejected in `.pushproof/feedback.json`. An accepted explanation is
bound to a hash of the code within 3 lines of the citation; if that code changes, the finding
returns.

## Push and merge gate (opt-in)

| Layer | Enable | Checks | Covers |
|---|---|---|---|
| Claude Code hook | `pushproof.py gate on` | HEAD has a passing verdict; `gh pr merge` pins a passing commit with `--match-head-commit` | Pushes and merges run by Claude |
| git `pre-push` hook | `pushproof.py gate install-git-hook` | Every pushed commit has a passing verdict | All pushes from that clone |
| GitHub required check | Branch protection: require status `pushproof` | The PR head has a successful `pushproof` status | Every merge, including the web UI |

Claude is blocked from turning the gate off; only you can.

## What this plugin runs, sends and writes

- **Runs:** `python3` with its bundled helper script and local `git` commands. In PR mode it
  also runs the GitHub CLI `gh`.
- **Hook:** a `PreToolUse` hook inspects every Bash command and file edit Claude makes. It only
  blocks `git push` / `gh pr merge` when you have turned the gate on and the commit lacks a
  passing verdict, plus direct edits to its own state files. It never changes Claude's
  permission settings.
- **Network:** none in branch mode. In PR mode, `gh` and `git fetch` contact your GitHub host
  using your existing `gh` sign-in to read the pull request. Only when you approve
  `publish` does it post a commit status (context `pushproof`) and, if asked, the review report
  as a PR comment. Nothing is sent to any other service. Code is reviewed by your own Claude
  session.
- **Writes:** `.pushproof/` at the repository root (added to `.git/info/exclude`), fetch refs
  under `refs/pushproof/pr/` in PR mode, and `.git/hooks/pre-push` only when you run
  `gate install-git-hook`. It refuses to overwrite an existing pre-push hook it did not create.
- **Credentials:** it never reads tokens itself; `gh` uses its own stored sign-in.

## Limits

- Reviews committed code only, not uncommitted changes.
- Diffs over 4,000 lines return `INCOMPLETE` unless explicitly allowed.
- The read log proves code was opened, not how carefully it was reasoned about.
- The Claude hook parses shell commands on a best-effort basis; pushes hidden in scripts or
  aliases are not seen. Use the git hook and the GitHub required check for enforcement.
- Anyone with push access can post a commit status, so a required `pushproof` check proves a
  passing status was published, not which tool produced it.
- State is per clone; verdicts and disputes are not shared with teammates.

## Licence

MIT. See `LICENSE`.
