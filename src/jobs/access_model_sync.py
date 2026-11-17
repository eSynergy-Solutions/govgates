"""Databricks job: the access-model adapter (specs/consumption.md, "Agreements and the access model").

Reads the access model (access_model_source = file: the YAML stub; api: not built), then projects it:
- gov.access_policies: one row_filter row per schema in scope and one mask row per (schema, masked class), with the
  mask EXCEPT lists from the model's entitlements. Written as one batch through the audited policy-row writer:
  every changed row gets a `policy_row_set` audit row, then one MERGE. A crash between the two leaves rows that
  differ from their newest audit, which the masks task's tamper check refuses (fails closed); the next run here
  re-asserts those rows with a new chained audit row and completes the MERGE.
- ref.user_attributes: one row per (group, region); stale rows deleted. Each change, and each unaudited row the
  adapter adopts, is audited `user_attributes_set` with before/after.

Fails closed, writing nothing: invalid model, an active policy row outside the model's scope, or no fresh passing
membership check for this model file (job check_access_model_membership, run as a person who can read the groups:
the CI SP can't, and no read-only group role exists). The path is a deploy-time bundle variable, never a run-time
parameter, so a runner can't point the adapter at their own model. Re-run with the same model = no writes.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import timedelta
from pathlib import Path

try:
    _SRC = Path(__file__).resolve().parents[1]
except NameError:
    _SRC = Path(os.getcwd()).resolve().parent
sys.path.insert(0, str(_SRC))

from gates.common.config import GateConfig  # noqa: E402
from gates.common.spark_io import AUDIT_MERGE, AUDIT_SCHEMA, _merge  # noqa: E402
from gates.consumption.access_model import (  # noqa: E402
    UserAttr,
    desired_rows,
    desired_user_attributes,
    load_model,
    membership_gate,
    model_sha,
    plan_rows,
    plan_user_attributes,
    unaudited,
)
from gates.consumption.policy_rows import ROW_MERGE, ROW_SCHEMA, rows_change  # noqa: E402
from gates.consumption.row_filter import PolicyRow  # noqa: E402

ROW_COLS = ("policy_id, policy_type, securable, match_tag_key, match_tag_value, function_name, except_principals, "
            "active")
ATTR_SCHEMA = "op string, principal string, region string, line_of_business string"
ATTR_MERGE = """
    MERGE INTO {t} t USING {s} s ON t.principal = s.principal AND t.region = s.region
    WHEN MATCHED AND s.op = 'delete' THEN DELETE
    WHEN MATCHED THEN UPDATE SET line_of_business = s.line_of_business
    WHEN NOT MATCHED AND s.op <> 'delete' THEN INSERT (principal, region, line_of_business)
      VALUES (s.principal, s.region, s.line_of_business)"""


def _args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    GateConfig.add_args(p)
    p.add_argument("--schemas", required=True, help="comma-separated schemas in scope (bundle var grant_schemas)")
    p.add_argument("--access-model-source", choices=["file", "api"], default="file")
    p.add_argument("--access-model-path", required=True)
    return p.parse_args()


def _attr_object(principal: str, region: str) -> str:
    return json.dumps([principal, region])


def _newest(spark, cfg: GateConfig, action: str) -> dict[str, tuple[str, object, object]]:
    """object -> (event_id, before, after) of the newest audit row for this action."""
    rows = spark.sql(f"""
        SELECT object, event_id, before_json, after_json FROM (
          SELECT *, row_number() OVER (PARTITION BY object ORDER BY event_at DESC, event_id DESC) AS rn
          FROM {cfg.gov('audit_log')} WHERE gate = 'consumption' AND action = :a) WHERE rn = 1""",
                     args={"a": action}).collect()
    return {r.object: (r.event_id, json.loads(r.before_json or "null"), json.loads(r.after_json or "null"))
            for r in rows}


def _latest_membership_check(spark, cfg: GateConfig):
    """(after, age, actor, future_rows): ages and dates come from SQL against the database clock, never Python."""
    t = cfg.gov("audit_log")
    future = spark.sql(f"""
        SELECT count(*) AS n FROM {t} WHERE gate = 'consumption' AND action = 'membership_check'
          AND event_at > current_timestamp() + INTERVAL 5 MINUTES""").collect()[0].n
    rows = spark.sql(f"""
        SELECT after_json, actor,
               unix_timestamp(current_timestamp()) - unix_timestamp(event_at) AS age_s
        FROM {t} WHERE gate = 'consumption' AND action = 'membership_check'
          AND event_at <= current_timestamp() + INTERVAL 5 MINUTES
        ORDER BY event_at DESC, event_id DESC LIMIT 1""").collect()
    if not rows:
        return None, None, None, future
    r = rows[0]
    return json.loads(r.after_json or "null"), timedelta(seconds=int(r.age_s)), r.actor, future


def main() -> None:
    a = _args()
    cfg = GateConfig.from_args(a)
    if a.access_model_source != "file":
        raise SystemExit("access_model_source = api is not built yet; use file")
    text = Path(a.access_model_path).read_text(encoding="utf-8")
    model = load_model(text)
    schemas = [s.strip() for s in a.schemas.split(",") if s.strip()]

    from pyspark.sql import SparkSession

    spark = SparkSession.builder.getOrCreate()
    actor = spark.sql("SELECT session_user()").collect()[0][0]
    # the CI SP can't read account groups; a person-run job records the membership check (option B, 9 Nov)
    latest, age, check_actor, future = _latest_membership_check(spark, cfg)
    failures = membership_gate(latest, age, model_sha(text), actor=check_actor, self_actor=actor,
                               future_rows=future)
    warnings = (latest or {}).get("warnings") or []

    stored_rows = [r.asDict() for r in spark.sql(
        f"SELECT {ROW_COLS} FROM {cfg.gov('access_policies')} WHERE policy_type IN ('mask', 'row_filter')").collect()]
    for d in stored_rows:
        d["except_principals"] = list(d["except_principals"] or [])
    stored = [PolicyRow(d["policy_id"], d["policy_type"], d["securable"] or "", d["match_tag_key"] or "",
                        d["match_tag_value"] or "", d["function_name"] or "", tuple(d["except_principals"]),
                        bool(d["active"])) for d in stored_rows]
    desired = desired_rows(model, schemas=schemas, stored=stored, catalog=cfg.catalog, gov_schema=cfg.gov_schema,
                           sensitivity_tag=cfg.sensitivity_tag, rls_tag=cfg.rls_tag,
                           hydration_principal=cfg.hydration_principal)
    newest_rows = _newest(spark, cfg, "policy_row_set")
    newest_after = {k: v[2] for k, v in newest_rows.items()}
    plan = plan_rows(desired, stored, newest_after)
    failures = tuple(failures) + plan.failures
    if failures:
        raise SystemExit(f"access model not applied, nothing written: {list(failures)}")

    changes = rows_change(stored_rows, plan.writes,
                          last_event_ids={k: v[0] for k, v in newest_rows.items()}, catalog=cfg.catalog,
                          rls_tag=cfg.rls_tag, sensitivity_tag=cfg.sensitivity_tag,
                          hydration_principal=cfg.hydration_principal, gov_schema=cfg.gov_schema,
                          allow_reserved=True, reassert=frozenset(unaudited(stored, newest_after)))

    stored_attrs = [UserAttr(r.principal, r.region, r.line_of_business) for r in spark.sql(
        f"SELECT principal, region, line_of_business FROM {cfg.ref('user_attributes')}").collect()]
    newest_attrs = _newest(spark, cfg, "user_attributes_set")
    last_attr, last_attr_after = {}, {}
    for obj, (eid, _, after) in newest_attrs.items():
        p, r = json.loads(obj)
        last_attr[(p, r)] = eid
        last_attr_after[(p, r)] = after
    attr_changes = plan_user_attributes(desired_user_attributes(model), stored_attrs, last_attr, last_attr_after)

    audit, rows_out, attrs_out = [], [], []
    # ids chain on the newest event: a retry after a crash adds a row, never hides one
    for before, after, eid in changes:
        pid = after["policy_id"]
        audit.append((eid, "consumption", "policy_row_set", actor, pid,
                      json.dumps(before, sort_keys=True) if before else None, json.dumps(after, sort_keys=True)))
        rows_out.append((pid, after["policy_type"], after["securable"], None, None, after["match_tag_key"],
                         after["match_tag_value"], after["function_name"], after["except_principals"],
                         after["active"]))
    for c in attr_changes:
        obj = _attr_object(c.principal, c.region)
        audit.append((c.event_id, "consumption", "user_attributes_set", actor, obj,
                      json.dumps(c.before, sort_keys=True) if c.before else None,
                      json.dumps(c.after, sort_keys=True) if c.after else None))
        if c.kind != "adopt":
            lob = (c.after or c.before or {}).get("line_of_business")
            attrs_out.append(("delete" if c.kind == "delete" else "upsert", c.principal, c.region, lob))

    # audit first, then the governance writes: a crash in between is caught by the tamper check, and the next
    # run re-asserts the rows (stored differs from audited) with a new chained audit row
    _merge(spark, audit, AUDIT_SCHEMA, cfg.gov("audit_log"), AUDIT_MERGE)
    _merge(spark, rows_out, ROW_SCHEMA, cfg.gov("access_policies"), ROW_MERGE)
    _merge(spark, attrs_out, ATTR_SCHEMA, cfg.ref("user_attributes"), ATTR_MERGE)
    print("ACCESS_MODEL_RESULT " + json.dumps({
        "policy_rows_changed": [r[0] for r in rows_out],
        "user_attributes": {k: sum(1 for c in attr_changes if c.kind == k)
                            for k in ("insert", "update", "delete", "adopt")},
        "audit_rows": len(audit), "membership_warnings": len(warnings)}, sort_keys=True))


if __name__ == "__main__":
    main()
