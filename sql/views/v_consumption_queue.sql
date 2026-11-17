-- Review queue, consumption (specs/consumption.md Front-end contract; specs/contracts.md Review-queue interface).
-- One row per request waiting for its owner. The permitted-use row is the one approve re-reads now: its values are
-- shown only when exactly one active row matches (more than one: approve is refused ambiguous_permitted_use). The
-- pattern columns are the CURRENT active pattern for the request's triple (approve does not re-check the pattern;
-- the pattern the request matched is in its pattern_check evidence).
-- can_decide: current_user() is an approver for the product and is not the request's requester or consumer (the
-- job refuses self-approval), compared trimmed and case-insensitive.
CREATE OR REPLACE VIEW ${catalog}.${gov_schema}.v_consumption_queue AS
WITH approvers AS (
  SELECT table_name, collect_set(principal) AS approvers
  FROM ${catalog}.${gov_schema}.table_approvers
  GROUP BY table_name
),
uses AS (
  SELECT product, use_code, count(*) AS n,
         max(approval) AS approval, max(conditions) AS conditions, max(max_term_days) AS max_term_days,
         max(version) AS version
  FROM ${catalog}.${gov_schema}.permitted_uses WHERE active
  GROUP BY product, use_code
),
patterns AS (
  SELECT use_mode, delivery, access_mode, count(*) AS n, max(pattern_id) AS pattern_id, max(allowed) AS allowed
  FROM ${catalog}.${gov_schema}.architecture_patterns WHERE active
  GROUP BY use_mode, delivery, access_mode
)
SELECT
  r.request_id, r.securable AS product, r.consumer, r.requester, r.intended_use, r.reason AS purpose_note,
  r.use_mode, r.delivery, r.access_mode, r.requested_at,
  coalesce(u.n, 0) AS use_rows,
  CASE WHEN u.n = 1 THEN u.approval END AS use_approval,
  CASE WHEN u.n = 1 THEN u.conditions END AS use_conditions,
  CASE WHEN u.n = 1 THEN coalesce(u.max_term_days, 365) END AS max_term_days,
  CASE WHEN u.n = 1 THEN u.version END AS use_version,
  coalesce(pt.n, 0) AS current_pattern_rows,
  CASE WHEN pt.n = 1 THEN pt.pattern_id END AS current_pattern_id,
  CASE WHEN pt.n = 1 THEN pt.allowed END AS current_pattern_allowed,
  coalesce(ap.approvers, CAST(array() AS ARRAY<STRING>)) AS approvers,
  exists(coalesce(ap.approvers, CAST(array() AS ARRAY<STRING>)),
         x -> lower(trim(x)) = lower(trim(current_user())))
    AND lower(trim(current_user())) <> lower(trim(coalesce(r.requester, '')))
    AND lower(trim(current_user())) <> lower(trim(coalesce(r.consumer, ''))) AS can_decide
FROM ${catalog}.${gov_schema}.access_requests r
LEFT JOIN uses u ON u.product = r.securable AND u.use_code = r.intended_use
LEFT JOIN patterns pt
  ON pt.use_mode = r.use_mode AND pt.delivery = r.delivery AND pt.access_mode = r.access_mode
LEFT JOIN approvers ap ON ap.table_name = r.securable
WHERE r.status = 'pending_owner'
