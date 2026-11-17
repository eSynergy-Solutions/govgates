"""Databricks job: apply step for one table (specs/hydration.md step 7).

Projects gov.approved_metadata to Unity Catalog: column comment = description, governed tag
<tag_prefix>_sensitivity, plain tag <tag_prefix>_business_term. The current UC state (information_schema) is
read only to find the difference; gov is the truth. Order, per column: gov.audit_log first (deterministic
event_id, MERGE, so a crashed run's replay adds no duplicate), then that column's UC statements, then its
applied_to_uc_at, stamped only where the row still equals what was projected (a row approve changed meanwhile
stays unstamped for the next run). A column whose statements fail gets an `apply_failed` audit row and no stamp;
the other columns still apply, and the job exits non-zero.
Rows that differ from their newest approve audit event are refused and audited. Our keys on columns with no
approved row are reported, not removed. Idempotent: a second run plans zero statements and writes nothing.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

try:
    _SRC = Path(__file__).resolve().parents[1]
except NameError:
    _SRC = Path(os.getcwd()).resolve().parent
sys.path.insert(0, str(_SRC))

from gates.common.config import GateConfig  # noqa: E402
from gates.common.spark_io import AUDIT_MERGE, AUDIT_SCHEMA, _iso, _merge  # noqa: E402,F401
from gates.hydration.apply import (  # noqa: E402
    APPLY_ACTIONS,
    Approved,
    TablePlan,
    UcColumn,
    applied_row,
    audit_rows,
    failed_row,
    plan_table,
)
from gates.hydration.records import qualify_table  # noqa: E402

_APPLY_IN = ", ".join(f"'{a}'" for a in APPLY_ACTIONS)  # constants, not input


def _args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    GateConfig.add_args(p)
    p.add_argument("--table", required=True, help="bare name (raw schema) or catalog.schema.table")
    return p.parse_args()


def _read(spark, cfg: GateConfig, table: str):
    cat, sch, tbl = table.split(".")
    rows = spark.sql(f"""
        SELECT table_name, column_name, term_id, description, sensitivity, approved_by, approved_at,
               applied_to_uc_at
        FROM {cfg.gov('approved_metadata')} WHERE lower(table_name) = lower(:t)""", args={"t": table}).collect()
    approved = [Approved(r.table_name, r.column_name, r.term_id, r.description, r.sensitivity, r.approved_by,
                         _iso(r.approved_at), _iso(r.applied_to_uc_at)) for r in rows]
    raw_ts = {r.column_name: r.approved_at for r in rows}

    # newest approve event per column that carries the approved row (apply's own events carry none). The event_id
    # tie-break is arbitrary, which is safe only because one approve run audits at most one approving action per
    # column (approve's plan() guarantees it; jobs/approve.py exits on two approved rows for one column).
    decided = {}
    for r in spark.sql(f"""
        SELECT after_json FROM (
          SELECT after_json, row_number() OVER (PARTITION BY get_json_object(after_json, '$.column_name')
                                                ORDER BY event_at DESC, event_id DESC) AS rn
          FROM {cfg.gov('audit_log')}
          WHERE gate = 'hydration' AND lower(object) = lower(:t)
            AND get_json_object(after_json, '$.approved') IS NOT NULL AND action NOT IN ({_APPLY_IN}))
        WHERE rn = 1""", args={"t": table}).collect():
        after = json.loads(r.after_json)
        decided[after["column_name"]] = after["approved"]

    # current UC state: used only to compute the difference, never as truth
    info = f"`{cat.replace('`', '``')}`.information_schema"
    uc_comments = {r.column_name: r.comment for r in spark.sql(f"""
        SELECT column_name, comment FROM {info}.columns
        WHERE table_catalog = lower(:c) AND table_schema = lower(:s) AND table_name = lower(:n)""",
        args={"c": cat, "s": sch, "n": tbl}).collect()}
    uc_tags: dict[str, dict[str, str]] = {c: {} for c in uc_comments}
    for r in spark.sql(f"""
        SELECT column_name, tag_name, tag_value FROM {info}.column_tags
        WHERE catalog_name = lower(:c) AND schema_name = lower(:s) AND table_name = lower(:n)""",
                       args={"c": cat, "s": sch, "n": tbl}).collect():
        uc_tags.setdefault(r.column_name, {})[r.tag_name] = r.tag_value
    if not uc_comments:  # a wrong or missing table would otherwise pass as "every column missing, 0 statements"
        raise SystemExit(f"{table}: no columns in information_schema (missing table, or no access)")
    uc = {c: UcColumn(uc_comments.get(c), uc_tags.get(c, {})) for c in uc_comments}
    return approved, raw_ts, decided, uc


STAMP_SCHEMA = ("table_name string, column_name string, term_id string, description string, sensitivity string, "
                "approved_by string, approved_at timestamp")
STAMP_MERGE = """
    MERGE INTO {t} t USING {s} s ON t.table_name = s.table_name AND t.column_name = s.column_name
      AND t.term_id <=> s.term_id AND t.description <=> s.description AND t.sensitivity <=> s.sensitivity
      AND t.approved_by <=> s.approved_by AND t.approved_at <=> s.approved_at
    WHEN MATCHED THEN UPDATE SET applied_to_uc_at = current_timestamp()"""


def _stamp_row(r: Approved, raw_ts: dict) -> tuple:
    return (r.table_name, r.column_name, r.term_id, r.description, r.sensitivity, r.approved_by,
            raw_ts[r.column_name])


def _write(spark, cfg: GateConfig, table: str, plan: TablePlan, raw_ts: dict, actor: str) -> list[tuple]:
    """Per column: audit, then statements, then stamp. Returns (column, error) for columns that failed."""
    audit_t, approved_t = cfg.gov("audit_log"), cfg.gov("approved_metadata")
    rows = {r[0]: r for r in audit_rows(plan, table, actor)}
    _merge(spark, [rows[f.event_id] for f in plan.refused], AUDIT_SCHEMA, audit_t, AUDIT_MERGE)
    by_col = {r.column_name: r for r in plan.stamp}
    failed = []
    for change in plan.changes:
        _merge(spark, [rows[change.event_id]], AUDIT_SCHEMA, audit_t, AUDIT_MERGE)  # apply_started
        try:
            for stmt in change.statements:
                spark.sql(stmt)
        except Exception as e:  # one bad column must not block the rest; it stays unstamped
            row = failed_row(change, table, actor, type(e).__name__, str(e))
            failed.append((change.column, json.loads(row[6])["error_class"]))
            _merge(spark, [row], AUDIT_SCHEMA, audit_t, AUDIT_MERGE)
            continue
        _merge(spark, [applied_row(change, table, actor)], AUDIT_SCHEMA, audit_t, AUDIT_MERGE)
        _merge(spark, [_stamp_row(by_col[change.column], raw_ts)], STAMP_SCHEMA, approved_t, STAMP_MERGE)
    changed = {c.column for c in plan.changes}
    _merge(spark, [_stamp_row(r, raw_ts) for r in plan.stamp if r.column_name not in changed],
           STAMP_SCHEMA, approved_t, STAMP_MERGE)
    return failed


def main() -> None:
    a = _args()
    cfg = GateConfig.from_args(a)
    from pyspark.sql import SparkSession

    spark = SparkSession.builder.getOrCreate()
    table = qualify_table(a.table.strip(), cfg)
    actor = spark.sql("SELECT current_user()").collect()[0][0]
    approved, raw_ts, decided, uc = _read(spark, cfg, table)
    plan = plan_table(table, approved, decided, uc, prefix=cfg.tag_prefix)
    failed = _write(spark, cfg, table, plan, raw_ts, actor)
    print("APPLY_RESULT " + json.dumps({
        "table_name": table, "actor": actor, "tag_prefix": cfg.tag_prefix, "approved": len(approved),
        "changed": len(plan.changes), "statements": plan.statement_count, "stamped": len(plan.stamp),
        "refused": [(f.column, f.reason) for f in plan.refused], "missing": plan.missing,
        "untracked": plan.untracked, "failed": failed}, sort_keys=True))
    if failed:
        raise SystemExit(f"{len(failed)} column(s) failed to apply: {failed}")


if __name__ == "__main__":
    main()
