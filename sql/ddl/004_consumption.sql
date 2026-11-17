-- Consumption decision gate (specs/consumption.md Flow; specs/contracts.md). Idempotent: CREATE IF NOT EXISTS, and
-- run_ddl adds only the missing columns of an ADD COLUMNS (SQL has no ADD COLUMN IF NOT EXISTS). No column DEFAULTs:
-- max_term_days NULL means 365, applied in code.

CREATE TABLE IF NOT EXISTS ${catalog}.${gov_schema}.permitted_uses (
  product STRING NOT NULL,
  use_code STRING NOT NULL,
  approval STRING NOT NULL,
  conditions STRING,
  max_term_days INT,
  set_by STRING,
  set_at TIMESTAMP,
  version INT,
  active BOOLEAN
) COMMENT 'Uses a product may serve (standing / approval_required / prohibited), set by the owner at hydration approval';

CREATE TABLE IF NOT EXISTS ${catalog}.${gov_schema}.architecture_patterns (
  pattern_id STRING NOT NULL,
  use_mode STRING,
  delivery STRING,
  access_mode STRING,
  allowed BOOLEAN,
  reason STRING,
  version INT,
  active BOOLEAN
) COMMENT 'Allowed use mode x delivery x access mode (synthetic stub until the client list)';

CREATE TABLE IF NOT EXISTS ${catalog}.${gov_schema}.consumption_agreements (
  agreement_id STRING NOT NULL,
  request_id STRING NOT NULL,
  consumer STRING,
  product STRING,
  use_code STRING,
  access_mode STRING,
  use_mode STRING,
  delivery STRING,
  decided_by STRING,
  status STRING,
  valid_from TIMESTAMP,
  valid_to TIMESTAMP,
  version INT
) COMMENT 'Approved consumption, one per approved request. Never deleted: revoked / expired retire one';

CREATE TABLE IF NOT EXISTS ${catalog}.${gov_schema}.consumption_decisions (
  decision_id STRING NOT NULL,
  request_id STRING NOT NULL,
  step STRING,
  outcome STRING,
  reason_code STRING,
  reason_text STRING,
  note STRING,
  decided_by STRING,
  decided_at TIMESTAMP,
  inputs_json STRING
) COMMENT 'Consumption evidence log, append-only';

ALTER TABLE ${catalog}.${gov_schema}.access_requests ADD COLUMNS (consumer STRING, intended_use STRING, use_mode STRING, delivery STRING, access_mode STRING);
