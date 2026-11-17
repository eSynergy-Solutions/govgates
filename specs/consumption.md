# Consumption gate spec

**Purpose:** decide whether a consuming system may use a data product for a stated purpose, record that decision as evidence, and project the resulting access into Unity Catalog. A decision gate first, an enforcement gate second. A persona test matrix proves who sees what.

## Scope

**In:**
- Request → pattern check → use check → owner decision → consumption agreement, with every step in the evidence log (C1, C2, C3, C4).
- System mode and proxy mode on-platform (C5).
- Projecting the authorisation service's access model inward into `gov.access_policies`, then into UC grants, masks and row filters (C6, C7).
- The decision interface the client's review UI reads and writes. One contract, shared with hydration (C8, H2).

**Out:**
- Changes to the authorisation service, or anything it does outward. Another team owns it (C-AC7).
- Off-platform enforcement. The authorisation service or the consuming system enforces there; UC enforces on-platform.
- The front end itself. We define the contract; the client's review UI renders it.
- The publication gate. It doesn't exist yet, so products may lack what consumption needs in UC. Bridge: permitted uses are read from `gov.permitted_uses`, set by the owner at hydration approval (H15), and product facts are seeded per product with the owner until publication exists (C9).

**Environment assumption:** one production platform; dev, test and prod are catalogs. Nothing assumes a separate dev workspace. Catalog and schema names stay config.

## Flow

One request, decided by deterministic code. No LLM anywhere in this gate (ground rule 4).

1. **Request.** The consuming system raises a request: consumer principal, product (securable), `intended_use` code, free-text purpose note, `use_mode`, `delivery`, `access_mode` (`system` or `proxy`), requested by, requested at. Written to `gov.access_requests`, status `received`.
2. **Pattern check (C3).** Look up `(use_mode, delivery, access_mode)` in `gov.architecture_patterns`. No active allowed row → **deny**, reason `pattern_misaligned`. The pattern list is a synthetic stub until the client supplies theirs.
3. **Use check (C1, C2).** Look up the product's active rows in `gov.permitted_uses` for `intended_use`:
   - row with `approval = standing` → **auto-approve**
   - row with `approval = approval_required` → **owner approval**, status `pending_owner`
   - row with `approval = prohibited`, or no row for this use → **deny**, reason `use_not_permitted`
   - product has no permitted-use rows at all → **deny**, reason `no_permitted_uses` (fail closed). **Decide:** deny, or route to the owner while the publication gap lasts.
4. **Owner decision.** Only a principal in `gov.table_approvers` for the product may decide; a requester can't approve their own request (refused, audited). Actions: `approve`, `reject` (reason required), `note`. Notes can be added at any status.
   - **Input and identity:** the owner INSERTs one row into `gov.request_decisions`; `consumption_decide` applies it. The decider is the principal that committed the INSERT (change data feed + DESCRIBE HISTORY, as hydration approve), never the typed `decided_by`, which must match it. Refused (audited, row stamped `refused: <reason>`): committer unknown or the row changed after insert; committer is the job principal, not in `table_approvers` for the product, or equal to the request's `requester` or `consumer`.
   - **Order and time:** decisions apply in commit order; the agreement's `valid_from` is the commit time, never the typed `decided_at`.
   - **State checks on approve / reject:** the request must be `pending_owner` and match its audited state (`request_received` fields and purpose-note hash, a `request_status_set → pending_owner` row, a `use_check` evidence row of `pending_owner`); a hand-edited request is refused. Approve re-reads the active permitted-use row: if the use is no longer `approval_required` or `standing`, refused `use_no_longer_permitted`; `max_term_days` comes from that row.
   - **Known limitation:** `requester` is caller-asserted (the submit job runs as the CI principal and the platform does not record who triggered a run). It is audited and locked once submitted, but a false requester at submit time defeats the self-approval check. Self-approval through membership of a consumer group is not checked.
5. **Agreement (C4).** Every approval, automatic or by the owner, registers one row in `gov.consumption_agreements` (status `active`). Agreements are never deleted; `revoked` or `expired` retires one, and the generator removes the access it projected on the next run.
6. **Evidence log (C4).** Every step writes one row to `gov.consumption_decisions` (append-only): step, outcome, reason code, reason text, note, who, when, and the exact pattern and permitted-use rows it used (ids and versions), so any decision can be replayed. Every state change also writes `gov.audit_log`, gate `consumption`.
7. **Projection.** The generator turns active agreements plus the access model into policy rows and UC objects (next two sections). Re-run = no-op.

**Precedence is fixed:** pattern check before use check; deny beats everything; nothing reaches `auto_approve` without a `standing` row. Steps 2 and 3 are one pure function, `decide(request, patterns, permitted_uses) -> Decision(outcome, reason_code, inputs)`, tests first. Same inputs, same answer.

**Reason codes for a denial:** `invalid_request` and `proxy_not_implemented` (step `request_check`, before the pattern check); `pattern_misaligned` and `ambiguous_pattern` (more than one active pattern row); `no_permitted_uses`, `use_not_permitted` and `ambiguous_permitted_use` (more than one active row for the use). `invalid_request` also at step `use_check`, when the matched permitted-use row has an invalid `max_term_days` (zero, negative or not a whole number). The request's `reason` column holds the consumer's free-text purpose note.

**Request status values:** `received`, `approved`, `pending_owner`, `denied`, `rejected`, `withdrawn`.

## Agreements and the access model

Two sources, two jobs:
- **Agreements say whether.** An active agreement is the only thing that produces a `grant` row: the consumer gets USE CATALOG, USE SCHEMA and SELECT on the agreed product, nothing wider.
- **The access model says what, within that.** The authorisation service's model (roles, members, entitlements such as PII visibility and regions) is read by an **access-model adapter** and written as `mask` exceptions, `row_filter` rows and `ref.user_attributes` rows. Until we can read the real model, the adapter reads `config/access_model.example.yaml` (synthetic). Source is config: `access_model_source = file | api`.

The adapter (task `access_model_sync` of job `consumption_apply`, never a standalone job; runs as the CI principal) writes policy rows only through the audited policy-row writer, as one batch (`policy_row_set` audit rows, then one MERGE), and is the only writer of `ref.user_attributes` (each change audited `user_attributes_set`; a retired region is deleted, because the filter reads every row). The adapter is the only writer of `mask` and `row_filter` rows; there is no job for people to write them, and the `am_` policy-id prefix is reserved for it. The model's path is a deploy-time bundle variable, never a run-time parameter. **Signed off (9 Nov):**
- **Existence comes from config, not the model.** Every schema in `grant_schemas` has one active `row_filter` row and one active `mask` row per masked class, whatever the model says. The model only decides who is in a mask's EXCEPT list and which regions a principal sees. A removed entitlement shrinks an EXCEPT list or deletes a `user_attributes` row; the adapter never retires a policy row (retiring a filter would show every region to anyone holding an agreement). An active row outside that scope fails the adapter run; a person retires it.
- **Entitlement granularity is the schema.** Masks are schema-level, so PII visibility is granted per schema, not per table. The persona table below names tables for readability; entitlements are per schema.
- **Membership is checked, never changed or trusted** (option B, 9 Nov). Groups and members belong to the identity provider. The CI principal can't read account groups and no read-only group role exists, so job `check_access_model_membership` runs as the deploying person and records `membership_check` (with the model file's sha256; a same-day re-run with the same result writes nothing, any other result is a new newest row). It fails if a declared group does not exist or an `isolated_members` principal is in any group (the proxied persona must inherit nothing); other drift is a warning. It runs daily at 06:00 (Europe/London); re-run it after any edit to the model file. The adapter writes nothing unless the newest check passed, is for the same model file, is under 26 hours old (by the database clock), was recorded by someone other than the adapter's own principal, and no check row is dated in the future. Known limits: as fresh as its last run; it trusts a `gov` audit row (Q17); and the identity APIs are not documented to show nested group membership, which Unity Catalog does honour, so the check can miss an isolated principal in a nested group. The persona matrix, queried as the proxied principal itself, is the proof that it sees nothing; the membership check is an early warning. A re-run writes a row only when the result or the UTC day changes, so freshness counts from the day's first identical result. A future-dated check row blocks the adapter and has no audited way out yet (only tampering produces one, Q17). A crash between the audit write and the `ref.user_attributes` write leaves the old attributes live until the next successful run.

## Access modes (C5)

- **System mode.** The consumer's service principal is the reader. The grant goes to that principal; masks and row filters evaluate it. Typical use: a reference-data feed.
- **Proxy mode.** The consuming system passes the end user's context. Queries must run as that user (on-behalf-of), so `session_user()` and group membership resolve to the user and the same masks and row filters apply. The grant goes to the user's group from the access model, scoped by the agreement. **Decide:** confirm the on-behalf-of route with `docs-checker` before building; if the platform can't carry user context this way, proxy mode is documented and stubbed for the demo, not built.

The persona matrix carries one proxied user (C-AC3).

## Front-end contract (C8)

The client's review UI reads a queue and writes decisions; it never writes agreements, policy rows or UC objects.
- **Read:** a view `v_consumption_queue` over `access_requests` with status `pending_owner`, the permitted-use row matched and the pattern row matched.
- **Write:** one row per decision into `gov.request_decisions`, which the gate validates and applies (approver check, reason required on reject), the same shape as hydration's `review_decisions`.
The shared interface for both gates is written up in `specs/contracts.md` (Tue 10 Nov slice).

## Config seams

- `permitted_use_codes`, `architecture_patterns`: synthetic stubs now, client lists later.
- `access_model_source`: `file` (YAML stub) or `api`.
- `no_permitted_uses_outcome`: `deny` (default) or `owner`.
- Group names, `tag_prefix`, catalog and schema names: bundle variables, as today.

## Personas (account groups; one service principal each for testing)

| Group | Sees | PII | Rows |
| --- | --- | --- | --- |
| `gs_analyst_uk` | curated, policies, claims | masked | UK only |
| `gs_analyst_global` | curated, policies, claims | masked | all regions |
| `gs_claims_handler` | claims, clients | visible | UK only |
| `gs_data_owner` | everything | visible | all |
| `gs_no_access` | nothing | — | none |

Groups stand in for an identity provider's groups (e.g. Entra). Group names are config.

## Policy model (`gov.access_policies`)

- `grant` rows → produced only from active consumption agreements (above): `GRANT USE CATALOG / USE SCHEMA / SELECT` on the agreed product to the agreed principal or group. Generator also revokes grants no live agreement backs (drift control), so a revoked agreement loses its access on the next run.
  - **Source of grants:** what is granted and revoked comes from verified live agreements; `grant` rows are their audited record (written only by the generator, tamper-checked, `gr_` ids reserved), active only while the grant is in place.
  - **Consumer:** never `account users`, `users`, `admins` or empty (checked by name; other workspace-local groups fail at GRANT time); an active agreement for one is refused and audited.
  - **Live agreement:** status `active`, `valid_to` > now (UTC), an `agreement` evidence row, `access_mode = system`, and the stored row equals its newest audited state (`agreement_registered` / `agreement_status_set`). Anything else produces no grant; a mismatch is refused and audited.
  - **Drift scope:** only the schemas in the `grant_schemas` bundle variable (default: the raw schema). Revoked: SELECT and USE SCHEMA granted directly on a managed schema or a table in it, and SELECT on the catalog, with no live agreement behind them. ALL PRIVILEGES, MANAGE or ownership in scope held by anyone else is a failure (audited, job non-zero), not revoked.
  - **USE CATALOG** is added, never revoked: it gives no data on its own and also gates gov/ref.
  - **Exempt from drift:** only the CI service principal and `owner_exception`. Any other owner in scope is a failure.
  - **Revokes never wait:** revokes run even when an earlier generator task failed; grants run only after masks succeed.
  - **Not yet applied:** a product with approved metadata not yet applied to UC (or none) grants nothing and keeps nothing until apply runs, so a column newly approved or revised as pii is never readable unmasked during the gap.
- `mask` rows → one masking function per sensitivity class in `gov`; one ABAC column-mask policy at schema level matching `<tag_prefix>_sensitivity = <class>`, applied to all users except entitled groups. Entitled groups come from the access model, via the adapter.
- `row_filter` rows → one function checking the caller's region against `ref.user_attributes` (group membership fallback); one ABAC row-filter policy matched on `<tag_prefix>_rls_attribute = region`. The row carries `securable` (the schema), `match_tag_key = <tag_prefix>_rls_attribute`, `match_tag_value = region`, `function_name` and `except_principals`.
  - **Region lookup:** the caller's `ref.user_attributes` rows are those whose `principal` equals `session_user()` (a service principal's applicationId) or names an account group the caller is in (`is_account_group_member`). If any matched row is `NONE` → no rows. Else if any is `ALL` → every row, including rows with a NULL region. Else rows whose region is one of the matched regions; a NULL region is never matched. No matched row → **no rows** (fail closed).
  - **Who may change `ref.user_attributes`:** it is an access-control table. Able to modify it: only the CI service principal (which is also the synthetic loader's principal) and the catalog owner, the one allowed exception, named in the `owner_exception` bundle variable (empty by default, so the check fails until it is set; it must equal the catalog owner). Nobody else. "Able to modify" counts ownership of the table, its schema or its catalog, and MODIFY, ALL PRIVILEGES or MANAGE at any of those levels. A check job (`check_user_attributes`, also the first task of the generator) enforces this by detection: it fails closed, audits each change of result as `user_attributes_check`, and the generator does not run while it fails.
  - **Who writes `row_filter` rows:** a small job that MERGEs the row into `gov.access_policies` and writes a `policy_row_set` audit row with before/after. No hand SQL. Rows are never deleted (contracts.md); `active = false` retires one.
  - **Retiring:** for a `row_filter` row that is `active = false`, the generator drops its policy (policies are named from `policy_id`, so it can find them) and unsets `<tag_prefix>_rls_attribute` on that securable's columns. At most one active `row_filter` row per securable; more is refused.
  - **Who sets the tag:** the consumption generator, never hydration and never a person by hand. For each active `row_filter` row it sets `<tag_prefix>_rls_attribute = region` on every column in `securable` whose `gov.approved_metadata.term_id` = `rls_term_id` (bundle variable, default `region_code`), and unsets it on any column in `securable` that carries it but no longer qualifies (drift control, as for grants). It reads `approved_metadata` from `gov`, never UC tags, as truth. A column approved as the region term is filtered on the next generator run with no other change, the same join-up as masks.
  - **EXCEPT:** the hydration job's principal is in the row-filter policy's `except_principals`, as for masks. Otherwise profiling sees one region's rows (or none) and pattern rates and row counts are wrong. The hydrate and eval jobs run as the CI service principal, so that is the hydration principal. Nobody else is exempt by default; admins and owners are filtered too.
  - **Tag ownership:** hydration apply owns `<tag_prefix>_sensitivity` and `<tag_prefix>_business_term`; the consumption generator owns `<tag_prefix>_rls_attribute`. Neither touches the other's keys. Each tag set or unset writes a `gov.audit_log` row; a re-run sets nothing.
- Generator records every statement it runs in `gov.audit_log`, and writes them to a run-local `.sql` file. Re-run = no-op.
- **Fallback:** if an ABAC policy shape doesn't work, emit per-table `SET MASK` / `SET ROW FILTER` from the same rows.
- Check current `CREATE POLICY` syntax in the Databricks docs before writing the generator. Do not write it from memory.

## Join-up

Hydration sets `<tag_prefix>_sensitivity = pii` on a column → the schema-level mask policy applies with no consumption-side change. The persona matrix must show this. Tag keys come from the `tag_prefix` bundle variable (default `gg`); both are account-wide governed tags (`sql/setup/governed_tags.sql`). The hydration job's principal must be in every mask policy's EXCEPT list (or profile an unmasked source): otherwise profiling reads masked values, pattern hits drop to zero and the `pii` floor silently disappears.

## Access test harness

- `tests/access/matrix.yaml` (human-written): persona × `table.column` → `visible | masked | denied`, plus expected row counts per region.
- Runs queries as each persona's service principal over a SQL warehouse (OAuth M2M). Never as an admin.
- Output: pass/fail grid as markdown — the UAT evidence format.
- Includes one proxied user (C-AC3) and a persona with no agreement, who must see nothing.

## Decision test matrix

Table-driven unit tests for `decide()`, one case per row of the outcome rules above: standing → `auto_approve`; approval_required → `pending_owner`; prohibited, unknown use, no permitted uses, pattern misaligned → `denied` with the right reason code; pattern misaligned with a standing use → `denied` (precedence). Plus owner-path tests: approve registers one agreement; reject without a reason is refused; self-approval is refused; re-applying the same decision changes nothing. This is C-AC1 and C-AC2's evidence.

## Decisions (8 Nov)

1. No permitted uses recorded → deny, reason_code `no_permitted_uses`.
2. Proxy mode is stubbed for the demo. Requests with `access_mode = proxy` are denied with
   reason_code `proxy_not_implemented` and logged to `consumption_decisions`. On-behalf-of
   is SOW scope.
3. Agreements always carry `valid_to` = `valid_from` + the permitted use's `max_term_days`
   (default 365). Expiry sets status `expired`; the next generator run removes access.
   The generator's sweep sets `active → expired` (audited) once `valid_to` ≤ now; a person can only set
   `revoked` (job `set_agreement_status`, reason and triggering person required). Status never returns to `active`.
4. Hydration approval captures permitted uses (see `hydration.md` step 6). Approval is
   refused without at least one row.

## Decisions (9 Nov)

5. A changed purpose note under the same `request_ref` is not a new request: it is refused like any other
   changed field ("raise a new request_ref"). The owner approves the note that is stored, so the
   `request_received` audit row carries the note's hash.
6. `requester` stays caller-asserted for the demo (see Flow 4, known limitation).

Client questions that change this spec are listed in `PLAN.md`.
