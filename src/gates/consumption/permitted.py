"""Permitted-use capture at hydration approval (specs/hydration.md step 6, H15; consumption.md Decision 4). Pure.

`set_permitted_use` path: an owner INSERTs a row into gov.permitted_use_inputs; the approve job attributes it to the
committing principal and plans it here. A change writes the next version of (product, use_code) and retires the
active one, which the job does in one MERGE, so a crash never leaves zero or two active rows. The same values again
change nothing. Every set or refusal is audited; ids are deterministic and the first version's id equals the seed's
(`event_id_of("permitted_use_set", product, use_code, version)`).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from datetime import datetime

from gates.consumption.decide import PermittedUse
from gates.hydration.apply import event_id_of

GATE = "consumption"
APPROVALS = ("standing", "approval_required", "prohibited")


@dataclass(frozen=True)
class UseInput:
    input_id: str
    product: str
    use_code: str
    approval: str
    conditions: str | None
    max_term_days: int | None
    decided_by: str
    decided_at: datetime | None
    committed_by: str | None = None
    committed_at: datetime | None = None
    insert_version: int | None = None
    attribution_error: str | None = None
    hold: bool = False


@dataclass(frozen=True)
class UsePlan:
    input_id: str
    kind: str  # set | unchanged | refused | held
    reason: str | None
    row: dict | None
    retire_versions: list[int]
    audit: list[tuple]
    outcome: str | None


def same_principal(a: str | None, b: str | None) -> bool:
    return a is not None and b is not None and a.strip().casefold() == b.strip().casefold()


def identity_refusal(*, attribution_error: str | None, committed_by: str | None, decided_by: str | None,
                     job_principal: str, approvers: set[str], securable: str) -> str | None:
    """Identity first, for every owner input: the committer is the decider, never the typed name."""
    if attribution_error:
        return attribution_error
    if committed_by is None:
        return ("committer unknown: no change-data-feed insert for this row in the retention window: "
                "insert the row again")
    if not same_principal(decided_by, committed_by):
        return f"decided_by {decided_by!r} does not match committer {committed_by!r}"
    if same_principal(committed_by, job_principal):
        return "committed by the job principal: the job never decides for an owner"
    if not any(same_principal(committed_by, a) for a in approvers):
        return f"{committed_by!r} is not an approver for {securable} (gov.table_approvers)"
    return None


def has_permitted_uses(product: str, current: list[PermittedUse]) -> bool:
    return any(u.active and u.product == product for u in current)


def _row_state(u: PermittedUse) -> dict:
    return {"product": u.product, "use_code": u.use_code, "approval": u.approval, "conditions": u.conditions,
            "max_term_days": u.max_term_days, "version": u.version, "active": u.active}


def _json(v) -> str | None:
    return None if v is None else json.dumps(v, sort_keys=True, default=str)


def _validation(i: UseInput, allowed_codes: tuple[str, ...]) -> str | None:
    if not (i.product or "").strip():
        return "product is required"
    if not (i.use_code or "").strip():
        return "use_code is required"
    if i.use_code not in allowed_codes:
        return f"use_code {i.use_code!r} not in permitted_use_codes {list(allowed_codes)}"
    if i.approval not in APPROVALS:
        return f"approval {i.approval!r} not in {list(APPROVALS)}"
    t = i.max_term_days
    if t is not None and (isinstance(t, bool) or not isinstance(t, int) or t <= 0):
        return f"max_term_days {t!r} must be empty or a positive whole number of days"
    return None


def _refused(i: UseInput, reason: str) -> UsePlan:
    eid = event_id_of("permitted_use_refused", i.input_id, i.insert_version)
    after = {"input_id": i.input_id, "product": i.product, "use_code": i.use_code, "reason": reason,
             "committed_by": i.committed_by, "claimed_decided_by": i.decided_by, "insert_version": i.insert_version}
    # the actor is the committer or "unknown", never the typed (caller-chosen) decided_by
    audit = [(eid, GATE, "permitted_use_refused", i.committed_by or "unknown",
              f"{i.product}:{i.use_code}", None, _json(after))]
    return UsePlan(i.input_id, "refused", reason, None, [], audit, f"refused: {reason}")


def refuse_duplicates(inputs: list[UseInput]) -> tuple[list[UseInput], list[UsePlan]]:
    """Pending rows sharing an input_id: none is applied, and ONE refusal per id."""
    counts: dict[str, int] = {}
    for i in inputs:
        counts[i.input_id] = counts.get(i.input_id, 0) + 1
    first: dict[str, UseInput] = {}
    for i in sorted(inputs, key=lambda x: (x.insert_version if x.insert_version is not None else -1, x.input_id)):
        if counts[i.input_id] > 1 and not i.hold:
            first.setdefault(i.input_id, i)
    return ([i for i in inputs if counts[i.input_id] == 1],
            [_refused(i, f"duplicate input_id ({counts[i.input_id]} pending rows)") for i in first.values()])


def plan_permitted_uses(inputs: list[UseInput], current: list[PermittedUse], approvers: dict[str, set[str]], *,
                        allowed_codes: tuple[str, ...], job_principal: str) -> list[UsePlan]:
    state = list(current)
    out = []
    for i in sorted(inputs, key=lambda x: (x.insert_version if x.insert_version is not None else -1, x.input_id)):
        if i.hold:
            out.append(UsePlan(i.input_id, "held", None, None, [], [], None))
            continue
        reason = identity_refusal(attribution_error=i.attribution_error, committed_by=i.committed_by,
                                  decided_by=i.decided_by, job_principal=job_principal,
                                  approvers=approvers.get(i.product, set()), securable=i.product)
        reason = reason or _validation(i, allowed_codes)
        if reason:
            out.append(_refused(i, reason))
            continue
        pair = [u for u in state if u.product == i.product and u.use_code == i.use_code]
        active = [u for u in pair if u.active]
        cond = i.conditions if (i.conditions or "").strip() else None
        if len(active) == 1 and (active[0].approval, active[0].conditions, active[0].max_term_days) == (
                i.approval, cond, i.max_term_days):
            out.append(UsePlan(i.input_id, "unchanged", None, None, [], [], "unchanged"))
            continue
        version = max((int(u.version) for u in pair), default=0) + 1
        row = {"product": i.product, "use_code": i.use_code, "approval": i.approval, "conditions": cond,
               "max_term_days": i.max_term_days, "set_by": i.committed_by, "set_at": i.committed_at,
               "version": version, "active": True}
        retire = sorted(int(u.version) for u in active)
        before = _row_state(max(active, key=lambda u: u.version)) if active else None
        audit = [(event_id_of("permitted_use_set", i.product, i.use_code, version), GATE, "permitted_use_set",
                  i.committed_by, f"{i.product}:{i.use_code}", _json(before), _json(row))]
        out.append(UsePlan(i.input_id, "set", None, row, retire, audit, "set"))
        state = [replace(u, active=False) if u in active else u for u in state]
        state.append(PermittedUse(i.product, i.use_code, i.approval, cond, i.max_term_days, version, True))
    return out
