#!/usr/bin/env bash
# Run SQL on the dev SQL warehouse through the Statement Execution API. No bundle deploy.
#
#   ./scripts/sql.sh "SELECT count(*) FROM \${catalog}.\${gov_schema}.term_proposals"
#   ./scripts/sql.sh -p persona-analyst "SELECT current_user()"   # run as another CLI profile
#   ./scripts/sql.sh -f spikes/abac/spike.sql      # one statement per ';'-terminated line block
#   echo "SHOW POLICIES ON SCHEMA \${catalog}.\${gov_schema}" | ./scripts/sql.sh -
#
# Always use this script. Never call `databricks api .../sql/statements` by hand: on Git Bash the
# /api/... path gets rewritten unless MSYS_NO_PATHCONV=1 is set, and the POST path needs the
# trailing slash (/api/2.0/sql/statements/). This script gets both right.
#
# Env (all optional):
#   DATABRICKS_CONFIG_PROFILE  CLI profile (default: the CLI default, currently "main")
#   SQL_WAREHOUSE_ID           warehouse id; else looked up by SQL_WAREHOUSE_NAME (default "testing")
#   SQL_MAX_ROWS               rows printed per statement (default 50)
#   SQL_TIMEOUT                seconds to wait per statement (default 300)
#   catalog / raw_schema / gov_schema / ref_schema / curated_schema   override bundle defaults
set -euo pipefail
export MSYS_NO_PATHCONV=1
cd "$(git rev-parse --show-toplevel)"
PY=".venv/Scripts/python.exe"; [ -x "$PY" ] || PY=".venv/bin/python"; [ -x "$PY" ] || PY="$(command -v python3 || command -v python)"
exec "$PY" scripts/sql_run.py "$@"
