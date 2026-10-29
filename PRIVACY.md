# pushproof privacy policy

Effective date: 29 October 2026

pushproof is a code review plugin for Claude, published by Jupiter Meta Labs. This policy
explains what data the plugin handles. In short: pushproof collects nothing, and Jupiter Meta
Labs receives no data from your use of it.

## What pushproof does not collect

- It has no servers, analytics, telemetry or crash reporting.
- It sends nothing to Jupiter Meta Labs or to any third party it operates.
- It does not read, store or transmit your GitHub sign-in or any other login details.

## Data that stays on your computer

pushproof writes these files in the repository you review, and nowhere else:

- `.pushproof/`: the review scope, a log of the lines read, findings, the report, the
  suggested review message, recorded disputes, verdicts per commit and the gate setting.
  pushproof adds this folder to `.git/info/exclude` so it is never committed.
- `refs/pushproof/pr/` (pull request reviews only): local git refs for the fetched PR commits.
- `.git/hooks/pre-push`: only if you run `gate install-git-hook`.

Delete `.pushproof/` and those refs at any time to remove everything pushproof stored.

## Services pushproof uses on your behalf

- **Claude.** Your Claude session reads the code under review. Anthropic processes that data
  under the terms and privacy policy of your Claude plan; pushproof adds no other recipient.
- **GitHub (pull request reviews only).** pushproof runs `gh pr view` and `git fetch` to read
  the pull request, using the GitHub CLI and git sign-ins already on your computer. It never
  writes to GitHub: no comments, reviews, statuses, pushes or merges. GitHub's own privacy
  policy applies to those requests.

## Children

pushproof is a developer tool and is not directed at children.

## Changes

Changes to this policy are published in this file, and its history is visible in the
repository's commit log.

## Contact

Questions about this policy: open an issue at
https://github.com/JupiterMetaLabs/pushproof/issues
