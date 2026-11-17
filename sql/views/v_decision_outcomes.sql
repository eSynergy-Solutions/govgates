-- What happened to every decision the review UI wrote (specs/contracts.md Review-queue interface). applied_at and
-- outcome stay NULL until the next approve / consumption_decide run; then the outcome or `refused: <reason>`.
CREATE OR REPLACE VIEW ${catalog}.${gov_schema}.v_decision_outcomes AS
SELECT 'hydration' AS gate, decision_id, proposal_id AS item_id, action, decided_by, decided_at, applied_at, outcome
FROM ${catalog}.${gov_schema}.review_decisions
UNION ALL
SELECT 'consumption' AS gate, decision_id, request_id AS item_id, action, decided_by, decided_at, applied_at, outcome
FROM ${catalog}.${gov_schema}.request_decisions
UNION ALL
SELECT 'hydration' AS gate, input_id AS decision_id, concat(product, ':', use_code) AS item_id,
       concat('permitted_use:', approval) AS action, decided_by, decided_at, applied_at, outcome
FROM ${catalog}.${gov_schema}.permitted_use_inputs
