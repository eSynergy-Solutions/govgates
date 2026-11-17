"""Owner decision on a pending consumption request (specs/consumption.md Flow 4-6, "Decision test matrix",
"Decisions (8 Nov)" item 3; Policy model `grant` "Live agreement"; specs/contracts.md consumption_decisions,
consumption_agreements, audit_log).

Pure: no Spark. plan_owner turns owner rows (decisions input) plus a read of each request into evidence, agreement,
status and audit writes. Identity comes from the Delta committer, never the typed name.

Fixture conventions (chosen here, not fixed by the spec):
- The prior decide() evidence and status audit are built with requests.plan_writes, as the job would have stored.
- The request_received audit is built by hand (keys per the approved interface), not via received_state().
- Principals, products and use codes are synthetic and made up here.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta

import pytest

from gates.consumption.decide import Decision, PermittedUse, Request
from gates.consumption.grants import agreement_from, agreement_state, verify_agreements
from gates.consumption.owner import OwnerDecision, OwnerPlan, RequestView, plan_owner
from gates.consumption.requests import plan_writes
from gates.hydration.apply import event_id_of

PRODUCT = "cat.raw.claims"
CONSUMER = "0000c1c1-0000-4000-8000-000000000001"
REQUESTER = "requester@example.com"
OWNER = "owner.a@example.com"
OTHER = "someone.else@example.com"
OWNER_B = "owner.b@example.com"
JOB = "ci-sp-0000"
USE = "claims_reporting"
RID = "req-0001"
SECRET = "purpose note SYNTHETIC-FREE-TEXT-7731"
T_REQ = datetime(2026, 11, 8, 9, 0, 0)
TYPED = datetime(2026, 11, 1, 8, 0, 0)  # typed decided_at: never trusted for time or order
CT = datetime(2026, 11, 9, 10, 0, 0)  # commit time
EVIDENCE_COLS = {"decision_id", "request_id", "step", "outcome", "reason_code", "reason_text", "note",
                 "decided_by", "decided_at", "inputs_json"}
_SAME = object()
_DEFAULT = object()


def sha(s: str | None) -> str:
    return hashlib.sha256((s or "").encode("utf-8")).hexdigest()


def request_row(**over) -> dict:
    base = dict(request_id=RID, requester=REQUESTER, consumer=CONSUMER, securable=PRODUCT, reason=SECRET,
                status="pending_owner", intended_use=USE, use_mode="analytical", delivery="batch",
                access_mode="system")
    base.update(over)
    return base


def received(**over) -> dict:
    base = dict(consumer=CONSUMER, product=PRODUCT, intended_use=USE, use_mode="analytical", delivery="batch",
                access_mode="system", requester=REQUESTER, status="received", request_ref="ref-1",
                reason_sha256=sha(SECRET))
    base.update(over)
    return base


def _req() -> Request:
    return Request(RID, CONSUMER, PRODUCT, USE, SECRET, "analytical", "batch", "system", REQUESTER,
                   "2026-11-08T08:55:00")


def _prior(outcome: str = "pending_owner") -> list[dict]:
    pat = {"pattern_id": "pat_analytical_batch_system", "version": 1}
    if outcome == "pending_owner":
        d = Decision("pending_owner", "use_check", None, "Owner approval.",
                     {"pattern": pat, "permitted_use": {"product": PRODUCT, "use_code": USE, "version": 1,
                                                        "max_term_days": 90}})
    else:
        d = Decision("denied", "use_check", "use_not_permitted", "Not permitted.",
                     {"pattern": pat, "permitted_use": None})
    return plan_writes(_req(), d, [], None, actor=JOB, now=T_REQ).evidence


def view(request=None, *, received_audit=_DEFAULT, status_audits=None, evidence=None, agreement=None) -> RequestView:
    return RequestView(request=request_row() if request is None else request,
                       received_audit=received() if received_audit is _DEFAULT else received_audit,
                       status_audits=[{"status": "pending_owner"}] if status_audits is None else status_audits,
                       evidence=_prior() if evidence is None else evidence, agreement=agreement)


def dec(did: str = "d1", action: str = "approve", *, reason=None, note=None, by: str = OWNER, committed_by=_SAME,
        committed_at=CT, v: int | None = 10, request_id: str = RID, decided_at=TYPED, **kw) -> OwnerDecision:
    return OwnerDecision(decision_id=did, request_id=request_id, action=action, reason=reason, note=note,
                         decided_by=by, decided_at=decided_at,
                         committed_by=by if committed_by is _SAME else committed_by, committed_at=committed_at,
                         insert_version=v, **kw)


def use(approval: str = "approval_required", term: int | None = 90, version: int = 2, active: bool = True,
        product: str = PRODUCT, use_code: str = USE) -> PermittedUse:
    return PermittedUse(product, use_code, approval, None, term, version, active)


def run(decisions, views=None, approvers=None, uses=None) -> list[OwnerPlan]:
    return plan_owner(decisions, {RID: view()} if views is None else views,
                      {PRODUCT: {OWNER}} if approvers is None else approvers,
                      [use()] if uses is None else uses, job_principal=JOB)


def one(decisions=None, **kw) -> OwnerPlan:
    [p] = run([dec()] if decisions is None else decisions, **kw)
    return p


def actions(p: OwnerPlan) -> list[str]:
    return [a[2] for a in p.audit]


def audit_of(p: OwnerPlan, action: str) -> tuple:
    [row] = [a for a in p.audit if a[2] == action]
    return row


def owner_id(did: str = "d1", v: int = 10) -> str:
    return event_id_of(RID, "owner", did, v)


def assert_refused(p: OwnerPlan, did: str = "d1", v: int = 10) -> None:
    assert p.kind == "refused"
    assert p.evidence == [] and p.agreement is None and p.new_status is None
    assert p.outcome is not None and p.outcome.startswith("refused: ")
    assert p.reason
    assert actions(p) == ["owner_decision_refused"]
    assert p.audit[0][0] == event_id_of("owner_decision_refused", did, v)
    assert p.audit[0][1] == "consumption"


# --- hold and order --------------------------------------------------------------------------------------------


def test_held_decision_plans_nothing():
    p = one([dec(hold=True)])
    assert (p.kind, p.evidence, p.agreement, p.audit, p.outcome, p.new_status) == ("held", [], None, [], None, None)


def test_plans_in_insert_version_then_decision_id_order_not_typed_time():
    ds = [dec("b", "note", note="n", v=5, decided_at=TYPED),
          dec("c", "note", note="n", v=3, decided_at=TYPED + timedelta(days=3)),
          dec("a", "note", note="n", v=5, decided_at=TYPED + timedelta(days=1))]
    assert [p.decision_id for p in run(ds)] == ["c", "a", "b"]


# --- identity refusals -----------------------------------------------------------------------------------------


@pytest.mark.parametrize("action, extra", [("approve", {}), ("reject", {"reason": "not needed"}),
                                           ("note", {"note": "looked at it"})])
@pytest.mark.parametrize("case", ["attribution_error", "committer_unknown", "typed_not_committer", "job_principal",
                                  "not_approver", "self_approval", "consumer"])
def test_identity_refusals(action, extra, case):
    approvers = {PRODUCT: {OWNER, OWNER_B, JOB, REQUESTER, CONSUMER}}
    d = {"attribution_error": dec(action=action, attribution_error="modified after insert", **extra),
         "committer_unknown": dec(action=action, committed_by=None, **extra),
         "typed_not_committer": dec(action=action, by=OWNER, committed_by=OWNER_B, **extra),
         "job_principal": dec(action=action, by=JOB, **extra),
         "not_approver": dec(action=action, by=OTHER, **extra),
         "self_approval": dec(action=action, by=REQUESTER, **extra),
         "consumer": dec(action=action, by=CONSUMER, **extra)}[case]
    p = one([d], approvers=approvers)
    assert_refused(p)
    if case == "self_approval":
        assert "self" in p.reason.lower()


def test_typed_name_differing_only_in_case_is_accepted():
    p = one([dec(by="Owner.A@Example.com", committed_by=OWNER)])
    assert p.kind == "approved"


@pytest.mark.parametrize("action, extra", [("approve", {}), ("note", {"note": "n"})])
def test_unknown_request_refused(action, extra):
    assert_refused(one([dec(action=action, request_id="req-missing", **extra)]))


def test_approvers_are_per_product():
    assert_refused(one(approvers={"cat.raw.payments": {OWNER}}))


# --- action validation -----------------------------------------------------------------------------------------


def test_unknown_action_refused():
    assert_refused(one([dec(action="escalate")]))


@pytest.mark.parametrize("text", [None, "", "   "])
def test_note_without_text_refused(text):
    assert_refused(one([dec(action="note", note=text)]))


@pytest.mark.parametrize("text", [None, "", "   "])
def test_reject_without_reason_refused(text):
    assert_refused(one([dec(action="reject", reason=text)]))


# --- tamper and state checks (approve and reject) --------------------------------------------------------------


ACTS = [("approve", {}), ("reject", {"reason": "no longer needed"})]


@pytest.mark.parametrize("action, extra", ACTS)
@pytest.mark.parametrize("status", ["received", "approved", "denied", "rejected", "withdrawn"])
def test_status_not_pending_owner_refused(action, extra, status):
    assert_refused(one([dec(action=action, **extra)], views={RID: view(request_row(status=status))}))


@pytest.mark.parametrize("action, extra", ACTS)
def test_no_received_audit_refused(action, extra):
    assert_refused(one([dec(action=action, **extra)], views={RID: view(received_audit=None)}))


@pytest.mark.parametrize("action, extra", ACTS)
@pytest.mark.parametrize("field", ["requester", "consumer", "product", "intended_use", "use_mode", "delivery",
                                   "access_mode"])
def test_request_row_differs_from_received_audit_refused(action, extra, field):
    v = view(received_audit=received(**{field: "tampered-value"}))
    assert_refused(one([dec(action=action, **extra)], views={RID: v}))


@pytest.mark.parametrize("action, extra", ACTS)
@pytest.mark.parametrize("rec, req", [
    ({k: v for k, v in received().items() if k != "reason_sha256"}, request_row()),
    (received(reason_sha256=sha("another purpose")), request_row()),
    (received(), request_row(reason="purpose edited by hand")),
])
def test_reason_hash_missing_or_mismatched_refused(action, extra, rec, req):
    assert_refused(one([dec(action=action, **extra)], views={RID: view(req, received_audit=rec)}))


def test_reason_none_hashes_as_empty_string():
    v = view(request_row(reason=None), received_audit=received(reason_sha256=sha("")))
    assert one(views={RID: v}).kind == "approved"


@pytest.mark.parametrize("action, extra", ACTS)
def test_no_pending_owner_status_audit_refused(action, extra):
    assert_refused(one([dec(action=action, **extra)], views={RID: view(status_audits=[])}))


@pytest.mark.parametrize("action, extra", ACTS)
def test_no_pending_owner_use_check_evidence_refused(action, extra):
    assert_refused(one([dec(action=action, **extra)], views={RID: view(evidence=[])}))


@pytest.mark.parametrize("action, extra", ACTS)
def test_status_hand_set_to_pending_owner_from_denied_refused(action, extra):
    v = view(status_audits=[{"status": "denied"}], evidence=_prior("denied"))
    assert_refused(one([dec(action=action, **extra)], views={RID: v}))


# --- note ------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("status", ["pending_owner", "approved", "denied"])
def test_note_at_any_status(status):
    p = one([dec(action="note", note="Checked with the consumer.")], views={RID: view(request_row(status=status))})
    assert (p.kind, p.new_status, p.agreement, p.outcome) == ("noted", None, None, "noted")
    [row] = p.evidence
    assert set(row) == EVIDENCE_COLS
    assert (row["decision_id"], row["step"], row["outcome"], row["note"]) == (owner_id(), "owner", "note",
                                                                              "Checked with the consumer.")
    assert actions(p) == ["owner_decision"]


# --- approve ---------------------------------------------------------------------------------------------------


def test_approve_plan():
    p = one()
    assert (p.kind, p.new_status, p.approver, p.outcome, p.reason) == ("approved", "approved", OWNER, "approved",
                                                                         None)
    assert (p.decision_id, p.request_id) == ("d1", RID)


def test_approve_evidence_rows():
    owner, agreement = one().evidence
    assert set(owner) == set(agreement) == EVIDENCE_COLS
    assert (owner["decision_id"], owner["request_id"], owner["step"], owner["outcome"]) == (
        owner_id(), RID, "owner", "approved")
    assert (owner["decided_by"], owner["decided_at"]) == (OWNER, CT)
    pu = json.loads(owner["inputs_json"])["permitted_use"]
    assert {k: pu[k] for k in ("product", "use_code", "version", "max_term_days")} == {
        "product": PRODUCT, "use_code": USE, "version": 2, "max_term_days": 90}
    assert (agreement["decision_id"], agreement["step"], agreement["outcome"]) == (
        event_id_of(RID, "agreement"), "agreement", "approved")


def test_approve_agreement_row_uses_commit_time_not_typed_time():
    assert one().agreement == {
        "agreement_id": event_id_of("agreement", RID), "request_id": RID, "consumer": CONSUMER, "product": PRODUCT,
        "use_code": USE, "access_mode": "system", "use_mode": "analytical", "delivery": "batch",
        "decided_by": OWNER, "status": "active", "valid_from": CT, "valid_to": CT + timedelta(days=90),
        "version": 1}


def test_approve_audit_rows():
    p = one()
    assert {"owner_decision", "agreement_registered", "request_status_set"} <= set(actions(p))
    reg = audit_of(p, "agreement_registered")
    assert reg[0] == event_id_of(RID, "agreement_registered")
    assert json.loads(reg[6]) == agreement_state(agreement_from(p.agreement))
    st = audit_of(p, "request_status_set")
    assert st[0] == event_id_of(RID, "status", "approved")
    assert (json.loads(st[5]), json.loads(st[6])) == ({"status": "pending_owner"}, {"status": "approved"})
    assert all(a[1] == "consumption" for a in p.audit)
    assert len({a[0] for a in p.audit}) == len(p.audit)


def test_audit_never_carries_free_text_purpose():
    for p in run([dec(), dec("d2", "note", note="n", v=11)]):
        assert all(SECRET not in (a[5] or "") + (a[6] or "") for a in p.audit)


def test_approved_agreement_is_live_for_the_grants_generator():
    p = one()
    a = agreement_from(p.agreement)
    live, refused = verify_agreements(
        [a], newest_audit={a.agreement_id: json.loads(audit_of(p, "agreement_registered")[6])},
        evidence_steps={RID: {r["step"] for r in _prior() + p.evidence}}, requests={RID: request_row()},
        now=(CT + timedelta(days=1)).replace(tzinfo=UTC))
    assert [x.agreement_id for x in live] == [a.agreement_id] and refused == []


# --- re-check of the permitted use at approve ------------------------------------------------------------------


@pytest.mark.parametrize("uses", [
    [],
    [use(approval="prohibited")],
    [use(active=False)],
    [use(active=False, version=2), use(approval="prohibited", version=3)],
    [use(product="cat.raw.payments")],
    [use(use_code="marketing")],
])
def test_use_no_longer_permitted_refused(uses):
    p = one(uses=uses)
    assert_refused(p)
    assert "use_no_longer_permitted" in p.reason


def test_standing_use_still_allows_owner_approve():
    assert one(uses=[use(approval="standing")]).kind == "approved"


def test_two_active_use_rows_refused():
    assert_refused(one(uses=[use(version=2), use(version=3, term=30)]))


@pytest.mark.parametrize("term", [0, -5])
def test_invalid_term_refused(term):
    assert_refused(one(uses=[use(term=term)]))


def test_term_comes_from_the_active_row():
    p = one(uses=[use(term=30, version=1, active=False), use(term=120, version=2)])
    assert p.agreement["valid_to"] == CT + timedelta(days=120)


def test_term_none_defaults_to_365_days():
    assert one(uses=[use(term=None)]).agreement["valid_to"] == CT + timedelta(days=365)


# --- reject ----------------------------------------------------------------------------------------------------


def test_reject_plan():
    p = one([dec(action="reject", reason="Purpose not covered by the product.")])
    assert (p.kind, p.new_status, p.approver, p.outcome, p.agreement) == ("rejected", "rejected", OWNER, "rejected",
                                                                            None)
    [row] = p.evidence
    assert (row["decision_id"], row["step"], row["outcome"], row["reason_text"], row["decided_by"]) == (
        owner_id(), "owner", "rejected", "Purpose not covered by the product.", OWNER)
    st = audit_of(p, "request_status_set")
    assert st[0] == event_id_of(RID, "status", "rejected")
    assert (json.loads(st[5]), json.loads(st[6])) == ({"status": "pending_owner"}, {"status": "rejected"})
    assert "agreement_registered" not in actions(p)


# --- two decisions on one request in one batch -----------------------------------------------------------------


def test_note_then_approve_both_apply():
    ps = run([dec("d1", "note", note="n", v=10), dec("d2", "approve", v=11)])
    assert [p.kind for p in ps] == ["noted", "approved"]
    ids = [r["decision_id"] for p in ps for r in p.evidence]
    assert len(set(ids)) == len(ids) and owner_id("d1", 10) in ids and owner_id("d2", 11) in ids


@pytest.mark.parametrize("second", [dec("d2", "approve", v=11), dec("d2", "reject", reason="changed mind", v=11)])
def test_second_decision_after_approve_is_refused(second):
    ps = run([second, dec("d1", "approve", v=10)])
    assert [p.kind for p in ps] == ["approved", "refused"]
    assert_refused(ps[1], "d2", 11)


# --- replay ----------------------------------------------------------------------------------------------------


def _stored_after(first: OwnerPlan, *, agreement=True, owner_decided_at=None) -> RequestView:
    ev = [dict(r) for r in first.evidence]
    if owner_decided_at is not None:
        ev = [dict(r, decided_at=owner_decided_at) if r["step"] == "owner" else r for r in ev]
    return view(request_row(status="approved"), status_audits=[{"status": "pending_owner"}, {"status": "approved"}],
                evidence=_prior() + ev, agreement=first.agreement if agreement else None)


def test_replay_reemits_audit_only():
    first = one()
    again = one(views={RID: _stored_after(first)})
    assert again.kind == "approved"
    assert again.evidence == [] and again.agreement is None
    assert sorted(a[0] for a in again.audit) == sorted(a[0] for a in first.audit)


def test_replay_finishes_even_if_approver_removed_since():
    # reviewer should-fix: the owner evidence row is written only after every check passed, so a crashed run is
    # finished (status, agreement, audit) rather than left half-applied with a live agreement and a refused stamp
    first = one()
    again = one(views={RID: _stored_after(first)}, approvers={PRODUCT: {OTHER}})
    assert again.kind == "approved"
    assert sorted(a[0] for a in again.audit) == sorted(a[0] for a in first.audit)


def test_replay_needs_a_known_request():
    first = one()
    assert_refused(one([dec()], views={"other": _stored_after(first)}))


# --- reviewer must-fixes ---------------------------------------------------------------------------------------


@pytest.mark.parametrize("action, extra", ACTS)
@pytest.mark.parametrize("terminal", ["approved", "rejected", "denied", "withdrawn"])
def test_decided_request_reset_to_pending_owner_by_hand_refused(action, extra, terminal):
    v = view(status_audits=[{"status": "pending_owner"}, {"status": terminal}])
    p = one([dec(action=action, **extra)], views={RID: v})
    assert_refused(p)
    assert terminal in p.reason


def test_refusal_actor_is_committer_or_unknown_never_typed_name():
    p = one([dec(by="claimed.owner@example.com", committed_by=None)])
    assert_refused(p)
    assert p.audit[0][3] == "unknown"
    assert json.loads(p.audit[0][6])["claimed_decided_by"] == "claimed.owner@example.com"


def test_duplicate_decision_ids_give_one_refusal_per_id():
    from gates.consumption.owner import refuse_duplicates
    ds = [dec("dup", v=10), dec("dup", v=11), dec("dup", v=12), dec("solo", v=13)]
    unique, refused = refuse_duplicates(ds)
    assert [d.decision_id for d in unique] == ["solo"]
    assert len(refused) == 1 and refused[0].kind == "refused" and "duplicate" in refused[0].reason
    assert len(refused[0].audit) == 1


def test_replay_missing_agreement_rebuilt_from_stored_owner_time():
    first = one()
    stored_at = CT - timedelta(hours=2)
    again = one(views={RID: _stored_after(first, agreement=False, owner_decided_at=stored_at)})
    assert again.agreement["agreement_id"] == event_id_of("agreement", RID)
    assert again.agreement["valid_from"] == stored_at
    assert again.agreement["valid_to"] == stored_at + timedelta(days=90)
    assert "owner" not in [r["step"] for r in again.evidence]


# --- idempotency -----------------------------------------------------------------------------------------------


def test_same_inputs_same_plans():
    ds = [dec("d1", "note", note="n", v=9), dec("d2", v=10), dec("d3", "reject", reason="r", v=11),
          dec("d4", by=OTHER, v=12)]
    assert run(ds) == run(ds)
    assert run(ds) == run(list(reversed(ds)))
