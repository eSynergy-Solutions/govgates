"""Approve step (specs/hydration.md step 6). Pure, deterministic; no model output decides.

Auto-rule: accept only when all three hold, otherwise human review:
1. a term was proposed (NO_MATCH never auto-accepts: a column with no term is for a person to look at);
2. confidence >= threshold;
3. no PII pattern hit any value of a column whose term is not pii: pattern_hits (>= 80% of values) or
   any_pattern_hits (at least one value), so free text with a few emails under a non-pii term goes to review.
   Deliberately stricter than the sensitivity floor, which reads pattern_hits only.
Known limitation (spec): a confident wrong non-pii term on a personal column with no value pattern is accepted;
pii_recall in the eval measures it.

Writer: plan_manual (owner rows from gov.review_decisions) and plan_auto turn proposals into Actions; the job
(src/jobs/approve.py) writes them. Only status 'proposed' is ever decided ('eval' never); a 'revise' corrects an
approved column through its newest accepted/edited proposal. Approved sensitivity is
the proposal's (or the owner's edit) re-floored in code, never the term's alone. Review rows stay 'proposed' (so
the guard still counts them as covered) and are routed once per proposal_id.
"""

from __future__ import annotations

import json
import math
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime

from gates.common.cdf import (  # noqa: F401 - re-exported: callers and tests import them from here
    cdf_start_version,
    commit_times,
    committers,
    insert_versions,
    modified_after_insert,
)
from gates.hydration.patterns import FORCE_PII
from gates.hydration.propose import NO_MATCH, SENSITIVITIES, enforce_floor

AUTO_ACCEPT_CONFIDENCE = 0.8
AUTO_ACTOR = "auto-rule"
MANUAL_ACTIONS = ("accept", "edit", "reject", "revise")
DECIDED = ("accepted", "edited")  # statuses a revise may correct
NO_USES_REASON = ("no permitted uses for this table: insert at least one row into gov.permitted_use_inputs "
                  "(hydration.md step 6), then insert the decision again")


def auto_decision(term_id: str | None, confidence: float, term_sensitivity: str | None, pattern_hits: list[str],
                  any_pattern_hits: list[str] | tuple[str, ...] = (),
                  threshold: float = AUTO_ACCEPT_CONFIDENCE) -> str:
    if not term_id or term_id == NO_MATCH:  # None, "" or the literal from a table read
        return "review"
    if not confidence >= threshold:  # fails closed on NaN from a table read
        return "review"
    if FORCE_PII.intersection([*pattern_hits, *any_pattern_hits]) and term_sensitivity != "pii":
        return "review"
    return "accept"


@dataclass(frozen=True)
class ProposalRow:
    proposal_id: str
    run_id: str
    table_name: str
    column_name: str
    term_id: str | None
    description: str | None
    sensitivity: str | None
    confidence: float
    method: str  # fuzzy | llm | rule
    status: str  # proposed | eval | accepted | edited | rejected
    created_at: datetime | None = None


@dataclass(frozen=True)
class ProfileHits:
    pattern_hits: tuple[str, ...] = ()
    any_pattern_hits: tuple[str, ...] = ()


@dataclass(frozen=True)
class ReviewDecision:
    decision_id: str
    proposal_id: str
    action: str  # accept | edit | reject | revise
    term_id: str | None = None
    description: str | None = None
    sensitivity: str | None = None
    decided_by: str = "owner"
    decided_at: datetime | None = None
    applied_at: datetime | None = None
    committed_by: str | None = None  # principal that committed the row (CDF + DESCRIBE HISTORY); the identity used
    attribution_error: str | None = None  # set by the job when the row cannot be trusted (e.g. modified after insert)
    hold: bool = False  # set by the job when attribution could not be read: stays pending, nothing decided
    insert_version: int | None = None  # CDF _commit_version of the row's insert (salts a reused-id refusal id)


@dataclass(frozen=True)
class ApprovedRow:
    table_name: str
    column_name: str
    term_id: str | None
    description: str | None
    sensitivity: str
    approved_by: str


@dataclass(frozen=True)
class GoldenCandidate:
    proposal_id: str
    table_name: str
    column_name: str
    proposed_term_id: str | None
    edited_term_id: str | None
    proposed_sensitivity: str | None
    edited_sensitivity: str
    edited_by: str


@dataclass(frozen=True)
class Action:
    proposal_id: str
    table_name: str
    column_name: str
    kind: str  # accepted | edited | rejected | revised | review_routed | refused
    new_status: str | None  # None = term_proposals.status unchanged
    approved: ApprovedRow | None
    golden: GoldenCandidate | None
    actor: str
    decision_id: str | None = None
    reason: str | None = None


def _term(term_id: str | None) -> str | None:
    return None if not term_id or term_id == NO_MATCH else term_id


def _floored(sensitivity: str | None, hits: ProfileHits, term_sensitivity: str | None) -> str:
    # enforce_floor returns None only when nothing is known; a stored row is never below internal then
    return enforce_floor(sensitivity, list(hits.pattern_hits), term_sensitivity) or "internal"


def _approved(p: ProposalRow, term_id: str | None, description: str | None, sensitivity: str,
              by: str) -> ApprovedRow:
    return ApprovedRow(p.table_name, p.column_name, term_id, description, sensitivity, by)


def audit_event_id(kind: str, proposal_id: str, decision_id: str | None) -> str:
    """Deterministic audit event_id: a re-run after a partial failure re-merges the same row, never a duplicate."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"govgates-approve|{kind}|{proposal_id}|{decision_id or ''}"))


def _newest(proposals: list[ProposalRow]) -> dict[tuple[str, str], ProposalRow]:
    """Newest non-eval row per (table, column). Older rows are superseded whatever their status."""
    newest: dict[tuple[str, str], ProposalRow] = {}
    for p in proposals:
        if p.status == "eval":
            continue
        key = (p.table_name, p.column_name)
        cur = newest.get(key)
        if cur is None or (p.created_at or datetime.min, p.proposal_id) > (cur.created_at or datetime.min,
                                                                          cur.proposal_id):
            newest[key] = p
    return newest


def latest_pending(proposals: list[ProposalRow]) -> list[ProposalRow]:
    """Columns whose newest non-eval row is 'proposed'. 'eval', decided and superseded rows are never read."""
    rows = [p for p in _newest(proposals).values() if p.status == "proposed"]
    return sorted(rows, key=lambda p: (p.table_name, p.column_name))


def plan_auto(proposals: list[ProposalRow], profiles: dict[tuple[str, str], ProfileHits], terms: dict[str, str],
              already_routed: set[str], skip: set[str] | frozenset[str] = frozenset()) -> list[Action]:
    """profiles: (run_id, column_name) -> the profile the proposal was made from. terms: term_id -> sensitivity."""
    out = []
    for p in latest_pending(proposals):
        if p.proposal_id in skip:
            continue
        hits = profiles.get((p.run_id, p.column_name))
        term_sens = terms.get(p.term_id) if p.term_id else None
        retired = _term(p.term_id) is not None and p.term_id not in terms
        ok = hits is not None and not retired and auto_decision(
            p.term_id, p.confidence, term_sens, list(hits.pattern_hits), hits.any_pattern_hits) == "accept"
        if ok:
            row = _approved(p, p.term_id, p.description, _floored(p.sensitivity, hits, term_sens), AUTO_ACTOR)
            out.append(Action(p.proposal_id, p.table_name, p.column_name, "accepted", "accepted", row, None,
                              AUTO_ACTOR))
        elif p.proposal_id not in already_routed:
            reason = ("no profile for this run" if hits is None
                      else "term not in business_terms" if retired else "auto-rule: review")
            out.append(Action(p.proposal_id, p.table_name, p.column_name, "review_routed", None, None, None,
                              AUTO_ACTOR, reason=reason))
    return out


def _same(a: str | None, b: str | None) -> bool:
    return a is not None and b is not None and a.strip().casefold() == b.strip().casefold()


def _refusal(d: ReviewDecision, proposals: dict[str, ProposalRow], decided: set[str],
             profiles: dict[tuple[str, str], ProfileHits], terms: dict[str, str],
             newest: dict[tuple[str, str], ProposalRow], approvers: dict[str, set[str]] | None,
             job_principal: str | None) -> str | None:
    p = proposals.get(d.proposal_id)
    if p is None:
        return "unknown proposal_id"
    reason = _identity_refusal(d, p, approvers, job_principal)
    if reason:
        return reason
    if d.action == "revise":
        return None  # state checks for a revise need the approved row: _revise_refusal
    return _state_refusal(d, p, decided, profiles, terms, newest)


def _revise_refusal(d: ReviewDecision, p: ProposalRow, decided: set[str],
                    profiles: dict[tuple[str, str], ProfileHits], terms: dict[str, str],
                    newest: dict[tuple[str, str], ProposalRow],
                    newest_decided: dict[tuple[str, str], ProposalRow],
                    current: dict[tuple[str, str], ApprovedRow], proposals: list[ProposalRow]) -> str | None:
    """A revise corrects the approved row of a column through its newest decided proposal."""
    if p.status not in DECIDED or d.proposal_id in decided:
        return (f"proposal status is {'decided this run' if d.proposal_id in decided else p.status}, "
                "not accepted or edited: revise corrects an approved column")
    reason = _revise_target_refusal(p, newest, newest_decided)
    if reason:
        return reason
    if d.term_id == "":
        return "term_id '' is ambiguous: use NO_MATCH to clear the term, or null to keep it"
    cur = current.get((p.table_name, p.column_name))
    if cur is None:
        return "no approved_metadata row for this column: nothing to revise"
    if (p.run_id, p.column_name) not in profiles:
        return "no profile for this proposal's run"
    missing = sorted(r for r in _newer_runs(p, proposals) if (r, p.column_name) not in profiles)
    if missing:  # fail closed: a newer run's evidence must be readable before sensitivity can move
        return f"no profile for newer run {missing[0]} of this column"
    if _term(d.term_id) is not None and d.term_id not in terms:
        return f"term_id {d.term_id!r} not in business_terms"
    if d.term_id is None and cur.term_id is not None and cur.term_id not in terms:
        return (f"approved term {cur.term_id!r} not in business_terms (retired?): give a term_id or NO_MATCH")
    if d.sensitivity is not None and d.sensitivity not in SENSITIVITIES:
        return f"sensitivity {d.sensitivity!r} not in {SENSITIVITIES}"
    row = _revised_row(d, p, _revise_hits(p, proposals, profiles), terms, cur, d.committed_by or d.decided_by)
    if (row.term_id, row.description, row.sensitivity) == (cur.term_id, cur.description, cur.sensitivity):
        return "no change: the revise gives the approved row as it stands"
    return None


def _revise_target_refusal(p: ProposalRow, newest: dict[tuple[str, str], ProposalRow],
                           newest_decided: dict[tuple[str, str], ProposalRow]) -> str | None:
    """The target is the column's newest accepted/edited proposal (a newer rejected one does not supersede it);
    a newer pending proposal does: decide that first."""
    key = (p.table_name, p.column_name)
    latest = newest.get(key)
    if latest is not None and latest.status == "proposed":
        return f"superseded by pending proposal {latest.proposal_id} for this column: decide it first"
    target = newest_decided.get(key)
    if target is None or target.proposal_id != p.proposal_id:
        return f"superseded by proposal {target.proposal_id if target else '?'} for this column"
    return None


def _newest_decided(proposals: list[ProposalRow]) -> dict[tuple[str, str], ProposalRow]:
    return _newest([p for p in proposals if p.status in DECIDED])


def _newer_runs(p: ProposalRow, proposals: list[ProposalRow]) -> set[str]:
    """Runs of non-eval proposals for p's column created at or after p."""
    since = p.created_at or datetime.min
    return {q.run_id for q in proposals
            if q.status != "eval" and (q.table_name, q.column_name) == (p.table_name, p.column_name)
            and (q.created_at or datetime.min) >= since and q.run_id != p.run_id}


def _revise_hits(p: ProposalRow, proposals: list[ProposalRow],
                 profiles: dict[tuple[str, str], ProfileHits]) -> ProfileHits:
    """Pattern hits of the target's run and of every newer non-eval run of the column: a newer run's evidence
    (e.g. a rejected proposal whose profile saw emails) still floors the revise."""
    runs = {p.run_id} | _newer_runs(p, proposals)
    found = [profiles[(r, p.column_name)] for r in sorted(runs) if (r, p.column_name) in profiles]
    return ProfileHits(tuple(sorted({h for f in found for h in f.pattern_hits})),
                       tuple(sorted({h for f in found for h in f.any_pattern_hits})))


def _revised_row(d: ReviewDecision, p: ProposalRow, hits: ProfileHits, terms: dict[str, str], cur: ApprovedRow,
                 who: str) -> ApprovedRow:
    """Null fields keep the CURRENTLY approved value; NO_MATCH clears the term; sensitivity re-floored."""
    term_id = _term(d.term_id) if d.term_id is not None else cur.term_id
    desc = d.description if d.description is not None else cur.description
    sens = _floored(d.sensitivity or cur.sensitivity, hits, terms.get(term_id) if term_id else None)
    return _approved(p, term_id, desc, sens, who)


def _identity_refusal(d: ReviewDecision, p: ProposalRow, approvers: dict[str, set[str]],
                      job_principal: str | None) -> str | None:
    """Identity and authority: checked before anything else, and before any replay."""
    if d.attribution_error:
        return d.attribution_error
    if d.committed_by is None:
        return ("committer unknown: no change-data-feed insert for this row in the retention window (stale, or "
                "inserted before CDF): insert the decision again")
    if not _same(d.decided_by or "", d.committed_by):
        return f"decided_by {d.decided_by!r} does not match committer {d.committed_by!r}"
    if _same(d.committed_by, job_principal):
        return "committed by the job principal: the job never approves its own proposals"
    if not any(_same(d.committed_by, a) for a in approvers.get(p.table_name, set())):
        return f"{d.committed_by!r} is not an approver for {p.table_name} (gov.table_approvers)"
    return None


def _state_refusal(d: ReviewDecision, p: ProposalRow, decided: set[str],
                   profiles: dict[tuple[str, str], ProfileHits], terms: dict[str, str],
                   newest: dict[tuple[str, str], ProposalRow]) -> str | None:
    if p.status != "proposed" or d.proposal_id in decided:
        return f"proposal status is {'decided' if d.proposal_id in decided else p.status}, not proposed"
    latest = newest.get((p.table_name, p.column_name))
    if latest is None or latest.proposal_id != p.proposal_id:
        return f"superseded by proposal {latest.proposal_id if latest else '?'} for this column"
    if d.action not in MANUAL_ACTIONS:
        return f"action {d.action!r} not in {MANUAL_ACTIONS}"
    if (p.run_id, p.column_name) not in profiles:
        return "no profile for this proposal's run"
    if d.action == "accept":
        if d.term_id is not None or d.description is not None or d.sensitivity is not None:
            return "accept takes the proposal as is; to change term, description or sensitivity use edit"
        if _term(p.term_id) is not None and p.term_id not in terms:
            return f"proposed term {p.term_id!r} not in business_terms (retired?): edit it instead"
    if d.action == "reject" and p.method == "rule":
        return "a rule proposal cannot be rejected (a re-run gives the same answer): edit it instead"
    if d.action == "edit":
        if d.term_id == "":
            return "term_id '' is ambiguous: use NO_MATCH to clear the term, or null to keep it"
        if _term(d.term_id) is not None and d.term_id not in terms:
            return f"term_id {d.term_id!r} not in business_terms"
        if d.term_id is None and _term(p.term_id) is not None and p.term_id not in terms:
            return f"proposed term {p.term_id!r} not in business_terms (retired?): give a term_id or NO_MATCH"
        if d.sensitivity is not None and d.sensitivity not in SENSITIVITIES:
            return f"sensitivity {d.sensitivity!r} not in {SENSITIVITIES}"
    return None


def _refused(d: ReviewDecision, p: ProposalRow | None, reason: str) -> Action:
    return Action(d.proposal_id, p.table_name if p else "", p.column_name if p else "", "refused", None, None, None,
                  d.committed_by or f"unverified:{d.decided_by}", d.decision_id, reason)


def plan_manual(decisions: list[ReviewDecision], proposals: list[ProposalRow],
                profiles: dict[tuple[str, str], ProfileHits], terms: dict[str, str], *,
                approvers: dict[str, set[str]] | None = None, job_principal: str | None = None,
                audited: dict[str, dict] | None = None,
                applied_ids: set[str] | frozenset[str] = frozenset(),
                pending_counts: dict[str, int] | None = None,
                current: dict[tuple[str, str], ApprovedRow] | None = None,
                has_uses: bool = True) -> list[Action]:
    """current: approved_metadata rows keyed by (table_name, column_name); a revise needs its column's row.
    has_uses False (no active gov.permitted_uses row for the table): accept and edit are refused; reject and
    revise still apply, so a correction never waits on permitted uses.
    One Action per pending decision_id (applied_at null), in decided_at order. Refusals change nothing but are
    returned so the decision is marked applied and audited. Held decisions (attribution unreadable) get no
    action. A decision already in the audit log (crash before applied_at was written) is replayed from it only
    if the row passes the identity checks and matches the audited decision, proposal, actor and status.
    approvers is required: table_name -> principals allowed to approve it."""
    if approvers is None:
        raise ValueError("approvers is required (gov.table_approvers); there is no off switch")
    by_id = {p.proposal_id: p for p in proposals}
    newest = _newest(proposals)
    newest_decided = _newest_decided(proposals)
    decided: set[str] = set()
    out = []
    pending = sorted((d for d in decisions if d.applied_at is None),
                     key=lambda d: (d.decided_at or datetime.min, d.decision_id))
    counts: dict[str, int] = {}
    for d in pending:
        counts[d.decision_id] = counts.get(d.decision_id, 0) + 1
    seen: set[str] = set()
    for d in pending:
        if d.decision_id in seen:
            continue
        seen.add(d.decision_id)
        p = by_id.get(d.proposal_id)
        if d.hold:
            continue
        n = max(counts[d.decision_id], (pending_counts or {}).get(d.decision_id, 0))
        if n > 1:  # pending_counts spans every table: a reused id never decides (or stamps) another table's row
            out.append(_refused(d, p, f"duplicate decision_id ({n} pending rows)"))
            continue
        if d.decision_id in applied_ids:
            out.append(_refused(d, p, "decision_id already used by an applied decision: insert with a new uuid()"))
            continue
        replay = _replayable(d, p, (audited or {}).get(manual_event_id(d.decision_id)), approvers, job_principal,
                             newest, decided, newest_decided)
        if replay is not None:
            if replay.new_status is not None:
                decided.add(replay.proposal_id)
            out.append(replay)
            continue
        reason = _refusal(d, by_id, decided, profiles, terms, newest, approvers, job_principal)
        if not reason and not has_uses and d.action in ("accept", "edit"):
            reason = NO_USES_REASON
        if not reason and p is not None and d.action == "revise":
            reason = _revise_refusal(d, p, decided, profiles, terms, newest, newest_decided, current or {},
                                     proposals)
        if reason or p is None:
            out.append(_refused(d, p, reason or "unknown proposal_id"))
            continue
        decided.add(p.proposal_id)
        who = d.committed_by or d.decided_by  # _refusal guarantees committed_by is set and matches
        hits = profiles[(p.run_id, p.column_name)]
        if d.action == "revise":
            row = _revised_row(d, p, _revise_hits(p, proposals, profiles), terms,
                               (current or {})[(p.table_name, p.column_name)], who)
            golden = GoldenCandidate(p.proposal_id, p.table_name, p.column_name, _term(p.term_id), row.term_id,
                                     p.sensitivity, row.sensitivity, who)
            out.append(Action(p.proposal_id, p.table_name, p.column_name, "revised", "edited", row, golden,
                              who, d.decision_id))
        elif d.action == "reject":
            out.append(Action(p.proposal_id, p.table_name, p.column_name, "rejected", "rejected", None, None,
                              who, d.decision_id))
        elif d.action == "accept":
            term_sens = terms.get(p.term_id) if p.term_id else None
            row = _approved(p, _term(p.term_id), p.description, _floored(p.sensitivity, hits, term_sens), who)
            out.append(Action(p.proposal_id, p.table_name, p.column_name, "accepted", "accepted", row, None,
                              who, d.decision_id))
        else:
            term_id = _term(d.term_id) if d.term_id is not None else _term(p.term_id)
            sens = _floored(d.sensitivity or p.sensitivity, hits, terms.get(term_id) if term_id else None)
            row = _approved(p, term_id, d.description if d.description is not None else p.description, sens, who)
            golden = GoldenCandidate(p.proposal_id, p.table_name, p.column_name, _term(p.term_id), term_id,
                                     p.sensitivity, sens, who)
            out.append(Action(p.proposal_id, p.table_name, p.column_name, "edited", "edited", row, golden,
                              who, d.decision_id))
    return out


def _replayable(d: ReviewDecision, p: ProposalRow | None, after: dict | None, approvers: dict[str, set[str]],
                job_principal: str | None, newest: dict[tuple[str, str], ProposalRow],
                decided: set[str], newest_decided: dict[tuple[str, str], ProposalRow]) -> Action | None:
    """The audited action for this decision, only if replaying it is exactly finishing what this row started.
    One decision per proposal per run: a second audited decision on an already-decided proposal is refused."""
    if after is None or p is None or p.proposal_id in decided or _identity_refusal(d, p, approvers, job_principal):
        return None
    if after.get("decision_id") != d.decision_id or after.get("proposal_id") != d.proposal_id:
        return None
    a = action_from_audit(after)
    if a.kind == "revised":
        if _revise_target_refusal(p, newest, newest_decided):  # superseded since: never write it back
            return None
    else:
        latest = newest.get((p.table_name, p.column_name))
        if latest is None or latest.proposal_id != p.proposal_id:  # superseded since: never write it back
            return None
    if a.kind != "refused" and not _same(a.actor, d.committed_by):
        return None
    allowed = DECIDED if a.kind == "revised" else ("proposed", a.new_status)
    if p.status not in allowed:
        return None
    return a


def plan(decisions: list[ReviewDecision], proposals: list[ProposalRow],
         profiles: dict[tuple[str, str], ProfileHits], terms: dict[str, str],
         already_routed: set[str], *, approvers: dict[str, set[str]] | None = None,
         job_principal: str | None = None, audited: dict[str, dict] | None = None,
         applied_ids: set[str] | frozenset[str] = frozenset(),
         pending_counts: dict[str, int] | None = None,
         current: dict[tuple[str, str], ApprovedRow] | None = None, has_uses: bool = True) -> list[Action]:
    """Owner decisions first, then the auto-rule on what they did not decide. Without permitted uses
    (has_uses False) the auto-rule plans nothing: proposals stay proposed, neither accepted nor routed. A refused
    decision changes nothing, so it does not take the proposal away from the auto-rule; a held one does (an
    owner is waiting). Audited actions are replayed."""
    manual = plan_manual(decisions, proposals, profiles, terms, approvers=approvers, job_principal=job_principal,
                         audited=audited, applied_ids=applied_ids, pending_counts=pending_counts,
                         current=current, has_uses=has_uses)
    skip = {a.proposal_id for a in manual if a.new_status is not None}
    skip |= {d.proposal_id for d in decisions if d.hold and d.applied_at is None}
    auto = plan_auto(proposals, profiles, terms, already_routed, skip=skip) if has_uses else []
    if audited:  # an auto-accept audited before a crash is replayed even if inputs have changed since
        replays = {}
        for p in latest_pending(proposals):
            after = audited.get(audit_event_id("accepted", p.proposal_id, None))
            if after is not None and p.proposal_id not in skip:
                replays[p.proposal_id] = action_from_audit(after)
        auto = [replays.pop(a.proposal_id, a) for a in auto] + list(replays.values())
    return manual + auto


# ---------------------------------------------------------------- audit rows and replay

AUDIT_ACTION = {"accepted": "proposal_accepted", "edited": "proposal_edited", "rejected": "proposal_rejected",
                "revised": "proposal_revised", "review_routed": "review_routed", "refused": "decision_refused"}


def manual_event_id(decision_id: str) -> str:
    """One audit event per owner decision, whatever it planned to, so a replay finds it."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"govgates-approve-decision|{decision_id}"))


def event_id_for(a: Action, audited: dict[str, dict] | None = None, insert_version: int | str | None = None) -> str:
    """manual_event_id for an owner decision, unless that id already holds a different action (a reused
    decision_id): then the refusal gets its own id, salted with the row's insert version so every reuse is
    audited and a re-run of the same reuse is not."""
    if a.decision_id is None:
        return audit_event_id(a.kind, a.proposal_id, None)
    eid = manual_event_id(a.decision_id)
    if audited and eid in audited and audited[eid] != audit_after(a):
        salt = a.decision_id if insert_version is None else f"{a.decision_id}@{insert_version}"
        return audit_event_id("refused", a.proposal_id, salt)
    return eid


def audit_after(a: Action) -> dict:
    return {"proposal_id": a.proposal_id, "table_name": a.table_name, "column_name": a.column_name,
            "kind": a.kind, "new_status": a.new_status, "actor": a.actor, "decision_id": a.decision_id,
            "reason": a.reason, "approved": asdict(a.approved) if a.approved else None,
            "golden": asdict(a.golden) if a.golden else None}


def action_from_audit(after: dict) -> Action:
    return Action(after["proposal_id"], after["table_name"], after["column_name"], after["kind"],
                  after.get("new_status"),
                  ApprovedRow(**after["approved"]) if after.get("approved") else None,
                  GoldenCandidate(**after["golden"]) if after.get("golden") else None,
                  after["actor"], after.get("decision_id"), after.get("reason"))


def _json_safe(v):
    return None if isinstance(v, float) and math.isnan(v) else v  # NaN is not valid JSON


def audit_rows(actions: list[Action], proposals: dict[str, ProposalRow], current: dict[str, dict], table: str,
               unknown_object: str, audited: dict[str, dict] | None = None,
               insert_versions: dict[str, int | str] | None = None) -> list[tuple]:  # noqa: F811
    """(event_id, gate, action, actor, object, before_json, after_json) per action. before holds the proposal and,
    when the action writes approved_metadata, the row it replaces (current is keyed by column_name)."""
    out = []
    for a in actions:
        p = proposals.get(a.proposal_id)
        before: dict = {"proposal": {k: _json_safe(getattr(p, k)) for k in (
            "proposal_id", "run_id", "column_name", "term_id", "description", "sensitivity", "confidence",
            "method", "status")} if p else None}
        if a.approved is not None:
            before["approved"] = current.get(a.approved.column_name)
        obj = table if p else unknown_object
        version = (insert_versions or {}).get(a.decision_id) if a.decision_id else None
        out.append((event_id_for(a, audited, version), "hydration", AUDIT_ACTION[a.kind], a.actor, obj,
                    json.dumps(before, default=str), json.dumps(audit_after(a))))
    return out


def refusal_salt(d: ReviewDecision) -> str:
    """Per-row salt for a reused-id refusal's audit event_id: the insert version when attribution read it,
    otherwise the row's own decided_at and principal, so two unattributed reuses still get two audit rows."""
    if d.insert_version is not None:
        return str(d.insert_version)
    return f"{d.decided_at.isoformat() if d.decided_at else ''}|{d.committed_by or d.decided_by}"
