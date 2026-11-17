# GovGates accelerator — agent instructions

Reusable governance-gate accelerator for Databricks Unity Catalog. Two gates share one metadata spine:

- **Hydration gate** — profiles a newly landed table, proposes business terms, descriptions and sensitivity (fuzzy match first, LLM second), a data owner approves, approved metadata is written to the `gov` schema and projected to Unity Catalog.
- **Consumption gate** — access policies live as rows in `gov.access_policies`; a generator turns them into UC grants, ABAC column masks and row filters. A persona test matrix proves who sees what.

Specs live in `specs/`. Read the relevant spec before changing code. If code and spec disagree, stop and flag it.

This file is written for a coding agent (Claude Code) working in this repo, and for the people who direct it.

## Ground rules (non-negotiable)

1. **Synthetic data only.** No real data, schemas, documents, screenshots or names from your organisation in this repo, its issues or its PRs.
2. **Generic naming.** Nothing is named after an organisation. Catalog, schema and endpoint names come from config/bundle variables, never hard-coded.
3. **`gov` is the source of truth; Unity Catalog is a projection.** Never read UC tags/comments back as truth.
4. **AI proposes, people and deterministic code decide.** No model output ever grants access.
5. **Idempotent.** Every job can be re-run with no change to the end state of governance tables. Run logs gain one row per run; that is their purpose (`gov.hydration_runs` is specified but not yet written).
6. **No raw values in LLM prompts.** Send profile stats and pattern hits only.

## Layout

```
databricks.yml            Asset Bundle (variables: catalog, schemas, llm provider, CI principal)
resources/                Job definitions
specs/                    hydration.md, consumption.md, contracts.md, eval.md
src/gates/common/         config, llm client, uc client, audit
src/gates/eval/           eval metrics (run by src/jobs/run_eval.py, logs to MLflow)
src/jobs/                 job entry points (run from the bundle's files)
src/gates/hydration/      profile, match, propose, guard, approve, apply
src/gates/consumption/    policy model, generator, apply, request flow
src/synthetic/            field catalogue, generator, glossary.yaml, loaders
sql/ddl/                  gov schema DDL (uses ${catalog} / ${gov_schema} placeholders)
sql/views/                review-queue views over gov
config/                   example access model (YAML stub)
tests/unit/               pure-python tests, run anywhere
tests/access/             persona matrix tests, run against a workspace
.claude/                  agents, slash commands and guard hooks for Claude Code
```

## Commands

```bash
pip install -e ".[dev]"                     # local deps
pytest tests/unit                           # unit tests (no workspace needed)
ruff check src tests
python -m synthetic.generate_data --out build/synthetic   # generate CSVs locally
databricks bundle validate -t dev --profile "${GG_BUNDLE_PROFILE}"
databricks bundle deploy -t dev --profile "${GG_BUNDLE_PROFILE}"
databricks bundle run -t dev --profile "${GG_BUNDLE_PROFILE}" setup_gov      # create schemas + gov tables
databricks bundle run -t dev --profile "${GG_BUNDLE_PROFILE}" load_synthetic # load raw tables, glossary, golden set
./scripts/check_client_names.sh             # must pass before every push (reads your local .client-denylist)
```

Always pass `--profile`: never let the CLI's default profile pick the workspace.

## Conventions

- Python 3.11+, type hints, `ruff` clean. Small pure functions; Spark only at the edges.
- Tests first for anything with logic (matchers, guards, generators, policy generation).
- Table names are always built as `f"{cfg.catalog}.{cfg.<schema>}.<table>"` from `gates.common.config`.
- Every write to a `gov` table that changes governance state also writes a row to `gov.audit_log`.
- Prompts live in `src/gates/hydration/prompts/` with a version in the filename. Never edit a released prompt in place; add a new version.

## Do not

- Do not write expected results for `tests/access/matrix.yaml` — a human writes those.
- Do not edit `src/synthetic/glossary.yaml` term_ids once the golden set references them.
- Do not deploy to any target other than `dev`. Other targets are deployed by a person.
- Do not push. A person reviews and pushes.
- Do not read `.client-denylist`.
- Do not hard-code catalog names, hosts, tokens or emails.
- Do not drop and recreate a `gov` table once it holds data. Change its schema with `ALTER TABLE` (add the column to the DDL file too).

## How we work

One lead session builds one slice at a time. A person writes the specs and the protected files and reviews; the agent writes code, tests and SQL.

**The loop for every slice** (`/slice <what> <done-when check>` runs it):
plan (≤8 bullets, wait for OK) → `test-writer` writes failing tests → implement until unit tests + ruff pass → `dbx-runner` deploys and runs on dev with the done-when check → `reviewer` on any high-risk diff → client-name check → commit (no push) → project log.

`/slice` and `/wrap` expect a `PLAN.md` (requirements and a day table with done-when checks) and a `LOG.md` (one entry per slice or day: done, broken, next, hours, evidence). Neither ships with this repo; create your own.

**Subagents** (`.claude/agents/`):

| Agent | Use it for | Never |
| --- | --- | --- |
| `test-writer` | Failing tests from the spec, before code | Touch `src/` or write ground truth |
| `dbx-runner` | Every deploy/run on dev; returns a 5-line result so logs stay out of context | Edit code, use another target |
| `reviewer` | Fresh-eyes review of diffs touching grants, policies, masks, filters, prompt/LLM client, approve, apply-to-UC | Edit anything |
| `docs-checker` | Current Databricks syntax (CREATE POLICY, governed tags, ai_query, groups API) with sources | Answer from memory |
| `access-tester` | Persona matrix as each persona's service principal; returns differences only | Edit anything |
| `release-auditor` | Read-only public-release check before any tag or public push | Edit anything |

**Iterate locally, deploy to confirm.** Inner loop is pure functions + unit tests (or Databricks Connect on a small sample). Deploy once per slice, not once per change.

**Running SQL on dev:** always `./scripts/sql.sh "<sql>"` or `./scripts/sql.sh -f file.sql` (Statement API, no deploy). The script handles Git Bash path rewriting and refuses destructive statements on product schemas.

**Commands:** `/slice`, `/wrap` (end-of-day check + log), `/eval <prompt_version> [runs]` (eval twice, compare with baseline, gate).

## Protected files — a human writes these

Hooks ask before any change to:

- `tests/access/matrix.yaml` — the persona answers the build is graded against
- `src/gates/hydration/prompts/` and `src/gates/hydration/abbreviations.yaml` — the prompts and abbreviation dictionary
- `src/synthetic/fields.py`, `src/synthetic/glossary.yaml` — the golden set comes from these
- `.claude/settings.json`, `.claude/hooks/` — the guards

If a slice needs one of these, stop, say exactly what is needed, and use a small local test fixture meanwhile. Never write the expected answers a test or eval grades you against.

## Guard hooks (`.claude/hooks/`)

- Bash: `bundle destroy` and any non-`dev` target are denied; `git push` is denied while a tracked file mentions a name in `.client-denylist`; shell writes to protected files ask first.
- Edits: content containing a denylisted name is denied (the name is never echoed); protected files ask first.
- After each `.py` edit: `ruff check` on that file. On stop: unit tests run if `.py` files are uncommitted.
- Hooks fail open on their own errors and print `hook error (ignored)`. If you see that, tell the person you work with.

`.client-denylist` is a local, gitignored file: one name per line (organisations, people, systems) that must never appear in the repo. Create it before your first push; the push guard denies pushes without it.
