# Data contracts: `gov` and `ref` schemas

All tables are Delta, managed, in `${catalog}.${gov_schema}` unless stated. DDL: `sql/ddl/`.

| Table | Purpose | Key columns |
| --- | --- | --- |
| `business_terms` | Glossary | term_id (PK), name, definition, synonyms (array), sensitivity, domain, version |
| `golden_set` | Eval ground truth | table_name, column_name, expected_term_id (null = no match), expected_sensitivity, difficulty |
| `column_profiles` | Profiler output | table_name, column_name, data_type, null_pct, distinct_pct, pattern_hits (array), any_pattern_hits (array), avg_length, sample_hash, schema_hash, profiled_at |
| `term_proposals` | Gate proposals | proposal_id, run_id, table_name, column_name, term_id, description, sensitivity, confidence, method (`fuzzy`/`llm`/`rule`), model, prompt_version, tokens, status, created_at |
| `approved_metadata` | Approved truth | table_name, column_name (PK pair), term_id, description, sensitivity, approved_by, approved_at, applied_to_uc_at |
| `review_decisions` | Manual approve path | decision_id, proposal_id, action (`accept`/`edit`/`reject`/`revise`), term_id, description, sensitivity, decided_by, decided_at, applied_at, outcome (`accepted`/`edited`/`rejected`/`revised`, or `refused: <reason>`) |
| `table_approvers` | Who may approve a table's metadata | table_name, principal, added_by, added_at |
| `golden_candidates` | Owner edits for the golden set | proposal_id, table_name, column_name, proposed_term_id, edited_term_id, proposed_sensitivity, edited_sensitivity, edited_by, edited_at |
| `access_policies` | Policy as code | policy_id, policy_type (`grant`/`mask`/`row_filter`), securable, principal, privilege, match_tag_key, match_tag_value, function_name, except_principals (array), active |
| `access_requests` | Consumption requests (extend with ALTER TABLE; no drop) | request_id, requester, securable, reason, status, approver, requested_at, decided_at, consumer, intended_use, use_mode, delivery, access_mode (`system`/`proxy`) |
| `permitted_uses` | Uses a product may serve, set by the owner at hydration approval | product, use_code, approval (`standing`/`approval_required`/`prohibited`), conditions, max_term_days (default 365), set_by, set_at, version, active |
| `architecture_patterns` | Allowed use mode × delivery × access mode | pattern_id, use_mode, delivery, access_mode, allowed, reason, version, active |
| `consumption_agreements` | Approved consumption, one per approved request | agreement_id, request_id, consumer, product, use_code, access_mode, use_mode, delivery, decided_by (`auto` or principal), status (`active`/`revoked`/`expired`), valid_from, valid_to, version |
| `consumption_decisions` | Evidence log, append-only | decision_id, request_id, step (`request_check`/`pattern_check`/`use_check`/`owner`/`agreement`), outcome, reason_code, reason_text, note, decided_by, decided_at, inputs_json |
| `request_decisions` | Owner decisions on a consumption request, inserted by SQL (CDF on) | decision_id, request_id, action (`approve`/`reject`/`note`), reason, note, decided_by, decided_at, applied_at, outcome (`approved`/`rejected`/`noted`, or `refused: <reason>`) |
| `permitted_use_inputs` | Owner-set permitted uses, inserted by SQL at hydration approval (CDF on) | input_id, product, use_code, approval, conditions, max_term_days, decided_by, decided_at, applied_at, outcome (`set`/`unchanged`, or `refused: <reason>`) |
| `audit_log` | Every governance change | event_id, gate (`hydration`/`consumption`), action, actor, object, before_json, after_json, event_at |
| `eval_runs` | Eval history | run_id, prompt_version, model, term_precision, coverage, no_match_accuracy, pii_recall, fuzzy_share, tokens_per_dataset, run_at |
| `hydration_runs` | Hydration run log, one row per attempt per table (append-only) | job_run_id, task_run_id, table_name, run_id (the proposal run; the eval run_id when forced; NULL on a guard exit), schema_hash, guard_hit, reason, forced, columns_total, columns_proposed, llm_calls, tokens, model, prompt_version, status (`running`/`skipped`/`succeeded`/`failed`), error_class, started_at, finished_at, duration_ms |

`${catalog}.${ref_schema}.user_attributes`: principal, region, line_of_business.

## Rules

- `table_name` is always the fully qualified three-part name.
- `term_proposals` status values: `proposed`, `accepted`, `edited`, `rejected` (plus `eval` for forced runs, which never enter the review queue).
- **Run logs are not governance state.** `hydration_runs` records that a job ran, not a decision. It is append-only, written with no `audit_log` row, and it grows by one row per attempt, so a re-run that changes nothing in the governance tables still adds a run-log row. `error_class` holds the exception type only, never the message, because Spark messages can contain data values. Anyone with MODIFY on `gov` can write rows here, so this table is evidence of what ran, not proof against tampering. Decisions are proven by `audit_log`.
- Sensitivity values: `public`, `internal`, `confidential`, `pii`.
- Nothing in `gov` is ever deleted by a gate; superseded rows get a new version or status.

## Review-queue interface (H2, C8) — draft (9 Nov), awaiting sign-off

One contract for both gates. The client's review UI reads views and writes rows; deterministic jobs validate and apply
them (H-AC8). Every rule below is what the code does today; where the code is narrower than a UI might assume, it
says so.

**Rules for the UI**
- **Read the six views below**, never `approved_metadata`, agreements, policy rows or UC directly.
- **Write only by INSERT** into `review_decisions`, `request_decisions` or `permitted_use_inputs`. Never UPDATE or
  DELETE (a changed row is refused as tampered). A new unique `decision_id` / `input_id` per row, generated by the UI (e.g.
  a UUID string; SQL `uuid()` is rejected inside INSERT ... VALUES / SELECT here); an id already pending twice, or
  (hydration) already applied, is refused.
- **Identity is the committer.** The job takes the decider from the principal that committed the INSERT (change data
  feed + table history), never from a typed name. `decided_by` must equal it (trimmed, case-insensitive). The UI
  writes as the person deciding, never as a shared service account; the jobs' own principal is always refused.
  Approvers are individual principals in `table_approvers`; a group there is not matched.
- **Feedback** is on the input row: `applied_at` and `outcome` are NULL until a job run applies or refuses it, then
  `outcome` is the result or `refused: <reason>` (human-readable text for display, not a code to parse). If the change
  feed can't be read, rows are held (still NULL) until it can. `v_decision_outcomes` lists every input row.
- **When decisions apply.** On the next run of `approve` for that table (hydration decisions and that table's
  permitted uses) or of `consumption_decide` (consumption). Neither job is scheduled yet: on dev they run on demand.
  Hydration UC changes land in the same `approve` run: its second task, apply, runs only if approve succeeded
  (Q20; signed off 10 Nov); consumption UC changes follow on `consumption_apply`. A row
  inserted more than 6 days before the run that reads it is refused as stale: insert it again. If the table is
  missing or not visible to the job, only reject and revise apply; accept/edit and permitted uses stay pending and
  the run fails, so apply does not run. The approve run also applies the demo auto-rule after the manual decisions, so
  a queued proposal can be auto-accepted by the same run.
- **No raw values** anywhere: profile statistics and pattern hits only, no sample or schema hashes.
- **Who can read the views:** nothing is granted yet. The views do not filter by caller: anyone with SELECT on them
  sees every open item, every approver list, and every refusal with committer names. Grant SELECT only to the review
  UI's users (the approvers) until a per-caller filter is decided. **Decide:** filter to the caller's items, or not.

**Read: views in `${catalog}.${gov_schema}`**

| View | One row per | Key columns |
| --- | --- | --- |
| `v_review_queue` | open item, both gates | gate (`hydration`/`consumption`), item_id, object, proposed (JSON), context (JSON), waiting_since, approvers, can_decide |
| `v_hydration_queue` | column whose newest non-eval proposal is `proposed` | proposal_id, table_name, column_name, term (id, name, definition: newest term version), description, sensitivity, confidence, method, prompt_version, rationale, profile (data_type, null_pct, distinct_pct, avg_length, pattern_hits, any_pattern_hits), current approved row if any, has_permitted_uses, approvers, can_decide |
| `v_consumption_queue` | `access_requests` row with status `pending_owner` | request_id, product, consumer, requester, intended_use, purpose_note, use_mode, delivery, access_mode, requested_at, use_rows and (when exactly one) the active permitted-use row, current_pattern_* (the pattern active now, not the one the request matched: that is in its evidence), approvers, can_decide |
| `v_approved_columns` | `approved_metadata` row | approved term, description, sensitivity, approved_by/at, applied_to_uc_at, revise_proposal_id (the only proposal a revise is applied to), pending_proposal_id (a newer open proposal: decide it first), approvers, can_decide |
| `v_permitted_uses` | active `permitted_uses` row | product, use_code, approval, conditions, max_term_days, version, set_by/at, approvers, can_decide |
| `v_decision_outcomes` | row in any of the three input tables | gate, decision_id, item_id, action, decided_by, decided_at, applied_at, outcome. Permitted-use rows: gate `hydration` (they are set at hydration approval; their audit rows say `consumption`), item_id `<product>:<use_code>`, action `permitted_use:<approval>` |

`can_decide` = `current_user()` is one of the item's approvers (trimmed, case-insensitive) and, for consumption, is
not the request's requester or consumer. It does not predict every refusal: the rules per action below still apply.

**Write: hydration — `review_decisions`** (every action: proposal exists, committer is an approver for the table,
the proposal's run has a profile)

| action | Send | Refused when | outcome |
| --- | --- | --- | --- |
| `accept` | proposal_id only | any of term_id / description / sensitivity is set (use edit); proposal not `proposed` or not the column's newest; proposed term no longer in `business_terms`; no active permitted use for the table | `accepted` |
| `edit` | proposal_id, plus the fields to change (NULL keeps the proposed value; `NO_MATCH` clears the term) | proposal not `proposed` or not newest; term_id `''`; term_id not in `business_terms`; proposed term retired and no term_id given; sensitivity not a valid class; no active permitted use. Sensitivity is re-floored from the profile | `edited` |
| `reject` | proposal_id | proposal not `proposed` or not newest; the proposal's method is `rule` (edit it instead) | `rejected` |
| `revise` | `revise_proposal_id` from `v_approved_columns`, plus the fields to change | not that column's newest accepted/edited proposal; a newer proposal is still `proposed`; no approved row; no profile for that run or a newer run of the column; term_id `''`; term not in `business_terms`; approved term retired and no term_id given; sensitivity invalid; no change | `revised` |

**Permitted uses — `permitted_use_inputs`** (hydration step 6, H15; applied by the approve run for that product,
before its decisions): product, use_code (from the `permitted_use_codes` list the deployment configures; not stored
in gov), approval (`standing`/`approval_required`/`prohibited`), conditions, max_term_days (empty or a positive whole
number; empty means 365). Committer must be an approver for the product. outcome `set` (a new version, the old one
retired) or `unchanged`. Accept and edit need at least one active permitted use for the table (any approval value,
including `prohibited`); reject and revise never wait for one.

**Write: consumption — `request_decisions`** (every action: committer is an approver for the product and is not the
request's requester or consumer)

| action | Send | Refused when | outcome |
| --- | --- | --- | --- |
| `approve` | request_id | request not `pending_owner`; request differs from its audited state (fields or purpose-note hash); use no longer `standing` / `approval_required` (`use_no_longer_permitted`); more than one active row for the use (`ambiguous_permitted_use`); invalid max_term_days | `approved`: one agreement, valid from the commit time for the use's max_term_days |
| `reject` | request_id, reason | reason empty; request not `pending_owner` or differs from its audited state | `rejected` |
| `note` | request_id, note | note empty. Allowed at any status | `noted` |

**Refused for every input row, all three tables** (audited, row stamped `refused: …`): committer unknown, row changed
after insert, or no change data in the last 6 days; `decided_by` (where the table has it) not the committer;
committer is the job's own principal; committer not an approver for the table / product; duplicate id.
