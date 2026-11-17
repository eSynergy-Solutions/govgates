"""Permitted-use capture at hydration approval (specs/hydration.md step 6 H15; specs/consumption.md Scope "Out"
bridge, "Decisions (8 Nov)" item 4; specs/contracts.md permitted_uses).

Pure: no Spark. plan_permitted_uses turns owner rows into new gov.permitted_uses versions. Rows are never deleted:
the previous active version is retired (active = false) and a new version written. Identity is the committer.

Fixture conventions (chosen here): the allowed use-code list is a tiny local tuple, not the config list.
"""

from __future__ import annotations

import json
from datetime import datetime

import pytest

from gates.consumption.decide import PermittedUse
from gates.consumption.permitted import UseInput, UsePlan, has_permitted_uses, plan_permitted_uses
from gates.hydration.apply import event_id_of

PRODUCT = "cat.raw.claims"
OWNER = "owner.a@example.com"
OWNER_B = "owner.b@example.com"
OTHER = "someone.else@example.com"
JOB = "ci-sp-0000"
USE = "claims_reporting"
ALLOWED = ("claims_reporting", "fraud_analytics", "marketing")
TYPED = datetime(2026, 11, 1, 8, 0, 0)
CT = datetime(2026, 11, 9, 10, 0, 0)
_SAME = object()


def inp(iid: str = "in-1", *, approval: str = "approval_required", conditions: str | None = None,
        term: int | None = 90, product: str = PRODUCT, use_code: str = USE, by: str = OWNER, committed_by=_SAME,
        v: int | None = 10, committed_at=CT, **kw) -> UseInput:
    return UseInput(input_id=iid, product=product, use_code=use_code, approval=approval, conditions=conditions,
                    max_term_days=term, decided_by=by, decided_at=TYPED,
                    committed_by=by if committed_by is _SAME else committed_by, committed_at=committed_at,
                    insert_version=v, **kw)


def pu(version: int = 1, *, approval: str = "approval_required", conditions: str | None = None, term: int | None = 90,
       active: bool = True, product: str = PRODUCT, use_code: str = USE) -> PermittedUse:
    return PermittedUse(product, use_code, approval, conditions, term, version, active)


def run(inputs, current=None, approvers=None) -> list[UsePlan]:
    return plan_permitted_uses(inputs, [] if current is None else current,
                               {PRODUCT: {OWNER, OWNER_B, JOB}} if approvers is None else approvers,
                               allowed_codes=ALLOWED, job_principal=JOB)


def one(inputs=None, **kw) -> UsePlan:
    [p] = run([inp()] if inputs is None else inputs, **kw)
    return p


def assert_refused(p: UsePlan, iid: str = "in-1", v: int = 10) -> None:
    assert p.kind == "refused"
    assert p.row is None and p.retire_versions == []
    assert p.reason and p.outcome.startswith("refused: ")
    assert [a[2] for a in p.audit] == ["permitted_use_refused"]
    assert p.audit[0][0] == event_id_of("permitted_use_refused", iid, v)


# --- hold and order --------------------------------------------------------------------------------------------


def test_held_input_plans_nothing():
    p = one([inp(hold=True)])
    assert (p.kind, p.row, p.retire_versions, p.audit, p.outcome) == ("held", None, [], [], None)


def test_output_in_insert_version_then_input_id_order():
    ins = [inp("b", v=5, use_code="marketing"), inp("c", v=3, use_code="fraud_analytics"), inp("a", v=5)]
    assert [p.input_id for p in run(ins)] == ["c", "a", "b"]


# --- identity refusals -----------------------------------------------------------------------------------------


@pytest.mark.parametrize("make", [
    lambda: inp(attribution_error="modified after insert"),
    lambda: inp(committed_by=None),
    lambda: inp(by=OWNER, committed_by=OWNER_B),
    lambda: inp(by=JOB),
    lambda: inp(by=OTHER),
], ids=["attribution_error", "committer_unknown", "typed_not_committer", "job_principal", "not_approver"])
def test_identity_refusals(make):
    assert_refused(one([make()]))


def test_approvers_are_per_product():
    assert_refused(one(approvers={"cat.raw.payments": {OWNER}}))


def test_typed_name_differing_only_in_case_is_accepted():
    assert one([inp(by="Owner.A@Example.com", committed_by=OWNER)]).kind == "set"


# --- validation ------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("over", [
    {"use_code": "resale"}, {"approval": "maybe"}, {"approval": None}, {"term": 0}, {"term": -30},
    {"use_code": ""}, {"use_code": "   "},
])
def test_invalid_input_refused(over):
    assert_refused(one([inp(**over)]))


def test_blank_product_refused():
    assert_refused(one([inp(product="")], approvers={PRODUCT: {OWNER}, "": {OWNER}}))


@pytest.mark.parametrize("approval", ["standing", "approval_required", "prohibited"])
def test_each_approval_value_is_accepted(approval):
    assert one([inp(approval=approval)]).row["approval"] == approval


def test_no_term_is_accepted():
    assert one([inp(term=None)]).kind == "set"


# --- set -------------------------------------------------------------------------------------------------------


def test_first_row_for_a_pair():
    p = one()
    assert (p.kind, p.outcome, p.reason, p.retire_versions) == ("set", "set", None, [])
    assert p.row == {"product": PRODUCT, "use_code": USE, "approval": "approval_required", "conditions": None,
                     "max_term_days": 90, "set_by": OWNER, "set_at": CT, "version": 1, "active": True}


def test_first_row_audit_matches_the_seed_event_id():
    p = one()
    [a] = p.audit
    assert (a[0], a[1], a[2]) == (event_id_of("permitted_use_set", PRODUCT, USE, 1), "consumption",
                                  "permitted_use_set")
    assert a[5] is None
    assert json.loads(a[6]) == json.loads(json.dumps(p.row, sort_keys=True, default=str))


def test_new_version_is_max_over_all_rows_plus_one_and_retires_active():
    current = [pu(1, active=False), pu(3, active=False, approval="standing"), pu(2, approval="prohibited"),
               pu(7, use_code="marketing")]
    p = one(current=current)
    assert p.row["version"] == 4 and p.row["active"] is True
    assert p.retire_versions == [2]
    [a] = p.audit
    assert a[0] == event_id_of("permitted_use_set", PRODUCT, USE, 4)
    before = json.loads(a[5])
    assert (before["version"], before["approval"]) == (2, "prohibited")


@pytest.mark.parametrize("over", [{"approval": "standing"}, {"conditions": "UK only"}, {"term": 30}])
def test_any_changed_value_sets_a_new_version(over):
    assert one([inp(**over)], current=[pu(1)]).kind == "set"


def test_same_values_as_active_row_is_unchanged():
    p = one([inp(conditions="UK only")], current=[pu(1, conditions="UK only")])
    assert (p.kind, p.row, p.retire_versions, p.audit, p.outcome) == ("unchanged", None, [], [], "unchanged")


def test_same_values_as_an_inactive_row_only_is_set():
    p = one(current=[pu(1, active=False)])
    assert p.kind == "set" and p.row["version"] == 2 and p.retire_versions == []


# --- two inputs for one pair in one batch ----------------------------------------------------------------------


def test_two_inputs_thread_state():
    ps = run([inp("in-2", approval="standing", v=11), inp("in-1", v=10)], current=[pu(1, approval="prohibited")])
    assert [p.input_id for p in ps] == ["in-1", "in-2"]
    assert [p.row["version"] for p in ps] == [2, 3]
    assert [p.retire_versions for p in ps] == [[1], [2]]


def test_second_identical_input_is_unchanged():
    ps = run([inp("in-1", v=10), inp("in-2", v=11)])
    assert [p.kind for p in ps] == ["set", "unchanged"]


# --- idempotency -----------------------------------------------------------------------------------------------


def test_same_inputs_same_plans():
    ins = [inp("in-1", v=10), inp("in-2", approval="standing", v=11), inp("in-3", by=OTHER, v=12),
           inp("in-4", use_code="marketing", approval="prohibited", v=13)]
    cur = [pu(1)]
    assert run(ins, cur) == run(ins, cur) == run(list(reversed(ins)), cur)


# --- has_permitted_uses ----------------------------------------------------------------------------------------


@pytest.mark.parametrize("current, expected", [
    ([], False),
    ([pu(1, active=False)], False),
    ([pu(1, product="cat.raw.payments")], False),
    ([pu(1)], True),
    ([pu(1, approval="prohibited")], True),
    ([pu(1, active=False), pu(2, approval="standing", use_code="marketing")], True),
])
def test_has_permitted_uses(current, expected):
    assert has_permitted_uses(PRODUCT, current) is expected


# --- reviewer must-fixes ---------------------------------------------------------------------------------------


def test_refusal_actor_is_committer_or_unknown_never_typed_name():
    p = one([inp(by="claimed.owner@example.com", committed_by=None)])
    assert_refused(p)
    assert p.audit[0][3] == "unknown"
    assert json.loads(p.audit[0][6])["claimed_decided_by"] == "claimed.owner@example.com"


def test_duplicate_input_ids_give_one_refusal_per_id():
    from gates.consumption.permitted import refuse_duplicates
    unique, refused = refuse_duplicates([inp("dup", v=10), inp("dup", v=11), inp("solo", v=12)])
    assert [i.input_id for i in unique] == ["solo"]
    assert len(refused) == 1 and refused[0].kind == "refused" and len(refused[0].audit) == 1
