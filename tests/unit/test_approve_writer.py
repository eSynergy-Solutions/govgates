"""Approve writer (specs/hydration.md step 6): plan the state changes for auto-rule and owner decisions.

Pure planning only: proposals + profiles + terms + decisions in, a list of Actions out. The Spark edge
(upsert approved_metadata, set term_proposals.status, audit_log, golden candidates) consumes the Actions.

Fixtures are small and synthetic; term ids, principals and table names are made up here, never read from
glossary.yaml or any protected file.
"""

from __future__ import annotations

import json
import math
from dataclasses import replace
from datetime import datetime, timedelta

import pytest

from gates.hydration.approve import (
    AUTO_ACTOR,
    Action,
    ProfileHits,
    ProposalRow,
    ReviewDecision,
    action_from_audit,
    audit_after,
    audit_event_id,
    audit_rows,
    cdf_start_version,
    committers,
    event_id_for,
    insert_versions,
    latest_pending,
    manual_event_id,
    modified_after_insert,
    plan,
    plan_auto,
    plan_manual,
    refusal_salt,
)

T0 = datetime(2026, 1, 1, 9, 0, 0)
TABLE = "cat.raw.claims"
OTHER_TABLE = "cat.raw.payments"
RUN = "run-1"
JOB = "svc-hydration-job"

TERMS: dict[str, str] = {
    "claim_reference": "internal",
    "claim_amount": "confidential",
    "email_address": "pii",
    "policy_status": "public",
    "free_notes": "confidential",
}


def prop(pid: str, column: str, term_id: str | None = "claim_reference", *, sensitivity: str | None = "internal",
         confidence: float = 0.95, method: str = "fuzzy", status: str = "proposed", run_id: str = RUN,
         table: str = TABLE, created_at: datetime | None = T0, description: str | None = "desc") -> ProposalRow:
    return ProposalRow(proposal_id=pid, run_id=run_id, table_name=table, column_name=column, term_id=term_id,
                       description=description, sensitivity=sensitivity, confidence=confidence, method=method,
                       status=status, created_at=created_at)


def hits(pattern: tuple[str, ...] = (), anyp: tuple[str, ...] = ()) -> ProfileHits:
    return ProfileHits(pattern_hits=pattern, any_pattern_hits=anyp)


def profiles_for(*proposals: ProposalRow, **overrides: ProfileHits) -> dict[tuple[str, str], ProfileHits]:
    out = {(p.run_id, p.column_name): hits() for p in proposals}
    for col, h in overrides.items():
        for p in proposals:
            if p.column_name == col:
                out[(p.run_id, col)] = h
    return out


def decision(did: str, pid: str, action: str, *, minutes: int = 0, decided_by: str = "owner.a",
             applied_at: datetime | None = None, **kw) -> ReviewDecision:
    kw.setdefault("committed_by", decided_by)  # the Delta committer; matches the typed name unless overridden
    return ReviewDecision(decision_id=did, proposal_id=pid, action=action, decided_by=decided_by,
                          decided_at=T0 + timedelta(minutes=minutes), applied_at=applied_at, **kw)


def allow_committers(decisions: list[ReviewDecision], proposals: list[ProposalRow]) -> dict[str, set[str]]:
    """Approver list that admits every committer in the batch on every table: keeps older tests' meaning."""
    who = {d.committed_by for d in decisions if d.committed_by}
    return {t: set(who) for t in {p.table_name for p in proposals} | {TABLE, OTHER_TABLE}}


def manual(decisions, proposals, profiles, terms, **kw) -> list[Action]:
    kw.setdefault("approvers", allow_committers(decisions, proposals))
    kw.setdefault("job_principal", JOB)
    return plan_manual(decisions, proposals, profiles, terms, **kw)


def full_plan(decisions, proposals, profiles, terms, routed, **kw) -> list[Action]:
    kw.setdefault("approvers", allow_committers(decisions, proposals))
    kw.setdefault("job_principal", JOB)
    return plan(decisions, proposals, profiles, terms, routed, **kw)


def event_id(a: Action) -> str:
    return manual_event_id(a.decision_id) if a.decision_id is not None else audit_event_id(a.kind, a.proposal_id,
                                                                                            None)


def assert_refused(a: Action, text: str | None = None):
    assert a.kind == "refused" and a.new_status is None
    assert a.approved is None and a.golden is None
    assert a.reason
    if text is not None:
        assert text in a.reason


# ---------------------------------------------------------------- latest_pending

class TestLatestPending:
    def test_only_proposed_rows(self):
        rows = [prop("p1", "a"), prop("p2", "b", status="eval"), prop("p3", "c", status="accepted"),
                prop("p4", "d", status="edited"), prop("p5", "e", status="rejected")]
        assert [p.proposal_id for p in latest_pending(rows)] == ["p1"]

    def test_newest_per_table_column(self):
        old = prop("old", "a", created_at=T0)
        new = prop("new", "a", created_at=T0 + timedelta(hours=1), run_id="run-2")
        other_table = prop("x", "a", table=OTHER_TABLE)
        got = {p.proposal_id for p in latest_pending([new, old, other_table])}
        assert got == {"new", "x"}

    def test_newer_eval_row_does_not_hide_pending_proposal(self):
        pending = prop("p1", "a", created_at=T0)
        ev = prop("e1", "a", status="eval", created_at=T0 + timedelta(hours=1), run_id="eval-run")
        assert [p.proposal_id for p in latest_pending([pending, ev])] == ["p1"]

    def test_empty(self):
        assert latest_pending([]) == []


# ---------------------------------------------------------------- plan_auto: accept path and sensitivity

class TestAutoAccept:
    def test_confident_term_no_hits_is_accepted(self):
        p = prop("p1", "claim_ref")
        [a] = plan_auto([p], profiles_for(p), TERMS, set())
        assert a.kind == "accepted"
        assert a.new_status == "accepted"
        assert a.actor == AUTO_ACTOR == "auto-rule"
        assert a.golden is None and a.decision_id is None
        assert a.approved is not None
        assert (a.approved.table_name, a.approved.column_name) == (TABLE, "claim_ref")
        assert a.approved.term_id == "claim_reference"
        assert a.approved.description == "desc"
        assert a.approved.approved_by == AUTO_ACTOR

    def test_stores_floored_proposal_sensitivity_not_terms(self):
        # must-pin 1a: term internal, proposal confidential -> confidential (the proposal raised it)
        p = prop("p1", "claim_ref", "claim_reference", sensitivity="confidential")
        [a] = plan_auto([p], profiles_for(p), TERMS, set())
        assert a.kind == "accepted"
        assert a.approved.sensitivity == "confidential"

    def test_pattern_hit_under_pii_term_floors_to_pii(self):
        # must-pin 1b: proposal internal, pattern_hits email, pii term -> pii
        p = prop("p1", "contact", "email_address", sensitivity="internal")
        prof = profiles_for(p, contact=hits(("email",), ("email",)))
        [a] = plan_auto([p], prof, TERMS, set())
        assert a.kind == "accepted"
        assert a.approved.sensitivity == "pii"

    def test_proposal_below_term_is_raised_to_term(self):
        p = prop("p1", "amt", "claim_amount", sensitivity="internal")
        [a] = plan_auto([p], profiles_for(p), TERMS, set())
        assert a.approved.sensitivity == "confidential"

    def test_public_only_when_term_public(self):
        p = prop("p1", "status", "policy_status", sensitivity="public")
        [a] = plan_auto([p], profiles_for(p), TERMS, set())
        assert a.approved.sensitivity == "public"

    def test_threshold_exactly_0_8_accepts(self):
        p = prop("p1", "claim_ref", confidence=0.8)
        [a] = plan_auto([p], profiles_for(p), TERMS, set())
        assert a.kind == "accepted"


# ---------------------------------------------------------------- plan_auto: review routing

class TestAutoReview:
    def assert_routed(self, a: Action):
        assert a.kind == "review_routed"
        assert a.new_status is None
        assert a.approved is None and a.golden is None
        assert a.actor == AUTO_ACTOR

    def test_any_pattern_hits_reaches_rule(self):
        # must-pin 2: confident non-pii term, a few emails in values -> review, not accepted
        p = prop("p1", "notes", "free_notes", sensitivity="confidential", confidence=0.99)
        prof = profiles_for(p, notes=hits((), ("email",)))
        [a] = plan_auto([p], prof, TERMS, set())
        self.assert_routed(a)

    def test_pattern_hits_under_non_pii_term_goes_to_review(self):
        p = prop("p1", "claim_ref", confidence=0.99)
        prof = profiles_for(p, claim_ref=hits(("postcode",), ("postcode",)))
        [a] = plan_auto([p], prof, TERMS, set())
        self.assert_routed(a)

    def test_iban_any_pattern_hit_under_non_pii_term_goes_to_review(self):
        p = prop("p1", "pay_ref", "claim_reference", sensitivity="internal", confidence=0.95)
        prof = profiles_for(p, pay_ref=hits((), ("iban",)))
        [a] = plan_auto([p], prof, TERMS, set())
        self.assert_routed(a)

    def test_iban_pattern_hit_under_non_pii_term_goes_to_review(self):
        p = prop("p1", "pay_ref", "claim_reference", sensitivity="internal", confidence=0.95)
        prof = profiles_for(p, pay_ref=hits(("iban",), ()))
        [a] = plan_auto([p], prof, TERMS, set())
        self.assert_routed(a)

    def test_non_pii_pattern_does_not_block(self):
        p = prop("p1", "amt", "claim_amount", sensitivity="confidential")
        prof = profiles_for(p, amt=hits(("currency_amount",), ("currency_amount",)))
        [a] = plan_auto([p], prof, TERMS, set())
        assert a.kind == "accepted"

    @pytest.mark.parametrize("term_id", [None, "NO_MATCH"])
    def test_no_match_goes_to_review(self, term_id):
        p = prop("p1", "col17", term_id, confidence=1.0, method="rule")
        [a] = plan_auto([p], profiles_for(p), TERMS, set())
        self.assert_routed(a)

    def test_below_threshold_goes_to_review(self):
        p = prop("p1", "claim_ref", confidence=0.79)
        [a] = plan_auto([p], profiles_for(p), TERMS, set())
        self.assert_routed(a)

    def test_missing_profile_fails_closed(self):
        p = prop("p1", "claim_ref", confidence=0.99)
        [a] = plan_auto([p], {}, TERMS, set())
        self.assert_routed(a)

    def test_profile_from_other_run_is_not_used(self):
        p = prop("p1", "claim_ref", confidence=0.99, run_id="run-2")
        [a] = plan_auto([p], {("run-1", "claim_ref"): hits()}, TERMS, set())
        self.assert_routed(a)

    def test_already_routed_is_not_repeated(self):
        p = prop("p1", "claim_ref", confidence=0.5)
        assert plan_auto([p], profiles_for(p), TERMS, {"p1"}) == []

    def test_already_routed_does_not_block_a_new_proposal(self):
        p = prop("p2", "claim_ref", confidence=0.5)
        [a] = plan_auto([p], profiles_for(p), TERMS, {"p1"})
        self.assert_routed(a)


# ---------------------------------------------------------------- plan_auto: what it reads

class TestAutoScope:
    def test_eval_rows_never_read(self):
        # must-pin 3 (auto)
        e = prop("e1", "claim_ref", status="eval", run_id="eval-run")
        assert plan_auto([e], profiles_for(e), TERMS, set()) == []

    @pytest.mark.parametrize("status", ["accepted", "edited", "rejected"])
    def test_decided_rows_not_redecided(self, status):
        p = prop("p1", "claim_ref", status=status)
        assert plan_auto([p], profiles_for(p), TERMS, set()) == []

    def test_only_latest_pending_per_column(self):
        old = prop("old", "claim_ref", created_at=T0, confidence=0.5)
        new = prop("new", "claim_ref", created_at=T0 + timedelta(hours=1), run_id="run-2")
        acts = plan_auto([old, new], profiles_for(old, new), TERMS, set())
        assert [a.proposal_id for a in acts] == ["new"]
        assert acts[0].kind == "accepted"

    def test_skip_set_stops_auto(self):
        p1 = prop("p1", "claim_ref")
        p2 = prop("p2", "amt", "claim_amount", sensitivity="confidential")
        acts = plan_auto([p1, p2], profiles_for(p1, p2), TERMS, set(), skip={"p1"})
        assert [a.proposal_id for a in acts] == ["p2"]


# ---------------------------------------------------------------- plan_manual: accept / edit / reject

class TestManualAccept:
    def test_accept_uses_proposal_and_decider(self):
        p = prop("p1", "claim_ref", confidence=0.3)  # owner can accept what auto would not
        [a] = manual([decision("d1", "p1", "accept")], [p], profiles_for(p), TERMS)
        assert a.kind == "accepted" and a.new_status == "accepted"
        assert a.decision_id == "d1"
        assert a.actor == "owner.a"
        assert a.approved.approved_by == "owner.a"
        assert a.approved.term_id == "claim_reference"
        assert a.golden is None

    def test_accept_floors_sensitivity(self):
        p = prop("p1", "contact", "email_address", sensitivity="internal")
        prof = profiles_for(p, contact=hits(("email",), ("email",)))
        [a] = manual([decision("d1", "p1", "accept")], [p], prof, TERMS)
        assert a.approved.sensitivity == "pii"

    def test_accept_keeps_raised_proposal_sensitivity(self):
        p = prop("p1", "claim_ref", sensitivity="confidential")
        [a] = manual([decision("d1", "p1", "accept")], [p], profiles_for(p), TERMS)
        assert a.approved.sensitivity == "confidential"


class TestManualEdit:
    def test_edit_term_and_golden_candidate(self):
        p = prop("p1", "amt", "claim_reference", sensitivity="internal")
        d = decision("d1", "p1", "edit", term_id="claim_amount", description="Amount claimed.",
                     sensitivity="confidential")
        [a] = manual([d], [p], profiles_for(p), TERMS)
        assert a.kind == "edited" and a.new_status == "edited"
        assert a.approved.term_id == "claim_amount"
        assert a.approved.description == "Amount claimed."
        assert a.approved.sensitivity == "confidential"
        assert a.approved.approved_by == "owner.a"
        g = a.golden
        assert g is not None
        assert (g.proposal_id, g.table_name, g.column_name) == ("p1", TABLE, "amt")
        assert (g.proposed_term_id, g.edited_term_id) == ("claim_reference", "claim_amount")
        assert (g.proposed_sensitivity, g.edited_sensitivity) == ("internal", "confidential")
        assert g.edited_by == "owner.a"

    def test_edit_none_keeps_proposal_values(self):
        p = prop("p1", "claim_ref", "claim_reference", sensitivity="confidential", description="orig")
        [a] = manual([decision("d1", "p1", "edit")], [p], profiles_for(p), TERMS)
        assert a.kind == "edited"
        assert a.approved.term_id == "claim_reference"
        assert a.approved.description == "orig"
        assert a.approved.sensitivity == "confidential"

    def test_edit_cannot_go_below_new_terms_sensitivity(self):
        p = prop("p1", "amt", "claim_reference", sensitivity="internal")
        d = decision("d1", "p1", "edit", term_id="claim_amount", sensitivity="public")
        [a] = manual([d], [p], profiles_for(p), TERMS)
        assert a.approved.sensitivity == "confidential"

    def test_edit_cannot_go_below_pattern_floor(self):
        p = prop("p1", "contact", "email_address", sensitivity="pii")
        prof = profiles_for(p, contact=hits(("email",), ("email",)))
        d = decision("d1", "p1", "edit", sensitivity="internal")
        [a] = manual([d], [p], prof, TERMS)
        assert a.approved.sensitivity == "pii"

    def test_edit_can_raise(self):
        p = prop("p1", "claim_ref", "claim_reference", sensitivity="internal")
        d = decision("d1", "p1", "edit", sensitivity="pii")
        [a] = manual([d], [p], profiles_for(p), TERMS)
        assert a.approved.sensitivity == "pii"

    def test_edit_public_on_non_public_term_floors_to_internal(self):
        p = prop("p1", "claim_ref", "claim_reference", sensitivity="internal")
        d = decision("d1", "p1", "edit", sensitivity="public")
        [a] = manual([d], [p], profiles_for(p), TERMS)
        assert a.approved.sensitivity == "internal"

    def test_edit_to_no_match_stores_none(self):
        p = prop("p1", "col17", "claim_reference", sensitivity="internal")
        d = decision("d1", "p1", "edit", term_id="NO_MATCH")
        [a] = manual([d], [p], profiles_for(p), TERMS)
        assert a.kind == "edited"
        assert a.approved.term_id is None
        assert a.approved.sensitivity == "internal"  # NO_MATCH never public
        assert a.golden.edited_term_id is None
        assert a.golden.proposed_term_id == "claim_reference"

    def test_edit_rule_proposal_allowed(self):
        p = prop("p1", "col17", None, method="rule", confidence=1.0)
        d = decision("d1", "p1", "edit", term_id="claim_reference")
        [a] = manual([d], [p], profiles_for(p), TERMS)
        assert a.kind == "edited"
        assert a.approved.term_id == "claim_reference"


class TestManualReject:
    def test_reject(self):
        p = prop("p1", "claim_ref", method="llm")
        [a] = manual([decision("d1", "p1", "reject")], [p], profiles_for(p), TERMS)
        assert a.kind == "rejected" and a.new_status == "rejected"
        assert a.approved is None and a.golden is None
        assert a.decision_id == "d1" and a.actor == "owner.a"


# ---------------------------------------------------------------- plan_manual: refusals

class TestManualRefused:
    def assert_refused(self, acts: list[Action], did: str):
        assert len(acts) == 1
        assert_refused(acts[0])
        assert acts[0].decision_id == did

    def test_unknown_proposal(self):
        p = prop("p1", "claim_ref")
        self.assert_refused(manual([decision("d1", "nope", "accept")], [p], profiles_for(p), TERMS), "d1")

    def test_eval_row_never_read(self):
        # must-pin 3 (manual)
        e = prop("e1", "claim_ref", status="eval", run_id="eval-run")
        self.assert_refused(manual([decision("d1", "e1", "accept")], [e], profiles_for(e), TERMS), "d1")

    @pytest.mark.parametrize("status", ["accepted", "edited", "rejected"])
    def test_already_decided(self, status):
        p = prop("p1", "claim_ref", status=status)
        self.assert_refused(manual([decision("d1", "p1", "accept")], [p], profiles_for(p), TERMS), "d1")

    def test_unknown_action(self):
        p = prop("p1", "claim_ref")
        self.assert_refused(manual([decision("d1", "p1", "approve")], [p], profiles_for(p), TERMS), "d1")

    def test_edit_unknown_term(self):
        p = prop("p1", "claim_ref")
        d = decision("d1", "p1", "edit", term_id="made_up_term")
        self.assert_refused(manual([d], [p], profiles_for(p), TERMS), "d1")

    def test_edit_bad_sensitivity(self):
        p = prop("p1", "claim_ref")
        d = decision("d1", "p1", "edit", sensitivity="secret")
        self.assert_refused(manual([d], [p], profiles_for(p), TERMS), "d1")

    def test_reject_rule_proposal(self):
        p = prop("p1", "col17", None, method="rule", confidence=1.0)
        self.assert_refused(manual([decision("d1", "p1", "reject")], [p], profiles_for(p), TERMS), "d1")

    def test_missing_profile(self):
        p = prop("p1", "claim_ref")
        self.assert_refused(manual([decision("d1", "p1", "accept")], [p], {}, TERMS), "d1")

    def test_second_decision_same_proposal_same_batch(self):
        p = prop("p1", "claim_ref")
        d_late = decision("d2", "p1", "reject", minutes=5, decided_by="owner.b")
        d_early = decision("d1", "p1", "accept", minutes=1)
        acts = manual([d_late, d_early], [p], profiles_for(p), TERMS)
        assert [a.decision_id for a in acts] == ["d1", "d2"]  # decided_at order
        assert acts[0].kind == "accepted"
        assert_refused(acts[1])

    def test_one_action_per_decision_mixed_batch(self):
        p1, p2 = prop("p1", "claim_ref"), prop("p2", "amt", "claim_amount", sensitivity="confidential")
        ds = [decision("d1", "p1", "accept", minutes=1), decision("d2", "zzz", "accept", minutes=2),
              decision("d3", "p2", "frobnicate", minutes=3), decision("d4", "p2", "reject", minutes=4)]
        acts = manual(ds, [p1, p2], profiles_for(p1, p2), TERMS)
        assert [a.decision_id for a in acts] == ["d1", "d2", "d3", "d4"]
        assert [a.kind for a in acts] == ["accepted", "refused", "refused", "rejected"]


class TestManualScope:
    def test_applied_decisions_ignored(self):
        p = prop("p1", "claim_ref")
        d = decision("d1", "p1", "accept", applied_at=T0 + timedelta(hours=1))
        assert manual([d], [p], profiles_for(p), TERMS) == []

    def test_decided_at_order(self):
        p1, p2 = prop("p1", "a"), prop("p2", "b")
        ds = [decision("d2", "p2", "accept", minutes=9), decision("d1", "p1", "accept", minutes=1)]
        acts = manual(ds, [p1, p2], profiles_for(p1, p2), TERMS)
        assert [a.decision_id for a in acts] == ["d1", "d2"]


# ---------------------------------------------------------------- idempotency (plan, apply, re-run)

def apply_state(proposals: list[ProposalRow], decisions: list[ReviewDecision], routed: set[str],
                actions: list[Action], *, write_status: bool = True, write_applied: bool = True
                ) -> tuple[list[ProposalRow], list[ReviewDecision], set[str]]:
    """Test-local model of what the Spark edge writes back (each part can be cut short to model a crash)."""
    status = {a.proposal_id: a.new_status for a in actions if a.new_status is not None} if write_status else {}
    new_props = [replace(p, status=status.get(p.proposal_id, p.status)) for p in proposals]
    done = {a.decision_id for a in actions if a.decision_id is not None} if write_applied else set()
    new_decs = [replace(d, applied_at=T0 + timedelta(days=1)) if d.decision_id in done else d for d in decisions]
    new_routed = routed | {a.proposal_id for a in actions if a.kind == "review_routed"}
    return new_props, new_decs, new_routed


class TestIdempotency:
    def setup_method(self):
        self.props = [
            prop("p1", "claim_ref"),                                              # auto accept
            prop("p2", "notes", "free_notes", sensitivity="confidential"),       # review (any email)
            prop("p3", "col17", None, method="rule", confidence=1.0),             # review (NO_MATCH)
            prop("p4", "amt", "claim_reference", confidence=0.5),                 # manual edit
            prop("p5", "contact", "email_address", sensitivity="internal"),     # manual reject (auto would accept)
            prop("e1", "claim_ref", status="eval", run_id="eval-run"),
        ]
        self.prof = profiles_for(*self.props, notes=hits((), ("email",)), contact=hits(("email",), ("email",)))
        self.decs = [decision("d1", "p4", "edit", term_id="claim_amount", minutes=1),
                     decision("d2", "p5", "reject", minutes=2),
                     decision("d3", "nope", "accept", minutes=3)]

    def run_once(self, props, decs, routed, audited=None):
        acts = full_plan(decs, props, self.prof, TERMS, routed, audited=audited)
        manual_acts = [a for a in acts if a.decision_id is not None]
        auto = [a for a in acts if a.decision_id is None]
        assert acts == manual_acts + auto, "plan() returns manual actions first, then auto"
        return manual_acts, auto

    def test_first_run_plan(self):
        manual_acts, auto = self.run_once(self.props, self.decs, set())
        assert [(a.decision_id, a.kind) for a in manual_acts] == [("d1", "edited"), ("d2", "rejected"),
                                                                    ("d3", "refused")]
        got = {a.proposal_id: a.kind for a in auto}
        assert got == {"p1": "accepted", "p2": "review_routed", "p3": "review_routed"}

    def test_rerun_on_resulting_state_is_empty(self):
        manual_acts, auto = self.run_once(self.props, self.decs, set())
        props, decs, routed = apply_state(self.props, self.decs, set(), manual_acts + auto)
        assert self.run_once(props, decs, routed) == ([], [])

    def test_partial_apply_crash_replays(self):
        manual_acts, auto = self.run_once(self.props, self.decs, set())
        first = manual_acts + auto
        audited = {event_id(a): audit_after(a) for a in first}
        # crash: proposal statuses and audit written, decisions not stamped applied
        props, decs, routed = apply_state(self.props, self.decs, set(), first, write_applied=False)
        m2, a2 = self.run_once(props, decs, routed, audited=audited)
        assert m2 == manual_acts  # replayed, not re-planned into "already decided" refusals
        assert a2 == []
        assert {event_id(a) for a in m2 + a2} <= set(audited)
        # finishing the apply then converges
        props, decs, routed = apply_state(props, decs, routed, m2 + a2)
        assert self.run_once(props, decs, routed, audited=audited) == ([], [])


# ---------------------------------------------------------------- superseded rows

class TestSuperseded:
    def test_old_proposed_hidden_by_newer_accepted(self):
        x = prop("x", "claim_ref", created_at=T0)
        y = prop("y", "claim_ref", status="accepted", created_at=T0 + timedelta(hours=1), run_id="run-2")
        assert latest_pending([x, y]) == []

    def test_old_proposed_hidden_by_newer_proposed(self):
        x = prop("x", "claim_ref", created_at=T0)
        y = prop("y", "claim_ref", created_at=T0 + timedelta(hours=1), run_id="run-2")
        assert [p.proposal_id for p in latest_pending([x, y])] == ["y"]

    @pytest.mark.parametrize("status", ["edited", "rejected"])
    def test_old_proposed_hidden_by_newer_decided(self, status):
        x = prop("x", "claim_ref", created_at=T0)
        y = prop("y", "claim_ref", status=status, created_at=T0 + timedelta(hours=1), run_id="run-2")
        assert latest_pending([x, y]) == []

    def test_manual_on_superseded_row_refused(self):
        x = prop("x", "claim_ref", created_at=T0)
        y = prop("y", "claim_ref", created_at=T0 + timedelta(hours=1), run_id="run-2")
        [a] = manual([decision("d1", "x", "accept")], [x, y], profiles_for(x, y), TERMS)
        assert_refused(a, "superseded")

    def test_old_x_newer_proposed_y_plan(self):
        x = prop("x", "claim_ref", created_at=T0)
        y = prop("y", "claim_ref", created_at=T0 + timedelta(hours=1), run_id="run-2")
        acts = full_plan([decision("d1", "x", "accept")], [x, y], profiles_for(x, y), TERMS, set())
        kinds = {a.proposal_id: a.kind for a in acts}
        assert kinds == {"x": "refused", "y": "accepted"}
        approved = [a.approved for a in acts if a.approved is not None]
        assert len(approved) == 1
        assert (approved[0].table_name, approved[0].column_name) == (TABLE, "claim_ref")

    def test_old_x_newer_edited_y(self):
        x = prop("x", "claim_ref", created_at=T0)
        y = prop("y", "claim_ref", status="edited", created_at=T0 + timedelta(hours=1), run_id="run-2")
        prof = profiles_for(x, y)
        [a] = manual([decision("d1", "x", "accept")], [x, y], prof, TERMS)
        assert_refused(a, "superseded")
        assert plan_auto([x, y], prof, TERMS, set()) == []


# ---------------------------------------------------------------- plan()

MIXED = [  # built lazily per test so a constructor change fails tests, not collection
    # same column, two proposed rows, a decision on each
    lambda: ([prop("x", "a", created_at=T0), prop("y", "a", created_at=T0 + timedelta(hours=1), run_id="run-2")],
             [decision("d1", "x", "accept", minutes=1),
              decision("d2", "y", "edit", term_id="claim_amount", minutes=2)]),
    # two decisions on one row; the second is refused and auto must not add another approved row
    lambda: ([prop("p1", "a")],
             [decision("d1", "p1", "accept", minutes=1), decision("d2", "p1", "edit", minutes=2)]),
    # refused decision on an auto-acceptable row, two tables with the same column name
    lambda: ([prop("p1", "a"), prop("p2", "a", table=OTHER_TABLE, run_id="run-p")],
             [decision("d1", "p1", "bogus"), decision("d2", "p2", "accept", minutes=1)]),
    # no decisions at all
    lambda: ([prop("p1", "a"), prop("p2", "b", "claim_amount", sensitivity="confidential")], []),
]


class TestPlan:
    def test_refused_rule_reject_still_routed(self):
        p = prop("p1", "col17", None, method="rule", confidence=1.0)
        acts = full_plan([decision("d1", "p1", "reject")], [p], profiles_for(p), TERMS, set())
        assert [(a.proposal_id, a.kind, a.decision_id) for a in acts] == [
            ("p1", "refused", "d1"), ("p1", "review_routed", None)]

    def test_refused_rule_reject_already_routed(self):
        p = prop("p1", "col17", None, method="rule", confidence=1.0)
        acts = full_plan([decision("d1", "p1", "reject")], [p], profiles_for(p), TERMS, {"p1"})
        assert [(a.kind, a.decision_id) for a in acts] == [("refused", "d1")]

    def test_refused_bad_action_does_not_block_auto(self):
        p = prop("p1", "claim_ref")
        acts = full_plan([decision("d1", "p1", "approve")], [p], profiles_for(p), TERMS, set())
        assert [(a.kind, a.decision_id) for a in acts] == [("refused", "d1"), ("accepted", None)]
        assert acts[1].approved.approved_by == AUTO_ACTOR

    def test_manual_decision_blocks_auto(self):
        p = prop("p1", "contact", "email_address", sensitivity="internal")  # auto would accept
        acts = full_plan([decision("d1", "p1", "reject")], [p], profiles_for(p), TERMS, set())
        assert [(a.kind, a.decision_id) for a in acts] == [("rejected", "d1")]

    @pytest.mark.parametrize("make", MIXED)
    def test_never_two_approved_rows_per_column(self, make):
        props, decs = make()
        acts = full_plan(decs, props, profiles_for(*props), TERMS, set())
        keys = [(a.approved.table_name, a.approved.column_name) for a in acts if a.approved is not None]
        assert keys, "fixture should approve something"
        assert len(keys) == len(set(keys))


# ---------------------------------------------------------------- retired term

class TestRetiredTerm:
    def test_auto_routes_retired_term(self):
        p = prop("p1", "claim_ref", "retired_term", confidence=0.99)
        [a] = plan_auto([p], profiles_for(p), TERMS, set())
        assert a.kind == "review_routed" and a.approved is None

    def test_manual_accept_retired_term_refused(self):
        p = prop("p1", "claim_ref", "retired_term")
        [a] = manual([decision("d1", "p1", "accept")], [p], profiles_for(p), TERMS)
        assert_refused(a, "business_terms")

    def test_manual_edit_to_valid_term_allowed(self):
        p = prop("p1", "claim_ref", "retired_term")
        d = decision("d1", "p1", "edit", term_id="claim_reference")
        [a] = manual([d], [p], profiles_for(p), TERMS)
        assert a.kind == "edited"
        assert a.approved.term_id == "claim_reference"
        assert a.golden.proposed_term_id == "retired_term"

    def test_edit_keeping_a_retired_proposed_term_is_refused(self):
        # keeping the proposal's term (term_id None) when that term is gone would drop its sensitivity floor
        p = prop("p1", "claim_ref", "retired_term")
        [a] = manual([decision("d1", "p1", "edit", sensitivity="internal")], [p], profiles_for(p), TERMS)
        assert_refused(a, "business_terms")


# ---------------------------------------------------------------- accept with fields

@pytest.mark.parametrize("fields", [{"term_id": "claim_amount"}, {"description": "x"}, {"sensitivity": "pii"}])
def test_accept_with_fields_refused(fields):
    p = prop("p1", "claim_ref")
    [a] = manual([decision("d1", "p1", "accept", **fields)], [p], profiles_for(p), TERMS)
    assert_refused(a)
    assert "edit" in a.reason.lower()


# ---------------------------------------------------------------- event ids

class TestEventIds:
    def test_audit_event_id_each_input_changes_id(self):
        base = audit_event_id("accepted", "p1", "d1")
        variants = [audit_event_id("edited", "p1", "d1"), audit_event_id("accepted", "p2", "d1"),
                    audit_event_id("accepted", "p1", "d2"), audit_event_id("accepted", "p1", None)]
        assert len({base, *variants}) == 5

    def test_manual_event_id_depends_on_decision_id(self):
        assert manual_event_id("d1") != manual_event_id("d2")

    def test_manual_event_id_is_stable_across_outcomes(self):
        # the same decision gets the same audit id whatever it planned to, so a replay finds it
        p = prop("p1", "claim_ref")
        d = decision("d1", "p1", "accept")
        [ok] = manual([d], [p], profiles_for(p), TERMS)
        [refused] = manual([d], [replace(p, status="accepted")], profiles_for(p), TERMS)
        assert ok.kind != refused.kind
        assert audit_rows([ok], {"p1": p}, {}, TABLE, "unknown")[0][0] == manual_event_id("d1")
        assert audit_rows([refused], {"p1": p}, {}, TABLE, "unknown")[0][0] == manual_event_id("d1")


# ---------------------------------------------------------------- approver identity = Delta committer

class TestCommitterIdentity:
    def test_committer_unknown_refused(self):
        p = prop("p1", "claim_ref")
        [a] = manual([decision("d1", "p1", "accept", committed_by=None)], [p], profiles_for(p), TERMS)
        assert_refused(a, "committer unknown")

    def test_typed_name_not_committer_refused(self):
        p = prop("p1", "claim_ref")
        d = decision("d1", "p1", "accept", decided_by="owner.a@x", committed_by="someone.else@x")
        [a] = manual([d], [p], profiles_for(p), TERMS)
        assert_refused(a, "does not match committer")

    def test_case_and_whitespace_insensitive_and_committer_is_identity(self):
        p = prop("p1", "claim_ref")
        d = decision("d1", "p1", "accept", decided_by=" Owner.A@x ", committed_by="owner.a@x")
        [a] = manual([d], [p], profiles_for(p), TERMS)
        assert a.kind == "accepted"
        assert a.approved.approved_by == "owner.a@x"
        assert a.actor == "owner.a@x"

    def test_edit_golden_and_actor_use_committer(self):
        p = prop("p1", "amt", "claim_reference")
        d = decision("d1", "p1", "edit", term_id="claim_amount", decided_by="Owner.A@x", committed_by="owner.a@x")
        [a] = manual([d], [p], profiles_for(p), TERMS)
        assert a.kind == "edited"
        assert (a.approved.approved_by, a.golden.edited_by, a.actor) == ("owner.a@x",) * 3

    def test_reject_actor_is_committer(self):
        p = prop("p1", "claim_ref", method="llm")
        d = decision("d1", "p1", "reject", decided_by="OWNER.A@x", committed_by="owner.a@x")
        [a] = manual([d], [p], profiles_for(p), TERMS)
        assert a.kind == "rejected" and a.actor == "owner.a@x"

    @pytest.mark.parametrize("status", ["eval", "accepted", "rejected"])
    def test_identity_checked_before_status(self, status):
        p = prop("p1", "claim_ref", status=status)
        [a] = manual([decision("d1", "p1", "accept", committed_by=None)], [p], profiles_for(p), TERMS)
        assert_refused(a, "committer unknown")
        d = decision("d2", "p1", "accept", decided_by="owner.a", committed_by="intruder")
        [b] = manual([d], [p], profiles_for(p), TERMS)
        assert_refused(b, "does not match committer")

    def test_identity_checked_before_action_and_term_checks(self):
        p = prop("p1", "claim_ref")
        d = decision("d1", "p1", "frobnicate", decided_by="owner.a", committed_by="intruder")
        [a] = manual([d], [p], profiles_for(p), TERMS)
        assert_refused(a, "does not match committer")

    def test_unknown_proposal_checked_first(self):
        p = prop("p1", "claim_ref")
        [a] = manual([decision("d1", "nope", "accept", committed_by=None)], [p], profiles_for(p), TERMS)
        assert_refused(a)
        assert "committer" not in a.reason

    def test_forged_row_does_not_consume_proposal(self):
        p = prop("p1", "claim_ref")
        forged = decision("d1", "p1", "reject", minutes=1, decided_by="owner.a", committed_by="intruder")
        real = decision("d2", "p1", "accept", minutes=2, decided_by="owner.b", committed_by="owner.b")
        acts = manual([forged, real], [p], profiles_for(p), TERMS)
        assert [(a.decision_id, a.kind) for a in acts] == [("d1", "refused"), ("d2", "accepted")]
        assert acts[1].approved.approved_by == "owner.b"

    def test_plan_applies_identity_checks(self):
        p = prop("p1", "contact", "email_address", sensitivity="internal", confidence=0.5)  # auto routes it
        d = decision("d1", "p1", "accept", decided_by="owner.a", committed_by="intruder")
        acts = full_plan([d], [p], profiles_for(p), TERMS, set())
        assert_refused(acts[0], "does not match committer")
        assert all(a.approved is None for a in acts)


# ---------------------------------------------------------------- tamper: CDF change types + attribution_error

class TestTamper:
    def test_modified_after_insert(self):
        changes = [("d1", 3, "insert"), ("d1", 4, "update_preimage"), ("d1", 4, "update_postimage"),
                   ("d2", 5, "insert"), ("d3", 6, "insert"), ("d3", 7, "delete")]
        assert modified_after_insert(changes) == {"d1", "d3"}

    @pytest.mark.parametrize("change", ["update_preimage", "update_postimage", "delete"])
    def test_each_non_insert_change_counts(self, change):
        assert modified_after_insert([("d1", 3, "insert"), ("d1", 4, change)]) == {"d1"}

    def test_inserts_only_is_clean(self):
        assert modified_after_insert([("d1", 3, "insert"), ("d2", 4, "insert")]) == set()
        assert modified_after_insert([]) == set()

    def test_committers_ignore_non_insert_rows(self):
        changes = [("d1", 3, "insert"), ("d1", 4, "update_preimage"), ("d1", 4, "update_postimage"),
                   ("d2", 6, "delete")]
        assert committers(changes, {3: "owner.a@x", 4: "intruder@x", 6: "intruder@x"}) == {"d1": "owner.a@x"}

    def test_attribution_error_is_the_reason(self):
        p = prop("p1", "claim_ref")
        d = decision("d1", "p1", "accept", attribution_error="modified after insert")
        [a] = manual([d], [p], profiles_for(p), TERMS)
        assert_refused(a, "modified after insert")

    def test_attribution_error_before_committer_unknown(self):
        p = prop("p1", "claim_ref")
        d = decision("d1", "p1", "accept", committed_by=None, attribution_error="modified after insert")
        [a] = manual([d], [p], profiles_for(p), TERMS)
        assert_refused(a, "modified after insert")
        assert "committer unknown" not in a.reason

    def test_unknown_proposal_before_attribution_error(self):
        p = prop("p1", "claim_ref")
        d = decision("d1", "nope", "accept", attribution_error="modified after insert")
        [a] = manual([d], [p], profiles_for(p), TERMS)
        assert_refused(a)
        assert "modified after insert" not in a.reason


# ---------------------------------------------------------------- committers(): CDF inserts + history

class TestCommitters:
    def test_maps_decision_to_user(self):
        got = committers([("d1", 3, "insert"), ("d2", 4, "insert")], {3: "owner.a@x", 4: "owner.b@x"})
        assert got == {"d1": "owner.a@x", "d2": "owner.b@x"}

    def test_version_missing_from_history_left_out(self):
        got = committers([("d1", 3, "insert"), ("d2", 9, "insert")], {3: "owner.a@x"})
        assert got == {"d1": "owner.a@x"}

    def test_same_decision_same_user_kept(self):
        got = committers([("d1", 3, "insert"), ("d1", 5, "insert")], {3: "owner.a@x", 5: "owner.a@x"})
        assert got == {"d1": "owner.a@x"}

    def test_same_decision_different_users_left_out(self):
        got = committers([("d1", 3, "insert"), ("d1", 5, "insert"), ("d2", 5, "insert")],
                         {3: "owner.a@x", 5: "intruder@x"})
        assert got == {"d2": "intruder@x"}

    def test_ambiguity_not_order_dependent(self):
        h = {3: "owner.a@x", 5: "intruder@x"}
        fwd = committers([("d1", 3, "insert"), ("d1", 5, "insert")], h)
        rev = committers([("d1", 5, "insert"), ("d1", 3, "insert")], h)
        assert fwd == rev == {}

    def test_empty(self):
        assert committers([], {1: "owner.a@x"}) == {}


# ---------------------------------------------------------------- duplicate decision_id

class TestDuplicateDecisionId:
    def test_two_pending_rows_one_refusal(self):
        p1, p2 = prop("p1", "claim_ref"), prop("p2", "amt", "claim_amount", sensitivity="confidential")
        ds = [decision("d1", "p1", "accept", minutes=1), decision("d1", "p2", "reject", minutes=2)]
        acts = manual(ds, [p1, p2], profiles_for(p1, p2), TERMS)
        assert len(acts) == 1
        assert_refused(acts[0], "duplicate decision_id")
        assert acts[0].decision_id == "d1"

    def test_same_proposal_duplicate_rows(self):
        p = prop("p1", "claim_ref")
        ds = [decision("d1", "p1", "accept", minutes=1), decision("d1", "p1", "accept", minutes=1)]
        acts = manual(ds, [p], profiles_for(p), TERMS)
        assert len(acts) == 1
        assert_refused(acts[0], "duplicate decision_id")

    def test_other_decisions_unaffected(self):
        p1, p2 = prop("p1", "claim_ref"), prop("p2", "amt", "claim_amount", sensitivity="confidential")
        ds = [decision("d1", "p1", "accept", minutes=1), decision("d1", "p1", "reject", minutes=2),
              decision("d2", "p2", "accept", minutes=3)]
        acts = manual(ds, [p1, p2], profiles_for(p1, p2), TERMS)
        assert [(a.decision_id, a.kind) for a in acts] == [("d1", "refused"), ("d2", "accepted")]


# ---------------------------------------------------------------- approvers (gov.table_approvers) + job principal

class TestApprovers:
    APPROVERS = {TABLE: {"owner.a@x"}, OTHER_TABLE: {"owner.b@x"}}

    def run(self, d: ReviewDecision, p: ProposalRow, **kw) -> Action:
        kw.setdefault("approvers", self.APPROVERS)
        kw.setdefault("job_principal", JOB)
        [a] = plan_manual([d], [p], profiles_for(p), TERMS, **kw)
        return a

    def test_listed_approver_accepted(self):
        a = self.run(decision("d1", "p1", "accept", decided_by="owner.a@x"), prop("p1", "claim_ref"))
        assert a.kind == "accepted" and a.approved.approved_by == "owner.a@x"

    def test_unlisted_committer_refused(self):
        a = self.run(decision("d1", "p1", "accept", decided_by="owner.c@x"), prop("p1", "claim_ref"))
        assert_refused(a, "not an approver")

    def test_approver_of_another_table_refused(self):
        a = self.run(decision("d1", "p1", "accept", decided_by="owner.b@x"), prop("p1", "claim_ref"))
        assert_refused(a, "not an approver")

    def test_table_missing_from_list_refused(self):
        a = self.run(decision("d1", "p1", "accept", decided_by="owner.a@x"), prop("p1", "claim_ref"),
                     approvers={})
        assert_refused(a, "not an approver")

    def test_approver_match_case_insensitive(self):
        a = self.run(decision("d1", "p1", "accept", decided_by="owner.a@x"), prop("p1", "claim_ref"),
                     approvers={TABLE: {"Owner.A@X"}})
        assert a.kind == "accepted"

    def test_approvers_none_is_an_error(self):
        # no off switch: an approver list is required
        p = prop("p1", "claim_ref")
        d = decision("d1", "p1", "accept", decided_by="anyone@x")
        with pytest.raises(ValueError):
            plan_manual([d], [p], profiles_for(p), TERMS, approvers=None, job_principal=JOB)
        with pytest.raises(ValueError):
            plan([d], [p], profiles_for(p), TERMS, set(), approvers=None, job_principal=JOB)

    def test_job_principal_refused_even_if_listed(self):
        a = self.run(decision("d1", "p1", "accept", decided_by=JOB), prop("p1", "claim_ref"),
                     approvers={TABLE: {JOB}})
        assert_refused(a, "job principal")

    def test_job_principal_case_insensitive(self):
        a = self.run(decision("d1", "p1", "accept", decided_by=JOB.upper()), prop("p1", "claim_ref"),
                     approvers={TABLE: {JOB.upper()}})
        assert_refused(a, "job principal")

    def test_job_principal_refused_with_explicit_allow_list(self):
        a = self.run(decision("d1", "p1", "accept", decided_by=JOB), prop("p1", "claim_ref"),
                     approvers={TABLE: {"owner.a@x", JOB}, OTHER_TABLE: {JOB}})
        assert_refused(a, "job principal")

    # order: unknown proposal -> attribution_error -> committer unknown -> mismatch -> job principal
    #        -> not an approver -> status and other checks
    def test_mismatch_before_job_principal(self):
        a = self.run(decision("d1", "p1", "accept", decided_by="owner.a@x", committed_by=JOB),
                     prop("p1", "claim_ref"))
        assert_refused(a, "does not match committer")

    def test_job_principal_before_not_an_approver(self):
        a = self.run(decision("d1", "p1", "accept", decided_by=JOB), prop("p1", "claim_ref"))
        assert_refused(a, "job principal")
        assert "not an approver" not in a.reason

    def test_attribution_error_before_approver_checks(self):
        a = self.run(decision("d1", "p1", "accept", decided_by="owner.c@x", attribution_error="modified after insert"),
                     prop("p1", "claim_ref"))
        assert_refused(a, "modified after insert")

    @pytest.mark.parametrize("status", ["eval", "accepted"])
    def test_not_an_approver_before_status(self, status):
        a = self.run(decision("d1", "p1", "accept", decided_by="owner.c@x"), prop("p1", "claim_ref", status=status))
        assert_refused(a, "not an approver")

    def test_not_an_approver_before_action_check(self):
        a = self.run(decision("d1", "p1", "frobnicate", decided_by="owner.c@x"), prop("p1", "claim_ref"))
        assert_refused(a, "not an approver")

    def test_refused_for_approver_does_not_consume_proposal(self):
        p = prop("p1", "claim_ref")
        ds = [decision("d1", "p1", "reject", minutes=1, decided_by="owner.c@x"),
              decision("d2", "p1", "accept", minutes=2, decided_by="owner.a@x")]
        acts = plan_manual(ds, [p], profiles_for(p), TERMS, approvers=self.APPROVERS, job_principal=JOB)
        assert [(a.decision_id, a.kind) for a in acts] == [("d1", "refused"), ("d2", "accepted")]

    def test_plan_passes_approvers_and_job_principal(self):
        p = prop("p1", "claim_ref", confidence=0.5)
        acts = plan([decision("d1", "p1", "accept", decided_by="owner.c@x")], [p], profiles_for(p), TERMS, set(),
                    approvers=self.APPROVERS, job_principal=JOB)
        assert_refused(acts[0], "not an approver")
        acts = plan([decision("d1", "p1", "accept", decided_by=JOB)], [p], profiles_for(p), TERMS, set(),
                    approvers={TABLE: {JOB}}, job_principal=JOB)
        assert_refused(acts[0], "job principal")


# ---------------------------------------------------------------- replay after a crash

def sample_actions() -> list[Action]:
    p1 = prop("p1", "claim_ref")
    p2 = prop("p2", "amt", "claim_reference")
    p3 = prop("p3", "notes", "free_notes", method="llm")
    p4 = prop("p4", "status", "policy_status", sensitivity="public")
    p5 = prop("p5", "col17", None, method="rule", confidence=1.0)
    props = [p1, p2, p3, p4, p5]
    ds = [decision("d1", "p1", "accept", minutes=1),
          decision("d2", "p2", "edit", term_id="claim_amount", description="Amount.", minutes=2),
          decision("d3", "p3", "reject", minutes=3),
          decision("d4", "nope", "accept", minutes=4)]
    acts = full_plan(ds, props, profiles_for(*props), TERMS, set())
    kinds = {a.kind for a in acts}
    assert {"accepted", "edited", "rejected", "refused", "review_routed"} <= kinds
    return acts


class TestReplay:
    def test_round_trip_every_kind(self):
        for a in sample_actions():
            assert action_from_audit(audit_after(a)) == a

    def test_round_trip_through_json(self):
        for a in sample_actions():
            assert action_from_audit(json.loads(json.dumps(audit_after(a)))) == a

    def test_edited_round_trip_keeps_golden(self):
        [a] = [x for x in sample_actions() if x.kind == "edited"]
        b = action_from_audit(json.loads(json.dumps(audit_after(a))))
        assert b.golden == a.golden and b.golden is not None
        assert b.approved == a.approved

    def test_pending_decision_with_audit_is_replayed(self):
        p = prop("p1", "claim_ref", confidence=0.3)
        d = decision("d1", "p1", "accept")
        [original] = full_plan([d], [p], profiles_for(p), TERMS, set())
        assert original.kind == "accepted"
        audited = {manual_event_id("d1"): audit_after(original)}
        # crash after status write: proposal already accepted, decision still pending
        acts = full_plan([d], [replace(p, status="accepted")], profiles_for(p), TERMS, set(), audited=audited)
        assert acts == [original]
        assert event_id(acts[0]) == manual_event_id("d1")

    def test_without_audit_same_state_is_refused(self):
        p = prop("p1", "claim_ref", status="accepted")
        acts = full_plan([decision("d1", "p1", "accept")], [p], profiles_for(p), TERMS, set(), audited={})
        assert_refused(acts[0])

    def test_auto_accept_with_audit_is_replayed(self):
        p = prop("p1", "claim_ref")
        [original] = full_plan([], [p], profiles_for(p), TERMS, set())
        assert original.kind == "accepted"
        audited = {audit_event_id("accepted", "p1", None): audit_after(original)}
        # inputs changed since (profile gone): a re-plan would route to review; replay returns the accept
        acts = full_plan([], [p], {}, TERMS, set(), audited=audited)
        assert acts == [original]

    def test_audited_none_is_ordinary_plan(self):
        p = prop("p1", "claim_ref")
        assert full_plan([], [p], profiles_for(p), TERMS, set(), audited=None) == \
            full_plan([], [p], profiles_for(p), TERMS, set())


# ---------------------------------------------------------------- audit_rows()

ACTION_NAMES = {"accepted": "proposal_accepted", "edited": "proposal_edited", "rejected": "proposal_rejected",
                "review_routed": "review_routed", "refused": "decision_refused"}


def _strict_json(s: str):
    def bad(c):
        raise ValueError(f"non-standard JSON constant {c}")
    return json.loads(s, parse_constant=bad)


class TestAuditRows:
    def setup_method(self):
        self.acts = sample_actions()
        self.props = {pid: prop(pid, col) for pid, col in
                      [("p1", "claim_ref"), ("p2", "amt"), ("p3", "notes"), ("p4", "status"), ("p5", "col17")]}
        self.current = {"claim_ref": {"term_id": "old_term", "sensitivity": "internal"}}
        self.rows = audit_rows(self.acts, self.props, self.current, TABLE, "unknown-object")

    def test_one_row_per_action_with_seven_fields(self):
        assert len(self.rows) == len(self.acts)
        assert all(len(r) == 7 for r in self.rows)

    def test_fields(self):
        for a, (eid, gate, action, actor, _obj, before, after) in zip(self.acts, self.rows, strict=True):
            assert eid == event_id(a)
            assert gate == "hydration"
            assert action == ACTION_NAMES[a.kind]
            assert actor == a.actor
            assert after == json.dumps(audit_after(a))
            _strict_json(before)

    def test_object_is_table_or_unknown(self):
        for a, r in zip(self.acts, self.rows, strict=True):
            assert r[4] == ("unknown-object" if a.proposal_id == "nope" else TABLE)

    def test_before_has_proposal_and_current_approved_only_with_approved_row(self):
        for a, r in zip(self.acts, self.rows, strict=True):
            before = _strict_json(r[5])
            assert "proposal" in before
            if a.approved is None:
                assert "approved" not in before
            else:
                assert before["approved"] == self.current.get(a.approved.column_name)

    def test_nan_confidence_written_as_null(self):
        p = prop("p1", "claim_ref", confidence=math.nan)
        acts = full_plan([decision("d1", "p1", "reject")], [p], profiles_for(p), TERMS, set())
        [row] = audit_rows(acts[:1], {"p1": p}, {}, TABLE, "unknown-object")
        before = _strict_json(row[5])
        assert before["proposal"]["confidence"] is None


# ---------------------------------------------------------------- cdf_start_version(): DESCRIBE HISTORY

NOW = T0 + timedelta(days=60)
WINDOW = timedelta(days=30)
CDF_ON = {"properties": '{"delta.enableChangeDataFeed":"true"}'}
CDF_OFF = {"properties": '{"delta.enableChangeDataFeed":"false"}'}
CDF_UNSET = {"properties": '["delta.enableChangeDataFeed"]'}


def row(v: int, op: str, params: dict | None = None, age_days: float = 1.0):
    return (v, NOW - timedelta(days=age_days), op, params or {})


def start(history) -> int | None:
    return cdf_start_version(history, NOW, WINDOW)


class TestCdfStartVersion:
    def test_empty_history(self):
        assert start([]) is None

    def test_never_enabled(self):
        assert start([row(0, "CREATE TABLE", {"properties": "{}"}), row(1, "WRITE", {"mode": "Append"})]) is None

    def test_no_toggle_visible(self):
        assert start([row(10, "WRITE"), row(11, "MERGE")]) is None

    @pytest.mark.parametrize("op", ["CREATE TABLE", "CREATE OR REPLACE TABLE", "CREATE TABLE AS SELECT"])
    def test_enabled_at_create(self, op):
        assert start([row(0, op, CDF_ON), row(1, "WRITE"), row(2, "MERGE")]) == 0

    def test_first_enable_wins(self):
        assert start([row(0, "CREATE TABLE"), row(1, "SET TBLPROPERTIES", CDF_ON),
                      row(5, "SET TBLPROPERTIES", CDF_ON)]) == 1

    def test_repeat_enable_after_create_does_not_move_start(self):
        assert start([row(0, "CREATE TABLE", CDF_ON), row(5, "SET TBLPROPERTIES", CDF_ON)]) == 0

    def test_case_insensitive(self):
        assert start([row(0, "CREATE TABLE"),
                      row(4, "set tblproperties", {"properties": '{"DELTA.ENABLECHANGEDATAFEED":"TRUE"}'})]) == 4

    def test_plain_dict_value(self):
        assert start([row(0, "CREATE TABLE"), row(1, "SET TBLPROPERTIES", {"delta.enableChangeDataFeed": "true"})]) == 1

    def test_set_false_latest_is_none(self):
        assert start([row(0, "CREATE TABLE", CDF_ON), row(1, "WRITE"), row(2, "SET TBLPROPERTIES", CDF_OFF)]) is None

    def test_unset_latest_is_none(self):
        assert start([row(0, "CREATE TABLE", CDF_ON), row(2, "UNSET TBLPROPERTIES", CDF_UNSET)]) is None

    def test_unset_case_insensitive(self):
        unset = {"properties": '["DELTA.ENABLECHANGEDATAFEED"]'}
        assert start([row(0, "CREATE TABLE", CDF_ON), row(2, "unset tblproperties", unset)]) is None

    @pytest.mark.parametrize("off", [CDF_OFF, CDF_UNSET], ids=["set-false", "unset"])
    def test_first_enable_after_latest_disable(self, off):
        op = "SET TBLPROPERTIES" if off is CDF_OFF else "UNSET TBLPROPERTIES"
        h = [row(0, "CREATE TABLE", CDF_ON), row(2, op, off), row(4, "SET TBLPROPERTIES", CDF_ON),
             row(6, "SET TBLPROPERTIES", CDF_ON)]
        assert start(h) == 4

    def test_unsorted_input(self):
        h = [row(6, "SET TBLPROPERTIES", CDF_ON), row(4, "SET TBLPROPERTIES", CDF_ON), row(0, "CREATE TABLE", CDF_ON),
             row(2, "SET TBLPROPERTIES", CDF_OFF), row(7, "WRITE")]
        assert start(h) == 4

    def test_other_property_does_not_count_or_disable(self):
        other = {"properties": '{"delta.appendOnly":"true"}'}
        assert start([row(0, "CREATE TABLE", other)]) is None
        assert start([row(0, "CREATE TABLE", CDF_ON), row(1, "SET TBLPROPERTIES", other)]) == 0
        assert start([row(0, "CREATE TABLE", CDF_ON),
                      row(1, "UNSET TBLPROPERTIES", {"properties": '["delta.appendOnly"]'})]) == 0

    def test_other_operations_ignored(self):
        assert start([row(0, "CREATE TABLE"), row(1, "WRITE", CDF_ON), row(2, "MERGE", CDF_ON)]) is None

    def test_clamped_to_retention_window(self):
        h = [row(0, "CREATE TABLE", CDF_ON, age_days=40), row(1, "WRITE", age_days=35),
             row(2, "WRITE", age_days=10), row(3, "WRITE", age_days=1)]
        assert start(h) == 2

    def test_window_boundary_inclusive(self):
        h = [row(0, "CREATE TABLE", CDF_ON, age_days=40), row(1, "WRITE", age_days=30), row(2, "WRITE")]
        assert start(h) == 1

    def test_recent_enable_not_moved_by_clamp(self):
        h = [row(0, "CREATE TABLE", age_days=40), row(1, "WRITE", age_days=20), row(3, "SET TBLPROPERTIES", CDF_ON,
                                                                                     age_days=5)]
        assert start(h) == 3

    def test_nothing_within_window_is_none(self):
        h = [row(0, "CREATE TABLE", CDF_ON, age_days=50), row(1, "WRITE", age_days=40)]
        assert start(h) is None


class TestCdfEnabledNow:
    NO_TOGGLE = [row(10, "WRITE", age_days=40), row(11, "WRITE", age_days=20), row(12, "MERGE", age_days=1)]

    def test_no_toggle_enabled_now_gives_smallest_version_in_window(self):
        assert cdf_start_version(self.NO_TOGGLE, NOW, WINDOW, enabled_now=True) == 11

    def test_no_toggle_enabled_now_nothing_in_window(self):
        h = [row(10, "WRITE", age_days=40), row(11, "WRITE", age_days=35)]
        assert cdf_start_version(h, NOW, WINDOW, enabled_now=True) is None

    @pytest.mark.parametrize("flag", [None, False])
    def test_no_toggle_without_enabled_now_is_none(self, flag):
        assert cdf_start_version(self.NO_TOGGLE, NOW, WINDOW, enabled_now=flag) is None

    def test_default_is_none(self):
        assert cdf_start_version(self.NO_TOGGLE, NOW, WINDOW) is None

    @pytest.mark.parametrize("off", [CDF_OFF, CDF_UNSET], ids=["set-false", "unset"])
    def test_visible_disable_wins_over_enabled_now(self, off):
        op = "SET TBLPROPERTIES" if off is CDF_OFF else "UNSET TBLPROPERTIES"
        h = [row(0, "CREATE TABLE", CDF_ON), row(2, op, off), row(3, "WRITE")]
        assert cdf_start_version(h, NOW, WINDOW, enabled_now=True) is None

    def test_visible_enable_unchanged_by_enabled_now(self):
        h = [row(0, "CREATE TABLE", age_days=10), row(1, "SET TBLPROPERTIES", CDF_ON, age_days=5), row(2, "WRITE")]
        assert cdf_start_version(h, NOW, WINDOW, enabled_now=True) == 1

    def test_empty_history_enabled_now(self):
        assert cdf_start_version([], NOW, WINDOW, enabled_now=True) is None


# ---------------------------------------------------------------- third review: reused decision_id / replay gate

class ReplayFixture:
    """owner.a accepted p1 under d1; the audit holds that action. Each test breaks one replay condition."""

    def setup_method(self):
        self.p = prop("p1", "claim_ref", confidence=0.3)
        self.d = decision("d1", "p1", "accept", decided_by="owner.a")
        self.approvers = {TABLE: {"owner.a", "owner.b", "mallory"}, OTHER_TABLE: {"owner.a"}}
        [self.original] = plan_manual([self.d], [self.p], profiles_for(self.p), TERMS,
                                      approvers=self.approvers, job_principal=JOB)
        assert self.original.kind == "accepted" and self.original.actor == "owner.a"
        self.audited = {manual_event_id("d1"): audit_after(self.original)}
        self.decided = replace(self.p, status="accepted")  # crash state: status written, decision still pending

    def run(self, d=None, proposals=None, audited=None, **kw) -> list[Action]:
        kw.setdefault("approvers", self.approvers)
        kw.setdefault("job_principal", JOB)
        proposals = [self.decided] if proposals is None else proposals
        return plan_manual([d or self.d], proposals, profiles_for(*proposals), TERMS,
                           audited=self.audited if audited is None else audited, **kw)

    def assert_not_replayed(self, acts: list[Action], text: str | None = None):
        assert len(acts) == 1
        assert acts[0] != self.original
        assert_refused(acts[0], text)


class TestReplayGate(ReplayFixture):
    def test_baseline_replays(self):
        assert self.run() == [self.original]

    def test_baseline_replays_when_proposal_still_proposed_even_if_inputs_changed(self):
        acts = plan_manual([self.d], [self.p], {}, TERMS, approvers=self.approvers, job_principal=JOB,
                           audited=self.audited)  # no profile: a re-plan would refuse
        assert acts == [self.original]

    # (a) identity and authority
    def test_attribution_error_no_replay(self):
        self.assert_not_replayed(self.run(replace(self.d, attribution_error="modified after insert")),
                                 "modified after insert")

    def test_committer_unknown_no_replay(self):
        self.assert_not_replayed(self.run(replace(self.d, committed_by=None)), "committer unknown")

    def test_committer_mismatch_no_replay(self):
        self.assert_not_replayed(self.run(replace(self.d, committed_by="mallory")), "does not match committer")

    def test_job_principal_no_replay(self):
        # audited actor set to the job principal so only the job-principal condition fails
        forged_after = audit_after(replace(self.original, actor=JOB,
                                           approved=replace(self.original.approved, approved_by=JOB)))
        d = replace(self.d, decided_by=JOB, committed_by=JOB)
        acts = self.run(d, audited={manual_event_id("d1"): forged_after},
                        approvers={TABLE: {JOB, "owner.a"}})
        assert len(acts) == 1
        assert_refused(acts[0], "job principal")

    def test_not_an_approver_no_replay(self):
        self.assert_not_replayed(self.run(approvers={TABLE: {"owner.b"}}), "not an approver")

    def test_reviewer_repro_mallory(self):
        # owner.a edited p2 under d7; a new row reuses d7: mallory, tampered, pointing at an unknown proposal
        p2 = prop("p2", "amt", "claim_reference")
        d7 = decision("d7", "p2", "edit", term_id="claim_amount", decided_by="owner.a")
        [edited] = plan_manual([d7], [p2], profiles_for(p2), TERMS, approvers=self.approvers, job_principal=JOB)
        assert edited.kind == "edited" and edited.actor == "owner.a"
        forged = replace(d7, proposal_id="zzz", decided_by="owner.a", committed_by="mallory",
                         attribution_error="modified after insert")
        acts = plan_manual([forged], [replace(p2, status="edited")], profiles_for(p2), TERMS,
                           approvers=self.approvers, job_principal=JOB,
                           audited={manual_event_id("d7"): audit_after(edited)})
        assert len(acts) == 1
        assert acts[0] != edited
        assert_refused(acts[0])
        assert acts[0].proposal_id == "zzz"

    # (b) audited after must be for this decision and this proposal
    def test_audited_decision_id_differs_no_replay(self):
        other = audit_after(replace(self.original, decision_id="d9"))
        self.assert_not_replayed(self.run(audited={manual_event_id("d1"): other}))

    def test_audited_proposal_id_differs_no_replay(self):
        # reused d1 now names p2 (pending, auto-acceptable): it must be planned fresh, not replayed as p1
        p2 = prop("p2", "amt", "claim_amount", sensitivity="confidential")
        acts = self.run(replace(self.d, proposal_id="p2"), proposals=[self.decided, p2])
        assert len(acts) == 1
        assert acts[0] != self.original
        assert acts[0].proposal_id == "p2"

    # (c) audited actor must be this committer
    def test_audited_actor_differs_no_replay(self):
        d = replace(self.d, decided_by="owner.b", committed_by="owner.b")
        self.assert_not_replayed(self.run(d))

    def test_audited_actor_match_case_insensitive_replays(self):
        d = replace(self.d, decided_by="OWNER.A", committed_by="Owner.A")
        assert self.run(d) == [self.original]

    # (d) proposal exists with status proposed or the audited new_status
    def test_proposal_missing_no_replay(self):
        other = prop("p9", "x")
        self.assert_not_replayed(self.run(proposals=[other]))

    def test_proposal_status_differs_no_replay(self):
        self.assert_not_replayed(self.run(proposals=[replace(self.p, status="rejected")]))


class TestAppliedIds(ReplayFixture):
    def test_applied_id_refused(self):
        p = prop("p5", "claim_ref")
        d = decision("d5", "p5", "accept")
        acts = manual([d], [p], profiles_for(p), TERMS, applied_ids={"d5"})
        assert len(acts) == 1
        assert_refused(acts[0], "decision_id already used")

    def test_applied_id_checked_before_replay(self):
        self.assert_not_replayed(self.run(applied_ids=frozenset({"d1"})), "decision_id already used")

    def test_duplicate_pending_checked_before_applied(self):
        p = prop("p5", "claim_ref")
        ds = [decision("d5", "p5", "accept", minutes=1), decision("d5", "p5", "reject", minutes=2)]
        acts = manual(ds, [p], profiles_for(p), TERMS, applied_ids={"d5"})
        assert len(acts) == 1
        assert_refused(acts[0], "duplicate decision_id")

    def test_other_ids_unaffected(self):
        p = prop("p5", "claim_ref")
        acts = manual([decision("d6", "p5", "accept")], [p], profiles_for(p), TERMS, applied_ids={"d5"})
        assert acts[0].kind == "accepted"

    def test_plan_passes_applied_ids(self):
        p = prop("p5", "claim_ref", confidence=0.3)
        acts = full_plan([decision("d5", "p5", "accept")], [p], profiles_for(p), TERMS, set(), applied_ids={"d5"})
        assert_refused(acts[0], "decision_id already used")
        assert all(a.approved is None for a in acts)


class TestEventIdFor(ReplayFixture):
    def test_manual_not_audited(self):
        assert event_id_for(self.original) == manual_event_id("d1")
        assert event_id_for(self.original, None) == manual_event_id("d1")

    def test_manual_audited_same_after(self):
        assert event_id_for(self.original, self.audited) == manual_event_id("d1")

    def test_manual_audited_different_after_gets_refusal_id(self):
        refusal = self.run(applied_ids=frozenset({"d1"}))[0]
        eid = event_id_for(refusal, self.audited)
        assert eid == audit_event_id("refused", refusal.proposal_id, "d1")
        assert eid != manual_event_id("d1")

    def test_auto_action(self):
        p = prop("p3", "claim_ref")
        [a] = plan_auto([p], profiles_for(p), TERMS, set())
        assert event_id_for(a) == audit_event_id("accepted", "p3", None)
        assert event_id_for(a, {audit_event_id("accepted", "p3", None): {"x": 1}}) == \
            audit_event_id("accepted", "p3", None)

    def test_audit_rows_reused_id_refusal_has_own_row(self):
        refusal = self.run(applied_ids=frozenset({"d1"}))[0]
        rows = audit_rows([refusal], {"p1": self.decided}, {}, TABLE, "unknown-object", audited=self.audited)
        assert len(rows) == 1
        eid, _gate, action, *_ = rows[0]
        assert action == "decision_refused"
        assert eid == audit_event_id("refused", "p1", "d1")
        assert eid not in self.audited


# ---------------------------------------------------------------- hold (change data feed unreadable)

class TestHold:
    def test_held_decision_produces_no_action(self):
        p = prop("p1", "claim_ref")
        assert manual([decision("d1", "p1", "accept", hold=True)], [p], profiles_for(p), TERMS) == []

    def test_held_decision_does_not_affect_others(self):
        p1, p2 = prop("p1", "claim_ref"), prop("p2", "amt", "claim_amount", sensitivity="confidential")
        ds = [decision("d1", "p1", "reject", hold=True), decision("d2", "p2", "accept", minutes=1)]
        acts = manual(ds, [p1, p2], profiles_for(p1, p2), TERMS)
        assert [(a.decision_id, a.kind) for a in acts] == [("d2", "accepted")]

    def test_plan_skips_held_proposal_for_auto(self):
        p1 = prop("p1", "claim_ref")                                 # auto would accept
        p2 = prop("p2", "col17", None, method="rule", confidence=1.0)  # auto would route
        p3 = prop("p3", "amt", "claim_amount", sensitivity="confidential")
        ds = [decision("d1", "p1", "reject", hold=True), decision("d2", "p2", "edit", hold=True)]
        acts = full_plan(ds, [p1, p2, p3], profiles_for(p1, p2, p3), TERMS, set())
        assert [(a.proposal_id, a.kind) for a in acts] == [("p3", "accepted")]

    def test_held_decision_not_audited_or_applied(self):
        p = prop("p1", "claim_ref")
        acts = full_plan([decision("d1", "p1", "accept", hold=True)], [p], profiles_for(p), TERMS, set())
        assert acts == []
        assert audit_rows(acts, {"p1": p}, {}, TABLE, "unknown-object") == []
        _, decs, _ = apply_state([p], [decision("d1", "p1", "accept", hold=True)], set(), acts)
        assert decs[0].applied_at is None


# ---------------------------------------------------------------- fourth review: table-wide duplicate decision_ids

class TestPendingCounts:
    def test_one_local_row_but_two_pending_table_wide_is_duplicate(self):
        p = prop("p1", "claim_ref")
        acts = manual([decision("d1", "p1", "accept")], [p], profiles_for(p), TERMS, pending_counts={"d1": 2})
        assert len(acts) == 1
        assert_refused(acts[0], "duplicate decision_id")
        assert acts[0].decision_id == "d1"

    def test_through_plan_nothing_decided(self):
        p = prop("p1", "claim_ref", confidence=0.3)  # auto would only route it, never accept
        acts = full_plan([decision("d1", "p1", "accept")], [p], profiles_for(p), TERMS, set(),
                         pending_counts={"d1": 2})
        assert_refused(acts[0], "duplicate decision_id")
        assert all(a.approved is None and a.new_status is None for a in acts)

    def test_count_of_one_is_not_duplicate(self):
        p = prop("p1", "claim_ref")
        [a] = manual([decision("d1", "p1", "accept")], [p], profiles_for(p), TERMS, pending_counts={"d1": 1})
        assert a.kind == "accepted"

    def test_local_count_still_counts_when_table_wide_is_lower(self):
        p = prop("p1", "claim_ref")
        ds = [decision("d1", "p1", "accept", minutes=1), decision("d1", "p1", "reject", minutes=2)]
        acts = manual(ds, [p], profiles_for(p), TERMS, pending_counts={"d1": 1})
        assert len(acts) == 1
        assert_refused(acts[0], "duplicate decision_id")

    def test_other_ids_counts_do_not_matter(self):
        p = prop("p1", "claim_ref")
        [a] = manual([decision("d1", "p1", "accept")], [p], profiles_for(p), TERMS, pending_counts={"d9": 3})
        assert a.kind == "accepted"

    def test_none_is_local_only(self):
        p = prop("p1", "claim_ref")
        [a] = manual([decision("d1", "p1", "accept")], [p], profiles_for(p), TERMS, pending_counts=None)
        assert a.kind == "accepted"
        ds = [decision("d1", "p1", "accept", minutes=1), decision("d1", "p1", "reject", minutes=2)]
        acts = manual(ds, [p], profiles_for(p), TERMS, pending_counts=None)
        assert len(acts) == 1
        assert_refused(acts[0], "duplicate decision_id")


# ---------------------------------------------------------------- fourth review: every reuse refusal audited

class TestInsertVersions:
    def test_max_insert_version_per_decision(self):
        changes = [("d1", 3, "insert"), ("d1", 7, "insert"), ("d1", 8, "update_postimage"),
                   ("d2", 4, "insert"), ("d3", 5, "delete")]
        assert insert_versions(changes) == {"d1": 7, "d2": 4}

    def test_non_insert_changes_ignored(self):
        assert insert_versions([("d1", 3, "update_preimage"), ("d1", 4, "delete")]) == {}

    def test_empty(self):
        assert insert_versions([]) == {}

    def test_decision_carries_insert_version(self):
        assert decision("d1", "p1", "accept", insert_version=5).insert_version == 5
        assert decision("d1", "p1", "accept").insert_version is None


class TestReuseEventIds(ReplayFixture):
    def reuse_refusal(self) -> Action:
        [r] = self.run(applied_ids=frozenset({"d1"}))
        assert r.kind == "refused"
        return r

    def test_reuse_uses_insert_version(self):
        r = self.reuse_refusal()
        assert event_id_for(r, self.audited, 5) == audit_event_id("refused", "p1", "d1@5")

    def test_two_reuses_get_different_ids(self):
        r = self.reuse_refusal()
        e5, e9 = event_id_for(r, self.audited, 5), event_id_for(r, self.audited, 9)
        assert e5 != e9
        assert manual_event_id("d1") not in {e5, e9}

    def test_same_reuse_twice_same_id(self):
        r = self.reuse_refusal()
        assert event_id_for(r, self.audited, 5) == event_id_for(r, self.audited, 5)

    def test_second_reuse_is_still_audited(self):
        r = self.reuse_refusal()
        first = audit_rows([r], {"p1": self.decided}, {}, TABLE, "unknown-object", audited=self.audited,
                           insert_versions={"d1": 5})
        audited = {**self.audited, first[0][0]: audit_after(r)}
        second = audit_rows([r], {"p1": self.decided}, {}, TABLE, "unknown-object", audited=audited,
                            insert_versions={"d1": 9})
        assert len(second) == 1 and second[0][2] == "decision_refused"
        assert second[0][0] not in audited
        assert second[0][0] == audit_event_id("refused", "p1", "d1@9")

    def test_audit_rows_same_version_idempotent(self):
        r = self.reuse_refusal()
        rows = [audit_rows([r], {"p1": self.decided}, {}, TABLE, "unknown-object", audited=self.audited,
                           insert_versions={"d1": 5})[0][0] for _ in range(2)]
        assert rows[0] == rows[1] == audit_event_id("refused", "p1", "d1@5")

    def test_non_reused_manual_id_ignores_insert_version(self):
        assert event_id_for(self.original, self.audited, 5) == manual_event_id("d1")
        assert event_id_for(self.original, None, 5) == manual_event_id("d1")


# ---------------------------------------------------------------- fourth review: no replay of a superseded proposal

class TestNoReplayOfSuperseded:
    def setup_method(self):
        self.old = prop("p1", "amt", "claim_reference", created_at=T0)
        self.d = decision("d1", "p1", "edit", term_id="claim_amount")
        [self.edited] = manual([self.d], [self.old], profiles_for(self.old), TERMS)
        assert self.edited.kind == "edited"
        self.audited = {manual_event_id("d1"): audit_after(self.edited)}
        self.newer = prop("p2", "amt", "claim_amount", sensitivity="confidential",
                          created_at=T0 + timedelta(hours=1), run_id="run-2")

    def test_plan_manual_refuses_superseded_instead_of_replaying(self):
        acts = manual([self.d], [self.old, self.newer], profiles_for(self.old, self.newer), TERMS,
                      audited=self.audited)
        assert len(acts) == 1
        assert acts[0] != self.edited
        assert_refused(acts[0], "superseded")

    def test_plan_never_two_approved_rows_for_the_column(self):
        props = [self.old, self.newer]
        acts = full_plan([self.d], props, profiles_for(*props), TERMS, set(), audited=self.audited)
        d1 = [a for a in acts if a.decision_id == "d1"]
        assert len(d1) == 1 and d1[0] != self.edited
        assert_refused(d1[0], "superseded")
        keys = [(a.approved.table_name, a.approved.column_name) for a in acts if a.approved is not None]
        assert len(keys) == len(set(keys))


# ---------------------------------------------------------------- verification-pass gaps

class TestTwoAuditedDecisionsOneProposal:
    def test_only_one_replays(self):
        # both audited before crashes; replaying both would give two approved rows for one column
        p = prop("p1", "claim_ref")
        d1, d2 = decision("d1", "p1", "accept", minutes=1), decision("d2", "p1", "accept", minutes=2)
        [a1] = manual([d1], [p], profiles_for(p), TERMS)
        [a2] = manual([d2], [p], profiles_for(p), TERMS)
        audited = {manual_event_id("d1"): audit_after(a1), manual_event_id("d2"): audit_after(a2)}
        acts = full_plan([d1, d2], [p], profiles_for(p), TERMS, set(), audited=audited)
        keys = [(a.approved.table_name, a.approved.column_name) for a in acts if a.approved is not None]
        assert len(keys) == 1
        [second] = [a for a in acts if a.decision_id == "d2"]
        assert_refused(second, "decided")


class TestRefusalSalt:
    def test_version_when_known(self):
        assert refusal_salt(decision("d1", "p1", "accept", insert_version=5)) == "5"

    def test_unattributed_rows_differ_by_time(self):
        a = decision("d1", "p1", "accept", minutes=1, committed_by=None)
        b = decision("d1", "p1", "accept", minutes=2, committed_by=None)
        assert refusal_salt(a) != refusal_salt(b)

    def test_same_row_same_salt(self):
        d = decision("d1", "p1", "accept", minutes=1, committed_by=None)
        assert refusal_salt(d) == refusal_salt(replace(d))


# ---------------------------------------------------------------- revise: correct an already-approved column
# specs/hydration.md step 6 + design agreed with the lead: action 'revise' on the newest decided proposal (accepted |
# edited) of a column; null fields keep the CURRENTLY APPROVED value (approved_metadata, passed as
# current={(table_name, column_name): ApprovedRow}); NO_MATCH clears the term; sensitivity re-floored.

from gates.hydration.approve import AUDIT_ACTION, MANUAL_ACTIONS, ApprovedRow  # noqa: E402

REV_COL = "chk_sum"


def approved_row(column: str = REV_COL, term_id: str | None = "claim_reference", *,
                 description: str | None = "approved desc", sensitivity: str = "internal", by: str = "owner.a",
                 table: str = TABLE) -> ApprovedRow:
    return ApprovedRow(table, column, term_id, description, sensitivity, by)


def cur(*rows: ApprovedRow) -> dict[tuple[str, str], ApprovedRow]:
    return {(r.table_name, r.column_name): r for r in rows}


def decided_prop(pid: str = "p1", column: str = REV_COL, term_id: str | None = "claim_reference", *,
                 status: str = "accepted", **kw) -> ProposalRow:
    return prop(pid, column, term_id, status=status, **kw)


def revise(did: str = "d1", pid: str = "p1", **kw) -> ReviewDecision:
    return decision(did, pid, "revise", **kw)


def rmanual(decisions, proposals, profiles=None, terms=TERMS, current=None, **kw) -> list[Action]:
    profiles = profiles_for(*proposals) if profiles is None else profiles
    current = cur(approved_row()) if current is None else current
    return manual(decisions, proposals, profiles, terms, current=current, **kw)


class TestReviseConstants:
    def test_manual_actions_include_revise(self):
        assert MANUAL_ACTIONS == ("accept", "edit", "reject", "revise")

    def test_audit_action_name(self):
        assert AUDIT_ACTION["revised"] == "proposal_revised"


class TestReviseApplies:
    def test_revise_accepted_column_becomes_edited(self):
        p = decided_prop(status="accepted")
        [a] = rmanual([revise(term_id="claim_amount")], [p])
        assert a.kind == "revised" and a.new_status == "edited"
        assert a.decision_id == "d1" and a.actor == "owner.a"
        assert (a.approved.table_name, a.approved.column_name) == (TABLE, REV_COL)
        assert a.approved.term_id == "claim_amount"
        assert a.approved.sensitivity == "confidential"  # floored by the new term
        assert a.approved.approved_by == "owner.a"

    def test_revise_edited_column_stays_edited(self):
        p = decided_prop(status="edited")
        [a] = rmanual([revise(term_id="claim_amount")], [p])
        assert a.kind == "revised" and a.new_status == "edited"

    def test_null_fields_keep_current_approved_not_proposal(self):
        p = decided_prop(term_id="claim_reference", sensitivity="internal", description="proposal desc")
        current = cur(approved_row(term_id="claim_amount", description="approved desc", sensitivity="pii"))
        [a] = rmanual([revise(description="fixed desc")], [p], current=current)
        assert a.kind == "revised"
        assert a.approved.term_id == "claim_amount"
        assert a.approved.description == "fixed desc"
        assert a.approved.sensitivity == "pii"

    def test_null_description_keeps_current(self):
        p = decided_prop(description="proposal desc")
        [a] = rmanual([revise(sensitivity="confidential")], [p],
                      current=cur(approved_row(description="approved desc")))
        assert a.approved.description == "approved desc"
        assert a.approved.sensitivity == "confidential"

    def test_no_match_clears_term(self):
        p = decided_prop(term_id="claim_amount", sensitivity="confidential")
        current = cur(approved_row(term_id="claim_amount", sensitivity="confidential"))
        [a] = rmanual([revise(term_id="NO_MATCH")], [p], current=current)
        assert a.kind == "revised"
        assert a.approved.term_id is None
        assert a.approved.sensitivity == "confidential"  # kept from current, not lowered
        assert a.golden.edited_term_id is None

    def test_golden_candidate(self):
        p = decided_prop(term_id="claim_reference", sensitivity="internal")
        d = revise(term_id="claim_amount", sensitivity="confidential", decided_by="Owner.A@x",
                   committed_by="owner.a@x")
        [a] = rmanual([d], [p], approvers={TABLE: {"owner.a@x"}})
        g = a.golden
        assert g is not None
        assert (g.proposal_id, g.table_name, g.column_name) == ("p1", TABLE, REV_COL)
        assert (g.proposed_term_id, g.edited_term_id) == ("claim_reference", "claim_amount")
        assert (g.proposed_sensitivity, g.edited_sensitivity) == ("internal", "confidential")
        assert g.edited_by == "owner.a@x"
        assert (a.approved.approved_by, a.actor) == ("owner.a@x", "owner.a@x")

    def test_golden_proposed_term_no_match_is_none(self):
        p = decided_prop(term_id="NO_MATCH", method="rule", confidence=1.0, status="edited")
        [a] = rmanual([revise(term_id="claim_reference")], [p], current=cur(approved_row(term_id=None)))
        assert a.golden.proposed_term_id is None
        assert a.golden.edited_term_id == "claim_reference"


class TestReviseSensitivityFloor:
    def test_pattern_hits_force_pii(self):
        p = decided_prop(column="tmp_col", term_id="email_address", sensitivity="pii")
        prof = profiles_for(p, tmp_col=hits(("email",), ("email",)))
        current = cur(approved_row("tmp_col", "email_address", sensitivity="pii"))
        # description changes so the revise is real; asking internal alone is a no-op (TestReviseNoOp)
        [a] = rmanual([revise(sensitivity="internal", description="new desc")], [p], profiles=prof,
                      current=current)
        assert a.kind == "revised"
        assert a.approved.sensitivity == "pii"
        assert a.golden.edited_sensitivity == "pii"

    def test_new_term_floor(self):
        p = decided_prop()
        [a] = rmanual([revise(term_id="claim_amount", sensitivity="public")], [p])
        assert a.approved.sensitivity == "confidential"

    def test_kept_current_term_floor(self):
        p = decided_prop(term_id="claim_reference")
        current = cur(approved_row(term_id="claim_amount", sensitivity="confidential"))
        [a] = rmanual([revise(sensitivity="public", description="new desc")], [p], current=current)
        assert a.approved.term_id == "claim_amount"
        assert a.approved.sensitivity == "confidential"

    def test_can_raise(self):
        [a] = rmanual([revise(sensitivity="pii")], [decided_prop()])
        assert a.approved.sensitivity == "pii"

    def test_public_allowed_on_public_term(self):
        p = decided_prop(term_id="policy_status", sensitivity="public")
        current = cur(approved_row(term_id="policy_status", sensitivity="internal"))
        [a] = rmanual([revise(sensitivity="public")], [p], current=current)
        assert a.approved.sensitivity == "public"


class TestReviseIdentity:
    APPROVERS = {TABLE: {"owner.a"}}

    def run(self, d, p=None, **kw):
        kw.setdefault("approvers", self.APPROVERS)
        return rmanual([d], [p or decided_prop()], **kw)

    def test_attribution_error(self):
        [a] = self.run(revise(attribution_error="modified after insert"))
        assert_refused(a, "modified after insert")

    def test_committer_unknown(self):
        [a] = self.run(revise(committed_by=None))
        assert_refused(a, "committer unknown")

    def test_decided_by_not_committer(self):
        [a] = self.run(revise(decided_by="owner.a", committed_by="intruder"))
        assert_refused(a, "does not match committer")

    def test_job_principal(self):
        [a] = self.run(revise(decided_by=JOB), approvers={TABLE: {JOB}})
        assert_refused(a, "job principal")

    def test_not_an_approver(self):
        [a] = self.run(revise(decided_by="owner.c"))
        assert_refused(a, "not an approver")

    @pytest.mark.parametrize("status", ["proposed", "rejected", "eval"])
    def test_identity_before_state(self, status):
        [a] = self.run(revise(committed_by=None), decided_prop(status=status))
        assert_refused(a, "committer unknown")

    def test_identity_before_missing_current(self):
        [a] = self.run(revise(decided_by="owner.c"), current={})
        assert_refused(a, "not an approver")


class TestReviseRefused:
    def one(self, acts: list[Action], text: str | None = None) -> Action:
        assert len(acts) == 1
        assert_refused(acts[0], text)
        assert acts[0].decision_id == "d1"
        return acts[0]

    @pytest.mark.parametrize("status", ["proposed", "rejected", "eval"])
    def test_status_not_accepted_or_edited(self, status):
        self.one(rmanual([revise(term_id="claim_amount")], [decided_prop(status=status)]))

    def test_superseded_by_newer_proposed(self):
        old = decided_prop("p1", created_at=T0)
        new = prop("p2", REV_COL, created_at=T0 + timedelta(hours=1), run_id="run-2")
        self.one(rmanual([revise(term_id="claim_amount")], [old, new]), "superseded")

    def test_superseded_by_newer_accepted(self):
        old = decided_prop("p1", created_at=T0)
        new = decided_prop("p2", created_at=T0 + timedelta(hours=1), run_id="run-2")
        self.one(rmanual([revise(term_id="claim_amount")], [old, new]), "superseded")

    def test_newer_eval_row_does_not_supersede(self):
        p = decided_prop("p1", created_at=T0)
        ev = prop("e1", REV_COL, status="eval", created_at=T0 + timedelta(hours=1), run_id="eval-run")
        [a] = rmanual([revise(term_id="claim_amount")], [p, ev])
        assert a.kind == "revised"

    def test_no_current_row(self):
        self.one(rmanual([revise(term_id="claim_amount")], [decided_prop()], current={}))

    def test_current_none_is_no_current_row(self):
        p = decided_prop()
        self.one(manual([revise(term_id="claim_amount")], [p], profiles_for(p), TERMS, current=None))

    def test_current_row_of_other_table_does_not_count(self):
        self.one(rmanual([revise(term_id="claim_amount")], [decided_prop()],
                         current=cur(approved_row(table=OTHER_TABLE))))

    def test_unknown_term(self):
        self.one(rmanual([revise(term_id="made_up_term")], [decided_prop()]), "business_terms")

    def test_bad_sensitivity(self):
        self.one(rmanual([revise(sensitivity="secret")], [decided_prop()]))

    def test_no_profile_for_run(self):
        self.one(rmanual([revise(term_id="claim_amount")], [decided_prop()], profiles={}))

    def test_profile_of_other_run_not_used(self):
        p = decided_prop(run_id="run-2")
        self.one(rmanual([revise(term_id="claim_amount")], [p], profiles={(RUN, REV_COL): hits()}))

    def test_unknown_proposal(self):
        self.one(rmanual([revise(pid="nope")], [decided_prop()]))

    def test_second_revise_same_proposal_same_run(self):
        p = decided_prop()
        ds = [revise("d2", sensitivity="pii", minutes=5), revise("d1", term_id="claim_amount", minutes=1)]
        acts = rmanual(ds, [p])
        assert [a.decision_id for a in acts] == ["d1", "d2"]
        assert acts[0].kind == "revised"
        assert_refused(acts[1])
        keys = [(a.approved.table_name, a.approved.column_name) for a in acts if a.approved is not None]
        assert keys == [(TABLE, REV_COL)]

    def test_accept_on_accepted_still_refused(self):
        # revise is the only way to touch a decided proposal
        [a] = rmanual([decision("d1", "p1", "accept")], [decided_prop()])
        assert_refused(a)

    def test_edit_on_accepted_still_refused(self):
        [a] = rmanual([decision("d1", "p1", "edit", term_id="claim_amount")], [decided_prop()])
        assert_refused(a)


class TestRevisePlan:
    def test_plan_passes_current(self):
        p = decided_prop()
        acts = full_plan([revise(term_id="claim_amount")], [p], profiles_for(p), TERMS, set(),
                         current=cur(approved_row()))
        assert [(a.kind, a.decision_id) for a in acts] == [("revised", "d1")]

    def test_auto_never_touches_revised_column_and_no_routing(self):
        p = decided_prop(confidence=0.2)  # auto would route it if it were pending
        other = prop("p9", "amt", "claim_amount", sensitivity="confidential")
        acts = full_plan([revise(term_id="claim_amount")], [p, other], profiles_for(p, other), TERMS, set(),
                         current=cur(approved_row()))
        mine = [a for a in acts if a.proposal_id == "p1"]
        assert [(a.kind, a.decision_id) for a in mine] == [("revised", "d1")]
        assert not any(a.kind == "review_routed" for a in acts)
        assert {(a.proposal_id, a.kind) for a in acts if a.decision_id is None} == {("p9", "accepted")}

    def test_refused_revise_does_not_route(self):
        p = decided_prop()
        acts = full_plan([revise(term_id="made_up_term")], [p], profiles_for(p), TERMS, set(),
                         current=cur(approved_row()))
        assert [a.kind for a in acts] == ["refused"]
        assert acts[0].reason and "business_terms" in acts[0].reason

    def test_idempotent_after_apply(self):
        p = decided_prop()
        d = revise(term_id="claim_amount")
        acts = full_plan([d], [p], profiles_for(p), TERMS, set(), current=cur(approved_row()))
        assert [a.kind for a in acts] == ["revised"]
        props, decs, routed = apply_state([p], [d], set(), acts)
        assert props[0].status == "edited" and decs[0].applied_at is not None
        new_current = cur(acts[0].approved)
        assert full_plan(decs, props, profiles_for(*props), TERMS, routed, current=new_current) == []
        assert full_plan(decs, props, profiles_for(*props), TERMS, routed, current=new_current) == []


class TestReviseAuditAndReplay:
    def setup_method(self):
        self.p = decided_prop(status="accepted")
        self.d = revise(term_id="claim_amount", description="Amount.")
        self.old = approved_row()
        [self.original] = rmanual([self.d], [self.p], current=cur(self.old))
        assert self.original.kind == "revised"
        self.audited = {manual_event_id("d1"): audit_after(self.original)}

    def test_audit_row_name_and_before_carries_replaced_row(self):
        old_dict = {"table_name": TABLE, "column_name": REV_COL, "term_id": "claim_reference",
                    "description": "approved desc", "sensitivity": "internal", "approved_by": "owner.a"}
        [r] = audit_rows([self.original], {"p1": self.p}, {REV_COL: old_dict}, TABLE, "unknown-object")
        eid, gate, action, actor, obj, before, after = r
        assert eid == manual_event_id("d1")
        assert (gate, action, actor, obj) == ("hydration", "proposal_revised", "owner.a", TABLE)
        b = _strict_json(before)
        assert b["approved"] == old_dict
        assert b["proposal"]["status"] == "accepted"
        assert json.loads(after)["approved"]["term_id"] == "claim_amount"

    def test_round_trip(self):
        assert action_from_audit(json.loads(json.dumps(audit_after(self.original)))) == self.original

    @pytest.mark.parametrize("status", ["accepted", "edited"])
    def test_replayed_after_crash(self, status):
        # audit written; status maybe written; approved_metadata maybe written; applied_at still null
        p = replace(self.p, status=status)
        for current in (cur(self.old), cur(self.original.approved)):
            assert rmanual([self.d], [p], current=current, audited=self.audited) == [self.original]

    def test_replay_through_plan(self):
        p = replace(self.p, status="edited")
        acts = full_plan([self.d], [p], profiles_for(p), TERMS, set(), current=cur(self.original.approved),
                         audited=self.audited)
        assert acts == [self.original]

    def test_not_replayed_when_superseded(self):
        newer = prop("p2", REV_COL, created_at=T0 + timedelta(hours=1), run_id="run-2")
        acts = rmanual([self.d], [replace(self.p, status="edited"), newer], current=cur(self.original.approved),
                       audited=self.audited)
        assert len(acts) == 1
        assert acts[0] != self.original
        assert_refused(acts[0], "superseded")

    def test_not_replayed_when_identity_fails(self):
        acts = rmanual([replace(self.d, committed_by="intruder")], [replace(self.p, status="edited")],
                       audited=self.audited)
        assert len(acts) == 1
        assert_refused(acts[0], "does not match committer")

    @pytest.mark.parametrize("status", ["rejected", "proposed"])
    def test_not_replayed_on_other_status(self, status):
        acts = rmanual([self.d], [replace(self.p, status=status)], audited=self.audited)
        assert len(acts) == 1
        assert acts[0] != self.original
        assert_refused(acts[0])


# ---------------------------------------------------------------- revise rulings (coordinator, from the lead)
# 1. no-op: refused when (term_id, description, sensitivity) after flooring equals the current approved row.
# 2. term_id null while the currently approved term is retired: refused (business_terms); NO_MATCH / valid ok.
# 3. method='rule' proposal: revise allowed, applies normally.

class TestReviseNoOp:
    def test_all_fields_null_refused(self):
        [a] = rmanual([revise()], [decided_prop()])
        assert_refused(a, "no change")

    def test_same_values_refused(self):
        d = revise(term_id="claim_reference", description="approved desc", sensitivity="internal")
        [a] = rmanual([d], [decided_prop()])
        assert_refused(a, "no change")

    def test_equal_after_flooring_refused(self):
        # public on an internal term floors back to internal = the current row
        [a] = rmanual([revise(sensitivity="public")], [decided_prop()])
        assert_refused(a, "no change")

    def test_equal_after_pattern_floor_refused(self):
        p = decided_prop(column="tmp_col", term_id="email_address", sensitivity="pii")
        prof = profiles_for(p, tmp_col=hits(("email",), ("email",)))
        current = cur(approved_row("tmp_col", "email_address", sensitivity="pii"))
        [a] = rmanual([revise(sensitivity="internal")], [p], profiles=prof, current=current)
        assert_refused(a, "no change")

    def test_only_approver_differs_is_still_no_change(self):
        current = cur(approved_row(by=AUTO_ACTOR))
        [a] = rmanual([revise(sensitivity="internal")], [decided_prop()], current=current)
        assert_refused(a, "no change")

    def test_no_op_does_not_consume_proposal(self):
        p = decided_prop()
        ds = [revise("d1", minutes=1), revise("d2", term_id="claim_amount", minutes=2)]
        acts = rmanual(ds, [p])
        assert [(a.decision_id, a.kind) for a in acts] == [("d1", "refused"), ("d2", "revised")]
        assert_refused(acts[0], "no change")

    def test_description_only_change_applies(self):
        [a] = rmanual([revise(description="new desc")], [decided_prop()])
        assert a.kind == "revised" and a.approved.description == "new desc"


class TestReviseRetiredCurrentTerm:
    def setup_method(self):
        self.p = decided_prop(term_id="retired_term", status="edited")
        self.current = cur(approved_row(term_id="retired_term", sensitivity="internal"))

    def test_null_term_keeping_retired_refused(self):
        [a] = rmanual([revise(sensitivity="confidential")], [self.p], current=self.current)
        assert_refused(a, "business_terms")

    def test_null_term_description_only_refused(self):
        [a] = rmanual([revise(description="new desc")], [self.p], current=self.current)
        assert_refused(a, "business_terms")

    def test_no_match_allowed(self):
        [a] = rmanual([revise(term_id="NO_MATCH")], [self.p], current=self.current)
        assert a.kind == "revised"
        assert a.approved.term_id is None

    def test_valid_term_allowed(self):
        [a] = rmanual([revise(term_id="claim_amount")], [self.p], current=self.current)
        assert a.kind == "revised"
        assert a.approved.term_id == "claim_amount"
        assert a.approved.sensitivity == "confidential"
        assert a.golden.proposed_term_id == "retired_term"

    def test_retired_proposal_term_but_valid_current_term_allowed(self):
        # the check reads the currently approved term, not the proposal's
        current = cur(approved_row(term_id="claim_reference"))
        [a] = rmanual([revise(sensitivity="confidential")], [self.p], current=current)
        assert a.kind == "revised"
        assert a.approved.term_id == "claim_reference"


class TestReviseRuleProposal:
    @pytest.mark.parametrize("status", ["accepted", "edited"])
    def test_rule_proposal_revise_applies(self, status):
        p = decided_prop(term_id=None, method="rule", confidence=1.0, status=status)
        current = cur(approved_row(term_id=None, sensitivity="internal"))
        [a] = rmanual([revise(term_id="claim_amount", description="Amount.")], [p], current=current)
        assert a.kind == "revised" and a.new_status == "edited"
        assert a.approved.term_id == "claim_amount"
        assert a.approved.description == "Amount."
        assert a.approved.sensitivity == "confidential"
        assert a.golden.proposed_term_id is None and a.golden.edited_term_id == "claim_amount"

    def test_rule_proposal_revise_through_plan(self):
        p = decided_prop(term_id=None, method="rule", confidence=1.0, status="edited")
        current = cur(approved_row(term_id=None))
        acts = full_plan([revise(sensitivity="pii")], [p], profiles_for(p), TERMS, set(), current=current)
        assert [(a.kind, a.decision_id) for a in acts] == [("revised", "d1")]
        assert acts[0].approved.sensitivity == "pii"


# ---------------------------------------------------------------- revise: review fixes (pattern floor, target, '')

class TestRevisePatternFloor:
    """A non-pii term with a pii pattern hit: only the pattern floor can force pii (the term floor cannot)."""

    def test_pattern_hits_force_pii_under_non_pii_term(self):
        p = decided_prop(term_id="claim_reference", sensitivity="pii")
        prof = profiles_for(p, **{REV_COL: hits(("email",), ("email",))})
        current = cur(approved_row(term_id="claim_reference", sensitivity="pii"))
        [a] = rmanual([revise(sensitivity="internal", description="new desc")], [p], profiles=prof,
                      current=current)
        assert a.kind == "revised" and a.approved.sensitivity == "pii"

    def test_no_match_with_pattern_hits_stays_pii(self):
        p = decided_prop(term_id="claim_reference", sensitivity="pii")
        prof = profiles_for(p, **{REV_COL: hits(("email",), ("email",))})
        current = cur(approved_row(term_id="claim_reference", sensitivity="pii"))
        [a] = rmanual([revise(term_id="NO_MATCH", sensitivity="public")], [p], profiles=prof, current=current)
        assert a.kind == "revised" and a.approved.term_id is None and a.approved.sensitivity == "pii"

    def test_profile_of_proposals_run_is_used(self):
        p = decided_prop(term_id="claim_reference", sensitivity="pii")
        prof = {(p.run_id, REV_COL): hits(("email",), ("email",)), ("other-run", REV_COL): hits()}
        current = cur(approved_row(term_id="claim_reference", sensitivity="pii"))
        [a] = rmanual([revise(sensitivity="internal", description="new desc")], [p], profiles=prof,
                      current=current)
        assert a.approved.sensitivity == "pii"

    def test_any_pattern_hits_alone_do_not_force_pii(self):
        # documented: the floor reads pattern_hits only (approve rule 3 is the stricter any_pattern_hits check)
        p = decided_prop(term_id="claim_reference", sensitivity="pii")
        prof = profiles_for(p, **{REV_COL: hits((), ("email",))})
        current = cur(approved_row(term_id="claim_reference", sensitivity="pii"))
        [a] = rmanual([revise(sensitivity="internal")], [p], profiles=prof, current=current)
        assert a.kind == "revised" and a.approved.sensitivity == "internal"


class TestReviseTarget:
    def test_newer_rejected_does_not_supersede_decided(self):
        old = decided_prop("p1", created_at=T0)
        rej = prop("p2", REV_COL, status="rejected", created_at=T0 + timedelta(hours=1), run_id="run-2")
        [a] = rmanual([revise(term_id="claim_amount")], [old, rej], profiles=profiles_for(old, rej))
        assert a.kind == "revised" and a.proposal_id == "p1"

    def test_revise_on_the_newer_rejected_is_refused(self):
        old = decided_prop("p1", created_at=T0)
        rej = prop("p2", REV_COL, status="rejected", created_at=T0 + timedelta(hours=1), run_id="run-2")
        [a] = rmanual([revise(pid="p2", term_id="claim_amount")], [old, rej], profiles=profiles_for(old, rej))
        assert_refused(a, "rejected")

    def test_newer_pending_refuses_with_decide_first(self):
        old = decided_prop("p1", created_at=T0)
        new = prop("p2", REV_COL, created_at=T0 + timedelta(hours=1), run_id="run-2")
        [a] = rmanual([revise(term_id="claim_amount")], [old, new], profiles=profiles_for(old, new))
        assert_refused(a, "decide it first")

    def test_replayed_when_newer_rejected(self):
        old = decided_prop("p1", created_at=T0)
        rej = prop("p2", REV_COL, status="rejected", created_at=T0 + timedelta(hours=1), run_id="run-2")
        d = revise(term_id="claim_amount")
        [first] = rmanual([d], [old, rej], profiles=profiles_for(old, rej))
        audited = {manual_event_id("d1"): audit_after(first)}
        [again] = rmanual([d], [replace(old, status="edited"), rej], profiles=profiles_for(old, rej),
                          current=cur(first.approved), audited=audited)
        assert again == first


class TestEmptyTermId:
    def test_revise_empty_term_refused(self):
        [a] = rmanual([revise(term_id="")], [decided_prop()])
        assert_refused(a, "NO_MATCH")

    def test_edit_empty_term_refused(self):
        p = prop("p1", REV_COL)
        [a] = manual([decision("d1", "p1", "edit", term_id="")], [p], profiles_for(p), TERMS)
        assert_refused(a, "NO_MATCH")


class TestReviseNewerRunEvidence:
    def test_newer_rejected_runs_pattern_hits_floor_the_revise(self):
        old = decided_prop("p1", created_at=T0)
        rej = prop("p2", REV_COL, status="rejected", created_at=T0 + timedelta(hours=1), run_id="run-2")
        prof = {(old.run_id, REV_COL): hits(), ("run-2", REV_COL): hits(("email",), ("email",))}
        [a] = rmanual([revise(sensitivity="internal", description="x")], [old, rej], profiles=prof)
        assert a.kind == "revised" and a.approved.sensitivity == "pii"

    def test_older_runs_hits_do_not_count(self):
        older = prop("p0", REV_COL, status="rejected", created_at=T0 - timedelta(hours=1), run_id="run-0")
        p = decided_prop("p1", created_at=T0)
        prof = {("run-0", REV_COL): hits(("email",), ("email",)), (p.run_id, REV_COL): hits()}
        [a] = rmanual([revise(sensitivity="confidential")], [older, p], profiles=prof)
        assert a.kind == "revised" and a.approved.sensitivity == "confidential"

    def test_newer_run_without_profile_refused(self):
        old = decided_prop("p1", created_at=T0)
        rej = prop("p2", REV_COL, status="rejected", created_at=T0 + timedelta(hours=1), run_id="run-2")
        [a] = rmanual([revise(sensitivity="confidential")], [old, rej], profiles=profiles_for(old))
        assert_refused(a, "no profile for newer run")
