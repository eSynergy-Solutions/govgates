"""Databricks job task: column-mask generator (specs/consumption.md Policy model `mask` rows, Join-up).

Runs after the row filter in `consumption_apply`, as the CI service principal. Reads active `mask` rows from
gov.access_policies and approved metadata (the truth), and the current UC mask policies, function bodies and column
types only to find the difference. Applies nothing and exits non-zero when: a policy row differs from its newest
audited write (hand SQL: `policy_refused`, record only), a pii column sits in a schema with no active pii mask, or
a masked column is the row-filter input. Otherwise the same audited apply as the row filter: `<kind>_started` with
the statement, then `<kind>` or `<kind>_failed`. A converged second run plans nothing and writes nothing.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

try:
    _SRC = Path(__file__).resolve().parents[1]
except NameError:
    _SRC = Path(os.getcwd()).resolve().parent
sys.path.insert(0, str(_SRC))

from gates.common.config import GateConfig  # noqa: E402
from gates.common.spark_io import AUDIT_MERGE, AUDIT_SCHEMA, _merge  # noqa: E402
from gates.consumption.masks import mask_prefix, plan_masks, safe_on_failure, tampered  # noqa: E402
from gates.consumption.row_filter import PolicyRow  # noqa: E402
from gates.hydration.apply import event_id_of  # noqa: E402
from jobs.consumption_apply import (  # noqa: E402
    _MATCH,
    GATE,
    _apply,
    _approved,
    _describe,
    _info,
    _last_events,
    _policy_rows,
    _write_sql,
)


def _args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    GateConfig.add_args(p)
    p.add_argument("--sql-out", default=os.path.join(tempfile.gettempdir(), "consumption", "masks.sql"))
    return p.parse_args()


def _rows(spark, cfg: GateConfig, policy_type: str) -> list[PolicyRow]:
    return [PolicyRow(r.policy_id, r.policy_type, r.securable or "", r.match_tag_key or "", r.match_tag_value or "",
                      r.function_name or "", tuple(r.except_principals or ()), bool(r.active))
            for r in spark.sql(f"""SELECT * FROM {cfg.gov('access_policies')} WHERE policy_type = :t""",
                               args={"t": policy_type}).collect()]


def _newest_after(spark, cfg: GateConfig) -> dict[str, dict | None]:
    return {r.o: json.loads(r.a) if r.a else None for r in spark.sql(f"""
        SELECT o, a FROM (SELECT object AS o, after_json AS a,
                 row_number() OVER (PARTITION BY object ORDER BY event_at DESC, event_id DESC) AS rn
               FROM {cfg.gov('audit_log')} WHERE gate = 'consumption' AND action = 'policy_row_set')
        WHERE rn = 1""").collect()}


def _current_mask_policies(spark, cfg: GateConfig) -> dict[tuple[str, str], dict]:
    out = {}
    for r in spark.sql(f"""
        SELECT policy_name, catalog_name, schema_name, to_principals, except_principals, for_securable_type,
               match_columns
        FROM {_info(cfg)}.abac_policy_definitions
        WHERE policy_type = 'COLUMN_MASK' AND startswith(lower(policy_name), :p)""",
                       args={"p": mask_prefix(cfg.tag_prefix)}).collect():
        m = _MATCH.search(" ".join(r.match_columns or []))
        d = _describe(spark, r.policy_name, r.catalog_name, r.schema_name)
        securable = f"{r.catalog_name}.{r.schema_name}"
        out[(securable.lower(), r.policy_name)] = {
            "securable": securable, "to_principals": tuple(r.to_principals or ()),
            "except_principals": tuple(r.except_principals or ()), "for_securable_type": r.for_securable_type,
            "match_tag_key": m.group(1) if m else None, "match_tag_value": m.group(2) if m else None,
            "match_alias": m.group(3) if m else None, "on_column": d.get("on column") or None,
            "function_name": d.get("function name") or None}
    return out


def _current_functions(spark, cfg: GateConfig, rows: list[PolicyRow]) -> dict[str, str]:
    wanted = {r.function_name.lower() for r in rows if r.active}
    return {f"{r.routine_catalog}.{r.routine_schema}.{r.routine_name}".lower(): r.routine_definition
            for r in spark.sql(f"""SELECT routine_catalog, routine_schema, routine_name, routine_definition
                                   FROM {_info(cfg)}.routines""").collect()
            if f"{r.routine_catalog}.{r.routine_schema}.{r.routine_name}".lower() in wanted
            and r.routine_definition is not None}


def _last_refused(spark, cfg: GateConfig) -> dict[str, str]:
    return {r.o: r.e for r in spark.sql(f"""
        SELECT object AS o, max_by(event_id, event_at) AS e FROM {cfg.gov('audit_log')}
        WHERE gate = 'consumption' AND action = 'policy_refused' GROUP BY object""").collect()}


def _column_types(spark, cfg: GateConfig) -> dict[tuple[str, str], str]:
    return {(f"{r.table_catalog}.{r.table_schema}.{r.table_name}".lower(), r.column_name.lower()): r.data_type
            for r in spark.sql(f"""SELECT table_catalog, table_schema, table_name, column_name, data_type
                                   FROM {_info(cfg)}.columns""").collect()}


def main() -> None:
    a = _args()
    cfg = GateConfig.from_args(a)
    from pyspark.sql import SparkSession

    spark = SparkSession.builder.getOrCreate()
    actor = spark.sql("SELECT session_user()").collect()[0][0]
    masks, rls = _rows(spark, cfg, "mask"), _policy_rows(spark, cfg)

    newest = _newest_after(spark, cfg)
    bad = tampered(masks + rls, newest)
    if bad:  # evidence: the stored row vs its newest audited write; chained so a repeat of the same state dedupes
        refused = _last_refused(spark, cfg)
        audit = []
        for pid in bad:
            stored = [r.__dict__ for r in masks + rls if r.policy_id == pid] or None
            before = json.dumps(newest.get(pid), sort_keys=True, default=str)
            after = json.dumps({"stored": stored, "reason": "row differs from its newest audited write (hand SQL, "
                                "or an interrupted access_model_sync run)"}, sort_keys=True, default=str)
            audit.append((event_id_of("policy_refused", pid, before, after, refused.get(pid)), GATE, "policy_refused",
                          actor, pid, before, after))
        _merge(spark, audit, AUDIT_SCHEMA, cfg.gov("audit_log"), AUDIT_MERGE)
        raise SystemExit(f"policy rows refused (not as last audited): {bad}")

    p = plan_masks(masks, _approved(spark, cfg), _current_mask_policies(spark, cfg),
                   _current_functions(spark, cfg, masks), catalog=cfg.catalog, sensitivity_tag=cfg.sensitivity_tag,
                   rls_term_id=cfg.rls_term_id, rls_rows=rls, hydration_principal=cfg.hydration_principal,
                   gov_schema=cfg.gov_schema, tag_prefix=cfg.tag_prefix, column_types=_column_types(spark, cfg),
                   last_events=_last_events(spark, cfg))
    result = {"actor": actor, "rows": len(masks), "warnings": list(p.warnings), "failures": list(p.failures)}
    actions = safe_on_failure(p.actions) if p.failures else p.actions  # still add/restore masking; no drops
    _write_sql(a.sql_out, actions)
    failed = _apply(spark, cfg, actions, actor)
    if p.failures:
        print("MASKS_RESULT " + json.dumps({**result, "applied": "masking-only", "statements": len(actions),
                                            "failed": failed}, sort_keys=True))
        raise SystemExit(f"mask plan has failures (drops skipped): {list(p.failures)}")
    print("MASKS_RESULT " + json.dumps({**result, "applied": True, "statements": len(actions),
                                        "objects": [(x.kind, x.object) for x in actions], "failed": failed},
                                       sort_keys=True))
    if failed:
        raise SystemExit(f"{len(failed)} mask action(s) failed: {failed}")


if __name__ == "__main__":
    main()
