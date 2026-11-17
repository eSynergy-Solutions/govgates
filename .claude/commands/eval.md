---
description: Run the hydration eval twice (or N times) for a prompt version and compare with a baseline. Gate for any change to the prompt, matcher, abbreviations, glossary or thresholds.
argument-hint: <prompt_version> [runs=2] [baseline run_id]
---

Eval: $ARGUMENTS

1. Parse: prompt version (required), runs (default 2), baseline run_id (default: the most recent `gov.eval_runs` row with a different prompt_version or fuzzy_accept than this config). State them back in one line.
2. Use the `dbx-runner` subagent: deploy once, then run `eval_hydration` with `--params prompt_version=<v>` the requested number of times, sequentially. Collect the run_ids.
3. With `./scripts/sql.sh`, pull the `gov.eval_runs` rows for those run_ids and the baseline.
4. With `./scripts/sql.sh`, list columns whose proposed `term_id` differs between this config's runs (join `gov.term_proposals` on table_name, column_name across the run_ids). These are the unstable columns.
5. Report:

```
config: <prompt_version>, fuzzy <n>, model <m>, runs <ids>
metric            baseline   mean    min-max   delta
term_precision    ...
coverage          ...
no_match_accuracy ...
pii_recall        ...
tokens_per_dataset ...
unstable columns: <table.column: termA / termB>, or "none"
GATE: PASS | FAIL (<reason>)
```

Gate fails if: pii_recall < 1.0 in any run; mean term_precision drops more than 0.01 below baseline; no_match_accuracy spread (max - min) above 0.1; or tokens per dataset up more than 15% with no accuracy gain. With 11 no-match cases one column is about 9 points, so name the columns behind any no-match change.

Do not change the prompt, abbreviations or glossary here; they are protected files. Append the result to `LOG.md` under today's entry.
