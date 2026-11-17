"""Consumption request write planner (specs/consumption.md Flow 1, 5, 6, "Request status values",
"Decisions (8 Nov)" item 3, Policy model `grant` "Live agreement"; specs/contracts.md consumption_decisions,
consumption_agreements, access_requests, audit_log).

Pure: no Spark, no workspace. `plan_writes` turns one Decision into the evidence rows, agreement row, request
status and audit rows to write; a replay (evidence already stored) rebuilds from the stored evidence, never from
the new Decision, and never from `now`.

Fixture conventions (not fixed by the spec, chosen here):
- Decisions are built directly from the Decision dataclass, so these tests do not depend on decide()'s rules.
- Timestamps may be returned as datetime or ISO string; `dt()` normalises both before comparing.
- Stored evidence for replay tests is the planner's own first-run output, as it would be read back from gov.
"""

from __future__ import annotations

import dataclasses
import json
from datetime import UTC, datetime, timedelta

import pytest

from gates.consumption.decide import Decision, Request
from gates.consumption.requests import Writes, plan_writes, request_id_for
from gates.hydration.apply import event_id_of

PRODUCT = "cat.raw.claims"
CONSUMER = "0000c1c1-0000-4000-8000-000000000001"
REQUESTER = "requester@example.com"
ACTOR = "ci-sp-0000"  # synthetic job principal
USE = "claims_reporting"
RID = "req-0001"
SECRET_NOTE = "purpose note SYNTHETIC-FREE-TEXT-7731"
T1 = datetime(2026, 11, 8, 9, 0, 0)
T2 = datetime(2026, 11, 9, 12, 30, 0)

PAT_IN = {"pattern_id": "pat_analytical_batch_system", "version": 1}


def pu_in(max_term_days=90, version: int = 1) -> dict:
    return {"product": PRODUCT, "use_code": USE, "version": version, "max_term_days": max_term_days}


def req(**over) -> Request:
    base = dict(request_id=RID, consumer=CONSUMER, product=PRODUCT, intended_use=USE, reason=SECRET_NOTE,
                use_mode="analytical", delivery="batch", access_mode="system", requester=REQUESTER,
                requested_at="2026-11-08T08:55:00")
    base.update(over)
    return Request(**base)


def auto(max_term_days=90) -> Decision:
    return Decision(outcome="auto_approve", step="use_check", reason_code=None, reason_text="Standing use.",
                    inputs={"pattern": PAT_IN, "permitted_use": pu_in(max_term_days)})


def pending() -> Decision:
    return Decision(outcome="pending_owner", step="use_check", reason_code=None, reason_text="Owner approval.",
                    inputs={"pattern": PAT_IN, "permitted_use": pu_in()})


def denied_pattern() -> Decision:
    return Decision(outcome="denied", step="pattern_check", reason_code="pattern_misaligned",
                    reason_text="No allowed architecture pattern.", inputs={"pattern": None, "permitted_use": None})


def denied_use() -> Decision:
    return Decision(outcome="denied", step="use_check", reason_code="use_not_permitted",
                    reason_text="Use is not permitted.", inputs={"pattern": PAT_IN, "permitted_use": None})


def denied_proxy() -> Decision:
    return Decision(outcome="denied", step="request_check", reason_code="proxy_not_implemented",
                    reason_text="Proxy mode is not implemented.", inputs={"pattern": None, "permitted_use": None})


def plan(decision, *, request=None, evidence=None, agreement=None, now=T1) -> Writes:
    return plan_writes(req() if request is None else request, decision, [] if evidence is None else evidence,
                       agreement, actor=ACTOR, now=now)


def dt(v) -> datetime:
    return v if isinstance(v, datetime) else datetime.fromisoformat(str(v))


def steps(w: Writes) -> list[str]:
    return [r["step"] for r in w.evidence]


def actions(w: Writes) -> list[str]:
    return [a[2] for a in w.audit]


# --- Writes shape ----------------------------------------------------------------------------------------------


def test_writes_is_frozen_with_fields():
    w = plan(auto())
    assert [f.name for f in dataclasses.fields(Writes)] == ["evidence", "agreement", "status", "audit"]
    with pytest.raises(dataclasses.FrozenInstanceError):
        w.status = "x"  # type: ignore[misc]


# --- evidence rows: one per step reached -----------------------------------------------------------------------


@pytest.mark.parametrize("decision, expected", [
    (denied_proxy(), ["request_check"]),
    (denied_pattern(), ["pattern_check"]),
    (denied_use(), ["pattern_check", "use_check"]),
    (pending(), ["pattern_check", "use_check"]),
    (auto(), ["pattern_check", "use_check", "agreement"]),
])
def test_evidence_one_row_per_step_reached(decision, expected):
    assert steps(plan(decision)) == expected


def test_evidence_ids_are_event_id_of_request_and_step():
    w = plan(auto())
    assert [r["decision_id"] for r in w.evidence] == [event_id_of(RID, s) for s in
                                                       ("pattern_check", "use_check", "agreement")]
    w = plan(denied_proxy())
    assert w.evidence[0]["decision_id"] == event_id_of(RID, "request_check")


@pytest.mark.parametrize("decision", [denied_proxy(), denied_pattern(), denied_use(), pending(), auto()])
def test_evidence_row_fields(decision):
    w = plan(decision)
    for r in w.evidence:
        assert r["request_id"] == RID
        assert r["decided_by"] == ACTOR
        assert dt(r["decided_at"]) == T1
        assert json.loads(r["inputs_json"]) == decision.inputs
        assert SECRET_NOTE not in json.dumps(r, default=str)


@pytest.mark.parametrize("decision", [denied_proxy(), denied_pattern(), denied_use(), pending()])
def test_final_evidence_row_carries_the_decision(decision):
    last = plan(decision).evidence[-1]
    assert last["step"] == decision.step
    assert last["outcome"] == decision.outcome
    assert last["reason_code"] == decision.reason_code
    assert last["reason_text"] == decision.reason_text


def test_use_check_row_carries_auto_approve():
    w = plan(auto())
    use_row = [r for r in w.evidence if r["step"] == "use_check"][0]
    assert use_row["outcome"] == "auto_approve"
    assert use_row["reason_code"] is None


# --- status ----------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("decision, status", [
    (auto(), "approved"), (pending(), "pending_owner"), (denied_pattern(), "denied"),
    (denied_use(), "denied"), (denied_proxy(), "denied"),
])
def test_status_follows_outcome(decision, status):
    assert plan(decision).status == status


# --- agreement -------------------------------------------------------------------------------------------------


def test_agreement_on_auto_approve():
    w = plan(auto(max_term_days=90))
    a = w.agreement
    assert a is not None
    assert a["agreement_id"] == event_id_of("agreement", RID)
    assert a["request_id"] == RID
    assert (a["consumer"], a["product"], a["use_code"]) == (CONSUMER, PRODUCT, USE)
    assert (a["access_mode"], a["use_mode"], a["delivery"]) == ("system", "analytical", "batch")
    assert a["decided_by"] == "auto"
    assert a["status"] == "active"
    assert a["version"] == 1
    assert dt(a["valid_from"]) == T1
    assert dt(a["valid_to"]) == T1 + timedelta(days=90)


def test_agreement_term_defaults_to_365_days():
    a = plan(auto(max_term_days=None)).agreement
    assert dt(a["valid_to"]) == T1 + timedelta(days=365)


@pytest.mark.parametrize("decision", [pending(), denied_pattern(), denied_use(), denied_proxy()])
def test_no_agreement_unless_auto_approve(decision):
    w = plan(decision)
    assert w.agreement is None
    assert "agreement" not in steps(w)
    assert "agreement_registered" not in actions(w)


# --- audit -----------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("decision", [auto(), pending(), denied_pattern(), denied_proxy()])
def test_audit_row_shape(decision):
    for a in plan(decision).audit:
        assert len(a) == 7
        event_id, gate, _action, actor, obj, _before, _after = a
        assert event_id
        assert gate == "consumption"
        assert actor == ACTOR
        assert obj == RID


def test_audit_on_first_auto_approve():
    w = plan(auto())
    acts = actions(w)
    assert acts.count("request_decided") == 1
    assert acts.count("agreement_registered") == 1
    status_rows = [a for a in w.audit if a[2] == "request_status_set"]
    assert [a[0] for a in status_rows] == [event_id_of(RID, "status", "approved")]


@pytest.mark.parametrize("decision, status", [(pending(), "pending_owner"), (denied_use(), "denied")])
def test_audit_status_id_per_status(decision, status):
    w = plan(decision)
    assert actions(w).count("request_decided") == 1
    assert [a[0] for a in w.audit if a[2] == "request_status_set"] == [event_id_of(RID, "status", status)]


def test_audit_ids_unique_within_plan():
    ids = [a[0] for a in plan(auto()).audit]
    assert len(ids) == len(set(ids))


def test_audit_never_carries_free_text_note():
    for d in (auto(), pending(), denied_use()):
        assert SECRET_NOTE not in json.dumps(plan(d).audit, default=str)


# --- determinism and replay ------------------------------------------------------------------------------------


def test_same_inputs_same_writes():
    assert plan(auto()) == plan(auto())


def first_run(decision):
    w = plan(decision, now=T1)
    return w, list(w.evidence), w.agreement


def test_replay_emits_nothing_new():
    first, evidence, agreement = first_run(auto())
    again = plan(auto(), evidence=evidence, agreement=agreement, now=T2)
    assert again.evidence == []
    assert again.agreement is None
    assert again.status == "approved"
    # A crash between the evidence and audit writes would otherwise lose the request_decided row for good, so the
    # replay re-emits it; its id is the first run's (insert-only MERGE dedupes), so nothing is added twice.
    assert actions(again).count("request_decided") == 1
    # Same for agreement_registered: the agreement may have landed before the crash while its audit row did not.
    assert [a[0] for a in again.audit if a[2] == "agreement_registered"] == [event_id_of(RID, "agreement_registered")]
    assert {a[0] for a in again.audit} <= {a[0] for a in first.audit}


def test_replay_rebuilds_from_stored_evidence_not_new_decision():
    _, evidence, agreement = first_run(auto())
    again = plan(denied_pattern(), evidence=evidence, agreement=agreement, now=T2)
    assert again.status == "approved"
    assert again.evidence == []
    assert again.agreement is None


def test_replay_pending_stays_pending_when_new_decision_would_auto_approve():
    _, evidence, _ = first_run(pending())
    again = plan(auto(), evidence=evidence, agreement=None, now=T2)
    assert again.status == "pending_owner"
    assert again.evidence == []
    assert again.agreement is None


def test_replay_denied_stays_denied():
    _, evidence, _ = first_run(denied_use())
    again = plan(auto(), evidence=evidence, agreement=None, now=T2)
    assert again.status == "denied"
    assert again.evidence == []
    assert again.agreement is None


def test_replay_status_audit_id_is_stable():
    _, evidence, agreement = first_run(auto())
    again = plan(auto(), evidence=evidence, agreement=agreement, now=T2)
    ids = [a[0] for a in again.audit if a[2] == "request_status_set"]
    assert set(ids) <= {event_id_of(RID, "status", "approved")}


def test_replay_missing_agreement_is_reemitted_with_first_write_times():
    # evidence landed on the first run, the agreement write did not
    _, evidence, _ = first_run(auto(max_term_days=90))
    again = plan(auto(max_term_days=30), evidence=evidence, agreement=None, now=T2)
    assert again.evidence == []
    assert again.status == "approved"
    a = again.agreement
    assert a is not None
    assert a["agreement_id"] == event_id_of("agreement", RID)
    assert dt(a["valid_from"]) == T1, "valid_from comes from stored evidence, never from now"
    assert dt(a["valid_to"]) == T1 + timedelta(days=90), "term comes from the stored inputs, not the new decision"
    assert actions(again).count("agreement_registered") == 1
    assert [a[0] for a in again.audit if a[2] == "request_decided"] == [event_id_of(RID, "decided", "auto_approve")]


def test_replay_partial_evidence_does_not_reemit_stored_rows():
    _, evidence, _ = first_run(auto())
    stored_ids = {r["decision_id"] for r in evidence}
    again = plan(auto(), evidence=evidence[:2], agreement=None, now=T2)
    assert all(r["decision_id"] not in {e["decision_id"] for e in evidence[:2]} for r in again.evidence)
    for r in again.evidence:
        assert r["decision_id"] in stored_ids
        assert dt(r["decided_at"]) == T1


def test_replay_does_not_mutate_stored_evidence():
    _, evidence, agreement = first_run(auto())
    snapshot = json.dumps(evidence, default=str, sort_keys=True)
    plan(denied_pattern(), evidence=evidence, agreement=agreement, now=T2)
    assert json.dumps(evidence, default=str, sort_keys=True) == snapshot


# --- request_id_for --------------------------------------------------------------------------------------------


def test_request_id_for_is_deterministic():
    a = request_id_for(CONSUMER, PRODUCT, USE, "ref-1")
    assert isinstance(a, str) and a
    assert a == request_id_for(CONSUMER, PRODUCT, USE, "ref-1")


@pytest.mark.parametrize("args", [
    (CONSUMER, PRODUCT, USE, "ref-2"),
    ("0000c1c1-0000-4000-8000-000000000002", PRODUCT, USE, "ref-1"),
    (CONSUMER, "cat.raw.policies", USE, "ref-1"),
    (CONSUMER, PRODUCT, "marketing", "ref-1"),
])
def test_request_id_for_differs_on_any_part(args):
    assert request_id_for(*args) != request_id_for(CONSUMER, PRODUCT, USE, "ref-1")


def test_replay_with_stored_agreement_still_audits_it_from_the_stored_row():
    # crash after evidence + agreement, before any audit row: the replay must still audit the stored agreement
    _, evidence, agreement = first_run(auto(max_term_days=90))
    again = plan(auto(max_term_days=30), evidence=evidence, agreement=agreement, now=T2)
    rows = [a for a in again.audit if a[2] == "agreement_registered"]
    assert len(rows) == 1 and rows[0][0] == event_id_of(RID, "agreement_registered")
    after = json.loads(rows[0][6])
    assert after == agreement_state(as_agreement(agreement)), "audited from the stored agreement, not recomputed"
    assert dt(after["valid_to"]) == (T1 + timedelta(days=90)).replace(tzinfo=UTC)
    assert again.agreement is None


@pytest.mark.parametrize("keep", [slice(0, 1), slice(1, 2)])
def test_incomplete_stored_evidence_fails_closed(keep):
    # pattern_check "pass" alone (or use_check alone) never happens from one MERGE; if it does, refuse to guess
    _, evidence, _ = first_run(auto())
    with pytest.raises(ValueError, match="incomplete"):
        plan(auto(), evidence=evidence[keep], agreement=None, now=T2)


def test_lone_pattern_denial_is_complete_evidence():
    _, evidence, _ = first_run(denied_pattern())
    assert plan(auto(), evidence=evidence, now=T2).status == "denied"


def test_unknown_stored_outcome_fails_closed_as_value_error():
    _, evidence, _ = first_run(pending())
    tampered = [dict(r, outcome="pass") if r["step"] == "use_check" else r for r in evidence]
    with pytest.raises(ValueError, match="outcome"):
        plan(auto(), evidence=tampered, now=T2)


# --- agreement_registered carries the full agreement state (Slice B: grants verify against it) -----------------


# gates.consumption.grants is imported inside these tests so a missing module fails only them, not this file.


def agreement_state(a):
    from gates.consumption.grants import agreement_state as state
    return state(a)


def as_agreement(row: dict):
    from gates.consumption.grants import Agreement
    return Agreement(**{f.name: row[f.name] for f in dataclasses.fields(Agreement)})


def registered_after(w: Writes) -> dict:
    (row,) = [a for a in w.audit if a[2] == "agreement_registered"]
    return json.loads(row[6])


def test_agreement_registered_after_is_the_full_agreement_state():
    w = plan(auto(max_term_days=90))
    assert registered_after(w) == agreement_state(as_agreement(w.agreement))


def test_agreement_registered_after_keeps_the_original_fields():
    after = registered_after(plan(auto(max_term_days=90)))
    assert after["agreement_id"] == event_id_of("agreement", RID)
    assert after["access_mode"] == "system"
    assert dt(after["valid_from"]) == T1.replace(tzinfo=UTC)
    assert dt(after["valid_to"]) == (T1 + timedelta(days=90)).replace(tzinfo=UTC)
    assert (after["consumer"], after["product"], after["status"], after["version"]) == (CONSUMER, PRODUCT, "active", 1)


def test_agreement_registered_after_is_the_same_whether_stored_row_is_naive_or_aware():
    # gov reads timestamps back naive (UTC); the replay audit must equal the first run's
    first, evidence, agreement = first_run(auto())
    aware = dict(agreement, valid_from=dt(agreement["valid_from"]).replace(tzinfo=UTC),
                 valid_to=dt(agreement["valid_to"]).replace(tzinfo=UTC))
    again = plan(auto(), evidence=evidence, agreement=aware, now=T2)
    assert registered_after(again) == registered_after(first)


def test_registered_agreement_verifies_live_for_the_grants_generator():
    w = plan(auto(max_term_days=90))
    from gates.consumption.grants import verify_agreements
    a = as_agreement(w.agreement)
    live, refused = verify_agreements(
        [a], newest_audit={a.agreement_id: registered_after(w)},
        evidence_steps={RID: {r["step"] for r in w.evidence}},
        requests={RID: {"request_id": RID, "consumer": CONSUMER, "securable": PRODUCT}},
        now=(T1 + timedelta(days=1)).replace(tzinfo=UTC))
    assert [x.agreement_id for x in live] == [a.agreement_id] and refused == []


# --- resubmission with the same request_ref (lead's decision: a changed purpose note is refused, not a new
# request) and the request_received audit state. New functions are imported inside the tests so the rest of this
# module keeps running before they exist.


def submitted(**over) -> dict:
    base = dict(consumer=CONSUMER, product=PRODUCT, intended_use=USE, use_mode="analytical", delivery="batch",
                access_mode="system", requester=REQUESTER, reason=SECRET_NOTE)
    base.update(over)
    return base


def test_submission_conflicts_none_when_identical():
    from gates.consumption.requests import submission_conflicts
    assert submission_conflicts(submitted(), submitted()) == []


@pytest.mark.parametrize("stored, new", [
    ({"reason": None}, {"reason": ""}),
    ({"reason": ""}, {"reason": None}),
    ({"reason": SECRET_NOTE}, {"reason": f"  {SECRET_NOTE}\n"}),
    ({"requester": REQUESTER}, {"requester": f" {REQUESTER} "}),
])
def test_submission_conflicts_ignore_none_vs_empty_and_whitespace(stored, new):
    from gates.consumption.requests import submission_conflicts
    assert submission_conflicts(submitted(**stored), submitted(**new)) == []


@pytest.mark.parametrize("field", ["requester", "use_mode", "delivery", "access_mode", "reason"])
def test_submission_conflicts_names_each_changed_field(field):
    from gates.consumption.requests import submission_conflicts
    assert submission_conflicts(submitted(), submitted(**{field: "changed"})) == [field]


def test_submission_conflicts_lists_every_changed_field():
    from gates.consumption.requests import submission_conflicts
    got = submission_conflicts(submitted(), submitted(reason="a different purpose", delivery="stream"))
    assert sorted(got) == ["delivery", "reason"]


def test_received_state_fields_and_reason_hash_not_text():
    import hashlib

    from gates.consumption.requests import received_state
    state = received_state(submitted(reason=f"  {SECRET_NOTE} "), "ref-1")
    assert state == {"consumer": CONSUMER, "product": PRODUCT, "intended_use": USE, "use_mode": "analytical",
                     "delivery": "batch", "access_mode": "system", "requester": REQUESTER, "status": "received",
                     "request_ref": "ref-1",
                     "reason_sha256": hashlib.sha256(SECRET_NOTE.encode("utf-8")).hexdigest()}
    assert SECRET_NOTE not in json.dumps(state)


def test_received_state_none_reason_hashes_empty_string():
    import hashlib

    from gates.consumption.requests import received_state
    assert received_state(submitted(reason=None), "ref-1")["reason_sha256"] == hashlib.sha256(b"").hexdigest()


def test_received_state_is_deterministic():
    from gates.consumption.requests import received_state
    assert received_state(submitted(), "ref-1") == received_state(submitted(), "ref-1")
