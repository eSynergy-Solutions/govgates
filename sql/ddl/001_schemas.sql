-- Schemas for the governance-gate sandbox. Catalog is created by the runner if missing.
CREATE SCHEMA IF NOT EXISTS ${catalog}.${raw_schema} COMMENT 'Raw tables as they land, before any gate';
CREATE SCHEMA IF NOT EXISTS ${catalog}.${gov_schema} COMMENT 'Governance tables: the source of truth for both gates';
CREATE SCHEMA IF NOT EXISTS ${catalog}.${ref_schema} COMMENT 'Reference data used by policies (user attributes)';
CREATE SCHEMA IF NOT EXISTS ${catalog}.${curated_schema} COMMENT 'Downstream consumer views';
