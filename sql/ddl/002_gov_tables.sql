-- gov schema: see specs/contracts.md. All CREATE IF NOT EXISTS so the runner is idempotent.

CREATE TABLE IF NOT EXISTS ${catalog}.${gov_schema}.business_terms (
  term_id STRING NOT NULL,
  name STRING NOT NULL,
  definition STRING,
  synonyms ARRAY<STRING>,
  sensitivity STRING,
  domain STRING,
  version INT,
  loaded_at TIMESTAMP
) COMMENT 'Business glossary';

CREATE TABLE IF NOT EXISTS ${catalog}.${gov_schema}.golden_set (
  table_name STRING NOT NULL,
  column_name STRING NOT NULL,
  expected_term_id STRING,
  expected_sensitivity STRING,
  difficulty STRING
) COMMENT 'Eval ground truth: expected term per column (null term = no correct match)';

CREATE TABLE IF NOT EXISTS ${catalog}.${gov_schema}.column_profiles (
  run_id STRING,
  table_name STRING NOT NULL,
  column_name STRING NOT NULL,
  data_type STRING,
  null_pct DOUBLE,
  distinct_pct DOUBLE,
  pattern_hits ARRAY<STRING>,
  sample_hash STRING,
  schema_hash STRING,
  profiled_at TIMESTAMP,
  any_pattern_hits ARRAY<STRING>,
  avg_length DOUBLE
) COMMENT 'Hydration profiler output. No raw values.';

CREATE TABLE IF NOT EXISTS ${catalog}.${gov_schema}.term_proposals (
  proposal_id STRING NOT NULL,
  run_id STRING,
  table_name STRING NOT NULL,
  column_name STRING NOT NULL,
  term_id STRING,
  description STRING,
  sensitivity STRING,
  confidence DOUBLE,
  method STRING,
  model STRING,
  prompt_version STRING,
  tokens INT,
  rationale STRING,
  status STRING,
  created_at TIMESTAMP
) COMMENT 'Hydration proposals awaiting or after owner review';

CREATE TABLE IF NOT EXISTS ${catalog}.${gov_schema}.approved_metadata (
  table_name STRING NOT NULL,
  column_name STRING NOT NULL,
  term_id STRING,
  description STRING,
  sensitivity STRING,
  approved_by STRING,
  approved_at TIMESTAMP,
  applied_to_uc_at TIMESTAMP
) COMMENT 'Owner-approved metadata. Source of truth; Unity Catalog is a projection of this table';

CREATE TABLE IF NOT EXISTS ${catalog}.${gov_schema}.access_policies (
  policy_id STRING NOT NULL,
  policy_type STRING NOT NULL,
  securable STRING,
  principal STRING,
  privilege STRING,
  match_tag_key STRING,
  match_tag_value STRING,
  function_name STRING,
  except_principals ARRAY<STRING>,
  active BOOLEAN
) COMMENT 'Consumption policy as code: grant, mask and row_filter rows';

CREATE TABLE IF NOT EXISTS ${catalog}.${gov_schema}.access_requests (
  request_id STRING NOT NULL,
  requester STRING,
  securable STRING,
  reason STRING,
  status STRING,
  approver STRING,
  requested_at TIMESTAMP,
  decided_at TIMESTAMP,
  consumer STRING,
  intended_use STRING,
  use_mode STRING,
  delivery STRING,
  access_mode STRING
) COMMENT 'Access requests and decisions';

CREATE TABLE IF NOT EXISTS ${catalog}.${gov_schema}.audit_log (
  event_id STRING NOT NULL,
  gate STRING,
  action STRING,
  actor STRING,
  object STRING,
  before_json STRING,
  after_json STRING,
  event_at TIMESTAMP
) COMMENT 'Every governance change made by either gate';

CREATE TABLE IF NOT EXISTS ${catalog}.${gov_schema}.eval_runs (
  run_id STRING NOT NULL,
  prompt_version STRING,
  model STRING,
  fuzzy_accept INT,
  term_precision DOUBLE,
  coverage DOUBLE,
  no_match_accuracy DOUBLE,
  pii_recall DOUBLE,
  fuzzy_share DOUBLE,
  tokens_per_dataset DOUBLE,
  run_at TIMESTAMP,
  extra_recall_at_threshold DOUBLE,
  extra_sensitivity_accuracy DOUBLE,
  extra_under_classified_count DOUBLE,
  extra_missing_count DOUBLE
) COMMENT 'Hydration eval history (spec metrics; extra_* are extras outside specs/eval.md metrics table)';

CREATE TABLE IF NOT EXISTS ${catalog}.${gov_schema}.review_decisions (
  decision_id STRING NOT NULL,
  proposal_id STRING NOT NULL,
  action STRING NOT NULL,
  term_id STRING,
  description STRING,
  sensitivity STRING,
  decided_by STRING,
  decided_at TIMESTAMP,
  applied_at TIMESTAMP,
  outcome STRING
) COMMENT 'Owner decisions on one proposal (accept/edit/reject), inserted by SQL; the approve job applies them'
TBLPROPERTIES (delta.enableChangeDataFeed = true);

-- The approver is the principal who committed the row (Change Data Feed + DESCRIBE HISTORY), not the typed
-- decided_by. Re-asserted on every setup so a table created before this property gets it (same end state).
ALTER TABLE ${catalog}.${gov_schema}.review_decisions SET TBLPROPERTIES (delta.enableChangeDataFeed = true);

CREATE TABLE IF NOT EXISTS ${catalog}.${gov_schema}.golden_candidates (
  proposal_id STRING NOT NULL,
  table_name STRING NOT NULL,
  column_name STRING NOT NULL,
  proposed_term_id STRING,
  edited_term_id STRING,
  proposed_sensitivity STRING,
  edited_sensitivity STRING,
  edited_by STRING,
  edited_at TIMESTAMP
) COMMENT 'Owner edits from approve; a human promotes them into the golden set';

CREATE TABLE IF NOT EXISTS ${catalog}.${gov_schema}.table_approvers (
  table_name STRING NOT NULL,
  principal STRING NOT NULL,
  added_by STRING,
  added_at TIMESTAMP
) COMMENT 'Who may approve metadata for a table. A manual decision committed by anyone else is refused';
