"""Consumption decision function (specs/consumption.md Flow 1-3, "Precedence is fixed", "Reason codes for a
denial", "Decision test matrix", "Decisions (8 Nov)"; specs/contracts.md permitted_uses, architecture_patterns).

Pure: no Spark, no workspace. Synthetic values only.

Fixed order, deny beats everything, first matching rule wins:
request_check (invalid_request, proxy_not_implemented) -> pattern_check -> use_check.
"""

from __future__ import annotations

import dataclasses

import pytest

from gates.consumption.decide import Decision, Pattern, PermittedUse, Request, decide

PRODUCT = "cat.raw.claims"
CONSUMER = "0000c1c1-0000-4000-8000-000000000001"
REQUESTER = "requester@example.com"
USE = "claims_reporting"
OTHER_USE = "marketing"
T0 = "2026-11-08T09:00:00"
SECRET_NOTE = "purpose note SYNTHETIC-FREE-TEXT-7731"

OUTCOMES = {"auto_approve", "pending_owner", "denied"}


def req(**over) -> Request:
    base = dict(request_id="req-0001", consumer=CONSUMER, product=PRODUCT, intended_use=USE, reason=SECRET_NOTE,
                use_mode="analytical", delivery="batch", access_mode="system", requester=REQUESTER,
                requested_at=T0)
    base.update(over)
    return Request(**base)


def pat(pattern_id: str = "pat_analytical_batch_system", *, use_mode: str = "analytical", delivery: str = "batch",
        access_mode: str = "system", allowed: bool = True, version: int = 1, active: bool = True) -> Pattern:
    return Pattern(pattern_id=pattern_id, use_mode=use_mode, delivery=delivery, access_mode=access_mode,
                   allowed=allowed, version=version, active=active)


def use(approval: str = "standing", *, use_code: str = USE, product: str = PRODUCT, conditions: str | None = None,
        max_term_days=90, version: int = 1, active: bool = True) -> PermittedUse:
    return PermittedUse(product=product, use_code=use_code, approval=approval, conditions=conditions,
                        max_term_days=max_term_days, version=version, active=active)


def run(request=None, patterns=None, uses=None, **kw) -> Decision:
    request = req() if request is None else request
    patterns = [pat()] if patterns is None else patterns
    uses = [use()] if uses is None else uses
    return decide(request, list(patterns), list(uses), **kw)


def assert_denied(d: Decision, step: str, code: str) -> None:
    assert d.outcome == "denied", d
    assert d.step == step, d
    assert d.reason_code == code, d
    assert isinstance(d.reason_text, str) and d.reason_text.strip(), "denials carry a human sentence"


# --- dataclasses -----------------------------------------------------------------------------------------------


def test_dataclasses_are_frozen():
    for obj in (req(), pat(), use(), run()):
        with pytest.raises(dataclasses.FrozenInstanceError):
            setattr(obj, dataclasses.fields(obj)[0].name, "x")


def test_decision_fields():
    assert [f.name for f in dataclasses.fields(Decision)] == ["outcome", "step", "reason_code", "reason_text",
                                                              "inputs"]


# --- decision matrix: happy paths ------------------------------------------------------------------------------


def test_standing_use_auto_approves():
    d = run()
    assert d.outcome == "auto_approve"
    assert d.step == "use_check"
    assert d.reason_code is None


def test_approval_required_goes_to_owner():
    d = run(uses=[use("approval_required")])
    assert d.outcome == "pending_owner"
    assert d.step == "use_check"
    assert d.reason_code is None


def test_outcome_is_always_one_of_three():
    cases = [run(), run(uses=[use("approval_required")]), run(uses=[use("prohibited")]), run(patterns=[]),
             run(request=req(access_mode="proxy")), run(uses=[])]
    assert {d.outcome for d in cases} <= OUTCOMES


# --- step 1: request_check -------------------------------------------------------------------------------------


@pytest.mark.parametrize("field", ["consumer", "product", "intended_use", "use_mode", "delivery", "access_mode",
                                   "requester"])
def test_empty_required_field_is_invalid_request(field):
    assert_denied(run(request=req(**{field: ""})), "request_check", "invalid_request")


@pytest.mark.parametrize("field", ["consumer", "product", "intended_use", "use_mode", "delivery", "access_mode",
                                   "requester"])
def test_none_required_field_is_invalid_request(field):
    assert_denied(run(request=req(**{field: None})), "request_check", "invalid_request")


@pytest.mark.parametrize("mode", ["human", "SYSTEM", "Proxy", "system ", "obo"])
def test_unknown_access_mode_is_invalid_request(mode):
    assert_denied(run(request=req(access_mode=mode)), "request_check", "invalid_request")


@pytest.mark.parametrize("product", ["claims", "raw.claims", "cat.raw.claims.extra", "cat..claims", ".raw.claims",
                                     "cat.raw.", "cat.raw.claims."])
def test_product_not_three_part_is_invalid_request(product):
    assert_denied(run(request=req(product=product), uses=[use(product=product)]), "request_check",
                  "invalid_request")


def test_proxy_mode_is_not_implemented_even_with_standing_use_and_allowed_pattern():
    d = run(request=req(access_mode="proxy"), patterns=[pat(access_mode="proxy")], uses=[use("standing")])
    assert_denied(d, "request_check", "proxy_not_implemented")


def test_invalid_request_beats_proxy():
    d = run(request=req(access_mode="proxy", product="claims"), patterns=[pat(access_mode="proxy")])
    assert_denied(d, "request_check", "invalid_request")


def test_request_check_denial_has_no_inputs_rows():
    d = run(request=req(access_mode="proxy"))
    assert d.inputs == {"pattern": None, "permitted_use": None}


def test_request_reason_free_text_is_not_required():
    assert run(request=req(reason="")).outcome == "auto_approve"
    assert run(request=req(reason=None)).outcome == "auto_approve"


# --- step 2: pattern_check -------------------------------------------------------------------------------------


def test_no_pattern_row_is_pattern_misaligned():
    d = run(patterns=[])
    assert_denied(d, "pattern_check", "pattern_misaligned")
    assert d.inputs["permitted_use"] is None


def test_disallowed_pattern_is_misaligned():
    assert_denied(run(patterns=[pat(allowed=False)]), "pattern_check", "pattern_misaligned")


def test_disallowed_row_beats_allowed_row_for_same_triple():
    d = run(patterns=[pat("p_ok"), pat("p_no", allowed=False)])
    assert_denied(d, "pattern_check", "pattern_misaligned")


def test_two_allowed_rows_for_same_triple_are_ambiguous():
    d = run(patterns=[pat("p_a"), pat("p_b")])
    assert_denied(d, "pattern_check", "ambiguous_pattern")


@pytest.mark.parametrize("over", [{"use_mode": "operational"}, {"delivery": "stream"}, {"access_mode": "proxy"},
                                  {"use_mode": "Analytical"}])
def test_pattern_must_match_triple_exactly(over):
    assert_denied(run(patterns=[pat(**over)]), "pattern_check", "pattern_misaligned")


def test_inactive_pattern_rows_never_count():
    assert_denied(run(patterns=[pat(active=False)]), "pattern_check", "pattern_misaligned")
    assert run(patterns=[pat("p_ok"), pat("p_no", allowed=False, active=False)]).outcome == "auto_approve"
    assert run(patterns=[pat("p_ok"), pat("p_old", active=False)]).outcome == "auto_approve"


def test_misaligned_pattern_with_standing_use_is_denied_precedence():
    d = run(patterns=[pat(allowed=False)], uses=[use("standing")])
    assert_denied(d, "pattern_check", "pattern_misaligned")
    d = run(patterns=[], uses=[use("standing")])
    assert_denied(d, "pattern_check", "pattern_misaligned")


def test_pattern_check_runs_before_use_check():
    # both would deny; the pattern reason wins
    assert_denied(run(patterns=[], uses=[]), "pattern_check", "pattern_misaligned")
    assert_denied(run(patterns=[], uses=[use("prohibited")]), "pattern_check", "pattern_misaligned")


# --- step 3: use_check -----------------------------------------------------------------------------------------


def test_no_permitted_uses_at_all_denies_by_default():
    assert_denied(run(uses=[]), "use_check", "no_permitted_uses")


def test_no_permitted_uses_explicit_deny():
    assert_denied(run(uses=[], no_uses_outcome="deny"), "use_check", "no_permitted_uses")


def test_no_permitted_uses_routes_to_owner_when_configured():
    d = run(uses=[], no_uses_outcome="owner")
    assert d.outcome == "pending_owner"
    assert d.step == "use_check"
    assert d.reason_code == "no_permitted_uses"


def test_owner_routing_does_not_apply_when_product_has_other_uses():
    d = run(uses=[use(use_code=OTHER_USE)], no_uses_outcome="owner")
    assert_denied(d, "use_check", "use_not_permitted")


def test_rows_for_other_products_do_not_count():
    assert_denied(run(uses=[use(product="cat.raw.policies")]), "use_check", "no_permitted_uses")


@pytest.mark.parametrize("stored", ["CAT.RAW.CLAIMS", "Cat.Raw.Claims", "claims", "raw.claims"])
def test_product_match_is_exact_string(stored):
    assert_denied(run(uses=[use(product=stored)]), "use_check", "no_permitted_uses")


def test_only_inactive_rows_means_no_permitted_uses():
    assert_denied(run(uses=[use(active=False)]), "use_check", "no_permitted_uses")


def test_unknown_use_is_not_permitted():
    assert_denied(run(uses=[use(use_code=OTHER_USE)]), "use_check", "use_not_permitted")


def test_prohibited_use_is_denied():
    d = run(uses=[use("prohibited")])
    assert_denied(d, "use_check", "use_not_permitted")


def test_prohibited_and_standing_for_same_use_is_not_permitted_not_ambiguous():
    d = run(uses=[use("standing", version=1), use("prohibited", version=2)])
    assert_denied(d, "use_check", "use_not_permitted")
    d = run(uses=[use("prohibited", version=2), use("standing", version=1)])
    assert_denied(d, "use_check", "use_not_permitted")


def test_two_active_rows_for_same_use_are_ambiguous():
    d = run(uses=[use("standing", version=1), use("approval_required", version=2)])
    assert_denied(d, "use_check", "ambiguous_permitted_use")
    d = run(uses=[use("standing", version=1), use("standing", version=2)])
    assert_denied(d, "use_check", "ambiguous_permitted_use")


def test_inactive_use_rows_never_count():
    assert run(uses=[use("standing", version=2), use("prohibited", version=1, active=False)]).outcome == \
        "auto_approve"
    assert run(uses=[use("standing", version=2), use("standing", version=1, active=False)]).outcome == \
        "auto_approve"
    assert_denied(run(uses=[use("standing", active=False), use(use_code=OTHER_USE)]), "use_check",
                  "use_not_permitted")


@pytest.mark.parametrize("approval", ["Standing", "auto", "", None, "approved"])
def test_unknown_approval_value_is_denied(approval):
    assert_denied(run(uses=[use(approval)]), "use_check", "use_not_permitted")


def test_nothing_reaches_auto_approve_without_standing():
    for approval in ("approval_required", "prohibited", "other"):
        assert run(uses=[use(approval)]).outcome != "auto_approve"


@pytest.mark.parametrize("bad", [0, -1, -365, "30", 1.5, 30.0])
def test_bad_max_term_days_is_invalid_request_at_use_check(bad):
    assert_denied(run(uses=[use(max_term_days=bad)]), "use_check", "invalid_request")


def test_bad_max_term_days_on_approval_required_also_denied():
    assert_denied(run(uses=[use("approval_required", max_term_days=0)]), "use_check", "invalid_request")


def test_none_max_term_days_is_allowed():
    d = run(uses=[use(max_term_days=None)])
    assert d.outcome == "auto_approve"
    # Spec: "max_term_days (default 365)". Either the raw None or the effective 365 is recorded; see report.
    assert d.inputs["permitted_use"]["max_term_days"] in (None, 365)


# --- inputs: exact rows used, never the free-text note ---------------------------------------------------------


def test_inputs_record_pattern_and_permitted_use_ids_and_versions():
    d = run(patterns=[pat("pat_x", version=3), pat("pat_other", use_mode="operational")],
            uses=[use(version=4, max_term_days=90), use(use_code=OTHER_USE, version=9)])
    assert d.inputs == {
        "pattern": {"pattern_id": "pat_x", "version": 3},
        "permitted_use": {"product": PRODUCT, "use_code": USE, "version": 4, "max_term_days": 90},
    }


def test_inputs_on_pending_owner_record_both_rows():
    d = run(patterns=[pat("pat_x", version=2)], uses=[use("approval_required", version=5, max_term_days=30)])
    assert d.inputs == {
        "pattern": {"pattern_id": "pat_x", "version": 2},
        "permitted_use": {"product": PRODUCT, "use_code": USE, "version": 5, "max_term_days": 30},
    }


def test_inputs_on_use_check_denial_record_pattern():
    d = run(patterns=[pat("pat_x", version=2)], uses=[])
    assert d.inputs["pattern"] == {"pattern_id": "pat_x", "version": 2}
    assert d.inputs["permitted_use"] is None
    d = run(patterns=[pat("pat_x", version=2)], uses=[use(use_code=OTHER_USE)])
    assert d.inputs["permitted_use"] is None


def test_inputs_keys_are_fixed():
    for d in (run(), run(patterns=[]), run(uses=[]), run(request=req(access_mode="proxy")),
              run(uses=[use("prohibited")])):
        assert set(d.inputs) == {"pattern", "permitted_use"}


def test_free_text_reason_never_in_decision():
    for d in (run(), run(patterns=[]), run(uses=[]), run(uses=[use("approval_required")]),
              run(request=req(access_mode="proxy")), run(request=req(product="claims"))):
        assert SECRET_NOTE not in repr(d.inputs)
        assert SECRET_NOTE not in (d.reason_text or "")


# --- determinism -----------------------------------------------------------------------------------------------


@pytest.mark.parametrize("kw", [
    {}, {"uses": [use("approval_required")]}, {"uses": [use("prohibited")]}, {"patterns": []},
    {"uses": []}, {"request": req(access_mode="proxy")}, {"patterns": [pat("a"), pat("b")]},
])
def test_same_inputs_same_decision(kw):
    assert run(**kw) == run(**kw)


def test_decide_does_not_mutate_inputs():
    patterns, uses = [pat("p_a"), pat("p_b", allowed=False)], [use(), use(use_code=OTHER_USE)]
    before = (list(patterns), list(uses))
    decide(req(), patterns, uses)
    assert (patterns, uses) == before
