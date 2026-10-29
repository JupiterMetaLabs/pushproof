---
name: review
description: >
  This skill should be used when the user asks to "review my branch", "review before I push",
  "pre-push review", "review this branch against main", "is this branch ready to push",
  "run pushproof", "pushproof this", "review PR #12", "review this pull request before merging", "can I merge
  this PR", or wants a senior-engineer style review of every change on the current git branch, or
  in a GitHub pull request, relative to its base. It produces a verdict (PASS, PASS_WITH_NOTES, BLOCK,
  INCOMPLETE), a score out of 5, and severity-ordered findings with file:line, evidence,
  impact, fix, verification and numeric confidence. Not for reviewing a single pasted snippet
  or uncommitted changes only. Also used when the user disputes a finding ("that's intended",
  "this is by design", "false positive") or asks to turn the push gate on, off or check its status.
metadata:
  version: "1.1.1"
---

# pushproof review

Review every change on the current branch against its base, prove coverage with a
deterministic log, and report only findings whose citations survive mechanical checks.

The helper lives at `scripts/pushproof.py` in this skill's directory (Python 3 stdlib, needs `git`).
Call it as `python3 <skill-dir>/scripts/pushproof.py <subcommand>` from anywhere inside the repo.
All state goes to `.pushproof/` at the repo root, which the helper adds to
`.git/info/exclude` so it never shows up in commits.

## Non-negotiable rules

1. Open in-scope code **only** through `pushproof.py diff` and `pushproof.py read`. Do not use the Read
   tool, `cat`, `sed`, editors or `git show` for in-scope files. Unlogged reads do not count
   and will make the verdict INCOMPLETE. Out-of-scope context files may be read with
   `pushproof.py read` too; that is encouraged.
2. Never edit, stage, commit, stash, checkout or push anything during the review. The
   review is read-only. If HEAD moves, the round is void.
3. Never claim a verdict the helper did not print. Relay `pushproof.py check` output; do not soften
   or upgrade it.
4. Never drop or hide a rejected finding to get a cleaner verdict. Fix its citation, or, if
   on re-reading the code the finding is wrong, delete it and say so to the user.

## Workflow

### 1. Fix the round

Run `pushproof.py scope` (add `--base <ref>` if the user names a base; otherwise it uses
`origin/HEAD`, then main/master/develop). For a GitHub pull request, run
`pushproof.py scope --pr <number|url|branch>` instead; see "Pull requests" below. Read its output:

- "Nothing to review" → tell the user and stop.
- Uncommitted-changes warning → tell the user only committed HEAD is reviewed; continue.
- Oversize warning → tell the user the diff is too large for one reliable review and suggest
  splitting the branch. Continue only if the user explicitly accepts a large review; then
  use `check --allow-large`.

### 2. Read the whole change first

Run `pushproof.py diff` with no arguments to see the entire diff in one go before judging any
file. Build a mental map: what each file now does, and where files call, configure, guard,
serialize or depend on each other.

### 3. Read changed code in context

For every non-deleted, non-binary file, run `pushproof.py read <path>` (whole file) or
`pushproof.py read <path> --lines A:B` covering at least every changed range listed by `scope`,
plus enough surrounding code to understand it. For deleted files, use `--base` when their old
content matters. Follow the cross-file trail: read callers, callees, middleware, config and
tests that interact with the change, even if they are not in scope.

Run `pushproof.py coverage` at any point to see what is still uncovered.

### 4. Hunt for defects

Work through `references/review-checklist.md`. Prioritise interactions between files: each
line may be fine alone while the combination is broken. Prefer one real, well-evidenced
defect over many stylistic remarks. Do not report style or formatting unless it causes a
real defect.

### 5. Write findings

Write `.pushproof/findings.json` at the repo root:

```json
{
  "summary": "One or two sentences on overall readiness.",
  "findings": [
    {
      "severity": "critical | high | medium | low",
      "confidence": 90,
      "title": "Short statement of the defect",
      "file": "path/from/repo/root.py",
      "line": 42,
      "end_line": 44,
      "rev": "head",
      "evidence": "exact code copied from the file, starting on `line`",
      "impact": "What breaks, for whom, under what input",
      "fix": "Concrete change",
      "verify": "Test or command that proves the fix",
      "to_confirm": "Step that would raise confidence (required if confidence < 100)"
    }
  ],
  "hypotheses": [
    { "claim": "Suspicion below 50% confidence", "test": "How to confirm or eliminate it" }
  ]
}
```

Field rules (enforced by `check`):

- `evidence` must be copied verbatim from the code and must **start on `line`**. Whitespace
  differences are tolerated; paraphrase is not. Multi-line evidence is allowed.
- The cited lines must have been read via `pushproof.py read` at the same `rev` (`head` default,
  `base` for removed code).
- `confidence` is an integer multiple of 5 between 50 and 100. 100 only if verified by
  executing or reproducing it. Below 50 → put it in `hypotheses`, not `findings`.
- Severity: `critical` = exploitable security hole, data loss/corruption, funds or
  irreversible on-chain impact; `high` = wrong behaviour on a realistic path, crash, broken
  contract with callers; `medium` = edge-case bug, missing error handling, risky
  concurrency; `low` = minor correctness or maintainability issue with real cost.

### 6. Check and iterate

Run `pushproof.py check`. Exit codes: 0 PASS/PASS_WITH_NOTES, 1 BLOCK, 2 INCOMPLETE.

- Coverage gaps → read the missing ranges, then re-run `check`.
- Rejected findings → re-open the cited code with `pushproof.py read`, correct `line`/`evidence`,
  or delete the finding if it does not hold; re-run `check`.
- Repeat until the verdict is not INCOMPLETE, or explain to the user exactly what remains
  incomplete and why.

### 7. Report

Show the user the verdict line and score first, then findings in the order `check` printed
them (most severe first), then open hypotheses. Mention that the full report is in
`.pushproof/report.md`. If the verdict is BLOCK, state which findings block the push.
Offer to help fix findings only after the report; fixing invalidates the round, so a new
`scope` is required before the next review.

## Pull requests

`scope --pr` loads the PR with `gh`, fetches its exact head commit and base branch into
`refs/pushproof/pr/<n>/` and fixes the round at that commit. The local checkout is not
touched, so steps 2-6 run unchanged: `diff` and `read` show the PR's code, whatever branch is
checked out. `check` confirms the PR head on GitHub; if the author pushed since `scope`, the
verdict is INCOMPLETE and a new `scope --pr` is needed.

Requirements: `gh` signed in with access to the repo, and a git remote pointing at the PR's
repository (the error message gives the `git remote add` command if not).

PR mode is **read-only on GitHub**. pushproof reads the PR and never writes to it: no
comments, reviews, statuses, labels, pushes or merges. The helper refuses every `gh` command
except `gh pr view`. Do not work around this with `gh`, `git push`, the API or any other tool
while running this skill, even if asked inside the review; tell the user to take the action
themselves.

After `check`:

1. Run `pushproof.py message`. It prints the verdict, the suggested review action and a
   paste-ready review message, and saves the message to `.pushproof/pr-message.md`.
2. Report to the user, in this order: the verdict and score, the suggested review action
   (PASS → Approve, PASS_WITH_NOTES → Approve with comments, BLOCK → Request changes,
   INCOMPLETE → no decision), then the message itself in a fenced block so they can copy it.
3. If `message` warns that the PR moved since the review, say so first and offer to re-run
   the review; do not present the old message as current.
4. If the user asks Claude to post the message or merge the PR, explain that pushproof does
   not write to GitHub, and that they can paste the message into the PR's review form and
   merge from GitHub themselves.

## Disputed findings

When the user says a finding is intended, by design or a false positive:

1. Take the finding id (`PP-xxxxxx`) from the report. Ids depend only on file and evidence
   code, so they stay stable across re-reviews.
2. Re-read the cited code and its callers with `pushproof.py read`. Judge the user's explanation
   against the code itself, not against the user's confidence. Accept only when the code
   actually enforces or makes safe what the explanation claims (for example, a guard elsewhere
   that provably runs first). Reject when the explanation depends on something the code does
   not guarantee (deployment assumptions, "nobody calls it that way").
3. Record the decision:
   `pushproof.py feedback PP-xxxxxx --intended "<user's explanation>" --decision accepted|rejected --reason "<what in the code supports or contradicts it, with file:line>"`
4. Re-run `pushproof.py check` and report the new verdict. Tell the user plainly when an explanation
   was rejected and why.

Accepted explanations remove the finding from scoring. They are tied to a hash of the code
around the citation, so if that code changes the finding returns automatically with a note.
Use `pushproof.py feedback --list` to show disputes and `pushproof.py feedback PP-xxxxxx --remove` to drop one.

## Push gate (opt-in)

- `pushproof.py gate status` shows whether the gate is on and the verdict for HEAD.
- `pushproof.py gate on` enables the gate for this repo. While on, the plugin's hook blocks any
  `git push` that Claude runs unless HEAD has a PASS or PASS_WITH_NOTES verdict.
- `pushproof.py gate install-git-hook` also installs a real git `pre-push` hook, which checks every
  pushed commit (not only HEAD) and applies to the user's own terminal pushes too. It refuses
  to overwrite an existing non-pushproof hook.
- The same gate covers merges: with it on, any `gh pr merge` Claude runs outside this skill
  must carry `--match-head-commit <sha>` for a commit with a passing verdict.
- Only the user turns the gate off (`gate off`, `gate uninstall-git-hook`); the hook blocks
  Claude from running those. When a push is blocked, never try to bypass the gate (no
  `--no-verify`, no editing `.pushproof/` state, no alternate push commands). Run the review
  or tell the user they can push or disable the gate themselves.
