-- Approved metadata as the review UI shows it, with what a `revise` must target (specs/contracts.md Review-queue
-- interface). revise_proposal_id is the column's newest accepted / edited non-eval proposal (a newer rejected one does
-- not supersede it), the only proposal a revise is applied to. pending_proposal_id is set when a newer proposal is
-- still `proposed`: the job refuses a revise until that one is decided.
CREATE OR REPLACE VIEW ${catalog}.${gov_schema}.v_approved_columns AS
WITH decided AS (
  SELECT table_name, column_name, proposal_id
  FROM ${catalog}.${gov_schema}.term_proposals
  WHERE status IN ('accepted', 'edited')
  QUALIFY row_number() OVER (PARTITION BY table_name, column_name
                             ORDER BY created_at DESC NULLS LAST, proposal_id DESC) = 1
),
newest AS (
  SELECT table_name, column_name, proposal_id, status
  FROM ${catalog}.${gov_schema}.term_proposals
  WHERE status <> 'eval'
  QUALIFY row_number() OVER (PARTITION BY table_name, column_name
                             ORDER BY created_at DESC NULLS LAST, proposal_id DESC) = 1
),
approvers AS (
  SELECT table_name, collect_set(principal) AS approvers
  FROM ${catalog}.${gov_schema}.table_approvers
  GROUP BY table_name
)
SELECT
  a.table_name, a.column_name, a.term_id, a.description, a.sensitivity, a.approved_by, a.approved_at,
  a.applied_to_uc_at,
  d.proposal_id AS revise_proposal_id,
  CASE WHEN n.status = 'proposed' THEN n.proposal_id END AS pending_proposal_id,
  coalesce(ap.approvers, CAST(array() AS ARRAY<STRING>)) AS approvers,
  exists(coalesce(ap.approvers, CAST(array() AS ARRAY<STRING>)),
         x -> lower(trim(x)) = lower(trim(current_user()))) AS can_decide
FROM ${catalog}.${gov_schema}.approved_metadata a
LEFT JOIN decided d ON d.table_name = a.table_name AND d.column_name = a.column_name
LEFT JOIN newest n ON n.table_name = a.table_name AND n.column_name = a.column_name
LEFT JOIN approvers ap ON ap.table_name = a.table_name
