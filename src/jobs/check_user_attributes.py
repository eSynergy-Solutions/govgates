"""Databricks job: who can modify ref.user_attributes (specs/consumption.md; PLAN.md Q17).

A detective check: it reads owners and grants and never changes them. Allowed: the CI service principal (it is also
the loader) and one configured exception, the catalog owner. Grants come from SHOW GRANTS (complete, incl. inherited)
on the table, its schema and its catalog; owners and table type from information_schema. The run principal must be
able to see every grant (owner, READ METADATA or MANAGE), otherwise the check fails rather than trust a short list.

The result is audited as `user_attributes_check` only when it differs from the previous one, so a second identical
run writes nothing. The job exits non-zero when the check fails, which also stops the row-filter generator that
runs after it.
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
from gates.common.spark_io import AUDIT_MERGE, AUDIT_SCHEMA, _merge  # noqa: E402
from gates.consumption.attr_guard import Grant, Securable, audit_change, canonical, verdict  # noqa: E402
from gates.hydration.apply import quote_ident, quote_table  # noqa: E402

ACTION = "user_attributes_check"


def _args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    GateConfig.add_args(p)
    p.add_argument("--ci-sp", required=True, help="CI service principal applicationId (also the loader principal)")
    p.add_argument("--owner-exception", default="",
                   help="the catalog owner, the one person allowed besides the CI SP (must equal it)")
    return p.parse_args()


def _one(spark, sql: str, args: dict):
    rows = spark.sql(sql, args=args).collect()
    return rows[0] if rows else None


def _securable(spark, cfg: GateConfig, table: str) -> Securable:
    cat, sch, tbl = table.split(".")
    info = f"{quote_ident(cat)}.information_schema"
    t = _one(spark, f"SELECT table_owner, table_type FROM {info}.tables "
                    "WHERE table_schema = :s AND table_name = :t", {"s": sch, "t": tbl})
    s = _one(spark, f"SELECT schema_owner FROM {info}.schemata WHERE schema_name = :s", {"s": sch})
    c = _one(spark, "SELECT catalog_owner FROM system.information_schema.catalogs WHERE catalog_name = :c",
             {"c": cat})
    return Securable(table, t.table_type if t else None,
                     {"table": t.table_owner if t else None, "schema": s.schema_owner if s else None,
                      "catalog": c.catalog_owner if c else None})


def _grants(spark, table: str) -> list[Grant]:
    cat, sch, _ = table.split(".")
    out = []
    for kind, key in (("TABLE", table), ("SCHEMA", f"{cat}.{sch}"), ("CATALOG", cat)):
        for r in spark.sql(f"SHOW GRANTS ON {kind} {quote_table(key)}").collect():
            out.append(Grant(str(r[0]), str(r[1]), str(r[2]), str(r[3])))
    return out


def _previous(spark, cfg: GateConfig, table: str):
    return _one(spark, f"""
        SELECT event_id, after_json FROM {cfg.gov('audit_log')}
        WHERE gate = 'consumption' AND action = '{ACTION}' AND object = :o
        ORDER BY event_at DESC, event_id DESC LIMIT 1""", {"o": table})


def main() -> None:
    a = _args()
    cfg = GateConfig.from_args(a)
    from pyspark.sql import SparkSession

    spark = SparkSession.builder.getOrCreate()
    actor = spark.sql("SELECT session_user()").collect()[0][0]
    table = cfg.ref("user_attributes")
    sec = _securable(spark, cfg, table)
    grants = _grants(spark, table) if sec.table_type is not None else []
    allowed = {a.ci_sp.strip()} | ({a.owner_exception.strip()} if a.owner_exception.strip() else set())
    result = verdict(sec, grants, allowed=allowed, run_principal=actor, owner_exception=a.owner_exception)
    current = canonical(result)
    prev = _previous(spark, cfg, table)
    change = audit_change(current, json.loads(prev.after_json) if prev else None, prev.event_id if prev else None)
    if change is not None:
        before, after, event_id = change
        _merge(spark, [(event_id, "consumption", ACTION, actor, table,
                        json.dumps(before, sort_keys=True) if before else None, json.dumps(after, sort_keys=True))],
               AUDIT_SCHEMA, cfg.gov("audit_log"), AUDIT_MERGE)
    print("USER_ATTRIBUTES_CHECK " + json.dumps({**current, "audited": change is not None, "actor": actor},
                                                sort_keys=True))
    if not result.ok:
        raise SystemExit(f"ref.user_attributes check failed: {list(result.failures)}")


if __name__ == "__main__":
    main()
