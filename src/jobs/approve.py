"""Databricks job: approve step for one table (specs/hydration.md step 6).

Reads non-eval proposals ('proposed' to decide, accepted/edited for a revise), applies owner decisions from
gov.review_decisions first, then the
auto-rule. Writes: gov.audit_log first (one row per decision, deterministic event_id, so a crash is replayed, not
re-planned), then gov.approved_metadata (MERGE, only when values change; applied_to_uc_at reset so apply
re-projects), gov.golden_candidates (owner edits), term_proposals.status, review_decisions.applied_at/outcome.
Idempotent: a re-run with no new proposals or decisions writes nothing.

Who approved: the principal that committed the decision row (change data feed + DESCRIBE HISTORY), never the
typed decided_by. It must match decided_by, be listed in gov.table_approvers for the table, and not be the job's
own principal. A row updated or deleted after its insert is refused. Run approve for one table at a time.

Permitted uses first (hydration.md step 6, H15): owner rows in gov.permitted_use_inputs for this table go through
set_permitted_use (gates.consumption.permitted), attributed like decisions. With no active gov.permitted_uses row
for the table afterwards, accept / edit are refused and the auto-rule accepts nothing; reject and revise still apply.
  INSERT INTO <catalog>.<gov>.permitted_use_inputs (input_id, product, use_code, approval, conditions,
    max_term_days, decided_by, decided_at)
  SELECT '<unique id>', '<catalog.schema.table>', 'reporting', 'standing', NULL, 365, current_user(),
    current_timestamp();

Owner decision (SQL, one INSERT per decision), applied on the next run:
  INSERT INTO <catalog>.<gov>.review_decisions (decision_id, proposal_id, action, term_id, description,
    sensitivity, decided_by, decided_at)
  SELECT '<unique id>', '<proposal_id>', 'edit', '<term_id>', '<description>', '<sensitivity>', current_user(),
    current_timestamp();
(uuid() is rejected in VALUES and was rejected here in SELECT too: use any literal id that is unique.)
action is accept | edit | reject | revise; for edit, null term_id/description/sensitivity keeps the proposal's
value. revise corrects an already-approved column: proposal_id = the column's newest accepted/edited proposal;
null fields keep the currently approved value, NO_MATCH clears the term, sensitivity is re-floored, e.g.
  SELECT '<unique id>', '<proposal_id>', 'revise', 'NO_MATCH', NULL, 'internal', current_user(), current_timestamp();
An empty-string term_id is refused (use NO_MATCH or null).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from collections import Counter
from dataclasses import asdict, replace
from pathlib import Path

try:
    _SRC = Path(__file__).resolve().parents[1]
except NameError:
    _SRC = Path(os.getcwd()).resolve().parent
sys.path.insert(0, str(_SRC))

from gates.common.attribution import attribute  # noqa: E402
from gates.common.config import GateConfig  # noqa: E402
from gates.common.spark_io import AUDIT_MERGE, AUDIT_SCHEMA  # noqa: E402
from gates.consumption.decide import PermittedUse  # noqa: E402
from gates.consumption.permitted import (  # noqa: E402
    UseInput,
    UsePlan,
    has_permitted_uses,
    plan_permitted_uses,
    refuse_duplicates,
)
from gates.hydration.approve import (  # noqa: E402
    Action,
    ApprovedRow,
    ProfileHits,
    ProposalRow,
    ReviewDecision,
    audit_event_id,
    audit_rows,
    latest_pending,
    manual_event_id,
    plan,
    refusal_salt,
)
from gates.hydration.records import qualify_table  # noqa: E402


def _args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    GateConfig.add_args(p)
    p.add_argument("--table", required=True, help="bare name (raw schema) or catalog.schema.table")
    p.add_argument("--permitted-use-codes", default="reporting,ml_training,external_share",
                   help="comma-separated controlled list of use codes")
    return p.parse_args()


def _rows(spark, sql: str, **args):
    return spark.sql(sql, args=args).collect()


def _read(spark, cfg: GateConfig, table: str):
    proposals_t, profiles_t = cfg.gov("term_proposals"), cfg.gov("column_profiles")
    proposals = [ProposalRow(r.proposal_id, r.run_id, r.table_name, r.column_name, r.term_id, r.description,
                             r.sensitivity, float(r.confidence if r.confidence is not None else float("nan")),
                             r.method, r.status, r.created_at)
                 for r in _rows(spark, f"SELECT * FROM {proposals_t} WHERE table_name = :t", t=table)]
    profiles = {(r.run_id, r.column_name): ProfileHits(tuple(r.pattern_hits or ()), tuple(r.any_pattern_hits or ()))
                for r in _rows(spark, f"SELECT run_id, column_name, pattern_hits, any_pattern_hits FROM {profiles_t} "
                                      "WHERE table_name = :t", t=table)}
    terms = {r.term_id: r.sensitivity for r in _rows(spark, f"""
        SELECT term_id, sensitivity FROM {cfg.gov('business_terms')}
        QUALIFY row_number() OVER (PARTITION BY term_id ORDER BY version DESC NULLS LAST, loaded_at DESC) = 1""")}
    # this table's pending decisions, plus ones naming no proposal at all (refused here so they never linger)
    decisions = [ReviewDecision(r.decision_id, r.proposal_id, r.action, r.term_id, r.description, r.sensitivity,
                                r.decided_by or "unknown", r.decided_at, r.applied_at)
                 for r in _rows(spark, f"""
        SELECT * FROM {cfg.gov('review_decisions')} d WHERE d.applied_at IS NULL AND (
          d.proposal_id IN (SELECT proposal_id FROM {proposals_t} WHERE table_name = :t)
          OR NOT EXISTS (SELECT 1 FROM {proposals_t} p WHERE p.proposal_id = d.proposal_id))""", t=table)]
    decisions = _attribute(spark, cfg.gov("review_decisions"), decisions)
    pending_counts = {r.decision_id: r.n for r in _rows(spark, f"""
        SELECT decision_id, count(*) AS n FROM {cfg.gov('review_decisions')}
        WHERE applied_at IS NULL GROUP BY decision_id HAVING count(*) > 1""")}
    applied_ids = {r.decision_id for r in _rows(spark, f"""
        SELECT DISTINCT decision_id FROM {cfg.gov('review_decisions')}
        WHERE applied_at IS NOT NULL AND decision_id IN (
          SELECT decision_id FROM {cfg.gov('review_decisions')} WHERE applied_at IS NULL)""")}
    approvers = {table: {r.principal for r in _rows(
        spark, f"SELECT principal FROM {cfg.gov('table_approvers')} WHERE table_name = :t", t=table)}}
    routed = {r[0] for r in _rows(spark, f"""
        SELECT get_json_object(after_json, '$.proposal_id') FROM {cfg.gov('audit_log')}
        WHERE gate = 'hydration' AND action = 'review_routed' AND object = :t""", t=table)}
    current = {r.column_name: r.asDict() for r in _rows(
        spark, f"SELECT * FROM {cfg.gov('approved_metadata')} WHERE table_name = :t", t=table)}
    audited = _audited(spark, cfg.gov("audit_log"),
                       [manual_event_id(d.decision_id) for d in decisions]
                       + [audit_event_id("accepted", p.proposal_id, None) for p in latest_pending(proposals)])
    return proposals, profiles, terms, decisions, applied_ids, pending_counts, approvers, routed, current, audited


def _audited(spark, audit_t: str, event_ids: list[str]) -> dict[str, dict]:
    """Audit rows already written for these event ids: a crash after the audit write is replayed from them."""
    if not event_ids:
        return {}
    view = f"ids_{uuid.uuid4().hex}"
    spark.createDataFrame([(e,) for e in event_ids], "event_id string").createOrReplaceTempView(view)
    return {r.event_id: json.loads(r.after_json) for r in spark.sql(
        f"SELECT a.event_id, a.after_json FROM {audit_t} a JOIN {view} i ON a.event_id = i.event_id").collect()}


def _attribute(spark, decisions_t: str, decisions: list[ReviewDecision]) -> list[ReviewDecision]:
    """committed_by = the principal that committed each decision's INSERT (gates.common.attribution, shared with
    the consumption owner inputs). Unknown -> None and a row changed after insert -> attribution_error;
    plan_manual refuses both. If the feed cannot be read the decisions are held: they stay pending, are not
    used up, and the auto-rule leaves their proposals alone. Nothing recent in history -> stale, refused."""
    if not decisions:
        return decisions
    who = attribute(spark, decisions_t, "decision_id", sorted({d.decision_id for d in decisions}))
    return [replace(d, committed_by=w.committed_by, insert_version=w.insert_version,
                    attribution_error=w.attribution_error, hold=w.hold)
            for d in decisions for w in [who[d.decision_id]]]


def _uses(spark, cfg: GateConfig, product: str) -> list[PermittedUse]:
    return [PermittedUse(r.product, r.use_code, r.approval, r.conditions, r.max_term_days, r.version, bool(r.active))
            for r in _rows(spark, f"SELECT * FROM {cfg.gov('permitted_uses')} WHERE product = :p", p=product)]


def _set_permitted_uses(spark, cfg: GateConfig, table: str, job_principal: str, codes: tuple[str, ...],
                        approvers: set[str]) -> tuple[bool, dict]:
    """set_permitted_use: apply this table's pending permitted_use_inputs, audited. Returns (has_uses, counts).
    Order: audit, then ONE MERGE into permitted_uses (new versions in, replaced versions retired), then stamp the
    inputs. A crash after the audit replays to the same versions (same ids, deduped); after the MERGE, the re-run
    finds the values in place and stamps 'unchanged'."""
    inputs_t, uses_t = cfg.gov("permitted_use_inputs"), cfg.gov("permitted_uses")
    # rows naming this table bare or in another case are read too and refused (not an approver for that name),
    # never left pending unseen
    rows = _rows(spark, f"""SELECT * FROM {inputs_t} WHERE applied_at IS NULL
                            AND lower(trim(product)) IN (lower(:p), lower(:bare))""",
                 p=table, bare=table.split(".")[-1])
    if not rows:
        return has_permitted_uses(table, _uses(spark, cfg, table)), {}
    ids = Counter(r.input_id for r in rows)
    who = attribute(spark, inputs_t, "input_id", sorted(ids))
    inputs = [UseInput(r.input_id, r.product, r.use_code, r.approval, r.conditions, r.max_term_days,
                       r.decided_by or "unknown", r.decided_at, who[r.input_id].committed_by,
                       who[r.input_id].committed_at, who[r.input_id].insert_version,
                       who[r.input_id].attribution_error, who[r.input_id].hold) for r in rows]
    unique, dupes = refuse_duplicates(inputs)
    plans: list[UsePlan] = plan_permitted_uses(unique, _uses(spark, cfg, table), {table: approvers},
                                               allowed_codes=codes, job_principal=job_principal) + dupes
    _merge(spark, [a for p in plans for a in p.audit], AUDIT_SCHEMA, cfg.gov("audit_log"), AUDIT_MERGE)
    sets = [p for p in plans if p.kind == "set"]
    last = {(p.row["product"], p.row["use_code"]): p.row["version"] for p in sets}  # set twice: last stays active
    src = []
    for p in sets:
        r = p.row
        src.append(("insert", r["product"], r["use_code"], r["version"], r["approval"], r["conditions"],
                    r["max_term_days"], r["set_by"], r["set_at"], last[(r["product"], r["use_code"])] == r["version"]))
        src += [("retire", r["product"], r["use_code"], v, None, None, None, None, None, False)
                for v in p.retire_versions]
    _merge(spark, src, "op string, product string, use_code string, version int, approval string, "
           "conditions string, max_term_days int, set_by string, set_at timestamp, active boolean", uses_t, """
        MERGE INTO {t} t USING {s} s ON t.product = s.product AND t.use_code = s.use_code AND t.version = s.version
        WHEN MATCHED AND s.op = 'retire' AND t.active THEN UPDATE SET active = false
        WHEN NOT MATCHED AND s.op = 'insert' THEN INSERT (product, use_code, approval, conditions, max_term_days,
          set_by, set_at, version, active)
          VALUES (s.product, s.use_code, s.approval, s.conditions, s.max_term_days, s.set_by, s.set_at, s.version,
          s.active)""")
    _merge(spark, [(p.input_id, p.outcome) for p in plans if p.outcome is not None],
           "input_id string, outcome string", inputs_t, """
        MERGE INTO {t} t USING {s} s ON t.input_id = s.input_id
        WHEN MATCHED AND t.applied_at IS NULL THEN UPDATE SET applied_at = current_timestamp(), outcome = s.outcome""")
    return has_permitted_uses(table, _uses(spark, cfg, table)), dict(Counter(p.kind for p in plans))


def _merge(spark, rows: list[tuple], schema: str, target: str, sql: str) -> None:
    if not rows:
        return
    view = f"src_{uuid.uuid4().hex}"
    spark.createDataFrame(rows, schema).createOrReplaceTempView(view)
    spark.sql(sql.format(t=target, s=view))


def _write(spark, cfg: GateConfig, table: str, actions: list[Action], proposals: dict[str, ProposalRow],
           current: dict[str, dict], audited: dict[str, dict], versions: dict[str, int | str]) -> None:
    """Audit first, keyed on a deterministic event_id: if a later write fails, the re-run replays the audited
    actions (no duplicates, no re-planning) and completes the state writes."""
    _merge(spark, audit_rows(actions, proposals, current, table, cfg.gov("review_decisions"), audited=audited,
                             insert_versions=versions),
           "event_id string, gate string, action string, actor string, object string, before_json string, "
           "after_json string", cfg.gov("audit_log"), """
        MERGE INTO {t} t USING {s} s ON t.event_id = s.event_id
        WHEN NOT MATCHED THEN INSERT (event_id, gate, action, actor, object, before_json, after_json, event_at)
          VALUES (s.event_id, s.gate, s.action, s.actor, s.object, s.before_json, s.after_json,
          current_timestamp())""")

    approved = [(a.approved.table_name, a.approved.column_name, a.approved.term_id, a.approved.description,
                 a.approved.sensitivity, a.approved.approved_by) for a in actions if a.approved]
    _merge(spark, approved, "table_name string, column_name string, term_id string, description string, "
           "sensitivity string, approved_by string", cfg.gov("approved_metadata"), """
        MERGE INTO {t} t USING {s} s ON t.table_name = s.table_name AND t.column_name = s.column_name
        WHEN MATCHED AND NOT (t.term_id <=> s.term_id AND t.description <=> s.description
                              AND t.sensitivity <=> s.sensitivity AND t.approved_by <=> s.approved_by)
          THEN UPDATE SET
          term_id = s.term_id, description = s.description, sensitivity = s.sensitivity,
          approved_by = s.approved_by, approved_at = current_timestamp(), applied_to_uc_at = NULL
        WHEN NOT MATCHED THEN INSERT (table_name, column_name, term_id, description, sensitivity, approved_by,
          approved_at, applied_to_uc_at)
          VALUES (s.table_name, s.column_name, s.term_id, s.description, s.sensitivity, s.approved_by,
          current_timestamp(), NULL)""")

    # a revise replaces the proposal's earlier candidate, so a corrected edit never stays a golden candidate
    golden = [tuple(asdict(a.golden).values()) for a in actions if a.golden]
    _merge(spark, golden, "proposal_id string, table_name string, column_name string, proposed_term_id string, "
           "edited_term_id string, proposed_sensitivity string, edited_sensitivity string, edited_by string",
           cfg.gov("golden_candidates"), """
        MERGE INTO {t} t USING {s} s ON t.proposal_id = s.proposal_id
        WHEN MATCHED AND NOT (t.edited_term_id <=> s.edited_term_id
                              AND t.edited_sensitivity <=> s.edited_sensitivity AND t.edited_by <=> s.edited_by)
          THEN UPDATE SET edited_term_id = s.edited_term_id, edited_sensitivity = s.edited_sensitivity,
          edited_by = s.edited_by, edited_at = current_timestamp()
        WHEN NOT MATCHED THEN INSERT (proposal_id, table_name, column_name, proposed_term_id, edited_term_id,
          proposed_sensitivity, edited_sensitivity, edited_by, edited_at)
          VALUES (s.proposal_id, s.table_name, s.column_name, s.proposed_term_id, s.edited_term_id,
          s.proposed_sensitivity, s.edited_sensitivity, s.edited_by, current_timestamp())""")

    # only 'proposed' rows are decided; a revise moves an accepted/edited row to edited; 'eval' is never touched
    status = [(a.proposal_id, a.new_status, a.kind) for a in actions if a.new_status]
    _merge(spark, status, "proposal_id string, new_status string, kind string", cfg.gov("term_proposals"), """
        MERGE INTO {t} t USING {s} s ON t.proposal_id = s.proposal_id
        WHEN MATCHED AND t.status <> s.new_status AND (t.status = 'proposed'
                         OR (s.kind = 'revised' AND t.status IN ('accepted', 'edited')))
          THEN UPDATE SET status = s.new_status""")

    # keyed on (decision_id, proposal_id): a row reusing another table's decision_id never stamps that row
    applied = [(a.decision_id, a.proposal_id, a.kind if a.kind != "refused" else f"refused: {a.reason}")
               for a in actions if a.decision_id]
    _merge(spark, applied, "decision_id string, proposal_id string, outcome string", cfg.gov("review_decisions"), """
        MERGE INTO {t} t USING {s} s ON t.decision_id = s.decision_id AND t.proposal_id = s.proposal_id
        WHEN MATCHED AND t.applied_at IS NULL THEN UPDATE SET applied_at = current_timestamp(), outcome = s.outcome""")


CORRECTIONS = ("reject", "revise")


def corrections_only(decisions: list[ReviewDecision]) -> list[ReviewDecision]:
    """Decisions that still apply when the table is missing or not visible to the job principal: reject and revise
    (a correction must not wait, spec step 6). Accept and edit are left pending, untouched, for a later run."""
    return [d for d in decisions if d.action in CORRECTIONS]


def missing_table_exit(table: str, exists: bool) -> None:
    """After the corrections are written: fail the approve task when the table is missing or not visible, so the
    chained apply task (ALL_SUCCESS) does not run. A revised row stays unstamped, so grants fail closed."""
    if not exists:
        raise SystemExit(f"approve: table {table} does not exist or is not visible to the job principal; "
                         "only reject/revise were applied, accept/edit, permitted uses and the auto-rule wait")


def main() -> None:
    a = _args()
    cfg = GateConfig.from_args(a)
    from pyspark.sql import SparkSession

    spark = SparkSession.builder.getOrCreate()
    table = qualify_table(a.table.strip(), cfg)
    exists = spark.catalog.tableExists(table)
    job_principal = spark.sql("SELECT current_user()").collect()[0][0]
    codes = tuple(c.strip() for c in a.permitted_use_codes.split(",") if c.strip())
    table_approvers = {r.principal for r in _rows(
        spark, f"SELECT principal FROM {cfg.gov('table_approvers')} WHERE table_name = :t", t=table)}
    # missing / not visible: permitted-use inputs stay pending and has_uses False stops accept/edit and the auto-rule
    has_uses, use_counts = (_set_permitted_uses(spark, cfg, table, job_principal, codes, table_approvers)
                            if exists else (False, {}))
    (proposals, profiles, terms, decisions, applied_ids, pending_counts, approvers, routed, current,
     audited) = _read(spark, cfg, table)
    held = sum(d.hold for d in decisions)  # counted before the missing-table filter, so waiting rows are included
    if not exists:
        decisions = corrections_only(decisions)
    approved_now = {(table, c): ApprovedRow(table, c, r["term_id"], r["description"], r["sensitivity"],
                                            r["approved_by"]) for c, r in current.items()}
    actions = plan(decisions, proposals, profiles, terms, routed, approvers=approvers, job_principal=job_principal,
                   audited=audited, applied_ids=applied_ids, pending_counts=pending_counts, current=approved_now,
                   has_uses=has_uses)
    keys = [(x.approved.table_name, x.approved.column_name) for x in actions if x.approved]
    if len(keys) != len(set(keys)):  # plan() guarantees this; a MERGE with two source rows would corrupt the PK
        raise SystemExit(f"two approved rows for one column: {sorted(keys)}")
    versions = {d.decision_id: refusal_salt(d) for d in decisions}
    _write(spark, cfg, table, actions, {p.proposal_id: p for p in proposals}, current, audited, versions)
    counts = Counter(x.kind for x in actions)
    print("APPROVE_RESULT " + json.dumps({"table_name": table, "job_principal": job_principal,
                                          "held": held,
                                          "has_permitted_uses": has_uses, "permitted_use_inputs": use_counts,
                                          "manual": sum(x.decision_id is not None
                                          for x in actions), "table_exists": exists,
                                          **dict(sorted(counts.items()))}, sort_keys=True))
    missing_table_exit(table, exists)


if __name__ == "__main__":
    main()
