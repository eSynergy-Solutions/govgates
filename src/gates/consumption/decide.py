"""Consumption decision (specs/consumption.md Flow 1-3, "Precedence is fixed", "Reason codes for a denial",
"Decisions (8 Nov)"). Pure, deterministic code; no model output decides anything (ground rule 4).

Fixed order, deny beats everything, first matching rule wins:
request_check (invalid_request, proxy_not_implemented) -> pattern_check -> use_check.
Only active rows count. `inputs` records the ids and versions of the rows read, never the consumer's free text,
so a decision can be replayed.
"""

from __future__ import annotations

from dataclasses import dataclass

ACCESS_MODES = ("system", "proxy")
DEFAULT_TERM_DAYS = 365
_REQUIRED = ("consumer", "product", "intended_use", "use_mode", "delivery", "access_mode", "requester")


@dataclass(frozen=True)
class Request:
    request_id: str
    consumer: str
    product: str
    intended_use: str
    reason: str | None
    use_mode: str
    delivery: str
    access_mode: str
    requester: str
    requested_at: str | None


@dataclass(frozen=True)
class Pattern:
    pattern_id: str
    use_mode: str
    delivery: str
    access_mode: str
    allowed: bool
    version: int
    active: bool


@dataclass(frozen=True)
class PermittedUse:
    product: str
    use_code: str
    approval: str | None
    conditions: str | None
    max_term_days: int | None
    version: int
    active: bool


@dataclass(frozen=True)
class Decision:
    outcome: str
    step: str
    reason_code: str | None
    reason_text: str
    inputs: dict


def _inputs(pattern: Pattern | None = None, use: PermittedUse | None = None) -> dict:
    return {"pattern": None if pattern is None else {"pattern_id": pattern.pattern_id, "version": pattern.version},
            "permitted_use": None if use is None else {"product": use.product, "use_code": use.use_code,
                                                       "version": use.version, "max_term_days": use.max_term_days}}


def _deny(step: str, code: str, text: str, inputs: dict) -> Decision:
    return Decision("denied", step, code, text, inputs)


def _three_part(name: str) -> bool:
    parts = name.split(".")
    return len(parts) == 3 and all(parts)


def _valid_term(v) -> bool:
    if v is None:
        return True
    return isinstance(v, int) and not isinstance(v, bool) and v > 0


def decide(request: Request, patterns: list[Pattern], permitted_uses: list[PermittedUse], *,
           no_uses_outcome: str = "deny") -> Decision:
    # request_check
    for f in _REQUIRED:
        if not getattr(request, f):
            return _deny("request_check", "invalid_request", f"The request has no {f}.", _inputs())
    if request.access_mode not in ACCESS_MODES:
        return _deny("request_check", "invalid_request",
                     f"Access mode must be one of {', '.join(ACCESS_MODES)}.", _inputs())
    if not _three_part(request.product):
        return _deny("request_check", "invalid_request", "The product must be a catalog.schema.table name.",
                     _inputs())
    if request.access_mode == "proxy":
        return _deny("request_check", "proxy_not_implemented",
                     "Proxy mode is not implemented yet; on-behalf-of access is out of scope for now.", _inputs())

    # pattern_check
    triple = (request.use_mode, request.delivery, request.access_mode)
    rows = [p for p in patterns if p.active and (p.use_mode, p.delivery, p.access_mode) == triple]
    banned = [p for p in rows if not p.allowed]
    if banned:
        return _deny("pattern_check", "pattern_misaligned",
                     "This use mode, delivery and access mode is not an allowed architecture pattern.",
                     _inputs(pattern=sorted(banned, key=lambda p: p.pattern_id)[0]))
    if not rows:
        return _deny("pattern_check", "pattern_misaligned",
                     "No allowed architecture pattern matches this use mode, delivery and access mode.", _inputs())
    if len(rows) > 1:
        return _deny("pattern_check", "ambiguous_pattern",
                     "More than one active architecture pattern matches; the pattern list needs fixing.", _inputs())
    pattern = rows[0]

    # use_check
    product_rows = [u for u in permitted_uses if u.active and u.product == request.product]
    if not product_rows:
        if no_uses_outcome == "owner":
            return Decision("pending_owner", "use_check", "no_permitted_uses",
                            "The product has no permitted uses recorded; the owner decides.", _inputs(pattern))
        return _deny("use_check", "no_permitted_uses", "The product has no permitted uses recorded.",
                     _inputs(pattern))
    use_rows = [u for u in product_rows if u.use_code == request.intended_use]
    if any(u.approval == "prohibited" for u in use_rows) or not use_rows:
        return _deny("use_check", "use_not_permitted", "This use is not permitted for the product.",
                     _inputs(pattern))
    if len(use_rows) > 1:
        return _deny("use_check", "ambiguous_permitted_use",
                     "More than one active permitted-use row covers this use; the owner needs to fix it.",
                     _inputs(pattern))
    use = use_rows[0]
    if not _valid_term(use.max_term_days):
        return _deny("use_check", "invalid_request", "The permitted use has an invalid maximum term.",
                     _inputs(pattern))
    if use.approval == "approval_required":
        return Decision("pending_owner", "use_check", None, "This use needs the owner's approval.",
                        _inputs(pattern, use))
    if use.approval == "standing":
        return Decision("auto_approve", "use_check", None, "This is a standing use for the product.",
                        _inputs(pattern, use))
    return _deny("use_check", "use_not_permitted", "This use is not permitted for the product.", _inputs(pattern))
