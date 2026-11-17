"""Databricks job task: grants generator (specs/consumption.md Policy model `grant` rows; Decisions (8 Nov) 3).

Two tasks in `consumption_apply`, both as the CI service principal:
- `--mode revoke` runs whatever happened upstream (run_if ALL_DONE): completes interrupted agreement status writes,
  sweeps agreements past valid_to to `expired`, retires grant rows of dead agreements and revokes in-scope SELECT /
  USE SCHEMA that no live agreement backs. Revocation never waits on a failed check, filter or mask.
- `--mode set` runs only after masks succeed: activates grant rows of live agreements and grants USE CATALOG /
  USE SCHEMA / SELECT. Nothing is granted while a grant row is tampered or the scope check fails.

Agreements are trusted only as audited (gates.consumption.grants.verify_agreements); a refused agreement is audited
`agreement_refused` (once per distinct refusal) and the task exits non-zero, other agreements proceed. Every
statement is audited `<kind>_started` (with the statement) then `<kind>` / `<kind>_failed`. A converged run plans
nothing and writes nothing.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

try:
    _SRC = Path(__file__).resolve().parents[1]
except NameError:
    _SRC = Path(os.getcwd()).resolve().parent
sys.path.insert(0, str(_SRC))

from gates.common.config import GateConfig  # noqa: E402
from gates.common.spark_io import AUDIT_MERGE, AUDIT_SCHEMA, _merge  # noqa: E402
from gates.consumption.grants import (  # noqa: E402
    GRANT_PREFIX,
    Grant,
    GrantRow,
    activations_after_apply,
    agreement_from,
    agreement_state,
    blocking_refusals,
    expiry_sweep,
    grant_row_dict,
    grantable,
    misfiled_grant_ids,
    pending_activations,
    plan_grants,
    principal_names,
    resume_grant_rows,
    scope_record,
    should_record,
    sync_grant_rows,
    tampered_grants,
    task_actions,
    task_problems,
    verify_agreements,
    wanted_grants,
)
from gates.consumption.policy_rows import ROW_MERGE, ROW_SCHEMA  # noqa: E402
from gates.hydration.apply import event_id_of, quote_ident, quote_table  # noqa: E402
from jobs.consumption_apply import GATE, _apply, _approved, _info, _last_events, _write_sql  # noqa: E402


def _args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    GateConfig.add_args(p)
    p.add_argument("--mode", choices=["revoke", "set"], required=True)
    p.add_argument("--grant-schemas", required=True, help="comma-separated schema names the generator manages")
    p.add_argument("--owner-exception", default="")
    p.add_argument("--plan-only", choices=["true", "false"], default="false",
                   help="print the plan, write nothing (dev: review revokes before the first apply)")
    p.add_argument("--sql-out", default=None)
    return p.parse_args()


# ---------------------------------------------------------------- reads

def _agreements(spark, cfg):
    return [agreement_from(r.asDict()) for r in spark.sql(f"SELECT * FROM {cfg.gov('consumption_agreements')}")
            .collect()]


def _newest_agreement_audit(spark, cfg) -> dict[str, dict]:
    """Newest agreement_registered (object = request_id) or agreement_status_set (object = agreement_id) per
    agreement: {agreement_id: {"after": dict, "before": dict|None, "event_id", "action"}}."""
    out: dict[str, tuple] = {}
    for r in spark.sql(f"""
        SELECT event_id, action, before_json, after_json, event_at FROM {cfg.gov('audit_log')}
        WHERE gate = 'consumption' AND action IN ('agreement_registered', 'agreement_status_set')""").collect():
        after = json.loads(r.after_json) if r.after_json else {}
        aid = after.get("agreement_id")
        if not aid:
            continue
        key = (r.event_at, r.event_id)
        if aid not in out or key > out[aid][0]:
            out[aid] = (key, {"after": after, "before": json.loads(r.before_json) if r.before_json else None,
                              "event_id": r.event_id, "action": r.action})
    return {aid: v for aid, (_, v) in out.items()}


def _evidence_steps(spark, cfg) -> dict[str, set[str]]:
    return {r.request_id: set(r.steps) for r in spark.sql(f"""
        SELECT request_id, collect_set(step) AS steps FROM {cfg.gov('consumption_decisions')}
        GROUP BY request_id""").collect()}


def _requests(spark, cfg) -> dict[str, dict]:
    return {r.request_id: {"request_id": r.request_id, "consumer": r.consumer, "securable": r.securable}
            for r in spark.sql(f"SELECT request_id, consumer, securable FROM {cfg.gov('access_requests')}").collect()}


def _grant_rows(spark, cfg) -> tuple[list[GrantRow], list[str]]:
    """Grant rows, and gr_ ids stored as another policy type (tampered)."""
    got = spark.sql(f"""SELECT * FROM {cfg.gov('access_policies')}
                        WHERE policy_type = 'grant' OR startswith(policy_id, :p)""",
                    args={"p": GRANT_PREFIX}).collect()
    rows = [GrantRow(r.policy_id, r.securable or "", r.principal or "", r.privilege or "", bool(r.active))
            for r in got if r.policy_type == "grant"]
    return rows, misfiled_grant_ids([(r.policy_id, r.policy_type) for r in got])


def _newest_row_audit(spark, cfg) -> dict[str, tuple[str, dict | None, dict | None]]:
    """policy_id -> (event_id, before, after) of its newest policy_row_set, for grant ids only."""
    return {r.o: (r.e, json.loads(r.b) if r.b else None, json.loads(r.a) if r.a else None) for r in spark.sql(f"""
        SELECT o, e, b, a FROM (SELECT object AS o, event_id AS e, before_json AS b, after_json AS a,
                 row_number() OVER (PARTITION BY object ORDER BY event_at DESC, event_id DESC) AS rn
               FROM {cfg.gov('audit_log')}
               WHERE gate = 'consumption' AND action = 'policy_row_set' AND startswith(object, :p))
        WHERE rn = 1""", args={"p": GRANT_PREFIX}).collect()}


def _show(spark, otype: str, key: str) -> list[Grant]:
    on = quote_ident(key) if otype == "CATALOG" else quote_table(key)
    return [Grant(r[0], r[1], r[2], r[3]) for r in spark.sql(f"SHOW GRANTS ON {otype} {on}").collect()]


def _scope(spark, cfg, managed: list[str]):
    """SHOW GRANTS on the catalog, each managed schema and each table in it; owners of all of them."""
    info = _info(cfg)
    current = {("CATALOG", cfg.catalog.lower()): _show(spark, "CATALOG", cfg.catalog)}
    owners = {}
    for r in spark.sql(f"SELECT catalog_owner FROM {info}.catalogs WHERE catalog_name = :c",
                       args={"c": cfg.catalog}).collect():
        owners[("CATALOG", cfg.catalog.lower())] = r.catalog_owner
    for s in managed:
        key = f"{cfg.catalog}.{s}"
        current[("SCHEMA", key.lower())] = _show(spark, "SCHEMA", key)
        for r in spark.sql(f"SELECT schema_owner FROM {info}.schemata WHERE schema_name = :s",
                           args={"s": s}).collect():
            owners[("SCHEMA", key.lower())] = r.schema_owner
        for r in spark.sql(f"SELECT table_name, table_owner FROM {info}.tables WHERE table_schema = :s",
                           args={"s": s}).collect():
            t = f"{key}.{r.table_name}"
            current[("TABLE", t.lower())] = _show(spark, "TABLE", t)
            owners[("TABLE", t.lower())] = r.table_owner
    return current, owners


# ---------------------------------------------------------------- writes

def _audit_row(event_id, action, actor, obj, before, after) -> tuple:
    return (event_id, GATE, action, actor, obj, None if before is None else json.dumps(before, sort_keys=True),
            None if after is None else json.dumps(after, sort_keys=True))


def write_status_change(spark, cfg, change, actor: str, newest: dict | None) -> None:
    """Audit first, then the UPDATE (only while the row is still `before`). A crash in between is completed by
    `resume_status_writes` on the next run, reusing the same audit row."""
    after_json = {**change.after, "meta": change.meta}
    eid = change.event_id
    if newest and newest["action"] == "agreement_status_set" and newest["before"] == change.before \
            and {k: v for k, v in newest["after"].items() if k != "meta"} == change.after:
        eid = newest["event_id"]
    _merge(spark, [_audit_row(eid, "agreement_status_set", actor, change.agreement_id, change.before, after_json)],
           AUDIT_SCHEMA, cfg.gov("audit_log"), AUDIT_MERGE)
    _update_status(spark, cfg, change.agreement_id, change.before, change.after)


def _update_status(spark, cfg, agreement_id: str, before: dict, after: dict) -> None:
    res = spark.sql(f"""UPDATE {cfg.gov('consumption_agreements')} SET status = :st, version = :v
                        WHERE agreement_id = :id AND status = :old_st AND version = :old_v""",
                    args={"st": after["status"], "v": int(after["version"]), "id": agreement_id,
                          "old_st": before["status"], "old_v": int(before["version"])}).collect()
    n = int(res[0]["num_affected_rows"]) if res else 0
    if n != 1:  # a concurrent status change won: this audited change is not stored (fails closed: not live)
        raise SystemExit(f"agreement {agreement_id}: status UPDATE changed {n} rows (expected 1); audited change "
                         f"{before['status']} v{before['version']} -> {after['status']} v{after['version']} not "
                         "stored, repair by hand")


def resume_status_writes(spark, cfg, agreements, newest_audit: dict[str, dict]) -> int:
    """An agreement_status_set audit row whose `before` is still the stored row: complete its UPDATE."""
    n = 0
    for a in agreements:
        nw = newest_audit.get(a.agreement_id)
        if nw and nw["action"] == "agreement_status_set" and nw["before"] == agreement_state(a):
            _update_status(spark, cfg, a.agreement_id, nw["before"], nw["after"])
            n += 1
    return n


def _merge_row(spark, cfg, r: dict) -> None:
    _merge(spark, [(r["policy_id"], "grant", r["securable"], r["principal"], r["privilege"], None, None, None,
                    None, r["active"])], ROW_SCHEMA, cfg.gov("access_policies"), ROW_MERGE)


def _write_rows(spark, cfg, changes, actor: str, newest_rows: dict) -> None:
    for c in changes:
        eid = c.event_id
        nw = newest_rows.get(c.policy_id)
        if nw and nw[1] == c.before and nw[2] == c.after:
            eid = nw[0]  # an earlier run audited this exact change, then stopped before its MERGE
        _merge(spark, [_audit_row(eid, "policy_row_set", actor, c.policy_id, c.before, c.after)], AUDIT_SCHEMA,
               cfg.gov("audit_log"), AUDIT_MERGE)
        _merge_row(spark, cfg, c.after)


def _record_once(spark, cfg, action: str, obj: str, after: dict, actor: str) -> bool:
    """Audit `action` on `obj` only when its content differs from the newest such row (no row per run)."""
    last = spark.sql(f"""SELECT event_id, after_json FROM {cfg.gov('audit_log')}
                         WHERE gate = 'consumption' AND action = :a AND object = :o
                         ORDER BY event_at DESC, event_id DESC LIMIT 1""", args={"a": action, "o": obj}).collect()
    if not should_record(json.loads(last[0].after_json or "null") if last else None, after):
        return False
    eid = event_id_of(action, obj, after, last[0].event_id if last else None)
    _merge(spark, [_audit_row(eid, action, actor, obj, None, after)], AUDIT_SCHEMA, cfg.gov("audit_log"),
           AUDIT_MERGE)
    return True


# ---------------------------------------------------------------- main

def main() -> None:
    a = _args()
    cfg = GateConfig.from_args(a)
    managed = [s.strip() for s in a.grant_schemas.split(",") if s.strip()]
    exempt = [cfg.hydration_principal, a.owner_exception]
    plan_only = a.plan_only == "true"
    from pyspark.sql import SparkSession

    spark = SparkSession.builder.getOrCreate()
    actor = spark.sql("SELECT session_user()").collect()[0][0]
    now = datetime.now(UTC)
    if not cfg.hydration_principal:
        raise SystemExit("hydration principal (CI SP) is not configured: no exempt principal, refusing to run")

    agreements = _agreements(spark, cfg)
    newest = _newest_agreement_audit(spark, cfg)
    ids = [x.agreement_id for x in agreements]
    unique = [x for x in agreements if ids.count(x.agreement_id) == 1]  # duplicates are refused, never resumed
    resumed = 0 if plan_only else resume_status_writes(spark, cfg, unique, newest)
    if resumed:
        agreements, newest = _agreements(spark, cfg), _newest_agreement_audit(spark, cfg)
    evidence, requests = _evidence_steps(spark, cfg), _requests(spark, cfg)

    def verify():
        return verify_agreements(agreements, newest_audit={k: v["after"] for k, v in newest.items()},
                                 evidence_steps=evidence, requests=requests, now=now)

    live, refused = verify()
    refused_ids = {aid for aid, _ in refused}
    swept = []
    if a.mode == "revoke":
        due = expiry_sweep([x for x in agreements if x.agreement_id not in refused_ids], now,
                           last_events={k: v["event_id"] for k, v in newest.items()})
        swept = [c.agreement_id for c in due]
        if due and not plan_only:
            for c in due:
                write_status_change(spark, cfg, c, actor, newest.get(c.agreement_id))
            agreements, newest = _agreements(spark, cfg), _newest_agreement_audit(spark, cfg)
            live, refused = verify()

    newly_refused = []
    if not plan_only:
        for aid, why in refused:
            stored = sorted((agreement_state(x) for x in agreements if x.agreement_id == aid),
                            key=lambda d: json.dumps(d, sort_keys=True))
            if _record_once(spark, cfg, "agreement_refused", aid, {"reason": why, "stored": stored}, actor):
                newly_refused.append(aid)

    rows, misfiled = _grant_rows(spark, cfg)
    newest_rows = _newest_row_audit(spark, cfg)
    # finish retirements audited before a crash (their audit rows already exist); activations are never resumed
    resumed_rows = resume_grant_rows(rows, newest_rows, retire_only=True)
    if resumed_rows and not plan_only:
        for c in resumed_rows:
            _merge_row(spark, cfg, c.after)
        rows, misfiled = _grant_rows(spark, cfg)
    pending = pending_activations(rows, newest_rows) | ({c.policy_id for c in resumed_rows} if plan_only else set())
    tampered = sorted({pid for pid in tampered_grants(rows, {k: v[2] for k, v in newest_rows.items()})
                       if pid not in pending} | set(misfiled))
    if tampered and not plan_only:
        for pid in tampered:
            stored = [grant_row_dict(r) for r in rows if r.policy_id == pid] or None
            _record_once(spark, cfg, "policy_refused", pid,
                         {"stored": stored, "audited": newest_rows.get(pid, (None, None, None))[2],
                          "reason": "grant row differs from its newest audited write"}, actor)

    approved = _approved(spark, cfg)
    granting, _ = grantable(live, approved, catalog=cfg.catalog, managed_schemas=managed)
    row_changes = [] if tampered else sync_grant_rows(
        agreements, {x.agreement_id for x in granting},
        {r.policy_id: r for r in rows if r.policy_id.startswith(GRANT_PREFIX)},
        {k: v[0] for k, v in newest_rows.items()})
    wanted, skipped = wanted_grants(live, approved, catalog=cfg.catalog, managed_schemas=managed)
    current, owners = _scope(spark, cfg, managed)
    p = plan_grants(wanted, current, owners, catalog=cfg.catalog, managed_schemas=managed, exempt=exempt,
                    last_events=_last_events(spark, cfg), names=principal_names(granting))
    actions, row_changes = task_actions(a.mode, p, row_changes, tampered=tampered)

    result = {"mode": a.mode, "actor": actor, "plan_only": plan_only, "live": sorted(x.agreement_id for x in live),
              "granting": sorted(x.agreement_id for x in granting), "refused": refused,
              "newly_refused": newly_refused, "swept": swept, "resumed": resumed,
              "resumed_rows": [c.policy_id for c in resumed_rows], "tampered": tampered,
              "failures": list(p.failures), "skipped": skipped,
              "row_changes": [(c.policy_id, c.after["active"]) for c in row_changes],
              "statements": len(actions), "objects": [(x.kind, x.object) for x in actions]}
    if plan_only:
        result["sql"] = [x.statement for x in actions]
        print("GRANTS_RESULT " + json.dumps(result, sort_keys=True, default=str))
        return

    out = a.sql_out or os.path.join(tempfile.gettempdir(), "consumption", f"grants_{a.mode}.sql")
    _write_sql(out, actions)
    if a.mode == "revoke":  # record the retirement, then revoke
        _write_rows(spark, cfg, row_changes, actor, newest_rows)
        failed = _apply(spark, cfg, actions, actor)
    else:  # grant first; a row goes active only once all of its agreement's grants are in place
        failed = _apply(spark, cfg, actions, actor)
        row_changes = activations_after_apply(row_changes, granting, {o for o, _, _ in failed},
                                              catalog=cfg.catalog)
        _write_rows(spark, cfg, row_changes, actor, newest_rows)
        result["row_changes"] = [(c.policy_id, c.after["active"]) for c in row_changes]
    scope_obj = f"{cfg.catalog}|{','.join(managed)}".lower()
    record = scope_record(p.failures, _last_scope(spark, cfg, scope_obj))
    if record is not None:
        _record_once(spark, cfg, "grant_scope_check", scope_obj, record, actor)
    result.update({"failed": failed, "scope_audited": record is not None})
    print("GRANTS_RESULT " + json.dumps(result, sort_keys=True, default=str))
    problems = task_problems(a.mode, failed=failed, refused=blocking_refusals(refused, agreements),
                             tampered=tampered, failures=p.failures)
    if problems:
        raise SystemExit("; ".join(problems))


def _last_scope(spark, cfg, obj: str) -> dict | None:
    last = spark.sql(f"""SELECT after_json FROM {cfg.gov('audit_log')}
                         WHERE gate = 'consumption' AND action = 'grant_scope_check' AND object = :o
                         ORDER BY event_at DESC, event_id DESC LIMIT 1""", args={"o": obj}).collect()
    return json.loads(last[0].after_json) if last and last[0].after_json else None


if __name__ == "__main__":
    main()
