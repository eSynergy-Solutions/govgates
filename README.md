# GovGates accelerator

Governance gates for Databricks Unity Catalog, built as a Databricks Asset Bundle:

- **Hydration gate** — when a table lands, profile it, propose a business term, description and sensitivity
  for every column (fuzzy match against a glossary first, an LLM second), let the data owner approve, then
  write the approved metadata to the `gov` schema and project it to Unity Catalog as comments and governed tags.
- **Consumption gate** — policy as code. Access requests become agreements; agreements and policy rows in
  `gov` are turned into Unity Catalog grants, ABAC column masks (by sensitivity tag) and row filters (by region).
  A persona test matrix proves who sees what.

Both gates share one metadata spine: **the `gov` schema is the source of truth; Unity Catalog is a projection.**
AI proposes; people and deterministic code decide. No model output ever grants access, and no raw column
values are ever sent to the model.

**Everything in this repo is synthetic**: data, glossary, abbreviations, golden set and prompt examples.

Version: see `VERSION` and `CHANGELOG.md`. Licence: Apache 2.0.

## What's in the box

| Path | What |
| --- | --- |
| `databricks.yml`, `resources/` | The bundle: variables and jobs |
| `src/gates/hydration/` | profile → match → propose → guard → approve → apply |
| `src/gates/consumption/` | policy model, request checks, grant / mask / row-filter generators |
| `src/synthetic/` | synthetic data generator, glossary (`glossary.yaml`) |
| `src/gates/hydration/prompts/` | proposer prompts (versioned; never edited in place) |
| `sql/ddl/`, `sql/views/` | `gov` schema and the review-queue views |
| `sql/setup/governed_tags.sql` | one-time account-level governed tag setup |
| `config/access_model.example.yaml` | example access model (who is excepted from masks, regions) |
| `src/gates/eval/`, `src/jobs/run_eval.py` | hydration eval harness (golden set from `src/synthetic/`), logs to MLflow |
| `tests/unit/` | pure-Python tests, no workspace needed |
| `tests/access/` | persona matrix, runs against a workspace |
| `specs/` | the design: `hydration.md`, `consumption.md`, `contracts.md`, `eval.md` |
| `.claude/` | agents, slash commands and guard hooks for building with Claude Code (see `CLAUDE.md`) |

## Prerequisites

- A Databricks workspace with **Unity Catalog** and **serverless jobs**.
- A catalog to build in (or `CREATE CATALOG` on the metastore). Use a non-production catalog first.
- A **SQL warehouse** (for `scripts/sql.sh` and the persona tests).
- An LLM: a Databricks **model serving endpoint** reachable with `ai_query` (default `databricks-gpt-oss-120b`),
  or any **OpenAI-compatible** endpoint (base URL + model name, key in a secret scope).
- A **CI service principal**. The governance jobs run as it, never as a person. You need its application ID,
  and the person deploying needs the *Service principal: User* role on it so jobs can run as it.
- **Governed tags** `<tag_prefix>_sensitivity` and `<tag_prefix>_rls_attribute`, created once per account by an
  account admin from `sql/setup/governed_tags.sql`, with ASSIGN on them granted to the CI service principal.
  Governed tags are account-wide: pick a `tag_prefix` that no one else in the account uses.
- Locally: Python 3.11+, the Databricks CLI (CI uses 1.12.1), Git Bash on Windows.

## Install

Take the tagged source (`git clone --branch v<version>`, or the tag's source archive through your mirror).
The GitHub Release also carries the Python wheel and sdist with a `SHA256SUMS` file; check them with
`sha256sum -c SHA256SUMS`. The bundle deploys from the full source tree, not from the wheel.

```bash
cd govgates
pip install -c requirements/jobs.txt -e ".[dev]"
pytest tests/unit                  # ~3,000 tests, no workspace needed
```

The wheel is attached for package mirrors that need one; jobs will run from it in a later release.

## Configure

1. Log in once: `databricks auth login --host https://<your-workspace> --profile <your-profile>`.
   The repo holds no workspace host; always pass `--profile`.
2. Set the bundle variables for your target. The ones you must set:

   | Variable | What |
   | --- | --- |
   | `catalog` | catalog to build in (default `gate_sandbox`) |
   | `ci_sp` | application ID of the CI service principal (no default) |
   | `owner_exception` | the catalog owner, the one person allowed to modify `ref.user_attributes` |
   | `tag_prefix` | prefix of your governed tags (default `gg`) |
   | `notify_on_failure` | list of addresses emailed when the `approve` job fails (every non-dev target) |
   | `llm_provider`, `llm_endpoint` (or `llm_base_url`, `llm_model`, `llm_secret_scope`, `llm_secret_key`) | the LLM |

   Put them in `.databricks/bundle/<target>/variable-overrides.json` (gitignored), for example:

   ```json
   { "catalog": "gate_uat", "ci_sp": "<application-id>", "owner_exception": "<owner user name>",
     "notify_on_failure": ["<address>"] }
   ```

   or pass `--var="name=value"` / set `BUNDLE_VAR_<name>` (simple variables only: `notify_on_failure` is a list,
   so set it in the overrides file or the target). Use the same method for deploy and run.
3. Add a target for your environment to `databricks.yml` (see the commented `uat` example there).

## A first run on synthetic data (about 15 minutes)

```bash
P=<your-profile>; T=dev
databricks bundle deploy -t $T --profile $P
databricks bundle run -t $T --profile $P setup_gov        # schemas, gov tables, review views
databricks bundle run -t $T --profile $P load_synthetic   # raw tables, glossary, golden set
databricks bundle run -t $T --profile $P hydrate --params table=claims
databricks bundle run -t $T --profile $P approve --params table=claims   # auto-rule + apply to Unity Catalog
databricks bundle run -t $T --profile $P hydrate --params table=claims   # re-run: guard, zero LLM calls
```

What to expect (seed 42, scale 1; taken from the reference dev workspace, not a fresh catalog):

| Check | Expected |
| --- | --- |
| `load_synthetic` row counts in `raw` | claims 2,500 · payments 8,000 · policies 6,000 · quotes 5,000 · clients 3,000 · client_master_us 800 · claims_extract_au 600 · brokers 150 · underwriters 60 |
| `raw.claims` by `rgn_cd` | UK 1,742 · US 488 · AU 270 |
| First `hydrate` on claims | 17 proposals, one per column: about 6 settled by fuzzy match, the rest by the LLM (exact split depends on the model) |
| Approved claims metadata | 17 columns; on the reference run 2 `pii`, 4 `confidential`, 10 `internal`, 1 `public` |
| Second `hydrate` on claims | guard exit: 0 LLM calls, nothing written to governance tables |
| `eval_hydration` (default prompt) | pii_recall 1.0 · term_precision ≈ 0.99 · no_match ≈ 0.91 |

Once row filters are applied, a person outside the persona groups sees 0 rows in `raw.claims` and `raw.payments`:
that is the row filter working, not a load failure.

Then the consumption gate: submit requests with `submit_request`, decide with `consumption_decide`, and apply with
`consumption_apply`. Run the persona matrix with `pytest tests/access` (one service principal and CLI profile per
persona; see `tests/access/matrix.yaml`).

## Deciding: the review queue

There is no review UI in this repo: your own review tool replaces it. Owners see open items in the `gov` views
(`v_review_queue`, `v_hydration_queue`, `v_consumption_queue`) and decide by inserting rows into
`gov.review_decisions` / `gov.request_decisions`. The contract is in `specs/contracts.md`. The `approve` job
applies decisions and then projects the result to Unity Catalog.

## Bringing your own glossary, prompts and abbreviations

- **Glossary**: `src/synthetic/glossary.yaml` is a synthetic example. Load your own terms into `gov` the same way.
- **Prompts**: `src/gates/hydration/prompts/propose_v*.md` are exemplars written against the synthetic glossary.
  Start from `prompts/templates/propose_template.md` (every `<<FILL: …>>` marker is yours to fill), save it as a
  new version, and select it with the `prompt_version` variable. `prompts/README.md` explains which file is which.
  Never edit a released prompt in place; measure each new version with `eval_hydration`.
- **Abbreviations**: `src/gates/hydration/abbreviations.yaml` is a synthetic example dictionary; replace it with
  yours, starting from `prompts/templates/abbreviations.template.yaml`.

## What's stubbed

These are deliberate seams, named so that replacing them is a change, not a defect fix:

- **Proxy (on-behalf-of) access mode**: requests are denied with `proxy_not_implemented`.
- **Access model source**: `access_model_source=file` reads the YAML stub; `api` is not built.
- **Permitted-use codes and architecture patterns**: synthetic lists; replace with yours.
- **Run log** `gov.hydration_runs`: specified in `specs/hydration.md`, not yet written.
- **Review UI**: none (see above).

## Development

```bash
pip install -e ".[dev]"
pytest tests/unit
ruff check src tests
```

`CLAUDE.md` describes how this repo is built with Claude Code: one slice at a time, tests first, a real run on a
dev workspace, fresh-eyes review for anything touching access. See `SECURITY.md` for reporting issues; never put
real data or secrets in an issue.
