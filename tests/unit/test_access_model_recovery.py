"""Reviewer must-fix 1, 3, 4, 5 (9 Nov): interrupted runs, hand edits, config-owned mask classes, isolated members.

Stored state that differs from its newest audit is repaired by the next run through the audited path; a crash
after the audit is completed with the same event id; the model can't remove the pii mask; an isolated member in
any group (declared in the model or not) fails.
"""

from __future__ import annotations

from gates.consumption import access_model as am
from gates.consumption.policy_rows import row_dict, rows_change
from gates.consumption.row_filter import PolicyRow

HP = "ci-sp"
DR = dict(catalog="cat", gov_schema="gov", sensitivity_tag="gg_sensitivity", rls_tag="gg_rls_attribute",
          hydration_principal=HP)
RC = dict(catalog="cat", rls_tag="gg_rls_attribute", sensitivity_tag="gg_sensitivity", hydration_principal=HP,
          gov_schema="gov", allow_reserved=True)


def rf(excepts=(HP,)) -> PolicyRow:
    return PolicyRow("rf_raw", "row_filter", "cat.raw", "gg_rls_attribute", "region", "cat.gov.rls_region_filter",
                     excepts, True)


def mk(excepts=(HP,)) -> PolicyRow:
    return PolicyRow("mk_raw_pii", "mask", "cat.raw", "gg_sensitivity", "pii", "cat.gov.mask_pii", excepts, True)


def model(entitled: bool = True, isolated: str = "[]") -> am.AccessModel:
    ent = "    schemas:\n      raw: {unmasked: [pii]}\n" if entitled else ""
    return am.load_model("version: 1\nmasked_classes: [pii]\ngroups:\n  gs_a:\n    members: [a_sp]\n"
                         f"    regions: [UK]\n{ent}isolated_members: {isolated}\n")


def audited(*rows: PolicyRow) -> dict[str, dict]:
    return {r.policy_id: row_dict(r) for r in rows}


# ---------------------------------------------------------------- policy rows: stored differs from its audit

def test_stored_equal_to_audit_and_desired_writes_nothing():
    stored = [rf(), mk((HP, "gs_a"))]
    want = am.desired_rows(model(), schemas=["raw"], stored=stored, **DR)
    assert am.plan_rows(want, stored, audited(*stored)).writes == []


def test_crash_then_model_revert_is_repaired_not_stuck():
    # run 1 audited mk -> {HP, gs_a} and crashed before the MERGE; the model was then reverted (no entitlement).
    stored = [rf(), mk()]
    newest_after = audited(rf(), mk((HP, "gs_a")))
    want = am.desired_rows(model(entitled=False), schemas=["raw"], stored=stored, **DR)
    plan = am.plan_rows(want, stored, newest_after)
    assert [r.policy_id for r in plan.writes] == ["mk_raw_pii"]
    (before, after, eid), = rows_change([row_dict(r) for r in stored], plan.writes,
                                        last_event_ids={"mk_raw_pii": "ev-crashed"},
                                        reassert=frozenset(am.unaudited(stored, newest_after)), **RC)
    assert before == after  # nothing changes in the table; the audit catches up with it
    assert after["except_principals"] == [HP]
    assert eid != "ev-crashed"


def test_hand_edited_row_equal_to_desired_is_reasserted():
    stored = [rf(), mk((HP, "gs_a"))]
    newest_after = audited(rf(), mk())  # audit says HP only; someone hand-edited the row
    want = am.desired_rows(model(), schemas=["raw"], stored=stored, **DR)
    assert [r.policy_id for r in am.plan_rows(want, stored, newest_after).writes] == ["mk_raw_pii"]


def test_row_never_audited_is_reasserted():
    stored = [rf(), mk()]
    want = am.desired_rows(model(entitled=False), schemas=["raw"], stored=stored, **DR)
    assert [r.policy_id for r in am.plan_rows(want, stored, audited(rf())).writes] == ["mk_raw_pii"]


def test_reassert_off_by_default_in_rows_change():
    stored = [rf(), mk()]
    assert rows_change([row_dict(r) for r in stored], [mk()], last_event_ids={}, **RC) == []


# ---------------------------------------------------------------- retry after a crash between audit and MERGE

def test_crash_after_audit_same_model_retries_with_a_new_chained_event():
    # review round 2: never reuse an event id (a reused id could hide a hand edit and its repair); a duplicate
    # audit row after a real crash is harmless, a missing one is not
    stored = [rf(), mk()]
    want = am.desired_rows(model(), schemas=["raw"], stored=stored, **DR)
    (b1, a1, e1), = rows_change([row_dict(r) for r in stored], am.plan_rows(want, stored, audited(*stored)).writes,
                                last_event_ids={"mk_raw_pii": "ev-0"}, **RC)
    newest_after = {**audited(rf()), "mk_raw_pii": a1}  # audit e1 written, table unchanged
    plan = am.plan_rows(want, stored, newest_after)
    (b2, a2, e2), = rows_change([row_dict(r) for r in stored], plan.writes, last_event_ids={"mk_raw_pii": e1},
                                reassert=frozenset(am.unaudited(stored, newest_after)), **RC)
    assert (b2, a2) == (b1, a1)
    assert e2 != e1


# ---------------------------------------------------------------- user_attributes: stored differs from its audit

UA = am.UserAttr


def test_attr_delete_audited_then_crash_then_model_readds_is_readopted():
    row = UA("gs_a", "UK", None)
    after_delete = None  # newest audit says deleted; the MERGE never ran; model re-adds the region
    ch = am.plan_user_attributes([row], [row], {("gs_a", "UK"): "ev-del"}, {("gs_a", "UK"): after_delete})
    assert [(c.kind, c.before, c.after) for c in ch] == [("adopt", row.__dict__, row.__dict__)]
    assert ch[0].event_id != "ev-del"


def test_attr_equal_to_audit_writes_nothing():
    row = UA("gs_a", "UK", None)
    assert am.plan_user_attributes([row], [row], {("gs_a", "UK"): "ev"}, {("gs_a", "UK"): dict(row.__dict__)}) == []


def test_attr_crash_after_audit_same_model_retries_with_a_new_chained_event():
    row = UA("gs_a", "UK", None)
    (c1,) = am.plan_user_attributes([], [row], {("gs_a", "UK"): "ev-0"}, {("gs_a", "UK"): dict(row.__dict__)})
    assert c1.kind == "delete"
    (c2,) = am.plan_user_attributes([], [row], {("gs_a", "UK"): c1.event_id}, {("gs_a", "UK"): None})
    assert (c2.kind, c2.before, c2.after) == ("delete", c1.before, c1.after)
    assert c2.event_id != c1.event_id


# ---------------------------------------------------------------- mask classes belong to config

def test_model_without_pii_cannot_remove_the_pii_mask():
    m = am.AccessModel(1, (), (), ())
    out = {r.policy_id: r for r in am.desired_rows(m, schemas=["raw"], stored=[], **DR)}
    assert out["am_mask_raw_pii"].active and out["am_mask_raw_pii"].except_principals == (HP,)


def test_model_may_not_name_a_class_config_does_not_mask():
    import pytest

    with pytest.raises(ValueError, match="not masked by config"):
        am.load_model("version: 1\nmasked_classes: [confidential]\ngroups: {}\n")


# ---------------------------------------------------------------- isolated members: any group

def test_isolated_member_in_an_undeclared_group_fails():
    m = model(isolated="[p_sp]")
    _, failures = am.membership_report(m, {"gs_a": {"a_sp"}}, {"p_sp": {"some_other_group"}})
    assert any("p_sp" in f and "some_other_group" in f for f in failures)


def test_isolated_member_that_cannot_be_looked_up_fails():
    m = model(isolated="[p_sp]")
    _, failures = am.membership_report(m, {"gs_a": {"a_sp"}}, {})
    assert any("could not be looked up" in f for f in failures)


def test_isolated_member_in_no_group_passes():
    m = model(isolated="[p_sp]")
    warnings, failures = am.membership_report(m, {"gs_a": {"a_sp"}}, {"p_sp": set()})
    assert failures == () and warnings == ()
