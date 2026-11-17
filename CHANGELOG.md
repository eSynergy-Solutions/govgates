# Changelog

All notable changes to this project are listed here. Versions follow [Semantic Versioning](https://semver.org);
before 1.0.0 a minor version may change behaviour. Published tags are never moved: a fix is a new tag.

## [0.1.0] - 2026-11-17

First public release. Runs end to end on the bundled synthetic data.

### Hydration gate

- Profiles a landed table (null and distinct rates, lengths, pattern hits; no raw values stored).
- Proposes a business term, description and sensitivity per column: fuzzy match against the glossary
  first, LLM second for what fuzzy can't settle. Wide tables are split into chunks
  (`llm_max_columns_per_call`, default 40).
- Code enforces a sensitivity floor from pattern hits and the chosen term; the model can raise a class,
  never lower it.
- Schema-hash guard: re-running an unchanged, fully covered table makes zero LLM calls and writes nothing.
- Owner review through `gov` views; approved metadata is written to `gov` and projected to Unity Catalog
  (comments and governed tags) by the `approve` job's `apply` task.
- LLM provider is configuration: a Databricks serving endpoint (`ai_query`) or any OpenAI-compatible
  base URL with the key in a secret scope.
- Eval harness with a synthetic golden set, logged to MLflow (`eval_hydration`).

### Consumption gate

- Access policies are rows in `gov.access_policies`; a generator turns them into Unity Catalog grants,
  ABAC column masks (by sensitivity tag) and row filters (by region attribute).
- Access requests with pattern check, permitted-use check and owner decision (`submit_request`,
  `consumption_decide`).
- Access-model adapter reading a YAML stub (`config/access_model.example.yaml`).
- Persona test matrix (`tests/access/`) proving who sees what, run as each persona's own service principal.

### Packaging

- Databricks Asset Bundle; catalog, schemas, tag prefix, LLM settings and CI principal are variables.
  No workspace host in the repo.
- One lock file for job dependencies (`requirements/jobs.txt`); version in `VERSION`.
- Wheel and source archive attached to the GitHub Release with SHA-256 checksums. Jobs still run from
  bundle files (`python_file` tasks); running them from the wheel is planned for 0.2.0.

### Known limitations

- Proxy (on-behalf-of) access mode is stubbed: requests are denied with `proxy_not_implemented`.
- The access-model adapter reads a YAML file only; the API source is not built.
- `gov.hydration_runs` (per-run log) is specified but not yet written.
- Permitted-use codes and architecture patterns are synthetic stubs to be replaced with your own lists.
- No review UI: owners decide by inserting rows the `gov` review views describe (see `specs/contracts.md`).
