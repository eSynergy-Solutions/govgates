---
description: Build one slice end to end — plan, tests first, code, real run, review, commit
argument-hint: <slice, e.g. "hydration guard + profiler"> <done-when check>
---

Slice: $ARGUMENTS

Work through this loop. Do not skip steps; stop and tell me if a step fails twice.

1. **Orient.** Read `LOG.md` (last entry), the day table in `PLAN.md` and the spec section(s) for this slice. If the done-when check is missing from my request, take it from today's row of the `PLAN.md` day table and state it back in one line. If the slice isn't in the day table, say so and ask before building: it may be scope creep.
2. **Plan.** Write a plan of at most 8 bullets: modules, functions, tables touched, how the done-when check will be proven. If the slice touches grants, policies, masks, row filters, governed tags, approve/revise, apply-to-UC or any gov table's status values, run the `reviewer` subagent in plan mode first and fold in its "must change". Show me the plan and the reviewer verdict. Wait for my OK.
3. **Tests first.** Use the `test-writer` subagent for the logic in this slice. Confirm the new tests fail for the right reason.
4. **Code.** Implement until `python -m pytest tests/unit -q` and `ruff check src tests` pass. Small pure functions; Spark only at the edges; names from `gates.common.config`.
5. **Real run.** Use the `dbx-runner` subagent, with the done-when check as its check. SQL-only checks go through `./scripts/sql.sh` with no deploy; deploy and run jobs only when code or resources changed. If you need Databricks syntax you have not used before in this repo, ask the `docs-checker` subagent first. If the slice changed access (grants, policies, tags, apply-to-UC), also run `access-tester` once personas exist. If it changed the prompt, matcher, abbreviations, glossary or thresholds, run `/eval` instead of a single eval run.
6. **Review.** If the diff touches grants, policies, masks, row filters, the prompt or LLM client, approve, or apply-to-UC, run the `reviewer` subagent and fix every "must fix". Otherwise skip.
7. **Commit.** `./scripts/check_client_names.sh`, then commit with a message naming the slice and the check that passed. Do not push.
8. **Log.** Append to `LOG.md`: done, broken, next, hours (ask me), and the evidence line for the done-when check.

Report at the end in five lines: what was built, the check result with evidence, tests added, anything cut, next slice.
