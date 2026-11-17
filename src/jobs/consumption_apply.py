"""Databricks job: row-filter generator (specs/consumption.md, `row_filter` bullet).

Reads `row_filter` rows from gov.access_policies and gov.approved_metadata (the truth), reads the current UC
column tags, ABAC policies and function bodies only to find the difference, and applies the planner's actions
in order. Per action: `<kind>_started` audit row (deterministic event_id, MERGE), the statement, then `<kind>` or
`<kind>_failed`. A failing action does not stop later independent ones, except that nothing after a failed
function or policy runs (tags must never point at a missing filter); the job then exits non-zero.
Idempotent: a converged second run plans no statements and writes no audit rows.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from pathlib import Path

try:
    _SRC = Path(__file__).resolve().parents[1]
except NameError:
    _SRC = Path(os.getcwd()).resolve().parent
sys.path.insert(0, str(_SRC))

from gates.common.config import GateConfig  # noqa: E402
from gates.common.spark_io import AUDIT_MERGE, AUDIT_SCHEMA, _iso, _merge  # noqa: E402
from gates.consumption.row_filter import HELPER_SUFFIX, Action, PolicyRow, plan, policy_prefix  # noqa: E402
from gates.hydration.apply import APPLY_ACTIONS, Approved, error_class, event_id_of, quote_ident  # noqa: E402

GATE = "consumption"
_APPLY_IN = ", ".join(f"'{a}'" for a in APPLY_ACTIONS)  # constants, not input
_MATCH = re.compile(r"has_tag_value\(\s*'([^']*)'\s*,\s*'([^']*)'\s*\)\s*AS\s+`?(\w+)`?", re.IGNORECASE)


def _args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    GateConfig.add_args(p)
    # job-local temp dir by default: the run principal may not write to the bundle's workspace folder, and
    # workspace writes fail asynchronously at exit (uncatchable). The audit log holds every statement anyway.
    p.add_argument("--sql-out", default=os.path.join(tempfile.gettempdir(), "consumption", "row_filter.sql"))
    return p.parse_args()


def _policy_rows(spark, cfg: GateConfig) -> list[PolicyRow]:
    return [PolicyRow(r.policy_id, r.policy_type, r.securable or "", r.match_tag_key or "", r.match_tag_value or "",
                      r.function_name or "", tuple(r.except_principals or ()), bool(r.active))
            for r in spark.sql(f"""
                SELECT policy_id, policy_type, securable, match_tag_key, match_tag_value, function_name,
                       except_principals, active
                FROM {cfg.gov('access_policies')} WHERE policy_type = 'row_filter'""").collect()]


def _approved(spark, cfg: GateConfig) -> list[Approved]:
    return [Approved(r.table_name, r.column_name, r.term_id, r.description, r.sensitivity, r.approved_by,
                     _iso(r.approved_at), _iso(r.applied_to_uc_at))
            for r in spark.sql(f"""
                SELECT table_name, column_name, term_id, description, sensitivity, approved_by, approved_at,
                       applied_to_uc_at
                FROM {cfg.gov('approved_metadata')}""").collect()]


def _decided(spark, cfg: GateConfig) -> dict:
    """Newest approve audit event per (table, column), as hydration apply reads it (jobs/apply.py)."""
    out = {}
    for r in spark.sql(f"""
        SELECT object, after_json FROM (
          SELECT object, after_json,
                 row_number() OVER (PARTITION BY lower(object), get_json_object(after_json, '$.column_name')
                                    ORDER BY event_at DESC, event_id DESC) AS rn
          FROM {cfg.gov('audit_log')}
          WHERE gate = 'hydration' AND get_json_object(after_json, '$.approved') IS NOT NULL
            AND action NOT IN ({_APPLY_IN}))
        WHERE rn = 1""").collect():
        after = json.loads(r.after_json)
        out[(r.object.lower(), after["column_name"].lower())] = after["approved"]
    return out


def _info(cfg: GateConfig) -> str:
    return f"{quote_ident(cfg.catalog)}.information_schema"


def _current_tags(spark, cfg: GateConfig) -> set[tuple[str, str]]:
    return {(f"{r.catalog_name}.{r.schema_name}.{r.table_name}", r.column_name) for r in spark.sql(f"""
        SELECT catalog_name, schema_name, table_name, column_name FROM {_info(cfg)}.column_tags
        WHERE tag_name = :k AND tag_value = 'region'""", args={"k": cfg.rls_tag}).collect()}


def _describe(spark, name: str, catalog: str, schema: str) -> dict[str, str]:
    """DESCRIBE POLICY as {info_name lowercased: value}; nested rows (Function Name, Using Columns) are indented."""
    out = {}
    for r in spark.sql(f"DESCRIBE POLICY {quote_ident(name)} ON SCHEMA "
                       f"{quote_ident(catalog)}.{quote_ident(schema)}").collect():
        if len(r) > 1 and r[0]:
            out[str(r[0]).strip().lower()] = "" if r[1] is None else str(r[1]).strip()
    return out


def _current_policies(spark, cfg: GateConfig) -> dict[tuple[str, str], dict]:
    """Our row-filter policies (by name prefix) keyed by (schema_lower, name), every part the planner compares."""
    out = {}
    for r in spark.sql(f"""
        SELECT policy_name, catalog_name, schema_name, to_principals, except_principals, for_securable_type,
               match_columns
        FROM {_info(cfg)}.abac_policy_definitions
        WHERE policy_type = 'ROW_FILTER' AND startswith(lower(policy_name), :p)""",
                       args={"p": policy_prefix(cfg.tag_prefix)}).collect():
        m = _MATCH.search(" ".join(r.match_columns or []))
        d = _describe(spark, r.policy_name, r.catalog_name, r.schema_name)
        using = tuple(u.strip() for u in d.get("using columns", "").split(",") if u.strip())
        securable = f"{r.catalog_name}.{r.schema_name}"
        out[(securable.lower(), r.policy_name)] = {
            "securable": securable, "to_principals": tuple(r.to_principals or ()),
            "except_principals": tuple(r.except_principals or ()), "for_securable_type": r.for_securable_type,
            "match_tag_key": m.group(1) if m else None, "match_tag_value": m.group(2) if m else None,
            "match_alias": m.group(3) if m else None, "using_columns": using,
            "function_name": d.get("function name") or None}
    return out


def _last_events(spark, cfg: GateConfig) -> dict[str, str]:
    """Newest completed consumption event per object (outcome or failure, never *_started)."""
    return {r.o: r.e for r in spark.sql(f"""
        SELECT lower(object) AS o, max_by(event_id, event_at) AS e FROM {cfg.gov('audit_log')}
        WHERE gate = 'consumption' AND NOT endswith(action, '_started')
          AND action NOT IN ('policy_row_set', 'tag_refused', 'user_attributes_check', 'policy_refused')
        GROUP BY lower(object)""").collect()}


def _current_functions(spark, cfg: GateConfig, rows: list[PolicyRow]) -> dict[str, str]:
    wanted = set()
    for r in rows:
        if r.active:
            wanted |= {r.function_name.lower(), (r.function_name + HELPER_SUFFIX).lower()}
    out = {}
    for r in spark.sql(f"""
        SELECT routine_catalog, routine_schema, routine_name, routine_definition
        FROM {_info(cfg)}.routines""").collect():
        name = f"{r.routine_catalog}.{r.routine_schema}.{r.routine_name}".lower()
        if name in wanted and r.routine_definition is not None:
            out[name] = r.routine_definition
    return out


def _audit(a: Action, action: str, event_id: str, actor: str, after: dict | None = None) -> tuple:
    return (event_id, GATE, action, actor, a.object, json.dumps(a.before, default=str, sort_keys=True),
            json.dumps(after if after is not None else a.after, default=str, sort_keys=True))


def _write_sql(path: str, actions: list[Action]) -> None:
    try:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("".join(f"-- {a.kind} {a.object}\n{a.statement};\n\n" for a in actions if a.statement),
                     encoding="utf-8")
    except OSError as e:  # the record is a convenience; the audit log is the evidence
        print(f"could not write {path}: {e}")


def _apply(spark, cfg: GateConfig, actions: list[Action], actor: str) -> list[tuple[str, str, str]]:
    audit_t = cfg.gov("audit_log")
    failed: list[tuple[str, str, str]] = []
    for a in actions:
        if a.statement is None:  # tag_refused: record only
            _merge(spark, [_audit(a, a.kind, a.event_id, actor)], AUDIT_SCHEMA, audit_t, AUDIT_MERGE)
            continue
        if failed and any(k in ("function_set", "policy_set") for _, k, _ in failed):
            continue  # never tag towards a filter that is not in place
        if a.kind == "policy_drop" and any(k == "tag_unset" for _, k, _ in failed):
            continue  # keep the policy while any of its tags remain; the next run retries the unset first
        started = {**(a.after or {}), "statement": a.statement}  # the audit log records every statement run
        _merge(spark, [_audit(a, f"{a.kind}_started", a.event_id, actor, started)], AUDIT_SCHEMA, audit_t,
               AUDIT_MERGE)
        try:
            spark.sql(a.statement)
        except Exception as e:  # recorded per object; later independent objects still run
            cls = error_class(str(e), type(e).__name__)
            failed.append((a.object, a.kind, cls))
            _merge(spark, [_audit(a, f"{a.kind}_failed", event_id_of(a.event_id, "failed", cls), actor,
                                  {"error_class": cls, "message": str(e)[:500]})],
                   AUDIT_SCHEMA, audit_t, AUDIT_MERGE)
            continue
        _merge(spark, [_audit(a, a.kind, event_id_of(a.event_id, "done"), actor)], AUDIT_SCHEMA, audit_t,
               AUDIT_MERGE)
    return failed


def main() -> None:
    a = _args()
    cfg = GateConfig.from_args(a)
    from pyspark.sql import SparkSession

    spark = SparkSession.builder.getOrCreate()
    actor = spark.sql("SELECT session_user()").collect()[0][0]
    rows = _policy_rows(spark, cfg)
    actions = plan(rows, _approved(spark, cfg), _decided(spark, cfg), _current_tags(spark, cfg),
                   _current_policies(spark, cfg), _current_functions(spark, cfg, rows), catalog=cfg.catalog,
                   rls_tag=cfg.rls_tag, rls_term_id=cfg.rls_term_id, hydration_principal=cfg.hydration_principal,
                   gov_schema=cfg.gov_schema, tag_prefix=cfg.tag_prefix, last_events=_last_events(spark, cfg),
                   user_attributes_table=cfg.ref("user_attributes"))
    _write_sql(a.sql_out, actions)
    failed = _apply(spark, cfg, actions, actor)
    counts: dict[str, int] = {}
    for x in actions:
        counts[x.kind] = counts.get(x.kind, 0) + 1
    print("CONSUMPTION_RESULT " + json.dumps({
        "actor": actor, "rls_tag": cfg.rls_tag, "rows": len(rows), "actions": counts,
        "statements": sum(1 for x in actions if x.statement), "objects": [(x.kind, x.object) for x in actions],
        "failed": failed}, sort_keys=True))
    if failed:
        raise SystemExit(f"{len(failed)} action(s) failed: {failed}")


if __name__ == "__main__":
    main()
