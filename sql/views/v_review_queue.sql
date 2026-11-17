-- One review queue for both gates (H2, C8): common columns, gate-specific detail as JSON. Reads the two gate views,
-- so this file sorts after them. Decisions go back as INSERTs into review_decisions / request_decisions.
-- can_decide is the gate view's: an approver for the item who is not refused on identity (self-approval for
-- consumption). For hydration, context.has_permitted_uses false means accept / edit will be refused.
CREATE OR REPLACE VIEW ${catalog}.${gov_schema}.v_review_queue AS
SELECT
  'hydration' AS gate,
  proposal_id AS item_id,
  concat(table_name, '.', column_name) AS object,
  to_json(named_struct('term_id', term_id, 'term_name', term_name, 'term_definition', term_definition,
                       'description', description, 'sensitivity', sensitivity, 'confidence', confidence,
                       'method', method, 'prompt_version', prompt_version, 'rationale', rationale)) AS proposed,
  to_json(named_struct('data_type', data_type, 'null_pct', null_pct, 'distinct_pct', distinct_pct,
                       'avg_length', avg_length, 'pattern_hits', pattern_hits, 'any_pattern_hits', any_pattern_hits,
                       'approved_term_id', approved_term_id, 'approved_sensitivity', approved_sensitivity,
                       'approved_description', approved_description,
                       'has_permitted_uses', has_permitted_uses)) AS context,
  created_at AS waiting_since,
  approvers,
  can_decide
FROM ${catalog}.${gov_schema}.v_hydration_queue
UNION ALL
SELECT
  'consumption' AS gate,
  request_id AS item_id,
  product AS object,
  to_json(named_struct('consumer', consumer, 'intended_use', intended_use, 'use_mode', use_mode,
                       'delivery', delivery, 'access_mode', access_mode)) AS proposed,
  to_json(named_struct('requester', requester, 'purpose_note', purpose_note, 'use_rows', use_rows,
                       'use_approval', use_approval, 'use_conditions', use_conditions,
                       'max_term_days', max_term_days, 'use_version', use_version,
                       'current_pattern_id', current_pattern_id,
                       'current_pattern_allowed', current_pattern_allowed)) AS context,
  requested_at AS waiting_since,
  approvers,
  can_decide
FROM ${catalog}.${gov_schema}.v_consumption_queue
