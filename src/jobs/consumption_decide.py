"""Databricks job: decide every received consumption request (specs/consumption.md Flow 2, 3, 5, 6).

Deterministic code only: no model output decides anything. For each request with status `received`: if evidence
for it is already stored (a crashed earlier run), the stored decision is completed, never re-decided; otherwise
decide() runs on the active pattern and permitted-use rows. Writes, in order, with insert-only MERGEs: evidence
rows (gov.consumption_decisions), the agreement (gov.consumption_agreements) on auto-approve, audit rows, then the
request status, updated only while it is still `received`. A second run finds no received request and writes
nothing.

Phase 2, owner decisions (Flow 4): pending rows in gov.request_decisions, attributed to the committing principal
(change data feed), planned by gates.consumption.owner. Writes in order: evidence, audit, agreement, request status
(only while still `pending_owner`), then the input row's applied_at / outcome. Both phases always run; the job exits
non-zero at the end if either failed. Owner decision (SQL, one INSERT per decision):
  INSERT INTO <catalog>.<gov>.request_decisions (decision_id, request_id, action, reason, note, decided_by,
    decided_at)
  SELECT '<unique id>', '<request_id>', 'approve', NULL, 'optional note', current_user(), current_timestamp();
action is approve | reject (reason required) | note (note required, any status).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

try:
    _SRC = Path(__file__).resolve().parents[1]
except NameError:
    _SRC = Path(os.getcwd()).resolve().parent
sys.path.insert(0, str(_SRC))

from gates.common.config import GateConfig  # noqa: E402
from gates.common.spark_io import AUDIT_MERGE, AUDIT_SCHEMA, _merge  # noqa: E402
from gates.consumption.decide import Decision, Pattern, PermittedUse, Request, decide  # noqa: E402
from gates.consumption.owner import (  # noqa: E402
    OwnerDecision,
    OwnerPlan,
    RequestView,
    owner_evidence_id,
    plan_owner,
    refuse_duplicates,
)
from gates.consumption.requests import plan_writes  # noqa: E402

EVIDENCE_COLS = ("decision_id", "request_id", "step", "outcome", "reason_code", "reason_text", "note", "decided_by",
                 "decided_at", "inputs_json")
EVIDENCE_SCHEMA = ("decision_id string, request_id string, step string, outcome string, reason_code string, "
                   "reason_text string, note string, decided_by string, decided_at timestamp, inputs_json string")
AGREEMENT_COLS = ("agreement_id", "request_id", "consumer", "product", "use_code", "access_mode", "use_mode",
                  "delivery", "decided_by", "status", "valid_from", "valid_to", "version")
AGREEMENT_SCHEMA = ("agreement_id string, request_id string, consumer string, product string, use_code string, "
                    "access_mode string, use_mode string, delivery string, decided_by string, status string, "
                    "valid_from timestamp, valid_to timestamp, version int")


# placeholder passed on a replay: plan_writes rebuilds from stored evidence and never reads it
_REPLAY = Decision("denied", "request_check", None, "replay", {"pattern": None, "permitted_use": None})


def _insert_only(key: str) -> str:
    return f"MERGE INTO {{t}} t USING {{s}} s ON t.{key} = s.{key} WHEN NOT MATCHED THEN INSERT *"


def _args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    GateConfig.add_args(p)
    p.add_argument("--no-permitted-uses-outcome", choices=["deny", "owner"], default="deny")
    p.add_argument("--stop-before-agreement", choices=["true", "false"], default="false",
                   help="dev proof only: stop after evidence + audit, before the agreement and status (replay path)")
    return p.parse_args()


def main() -> None:
    a = _args()
    cfg = GateConfig.from_args(a)
    from pyspark.sql import SparkSession

    spark = SparkSession.builder.getOrCreate()
    actor = spark.sql("SELECT session_user()").collect()[0][0]
    now = datetime.now(UTC).replace(tzinfo=None, microsecond=0)
    reqs = [Request(r.request_id, r.consumer, r.securable, r.intended_use, r.reason, r.use_mode, r.delivery,
                    r.access_mode, r.requester, str(r.requested_at) if r.requested_at else None)
            for r in spark.sql(f"""SELECT * FROM {cfg.gov('access_requests')} WHERE status = 'received'
                                   ORDER BY requested_at, request_id""").collect()]
    patterns = [Pattern(r.pattern_id, r.use_mode, r.delivery, r.access_mode, bool(r.allowed), r.version,
                        bool(r.active))
                for r in spark.sql(f"SELECT * FROM {cfg.gov('architecture_patterns')}").collect()]
    uses = [PermittedUse(r.product, r.use_code, r.approval, r.conditions, r.max_term_days, r.version, bool(r.active))
            for r in spark.sql(f"SELECT * FROM {cfg.gov('permitted_uses')}").collect()]
    results, failed = [], []
    for req in reqs:
        evidence = [r.asDict() for r in spark.sql(
            f"SELECT * FROM {cfg.gov('consumption_decisions')} WHERE request_id = :r", args={"r": req.request_id})
            .collect()]
        agreement = spark.sql(f"SELECT * FROM {cfg.gov('consumption_agreements')} WHERE request_id = :r",
                              args={"r": req.request_id}).collect()
        # a replay completes the stored decision; current policy rows are not read for it at all
        decision = (_REPLAY if evidence else
                    decide(req, patterns, uses, no_uses_outcome=a.no_permitted_uses_outcome))
        try:
            w = plan_writes(req, decision, evidence, agreement[0].asDict() if agreement else None, actor=actor,
                            now=now)
        except ValueError as e:  # stored evidence no single run could have written: leave it received, say so
            failed.append((req.request_id, str(e)))
            continue
        # order: evidence, audit, agreement, status. An active agreement never exists without its audit row.
        _merge(spark, [tuple(r[c] for c in EVIDENCE_COLS) for r in w.evidence], EVIDENCE_SCHEMA,
               cfg.gov("consumption_decisions"), _insert_only("decision_id"))
        _merge(spark, w.audit, AUDIT_SCHEMA, cfg.gov("audit_log"), AUDIT_MERGE)
        if a.stop_before_agreement == "true":
            results.append({"request_id": req.request_id, "stopped": True, "replay": bool(evidence)})
            continue
        if w.agreement:
            _merge(spark, [tuple(w.agreement[c] for c in AGREEMENT_COLS)], AGREEMENT_SCHEMA,
                   cfg.gov("consumption_agreements"), _insert_only("agreement_id"))
        decided_at = min([r["decided_at"] for r in evidence] or [now])  # the first decision, also on a replay
        spark.sql(f"""UPDATE {cfg.gov('access_requests')} SET status = :st, decided_at = :at
                      WHERE request_id = :r AND status = 'received'""",
                  args={"st": w.status, "at": decided_at, "r": req.request_id})
        results.append({"request_id": req.request_id, "status": w.status, "replay": bool(evidence),
                        "reason_code": None if evidence else decision.reason_code,
                        "evidence_rows": len(w.evidence), "agreement": bool(w.agreement)})
    print("DECIDE_RESULT " + json.dumps({"actor": actor, "received": len(reqs), "results": results,
                                         "failed": failed}, sort_keys=True, default=str))
    # phase 2 runs whatever phase 1 did: a stuck received request never blocks an owner's decision, nor the reverse
    try:
        owner_failed = _owner_phase(spark, cfg, actor, uses)
    except Exception as e:  # noqa: BLE001 - reported below, after phase 1's own result
        owner_failed = [f"owner phase failed: {type(e).__name__}: {e}"]
    if failed or owner_failed:
        raise SystemExit(f"{len(failed)} request(s) left received: {failed}; owner phase: {owner_failed}")


def _ids_view(spark, ids: list[str]) -> str:
    view = f"ids_{uuid.uuid4().hex}"
    spark.createDataFrame([(i,) for i in ids], "id string").createOrReplaceTempView(view)
    return view


def _after(spark, cfg: GateConfig, action: str, rids: list[str]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {r: [] for r in rids}
    ids_view = _ids_view(spark, rids)
    for r in spark.sql(f"""SELECT object, after_json FROM {cfg.gov('audit_log')}
                           WHERE gate = 'consumption' AND action = :a AND object IN (SELECT id FROM {ids_view})""",
                       args={"a": action}).collect():
        out[r.object].append(json.loads(r.after_json) if r.after_json else {})
    return out


def _views(spark, cfg: GateConfig, rids: list[str]) -> dict[str, RequestView]:
    if not rids:
        return {}
    ids_view = _ids_view(spark, rids)
    reqs = {r.request_id: r.asDict() for r in spark.sql(
        f"SELECT * FROM {cfg.gov('access_requests')} WHERE request_id IN (SELECT id FROM {ids_view})").collect()}
    evidence: dict[str, list[dict]] = {r: [] for r in rids}
    for r in spark.sql(f"SELECT * FROM {cfg.gov('consumption_decisions')} "
                       f"WHERE request_id IN (SELECT id FROM {ids_view})").collect():
        evidence[r.request_id].append(r.asDict())
    agreements = {r.request_id: r.asDict() for r in spark.sql(
        f"SELECT * FROM {cfg.gov('consumption_agreements')} WHERE request_id IN (SELECT id FROM {ids_view})"
    ).collect()}
    received, statuses = _after(spark, cfg, "request_received", rids), _after(spark, cfg, "request_status_set", rids)
    # two request_received rows for one request can't come from submit_request: treat as no trusted state
    return {rid: RequestView(row, received[rid][0] if len(received[rid]) == 1 else None, statuses[rid],
                             evidence[rid], agreements.get(rid)) for rid, row in reqs.items()}


def _owner_phase(spark, cfg: GateConfig, actor: str, uses: list[PermittedUse]) -> list[str]:
    from gates.common.attribution import attribute

    inputs_t = cfg.gov("request_decisions")
    rows = spark.sql(f"SELECT * FROM {inputs_t} WHERE applied_at IS NULL").collect()
    if not rows:
        print("OWNER_RESULT " + json.dumps({"pending": 0}))
        return []
    ids = Counter(r.decision_id for r in rows)
    who = attribute(spark, inputs_t, "decision_id", sorted(ids))
    decisions = [OwnerDecision(r.decision_id, r.request_id, r.action, r.reason, r.note, r.decided_by or "unknown",
                               r.decided_at, who[r.decision_id].committed_by, who[r.decision_id].committed_at,
                               who[r.decision_id].insert_version, who[r.decision_id].attribution_error,
                               who[r.decision_id].hold) for r in rows]
    views = _views(spark, cfg, sorted({d.request_id for d in decisions}))
    approvers: dict[str, set[str]] = {}
    for r in spark.sql(f"SELECT table_name, principal FROM {cfg.gov('table_approvers')}").collect():
        approvers.setdefault(r.table_name, set()).add(r.principal)
    unique, dupes = refuse_duplicates(decisions)
    plans: list[OwnerPlan] = plan_owner(unique, views, approvers, uses, job_principal=actor) + dupes
    by_id = {d.decision_id: d for d in decisions}
    # order: evidence, audit, agreement, status, stamp. An active agreement never exists without its audit row.
    _merge(spark, [tuple(r[c] for c in EVIDENCE_COLS) for p in plans for r in p.evidence], EVIDENCE_SCHEMA,
           cfg.gov("consumption_decisions"), _insert_only("decision_id"))
    _merge(spark, [a for p in plans for a in p.audit], AUDIT_SCHEMA, cfg.gov("audit_log"), AUDIT_MERGE)
    _merge(spark, [tuple(p.agreement[c] for c in AGREEMENT_COLS) for p in plans if p.agreement], AGREEMENT_SCHEMA,
           cfg.gov("consumption_agreements"), _insert_only("agreement_id"))
    for p in plans:
        if p.new_status:
            d = by_id[p.decision_id]
            stored = [e for e in views[p.request_id].evidence if e["decision_id"] == owner_evidence_id(d)]
            at = stored[0]["decided_at"] if stored else d.committed_at
            spark.sql(f"""UPDATE {cfg.gov('access_requests')} SET status = :st, approver = :ap, decided_at = :at
                          WHERE request_id = :r AND status = 'pending_owner'""",
                      args={"st": p.new_status, "ap": p.approver, "at": at, "r": p.request_id})
    _merge(spark, [(p.decision_id, p.request_id, p.outcome) for p in plans if p.outcome is not None],
           "decision_id string, request_id string, outcome string", inputs_t, """
        MERGE INTO {t} t USING {s} s ON t.decision_id = s.decision_id AND t.request_id = s.request_id
        WHEN MATCHED AND t.applied_at IS NULL THEN UPDATE SET applied_at = current_timestamp(), outcome = s.outcome""")
    print("OWNER_RESULT " + json.dumps({"pending": len(rows), "results": [
        {"decision_id": p.decision_id, "request_id": p.request_id, "kind": p.kind, "reason": p.reason,
         "agreement": bool(p.agreement)} for p in plans]}, sort_keys=True, default=str))
    return []


if __name__ == "__main__":
    main()
