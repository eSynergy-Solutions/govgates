"""Workspace edge of the persona harness: run one statement as one persona's CLI profile.

Only `persona-*` profiles are ever used for queries; there is no fallback to the default, admin or CI profile.
Statements go through the Statement Execution API via the Databricks CLI (as scripts/sql_run.py does), so auth is
exactly the persona's OAuth M2M profile. Bundle facts (catalog, CI principal id) come from `bundle validate`, which
reads config and runs no query.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time

POST_PATH = "/api/2.0/sql/statements/"  # trailing slash required
GET_PATH = "/api/2.0/sql/statements/{id}"
PERSONA_PREFIX = "persona-"


class QueryError(Exception):
    pass


def _cli() -> str:
    exe = shutil.which("databricks")
    if not exe:
        raise RuntimeError("databricks CLI not on PATH")
    return exe


def _api(profile: str | None, method: str, path: str, body: dict | None = None) -> dict:
    args = [_cli(), "api", method, path]
    if profile is not None:
        args += ["--profile", profile]
    tmp = None
    if body is not None:
        tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
        json.dump(body, tmp)
        tmp.close()
        args += ["--json", f"@{tmp.name}"]
    env = dict(os.environ, MSYS_NO_PATHCONV="1")
    try:
        out = subprocess.run(args, capture_output=True, text=True, env=env)
    finally:
        if tmp:
            os.unlink(tmp.name)
    if out.returncode != 0:
        raise QueryError(out.stderr.strip() or out.stdout.strip())
    return json.loads(out.stdout or "{}")


def bundle_facts() -> dict[str, str]:
    """catalog and ci_sp from the dev bundle. Uses the bundle profile (GG_BUNDLE_PROFILE, default `main`) only to
    resolve config and the CI principal's id; no data query ever runs as it."""
    profile = os.environ.get("GG_BUNDLE_PROFILE", "main")
    if profile.startswith(PERSONA_PREFIX):
        raise ValueError("GG_BUNDLE_PROFILE must not be a persona profile")
    out = subprocess.run([_cli(), "bundle", "validate", "-t", "dev", "-o", "json", "--profile", profile],
                         capture_output=True, text=True, env=dict(os.environ, MSYS_NO_PATHCONV="1"))
    if out.returncode != 0:
        raise RuntimeError(f"bundle validate failed: {out.stderr.strip()}")
    variables = json.loads(out.stdout).get("variables", {})
    return {k: str(variables[k].get("value") or variables[k].get("default") or "") for k in ("catalog", "ci_sp")}


def warehouse_id(profile: str) -> str:
    """SQL_WAREHOUSE_ID, else the warehouse named SQL_WAREHOUSE_NAME (default testing) as the persona sees it."""
    if os.environ.get("SQL_WAREHOUSE_ID"):
        return os.environ["SQL_WAREHOUSE_ID"]
    name = os.environ.get("SQL_WAREHOUSE_NAME", "testing")
    for wh in _api(profile, "get", "/api/2.0/sql/warehouses").get("warehouses", []):
        if wh.get("name") == name:
            return wh["id"]
    raise RuntimeError(f"persona profile {profile!r} sees no warehouse named {name!r} (CAN_USE missing?)")


def run_as(profile: str, sql: str, *, warehouse: str, catalog: str,
           timeout: int = 600) -> tuple[list[dict] | None, str | None]:
    """(rows as dicts, None) on success; (None, error text) on a failed statement. Never runs as a non-persona."""
    if not profile.startswith(PERSONA_PREFIX):
        raise ValueError(f"refusing to query as {profile!r}: only {PERSONA_PREFIX}* profiles are allowed")
    resp = _api(profile, "post", POST_PATH, {
        "warehouse_id": warehouse, "statement": sql, "wait_timeout": "50s", "catalog": catalog,
        "on_wait_timeout": "CONTINUE", "disposition": "INLINE", "format": "JSON_ARRAY"})
    sid = resp.get("statement_id")
    deadline = time.time() + timeout
    while resp.get("status", {}).get("state") in ("PENDING", "RUNNING"):
        if time.time() > deadline:
            _api(profile, "post", GET_PATH.format(id=sid) + "/cancel", {})
            return None, f"TIMEOUT after {timeout}s"
        time.sleep(2)
        resp = _api(profile, "get", GET_PATH.format(id=sid))
    status = resp.get("status", {})
    if status.get("state") != "SUCCEEDED":
        err = status.get("error", {})
        return None, f"{err.get('error_code', '')}: {err.get('message', status.get('state'))}"
    cols = [c["name"] for c in resp.get("manifest", {}).get("schema", {}).get("columns", [])]
    data = (resp.get("result") or {}).get("data_array") or []
    return [dict(zip(cols, r)) for r in data], None
