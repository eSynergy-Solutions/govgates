"""Write planner for one decided request (specs/consumption.md Flow 1, 5, 6; contracts.md). Pure: no Spark.

Evidence first, agreement next, request status last. Replay-safe: when evidence for the request is already stored,
the outcome is rebuilt from that evidence (never decided again from current rows), stored rows are not re-emitted,
and valid_from / decided_at come from the first write, never from `now`. All ids are deterministic, and the job
writes with insert-only MERGEs, so a crashed run's replay adds nothing twice.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta

from gates.consumption.decide import DEFAULT_TERM_DAYS, Decision, Request
from gates.consumption.grants import agreement_from, agreement_state
from gates.hydration.apply import event_id_of

GATE = "consumption"
_STATUS = {"auto_approve": "approved", "pending_owner": "pending_owner", "denied": "denied"}


@dataclass(frozen=True)
class Writes:
    evidence: list[dict]
    agreement: dict | None
    status: str | None
    audit: list[tuple]


def request_id_for(consumer: str, product: str, intended_use: str, request_ref: str) -> str:
    """The caller's request_ref is part of the id, so a request can be raised again after a denial."""
    return event_id_of("access_request", consumer, product, intended_use, request_ref)


_LOCKED = ("requester", "use_mode", "delivery", "access_mode", "reason")
_RECEIVED = ("consumer", "product", "intended_use", "use_mode", "delivery", "access_mode", "requester")


def _norm(v) -> str:
    return (v or "").strip()


def reason_sha256(reason: str | None) -> str:
    """Hash of the purpose note, stripped (the stored note is stripped). The audit never carries the text itself."""
    return hashlib.sha256(_norm(reason).encode("utf-8")).hexdigest()


def submission_conflicts(stored: dict, submitted: dict) -> list[str]:
    """Fields that differ when a request_ref is reused. The purpose note counts (Decision 5): a changed note under
    the same ref is refused, never a new request and never a silent keep. None, "" and outer whitespace are equal."""
    return [k for k in _LOCKED if _norm(stored.get(k)) != _norm(submitted.get(k))]


def received_state(submitted: dict, request_ref: str) -> dict:
    """after_json of the request_received audit row: what the owner's approval is checked against."""
    return ({k: _norm(submitted.get(k)) for k in _RECEIVED}
            | {"status": "received", "request_ref": request_ref,
               "reason_sha256": reason_sha256(submitted.get("reason"))})


def _steps(decision: Decision) -> list[str]:
    if decision.step == "request_check":
        return ["request_check"]
    if decision.step == "pattern_check":
        return ["pattern_check"]
    steps = ["pattern_check", "use_check"]
    return steps + ["agreement"] if decision.outcome == "auto_approve" else steps


def _ts(v) -> datetime:
    return v if isinstance(v, datetime) else datetime.fromisoformat(str(v))


def _evidence(request: Request, decision: Decision, actor: str, now: datetime) -> list[dict]:
    rows = []
    inputs = json.dumps(decision.inputs, sort_keys=True, default=str)
    for step in _steps(decision):
        final = step == decision.step
        if step == "pattern_check" and not final:
            outcome, code, text = "pass", None, "An allowed architecture pattern matches."
        elif step == "agreement":
            outcome, code, text = "auto_approve", None, "Consumption agreement registered."
        else:
            outcome, code, text = decision.outcome, decision.reason_code, decision.reason_text
        rows.append({"decision_id": event_id_of(request.request_id, step), "request_id": request.request_id,
                     "step": step, "outcome": outcome, "reason_code": code, "reason_text": text, "note": None,
                     "decided_by": actor, "decided_at": now, "inputs_json": inputs})
    return rows


def _final_row(evidence: list[dict]) -> dict:
    """The stored decision's final row, never a fresh decide(). Fails closed on a set one MERGE can't produce."""
    by_step = {r["step"]: r for r in evidence}
    if "request_check" in by_step:
        return by_step["request_check"]
    pattern = by_step.get("pattern_check")
    if pattern is None:
        raise ValueError(f"incomplete stored evidence: no pattern_check row ({sorted(by_step)})")
    if pattern["outcome"] == "denied":
        return pattern
    if "use_check" not in by_step:
        raise ValueError("incomplete stored evidence: pattern_check passed but no use_check row")
    return by_step["use_check"]


def _agreement(request: Request, inputs: dict, valid_from: datetime) -> dict:
    use = inputs.get("permitted_use") or {}
    term = use.get("max_term_days") or DEFAULT_TERM_DAYS
    return {"agreement_id": event_id_of("agreement", request.request_id), "request_id": request.request_id,
            "consumer": request.consumer, "product": request.product, "use_code": request.intended_use,
            "access_mode": request.access_mode, "use_mode": request.use_mode, "delivery": request.delivery,
            "decided_by": "auto", "status": "active", "valid_from": valid_from,
            "valid_to": valid_from + timedelta(days=int(term)), "version": 1}


def _audit(event_id: str, action: str, actor: str, request_id: str, before, after) -> tuple:
    return (event_id, GATE, action, actor, request_id,
            None if before is None else json.dumps(before, sort_keys=True, default=str),
            None if after is None else json.dumps(after, sort_keys=True, default=str))


def plan_writes(request: Request, decision: Decision, existing_evidence: list[dict], existing_agreement: dict | None,
                *, actor: str, now: datetime) -> Writes:
    rid = request.request_id
    audit: list[tuple] = []
    if existing_evidence:
        stored = list(existing_evidence)
        final = _final_row(stored)
        outcome = final["outcome"]
        if outcome not in _STATUS:
            raise ValueError(f"stored evidence has an unknown final outcome {outcome!r}")
        first = min(_ts(r["decided_at"]) for r in stored)
        inputs = json.loads(stored[0]["inputs_json"])
        valid_from = first
        # The step rows of one decision are written in one MERGE, so the only partial state is an auto_approve whose
        # `agreement` evidence row is missing; complete it with the first write's time and actor.
        evidence = []
        if outcome == "auto_approve" and "agreement" not in {r["step"] for r in stored}:
            evidence = [{"decision_id": event_id_of(rid, "agreement"), "request_id": rid, "step": "agreement",
                         "outcome": "auto_approve", "reason_code": None,
                         "reason_text": "Consumption agreement registered.", "note": None,
                         "decided_by": stored[0]["decided_by"], "decided_at": first,
                         "inputs_json": stored[0]["inputs_json"]}]
        step, reason_code = final["step"], final["reason_code"]
    else:
        outcome, inputs, valid_from = decision.outcome, decision.inputs, now
        step, reason_code = decision.step, decision.reason_code
        evidence = _evidence(request, decision, actor, now)
    # emitted on a replay too: a crash between the evidence and audit writes must not lose it; same id, deduped
    audit.append(_audit(event_id_of(rid, "decided", outcome), "request_decided", actor, rid, None,
                        {"outcome": outcome, "step": step, "reason_code": reason_code, "inputs": inputs}))

    agreement = None
    if outcome == "auto_approve":
        # audited even when the agreement is already stored: a crash can land the agreement but not its audit row.
        # The audit row is built from the stored agreement (never recomputed); same id, so the MERGE dedupes it.
        registered = existing_agreement
        if registered is None:
            agreement = registered = _agreement(request, inputs, valid_from)
        audit.append(_audit(event_id_of(rid, "agreement_registered"), "agreement_registered", actor, rid, None,
                            agreement_state(agreement_from(registered))))

    status = _STATUS[outcome]
    audit.append(_audit(event_id_of(rid, "status", status), "request_status_set", actor, rid,
                        {"status": "received"}, {"status": status}))
    return Writes(evidence, agreement, status, audit)
