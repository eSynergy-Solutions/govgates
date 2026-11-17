-- Review queue, hydration (specs/contracts.md, Review-queue interface). One row per open proposal: the newest
-- non-eval proposal for its column (created_at, then proposal_id, as the approve job orders), still `proposed`.
-- Profile statistics and pattern hits only, never values or hashes.
-- can_decide: current_user() is an approver for the table, compared trimmed and case-insensitive (as the job does).
-- has_permitted_uses: false means accept / edit will be refused until a permitted use is set; reject is not blocked.
CREATE OR REPLACE VIEW ${catalog}.${gov_schema}.v_hydration_queue AS
WITH ranked AS (
  SELECT p.*, row_number() OVER (PARTITION BY p.table_name, p.column_name
                                 ORDER BY p.created_at DESC NULLS LAST, p.proposal_id DESC) AS rn
  FROM ${catalog}.${gov_schema}.term_proposals p
  WHERE p.status <> 'eval'
),
terms AS (
  SELECT term_id, name, definition
  FROM ${catalog}.${gov_schema}.business_terms
  QUALIFY row_number() OVER (PARTITION BY term_id ORDER BY version DESC, loaded_at DESC) = 1
),
approvers AS (
  SELECT table_name, collect_set(principal) AS approvers
  FROM ${catalog}.${gov_schema}.table_approvers
  GROUP BY table_name
),
uses AS (
  SELECT DISTINCT product FROM ${catalog}.${gov_schema}.permitted_uses WHERE active
)
SELECT
  p.proposal_id, p.run_id, p.table_name, p.column_name,
  p.term_id, t.name AS term_name, t.definition AS term_definition,
  p.description, p.sensitivity, p.confidence, p.method, p.model, p.prompt_version, p.rationale, p.created_at,
  c.data_type, c.null_pct, c.distinct_pct, c.avg_length, c.pattern_hits, c.any_pattern_hits,
  a.term_id AS approved_term_id, a.description AS approved_description, a.sensitivity AS approved_sensitivity,
  a.approved_by, a.approved_at,
  u.product IS NOT NULL AS has_permitted_uses,
  coalesce(ap.approvers, CAST(array() AS ARRAY<STRING>)) AS approvers,
  exists(coalesce(ap.approvers, CAST(array() AS ARRAY<STRING>)),
         x -> lower(trim(x)) = lower(trim(current_user()))) AS can_decide
FROM ranked p
LEFT JOIN ${catalog}.${gov_schema}.column_profiles c
  ON c.run_id = p.run_id AND c.table_name = p.table_name AND c.column_name = p.column_name
LEFT JOIN terms t ON t.term_id = p.term_id
LEFT JOIN ${catalog}.${gov_schema}.approved_metadata a
  ON a.table_name = p.table_name AND a.column_name = p.column_name
LEFT JOIN approvers ap ON ap.table_name = p.table_name
LEFT JOIN uses u ON u.product = p.table_name
WHERE p.rn = 1 AND p.status = 'proposed'
