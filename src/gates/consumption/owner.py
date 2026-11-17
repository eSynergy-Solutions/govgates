"""Owner decision on a consumption request (specs/consumption.md Flow 4-6). Pure, deterministic; no model output.

An owner INSERTs a row into gov.request_decisions (approve / reject with reason / note). The job attributes it to
the principal that committed the INSERT; that principal is the decider, never the typed decided_by. Checks, in order:
identity (committer known, equals decided_by, not the job principal, a table approver for the product, not the
request's requester or consumer), then the action, then for approve / reject the request's audited state (a row
edited by hand is refused), and for approve the permitted use as it stands now. Decisions apply in commit order and
time comes from the commit, never the typed decided_at. A decision whose owner evidence row is already stored is a
replay: identity is re-checked, state is not (it has moved on), and only missing writes are re-emitted.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta

from gates.consumption.decide import DEFAULT_TERM_DAYS, PermittedUse
from gates.consumption.grants import agreement_from, agreement_state
from gates.consumption.permitted import identity_refusal, same_principal
from gates.consumption.requests import reason_sha256
from gates.hydration.apply import event_id_of

GATE = "consumption"
ACTIONS = ("approve", "reject", "note")
_KIND = {"approve": "approved", "reject": "rejected", "note": "noted"}
_EVIDENCE_OUTCOME = {"approve": "approved", "reject": "rejected", "note": "note"}
_ACTION_OF = {v: k for k, v in _EVIDENCE_OUTCOME.items()}
_STATUS = {"approve": "approved", "reject": "rejected"}
TERMINAL = ("approved", "rejected", "denied", "withdrawn")
_RECEIVED_FIELDS = (("requester", "requester"), ("consumer", "consumer"), ("product", "securable"),
                    ("intended_use", "intended_use"), ("use_mode", "use_mode"), ("delivery", "delivery"),
                    ("access_mode", "access_mode"))


@dataclass(frozen=True)
class OwnerDecision:
    decision_id: str
    request_id: str
    action: str
    reason: str | None
    note: str | None
    decided_by: str
    decided_at: datetime | None
    committed_by: str | None = None
    committed_at: datetime | None = None
    insert_version: int | None = None
    attribution_error: str | None = None
    hold: bool = False


@dataclass(frozen=True)
class RequestView:
    request: dict
    received_audit: dict | None
    status_audits: list[dict]
    evidence: list[dict]
    agreement: dict | None


@dataclass(frozen=True)
class OwnerPlan:
    decision_id: str
    request_id: str
    kind: str  # approved | rejected | noted | refused | held
    reason: str | None
    evidence: list[dict]
    agreement: dict | None
    audit: list[tuple]
    new_status: str | None
    approver: str | None
    outcome: str | None


def owner_evidence_id(d: OwnerDecision) -> str:
    return event_id_of(d.request_id, "owner", d.decision_id, d.insert_version)


def _json(v) -> str | None:
    return None if v is None else json.dumps(v, sort_keys=True, default=str)


def _blank(v: str | None) -> bool:
    return not (v or "").strip()


def _refused(d: OwnerDecision, reason: str) -> OwnerPlan:
    after = {"decision_id": d.decision_id, "request_id": d.request_id, "action": d.action, "reason": reason,
             "committed_by": d.committed_by, "claimed_decided_by": d.decided_by, "insert_version": d.insert_version}
    # the actor is the committer or "unknown", never the typed (caller-chosen) decided_by
    audit = [(event_id_of("owner_decision_refused", d.decision_id, d.insert_version), GATE, "owner_decision_refused",
              d.committed_by or "unknown", d.request_id, None, _json(after))]
    return OwnerPlan(d.decision_id, d.request_id, "refused", reason, [], None, audit, None, None, f"refused: {reason}")


def _identity(d: OwnerDecision, view: RequestView | None, approvers: dict[str, set[str]],
              job_principal: str) -> str | None:
    if view is None:  # no product, so no approver list: nothing to decide on
        return d.attribution_error or f"unknown request {d.request_id!r}"
    r = view.request
    product = r.get("securable") or ""
    reason = identity_refusal(attribution_error=d.attribution_error, committed_by=d.committed_by,
                              decided_by=d.decided_by, job_principal=job_principal,
                              approvers=approvers.get(product, set()), securable=product)
    if reason:
        return reason
    if same_principal(d.committed_by, r.get("requester")):
        return "self-approval: the committer raised this request (requester)"
    if same_principal(d.committed_by, r.get("consumer")):
        return "self-approval: the committer is this request's consumer"
    return None


def _tampered(view: RequestView) -> str | None:
    """The request row must equal what was audited when it was received and decided."""
    r, rec = view.request, view.received_audit
    if rec is None:
        return "no request_received audit row for this request"
    for audit_key, row_key in _RECEIVED_FIELDS:
        if (rec.get(audit_key) or "").strip() != (r.get(row_key) or "").strip():
            return f"request {row_key} differs from its request_received audit"
    if rec.get("reason_sha256") != reason_sha256(r.get("reason")):
        return "purpose note differs from its request_received audit (or was submitted before the note was audited)"
    if not any(a.get("status") == "pending_owner" for a in view.status_audits):
        return "no audited move to pending_owner for this request"
    decided = sorted({a.get("status") for a in view.status_audits} & set(TERMINAL))
    if decided:  # status never returns to pending_owner: a decided request reset by hand is not re-opened
        return f"request already audited as {decided}; its pending_owner status was not set by the gate"
    if not any(e.get("step") == "use_check" and e.get("outcome") == "pending_owner" for e in view.evidence):
        return "no use_check evidence of pending_owner for this request"
    return None


def _current_use(r: dict, uses: list[PermittedUse]) -> tuple[PermittedUse | None, str | None]:
    rows = [u for u in uses if u.active and u.product == r.get("securable") and u.use_code == r.get("intended_use")]
    if len(rows) > 1:
        return None, f"ambiguous_permitted_use: {len(rows)} active rows for this use"
    if not rows or rows[0].approval not in ("approval_required", "standing"):
        now = rows[0].approval if rows else "no active row"
        return None, f"use_no_longer_permitted: the permitted use is now {now}"
    t = rows[0].max_term_days
    if t is not None and (isinstance(t, bool) or not isinstance(t, int) or t <= 0):
        return None, f"invalid_request: max_term_days {t!r} on the permitted use"
    return rows[0], None


def _agreement(r: dict, decided_by: str, valid_from: datetime, term: int | None) -> dict:
    rid = r["request_id"]
    return {"agreement_id": event_id_of("agreement", rid), "request_id": rid, "consumer": r.get("consumer"),
            "product": r.get("securable"), "use_code": r.get("intended_use"), "access_mode": r.get("access_mode"),
            "use_mode": r.get("use_mode"), "delivery": r.get("delivery"), "decided_by": decided_by,
            "status": "active", "valid_from": valid_from,
            "valid_to": valid_from + timedelta(days=int(term or DEFAULT_TERM_DAYS)), "version": 1}


def _audit(owner_row: dict, action: str, agreement: dict | None, decision: OwnerDecision) -> list[tuple]:
    """Built only from the stored (or about-to-be-stored) evidence row, so a replay re-emits identical rows."""
    rid, who = owner_row["request_id"], owner_row["decided_by"]
    after = {"decision_id": decision.decision_id, "insert_version": decision.insert_version, "action": action,
             "outcome": owner_row["outcome"], "decided_by": who, "decided_at": owner_row["decided_at"],
             "reason_text": owner_row["reason_text"], "note": owner_row["note"],
             "evidence_id": owner_row["decision_id"]}
    rows = [(event_id_of(rid, "owner_decision", decision.decision_id, decision.insert_version), GATE,
             "owner_decision", who, rid, None, _json(after))]
    if agreement is not None:
        rows.append((event_id_of(rid, "agreement_registered"), GATE, "agreement_registered", who, rid, None,
                     _json(agreement_state(agreement_from(agreement)))))
    if action in _STATUS:
        rows.append((event_id_of(rid, "status", _STATUS[action]), GATE, "request_status_set", who, rid,
                     _json({"status": "pending_owner"}), _json({"status": _STATUS[action]})))
    return rows


def _evidence_row(d: OwnerDecision, rid: str, step: str, outcome: str, *, eid: str, reason_text=None, note=None,
                  inputs: dict) -> dict:
    return {"decision_id": eid, "request_id": rid, "step": step, "outcome": outcome, "reason_code": None,
            "reason_text": reason_text, "note": note, "decided_by": d.committed_by, "decided_at": d.committed_at,
            "inputs_json": _json(inputs)}


def _plan_new(d: OwnerDecision, view: RequestView, uses: list[PermittedUse]) -> OwnerPlan:
    r = view.request
    rid = r["request_id"]
    inputs: dict = {"owner_decision_id": d.decision_id, "insert_version": d.insert_version}
    agreement = None
    if d.action == "approve":
        use, why = _current_use(r, uses)
        if why:
            return _refused(d, why)
        inputs["permitted_use"] = {"product": use.product, "use_code": use.use_code, "version": use.version,
                                   "max_term_days": use.max_term_days, "approval": use.approval}
        agreement = _agreement(r, d.committed_by, d.committed_at, use.max_term_days)
    owner = _evidence_row(d, rid, "owner", _EVIDENCE_OUTCOME[d.action], eid=owner_evidence_id(d),
                          reason_text=None if _blank(d.reason) else d.reason.strip(),
                          note=None if _blank(d.note) else d.note.strip(), inputs=inputs)
    evidence = [owner]
    if agreement is not None:
        evidence.append(_evidence_row(d, rid, "agreement", "approved", eid=event_id_of(rid, "agreement"),
                                      reason_text="Consumption agreement registered by the owner.", inputs=inputs))
    return OwnerPlan(d.decision_id, rid, _KIND[d.action], None, evidence, agreement,
                     _audit(owner, d.action, agreement, d), _STATUS.get(d.action),
                     d.committed_by if d.action in _STATUS else None, _KIND[d.action])


def _plan_replay(d: OwnerDecision, view: RequestView, stored: dict) -> OwnerPlan:
    """Finish what a crashed run started, from the stored owner evidence row only."""
    r = view.request
    rid = r["request_id"]
    action = _ACTION_OF.get(stored.get("outcome"))
    if action is None:
        return _refused(d, f"stored owner evidence has an unknown outcome {stored.get('outcome')!r}")
    evidence, agreement = [], None
    if action == "approve":
        inputs = json.loads(stored.get("inputs_json") or "{}")
        term = (inputs.get("permitted_use") or {}).get("max_term_days")
        registered = view.agreement
        if registered is None:
            agreement = registered = _agreement(r, stored["decided_by"], stored["decided_at"], term)
        if not any(e.get("step") == "agreement" for e in view.evidence):
            evidence.append({"decision_id": event_id_of(rid, "agreement"), "request_id": rid, "step": "agreement",
                             "outcome": "approved", "reason_code": None,
                             "reason_text": "Consumption agreement registered by the owner.", "note": None,
                             "decided_by": stored["decided_by"], "decided_at": stored["decided_at"],
                             "inputs_json": stored.get("inputs_json")})
        audit = _audit(stored, action, registered, d)
    else:
        audit = _audit(stored, action, None, d)
    return OwnerPlan(d.decision_id, rid, _KIND[action], None, evidence, agreement, audit, _STATUS.get(action),
                     stored["decided_by"] if action in _STATUS else None, _KIND[action])


def refuse_duplicates(decisions: list[OwnerDecision]) -> tuple[list[OwnerDecision], list[OwnerPlan]]:
    """Pending rows sharing a decision_id: none is decided, and ONE refusal per id (one audit row and one stamp
    source row, so the stamp MERGE never matches a target row to several source rows)."""
    counts: dict[str, int] = {}
    for d in decisions:
        counts[d.decision_id] = counts.get(d.decision_id, 0) + 1
    first: dict[str, OwnerDecision] = {}
    for d in sorted(decisions, key=lambda x: (x.insert_version if x.insert_version is not None else -1,
                                              x.decision_id)):
        if counts[d.decision_id] > 1 and not d.hold:
            first.setdefault(d.decision_id, d)
    return ([d for d in decisions if counts[d.decision_id] == 1],
            [_refused(d, f"duplicate decision_id ({counts[d.decision_id]} pending rows)") for d in first.values()])


def plan_owner(decisions: list[OwnerDecision], views: dict[str, RequestView], approvers: dict[str, set[str]],
               uses: list[PermittedUse], *, job_principal: str) -> list[OwnerPlan]:
    status = {rid: v.request.get("status") for rid, v in views.items()}
    out = []
    for d in sorted(decisions, key=lambda x: (x.insert_version if x.insert_version is not None else -1,
                                              x.decision_id)):
        if d.hold:
            out.append(OwnerPlan(d.decision_id, d.request_id, "held", None, [], None, [], None, None, None))
            continue
        view = views.get(d.request_id)
        # Replay first: the owner evidence row is written only after every check passed, so a crashed run is
        # finished from it even if the approver was removed since (a half-applied decision is worse).
        stored = None if view is None else next(
            (e for e in view.evidence if e.get("decision_id") == owner_evidence_id(d)), None)
        if stored is None:
            reason = _identity(d, view, approvers, job_principal)
            if reason:
                out.append(_refused(d, reason))
                continue
        if stored is not None:
            p = _plan_replay(d, view, stored)
            if p.new_status:
                status[d.request_id] = p.new_status
            out.append(p)
            continue
        if d.action not in ACTIONS:
            reason = f"action {d.action!r} not in {list(ACTIONS)}"
        elif d.action == "note" and _blank(d.note):
            reason = "a note needs text"
        elif d.action == "reject" and _blank(d.reason):
            reason = "a rejection needs a reason"
        elif d.action in _STATUS:
            if status.get(d.request_id) != "pending_owner":
                reason = f"request status is {status.get(d.request_id)!r}, not pending_owner"
            else:
                reason = _tampered(view)
        if reason:
            out.append(_refused(d, reason))
            continue
        p = _plan_new(d, view, uses)
        if p.new_status:
            status[d.request_id] = p.new_status
        out.append(p)
    return out
