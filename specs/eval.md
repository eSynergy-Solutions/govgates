# Eval spec

**Purpose:** measure hydration proposal quality against a golden set, so quality is shown, not claimed.

## Golden set

Derived from the synthetic table specs (`src/synthetic/fields.py`): every physical column maps to the term it was generated from, or to no term for deliberate no-match columns. Difficulty labels: `easy` (clean name), `abbrev` (abbreviated / variant name), `hard` (ambiguous or misleading), `none` (no correct term).

Owner edits during approval append to the `gov.golden_candidates` table; a human promotes them into the golden set.

## Metrics (per run)

| Metric | Definition |
| --- | --- |
| term_precision | correct term proposals / term proposals made (excl. NO_MATCH) |
| coverage | columns with a term proposal (not `NO_MATCH`) at or above threshold / columns with a correct term |
| no_match_accuracy | NO_MATCH proposed / columns whose truth is no match |
| pii_recall | columns proposed `pii` / columns whose truth is `pii` (the one that matters most) |
| fuzzy_share | proposals resolved by fuzzy match / all proposals |
| tokens_per_dataset | LLM tokens / tables processed |

Also break term_precision down by difficulty. Threshold = the approve auto-rule confidence (0.8).

A run is scoped by `run_id`: the eval hydrates every golden-set table in force mode under one shared `run_id` and joins only that run's proposals to `gov.golden_set`. A golden column with no proposal in the run counts as missing (wrong for every metric that needs a proposal).

**Extras** (logged with an `extra_` prefix; not part of the spec metrics above):

| Extra | Definition |
| --- | --- |
| extra_recall_at_threshold | columns whose correct term is proposed at confidence ≥ threshold / columns with a correct term |
| extra_sensitivity_accuracy | columns whose proposed sensitivity equals the truth / all golden columns |
| extra_under_classified_count | columns whose proposed sensitivity is below the truth |
| extra_rule_share | proposals settled by the noise-name rule (method `rule`) / all proposals |

## Logging

One MLflow run per eval `run_id` (two evals of the same prompt_version, model and fuzzy_accept are two runs, so run 1 vs run 2 stays visible); params prompt_version, model and fuzzy_accept (threshold also logged). The experiment path comes from config (bundle variable `mlflow_experiment`), never hard-coded.  Metrics: the table above. Artifact: per-column results CSV. Summary row to `gov.eval_runs`.

## Targets

Set after the first run, not before. Show run 1 vs run 2 (after one tuning pass) in the demo.
