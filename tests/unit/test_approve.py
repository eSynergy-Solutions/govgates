"""Demo auto-rule for the approve step (specs/hydration.md step 6).

Auto-accept only when: a term was proposed, confidence >= threshold, and the pattern floor
(pii on any FORCE_PII hit) did not raise sensitivity above the term's own sensitivity.
"""

import pytest

from gates.hydration.approve import AUTO_ACCEPT_CONFIDENCE, auto_decision
from gates.hydration.patterns import FORCE_PII
from gates.hydration.propose import enforce_floor

PII_HITS = ["email", "uk_ni_number", "phone", "postcode", "sort_code", "iban"]


def test_default_threshold_is_0_8():
    assert AUTO_ACCEPT_CONFIDENCE == 0.8


def test_pii_hit_list_matches_force_pii():
    assert set(PII_HITS) == FORCE_PII


def test_term_at_threshold_without_hits_is_accepted():
    assert auto_decision("claim_reference", 0.8, "internal", []) == "accept"


def test_term_below_threshold_goes_to_review():
    assert auto_decision("claim_reference", 0.79, "internal", []) == "review"


@pytest.mark.parametrize("no_match", [None, "", "NO_MATCH"])
def test_no_match_never_auto_accepts(no_match):
    assert auto_decision(no_match, 0.99, None, []) == "review"
    assert auto_decision(no_match, 0.99, "internal", []) == "review"


def test_pii_hit_above_internal_term_goes_to_review():
    assert auto_decision("customer_contact", 0.95, "internal", ["email"]) == "review"


def test_pii_hit_on_pii_term_is_accepted():
    assert auto_decision("email_address", 0.95, "pii", ["email"]) == "accept"


@pytest.mark.parametrize("hit", PII_HITS)
def test_each_pii_hit_on_confidential_term_goes_to_review(hit):
    assert auto_decision("gross_written_premium", 0.95, "confidential", [hit]) == "review"


@pytest.mark.parametrize("hit", ["currency_amount", "iso_date"])
def test_non_pii_hit_does_not_raise_floor(hit):
    assert auto_decision("premium_amount", 0.95, "internal", [hit]) == "accept"


def test_pii_hit_with_unknown_term_sensitivity_goes_to_review():
    assert auto_decision("claim_reference", 0.95, None, ["email"]) == "review"


def test_pii_hit_among_other_hits_still_goes_to_review():
    assert auto_decision("premium_amount", 0.95, "internal", ["currency_amount", "phone"]) == "review"


def test_threshold_is_configurable():
    assert auto_decision("claim_reference", 0.85, "internal", [], threshold=0.9) == "review"
    assert auto_decision("claim_reference", 0.6, "internal", [], threshold=0.5) == "accept"
    assert auto_decision(None, 0.6, None, [], threshold=0.5) == "review"


def test_same_input_same_decision():
    args = ("email_address", 0.95, "pii", ["email"])
    assert auto_decision(*args) == auto_decision(*args) == "accept"
    args = ("claim_reference", 0.95, "internal", ["email"])
    assert auto_decision(*args) == auto_decision(*args) == "review"


def test_nan_confidence_fails_closed():
    # a double column read from a table can be NaN; nan < 0.8 is False, so the rule must not rely on it
    assert auto_decision("claim_reference", float("nan"), "internal", []) == "review"


def test_public_term_with_a_pii_hit_goes_to_review():
    assert auto_decision("currency_code", 0.95, "public", ["email"]) == "review"


@pytest.mark.parametrize("term_sens", ["public", "internal", "confidential", "pii", None])
@pytest.mark.parametrize("hits", [[], ["iso_date"], ["currency_amount"], *[[h] for h in sorted(FORCE_PII)]])
def test_rule_3_agrees_with_the_propose_floor(term_sens, hits):
    # review on rule 3 exactly when enforce_floor raises the term's own sensitivity
    # no any_pattern_hits here: rule 3 is deliberately stricter than the floor, which uses pattern_hits only
    raised = enforce_floor(term_sens, hits, term_sens) != term_sens and bool(FORCE_PII.intersection(hits))
    assert auto_decision("some_term", 0.95, term_sens, hits) == ("review" if raised else "accept")


# Rule 3 widened: a PII pattern on any value (any_pattern_hits) also sends a non-pii term to review.

def test_any_pattern_email_under_confident_non_pii_term_is_review():
    # free text with a few emails
    assert auto_decision("claim_notes", 0.95, "confidential", [], any_pattern_hits=["email"]) == "review"


def test_any_pattern_email_under_pii_term_is_accept():
    assert auto_decision("claim_notes", 0.95, "pii", [], any_pattern_hits=["email"]) == "accept"


def test_any_pattern_non_pii_hits_only_is_accept():
    assert auto_decision("premium_amount", 0.95, "internal", [],
                         any_pattern_hits=["currency_amount", "iso_date"]) == "accept"


@pytest.mark.parametrize("hit", sorted(FORCE_PII))
def test_each_force_pii_any_pattern_hit_on_internal_term_is_review(hit):
    assert auto_decision("some_term", 0.95, "internal", [], any_pattern_hits=[hit]) == "review"


def test_omitting_any_pattern_hits_equals_empty():
    for args in [("t", 0.95, "internal", []), ("t", 0.95, "internal", ["email"]), ("t", 0.5, "pii", [])]:
        assert auto_decision(*args) == auto_decision(*args, any_pattern_hits=[])


def test_no_match_still_review_with_empty_any_pattern_hits():
    assert auto_decision("NO_MATCH", 0.99, "internal", [], any_pattern_hits=[]) == "review"
    assert auto_decision(None, 0.99, None, [], any_pattern_hits=[]) == "review"


def test_below_threshold_still_review_with_any_pattern_hits():
    assert auto_decision("t", 0.79, "pii", [], any_pattern_hits=["email"]) == "review"
    assert auto_decision("t", 0.85, "internal", [], any_pattern_hits=[], threshold=0.9) == "review"


# IBAN is a PII pattern (specs/hydration.md step 6 rule 3).

def test_iban_pattern_hit_under_confident_non_pii_term_is_review():
    assert auto_decision("payment_reference", 0.95, "confidential", ["iban"]) == "review"


def test_iban_any_pattern_hit_under_confident_non_pii_term_is_review():
    assert auto_decision("payment_reference", 0.95, "internal", [], any_pattern_hits=["iban"]) == "review"


def test_iban_hit_on_pii_term_is_accepted():
    assert auto_decision("bank_account", 0.95, "pii", ["iban"], any_pattern_hits=["iban"]) == "accept"


# --- approval gate on permitted uses (specs/hydration.md step 6 H15; consumption.md "Decisions (8 Nov)" item 4):
# with has_uses=False, accept and edit are refused and the auto-rule accepts nothing; reject and revise still apply.

from datetime import datetime, timedelta  # noqa: E402

from gates.hydration.approve import ApprovedRow, ProfileHits, ProposalRow, ReviewDecision, plan  # noqa: E402

_T0 = datetime(2026, 1, 1, 9, 0, 0)
_TABLE = "cat.raw.claims"
_JOB = "svc-hydration-job"
_OWNER = "owner.a"
_TERMS = {"claim_reference": "internal", "claim_amount": "confidential"}


def _prop(pid: str, column: str, *, status: str = "proposed", confidence: float = 0.95) -> ProposalRow:
    return ProposalRow(pid, "run-1", _TABLE, column, "claim_reference", "desc", "internal", confidence, "fuzzy",
                       status, _T0)


def _dec(did: str, pid: str, action: str, **kw) -> ReviewDecision:
    return ReviewDecision(did, pid, action, decided_by=_OWNER, decided_at=_T0 + timedelta(minutes=1),
                          committed_by=_OWNER, insert_version=5, **kw)


def _plan(decisions, proposals, *, has_uses, current=None):
    profiles = {(p.run_id, p.column_name): ProfileHits() for p in proposals}
    return plan(decisions, proposals, profiles, _TERMS, set(), approvers={_TABLE: {_OWNER}}, job_principal=_JOB,
                current=current, has_uses=has_uses)


@pytest.mark.parametrize("action, kw", [("accept", {}), ("edit", {"term_id": "claim_amount"})])
def test_accept_and_edit_refused_without_permitted_uses(action, kw):
    p = _prop("p1", "clm_ref")
    out = _plan([_dec("d1", "p1", action, **kw)], [p], has_uses=False)
    assert [a.kind for a in out if a.proposal_id == "p1"] == ["refused"]
    assert "no permitted uses" in out[0].reason
    assert out[0].approved is None and out[0].new_status is None


@pytest.mark.parametrize("action, kw", [("accept", {}), ("edit", {"term_id": "claim_amount"})])
def test_accept_and_edit_unchanged_with_permitted_uses(action, kw):
    out = _plan([_dec("d1", "p1", action, **kw)], [_prop("p1", "clm_ref")], has_uses=True)
    assert [a.kind for a in out] == [{"accept": "accepted", "edit": "edited"}[action]]


def test_reject_still_applies_without_permitted_uses():
    out = _plan([_dec("d1", "p1", "reject")], [_prop("p1", "clm_ref")], has_uses=False)
    assert [a.kind for a in out] == ["rejected"]


def test_revise_still_applies_without_permitted_uses():
    p = _prop("p1", "clm_ref", status="accepted")
    current = {(_TABLE, "clm_ref"): ApprovedRow(_TABLE, "clm_ref", "claim_reference", "desc", "internal", _OWNER)}
    out = _plan([_dec("d1", "p1", "revise", term_id="claim_amount")], [p], has_uses=False, current=current)
    assert [a.kind for a in out] == ["revised"]


def test_auto_rule_accepts_nothing_without_permitted_uses():
    confident = _prop("p1", "clm_ref")
    out = _plan([], [confident], has_uses=False)
    assert [a for a in out if a.proposal_id == "p1"] == []  # stays proposed: not accepted, not routed


def test_auto_rule_accepts_by_default():
    out = plan([], [_prop("p1", "clm_ref")], {("run-1", "clm_ref"): ProfileHits()}, _TERMS, set(),
               approvers={_TABLE: {_OWNER}}, job_principal=_JOB)
    assert [a.kind for a in out] == ["accepted"]


# --- commit time attribution: decision_id -> timestamp of the commit that inserted it (latest insert version)


def test_commit_times_latest_insert_version():
    from gates.hydration.approve import commit_times
    t3, t4, t5 = (datetime(2026, 11, 9, h) for h in (3, 4, 5))
    changes = [("d1", 3, "insert"), ("d1", 5, "insert"), ("d2", 4, "update_postimage"), ("d2", 4, "insert"),
               ("d3", 9, "insert"), ("d4", 5, "delete")]
    assert commit_times(changes, {3: t3, 4: t4, 5: t5}) == {"d1": t5, "d2": t4}


def test_commit_times_missing_version_gives_no_entry():
    from gates.hydration.approve import commit_times
    assert commit_times([("d1", 7, "insert")], {6: datetime(2026, 11, 9)}) == {}


def test_commit_times_ignores_non_insert_changes():
    from gates.hydration.approve import commit_times
    t = datetime(2026, 11, 9, 1)
    changes = [("d1", 1, "insert"), ("d1", 2, "update_preimage"), ("d1", 2, "update_postimage")]
    assert commit_times(changes, {1: t, 2: datetime(2026, 11, 9, 2)}) == {"d1": t}
