---
description: End-of-day wrap — run the day's check, confirm clean state, update LOG.md
argument-hint: <today's done-when check>
---

End of day. Today's check: $ARGUMENTS

1. Run `python -m pytest tests/unit -q`, `ruff check src tests` and `./scripts/check_client_names.sh`. Report each as pass/fail.
2. Use the `dbx-runner` subagent to run today's done-when check on dev and return the evidence. If no check was given, take it from today's row of the day table in `PLAN.md` (match on `date +%F`).
3. Idempotency: have `dbx-runner` re-run today's job once more and confirm nothing changed (row counts, audit rows, LLM calls). Report the numbers.
4. `git status`: list anything uncommitted. Do not commit it without asking. Never end the day with work only on this laptop: once committed, if the branch is ahead of origin, ask me to approve `git push` to the private origin.
5. Worktrees: `git worktree prune`, then `git worktree list`. For any worktree whose branch is merged into main, ask me before `git worktree remove`.
6. Append one entry to `LOG.md`, headed with today's date from `date +%F` (not a guess), in the existing format: Done, Broken, Next, plus `Hours:` (ask me) and `Evidence:` (the check output in one line).
7. `PLAN.md`: update only the Status columns (today's day-table row, and any requirement or finding this work moved), with the commit or run id. Never edit Build, Done-when, requirement wording or acceptance criteria; if one looks wrong, tell me.
8. Tell me in three lines: green or not, the biggest risk for tomorrow, and the first prompt for tomorrow morning.

If today's check failed and a freeze is near, remind me of the cut order in `PLAN.md`.
