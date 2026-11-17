-- Owner inputs for both gates (specs/consumption.md Flow 4, Front-end contract; specs/hydration.md step 6).
-- Owners INSERT one row per decision by SQL; the gate validates and applies it, then stamps applied_at / outcome.
-- The decider is the principal that committed the INSERT (Change Data Feed + DESCRIBE HISTORY), never the typed
-- decided_by, so CDF is on and re-asserted on every setup (same end state). Owners need INSERT here only, never
-- MODIFY on access_requests, consumption_agreements, permitted_uses or table_approvers (Q17).

CREATE TABLE IF NOT EXISTS ${catalog}.${gov_schema}.request_decisions (
  decision_id STRING NOT NULL,
  request_id STRING NOT NULL,
  action STRING NOT NULL,
  reason STRING,
  note STRING,
  decided_by STRING,
  decided_at TIMESTAMP,
  applied_at TIMESTAMP,
  outcome STRING
) COMMENT 'Owner decisions on a consumption request (approve / reject with reason / note), inserted by SQL; consumption_decide applies them'
TBLPROPERTIES (delta.enableChangeDataFeed = true);

ALTER TABLE ${catalog}.${gov_schema}.request_decisions SET TBLPROPERTIES (delta.enableChangeDataFeed = true);

CREATE TABLE IF NOT EXISTS ${catalog}.${gov_schema}.permitted_use_inputs (
  input_id STRING NOT NULL,
  product STRING NOT NULL,
  use_code STRING NOT NULL,
  approval STRING NOT NULL,
  conditions STRING,
  max_term_days INT,
  decided_by STRING,
  decided_at TIMESTAMP,
  applied_at TIMESTAMP,
  outcome STRING
) COMMENT 'Owner-set permitted uses for a product, inserted by SQL at hydration approval; the approve job applies them via set_permitted_use'
TBLPROPERTIES (delta.enableChangeDataFeed = true);

ALTER TABLE ${catalog}.${gov_schema}.permitted_use_inputs SET TBLPROPERTIES (delta.enableChangeDataFeed = true);
