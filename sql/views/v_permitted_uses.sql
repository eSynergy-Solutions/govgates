-- Active permitted uses per product, as the review UI shows them before an owner sets or changes one
-- (specs/contracts.md Review-queue interface; H15). Any active row counts as "has permitted uses" for the approve
-- job, including a `prohibited` one. The allowed use codes are configuration (bundle variable permitted_use_codes),
-- given to the UI with its deployment, not stored in gov.
CREATE OR REPLACE VIEW ${catalog}.${gov_schema}.v_permitted_uses AS
WITH approvers AS (
  SELECT table_name, collect_set(principal) AS approvers
  FROM ${catalog}.${gov_schema}.table_approvers
  GROUP BY table_name
)
SELECT
  u.product, u.use_code, u.approval, u.conditions, coalesce(u.max_term_days, 365) AS max_term_days,
  u.version, u.set_by, u.set_at,
  coalesce(ap.approvers, CAST(array() AS ARRAY<STRING>)) AS approvers,
  exists(coalesce(ap.approvers, CAST(array() AS ARRAY<STRING>)),
         x -> lower(trim(x)) = lower(trim(current_user()))) AS can_decide
FROM ${catalog}.${gov_schema}.permitted_uses u
LEFT JOIN approvers ap ON ap.table_name = u.product
WHERE u.active
