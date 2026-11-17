CREATE TABLE IF NOT EXISTS ${catalog}.${ref_schema}.user_attributes (
  principal STRING NOT NULL,
  region STRING,
  line_of_business STRING
) COMMENT 'Attributes used by row filters. region ALL = no filter, NONE = no rows';
