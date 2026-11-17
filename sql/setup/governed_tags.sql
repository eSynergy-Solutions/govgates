-- Governed tags for the gates. ACCOUNT-wide, not per catalog.
-- One-time human setup step, NOT run by any job: it needs account-level CREATE on governed tags, and the
-- tags already exist on the dev account (created 2026-11-07), so a re-run fails. Kept as the record of the
-- allowed values: tests/unit/test_contract.py checks them against gates.common.contract.SENSITIVITIES.
-- After creating, grant ASSIGN on ${tag_prefix}_sensitivity to the CI service principal (UI only:
-- Catalog > Govern > Governed Tags > Permissions). ${tag_prefix}_business_term is a plain tag, not governed.
-- Check the live values with: DESCRIBE GOVERNED TAG ${tag_prefix}_sensitivity AS JSON;

CREATE GOVERNED TAG ${tag_prefix}_sensitivity
  DESCRIPTION 'Sensitivity class projected from gov.approved_metadata by the hydration apply step'
  VALUES ('public', 'internal', 'confidential', 'pii');

CREATE GOVERNED TAG ${tag_prefix}_rls_attribute
  DESCRIPTION 'Marks the column a row-filter policy matches on'
  VALUES ('region');
