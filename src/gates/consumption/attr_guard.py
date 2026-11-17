"""Who can modify an access-control table (specs/consumption.md; PLAN.md Q17). Pure: no Spark.

A detective check: it reads owners and grants and never changes them. Works on any table, so it can later cover
gov.access_policies too. A principal counts as able to modify when it owns the table, its schema or its catalog
(owners can grant themselves anything below them), or holds MODIFY, ALL PRIVILEGES or MANAGE at any of those levels
(MANAGE can grant itself MODIFY). Groups are reported as the group and are never accepted in the allowed set:
membership changes would be invisible to this check.

It fails closed: a missing or non-managed table, an unknown owner, or a run principal that cannot see every grant
(not an owner, no READ METADATA or MANAGE) is a failure, never a pass on a short list.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from gates.hydration.apply import event_id_of

LEVELS = ("table", "schema", "catalog")
_MODIFY = {"MODIFY", "ALL PRIVILEGES", "MANAGE"}
_SEE_ALL = {"READ METADATA", "MANAGE"}
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE)


@dataclass(frozen=True)
class Grant:
    principal: str
    privilege: str
    object_type: str
    object_key: str


@dataclass(frozen=True)
class Securable:
    table: str
    table_type: str | None
    owners: dict[str, str | None]


@dataclass(frozen=True)
class Result:
    ok: bool
    failures: tuple[str, ...]
    modifiers: dict[str, tuple[str, ...]]
    unexpected: tuple[str, ...]
    allowed: tuple[str, ...]
    table: str = ""


def _priv(p: str) -> str:
    return p.strip().upper().replace("_", " ")


def modifiers(sec: Securable, grants: list[Grant]) -> dict[str, tuple[str, ...]]:
    found: dict[str, set[str]] = {}
    for level in LEVELS:
        owner = sec.owners.get(level)
        if owner:
            found.setdefault(owner.lower(), set()).add(f"owner:{level}")
    for g in grants:
        priv = _priv(g.privilege)
        if priv in _MODIFY:
            found.setdefault(g.principal.lower(), set()).add(f"{priv}@{g.object_type.lower()}")
    return {p: tuple(sorted(r)) for p, r in sorted(found.items())}


def unseen_levels(run_principal: str, sec: Securable, grants: list[Grant]) -> tuple[str, ...]:
    """Levels whose grant list the run principal may not see in full. A level is covered by ownership of it or a
    level above it, or by READ METADATA / MANAGE on it or above (SHOW GRANTS then lists every principal)."""
    me = run_principal.lower()
    owned = {lvl for lvl in LEVELS if (sec.owners.get(lvl) or "").lower() == me}
    seeing = {g.object_type.lower() for g in grants if g.principal.lower() == me and _priv(g.privilege) in _SEE_ALL}
    covers = owned | seeing
    return tuple(lvl for i, lvl in enumerate(LEVELS) if not covers & set(LEVELS[i:]))


def _is_user_or_sp(p: str) -> bool:
    return "@" in p or bool(_UUID.match(p))


def verdict(sec: Securable, grants: list[Grant], *, allowed: set[str], run_principal: str,
            owner_exception: str = "") -> Result:
    failures: list[str] = []
    allow = {a.strip().lower() for a in allowed}
    if sec.table_type is None:
        failures.append(f"{sec.table} is missing")
    elif sec.table_type.upper() != "MANAGED":
        failures.append(f"{sec.table} is {sec.table_type}, not MANAGED (its files could be changed outside UC)")
    for level in LEVELS:
        if not sec.owners.get(level):
            failures.append(f"owner of the {level} is unknown")
    for level in unseen_levels(run_principal, sec, grants):
        failures.append(f"run principal {run_principal} cannot see every grant on the {level} "
                        "(needs ownership, READ METADATA or MANAGE there or above)")
    exc = owner_exception.strip().lower()
    if exc and exc != (sec.owners.get("catalog") or "").lower():
        failures.append(f"owner_exception {exc} is not the catalog owner")
    if not allow or "" in allow:
        failures.append("allowed set is empty or has an empty entry")
    for a in sorted(allow - {""}):
        if not _is_user_or_sp(a):
            failures.append(f"allowed entry {a!r} is not a user or service principal (groups are refused)")
    mods = modifiers(sec, grants)
    unexpected = tuple(sorted(p for p in mods if p not in allow))
    for p in unexpected:
        failures.append(f"{p} can modify {sec.table} ({', '.join(mods[p])}) and is not allowed")
    failures_t = tuple(sorted(failures))
    return Result(not failures_t, failures_t, mods, unexpected, tuple(sorted(allow)), sec.table)


def canonical(result: Result) -> dict:
    return {"ok": result.ok, "failures": sorted(result.failures),
            "modifiers": {p: sorted(r) for p, r in sorted(result.modifiers.items())},
            "unexpected": sorted(result.unexpected), "allowed": sorted(result.allowed), "table": result.table}


def audit_change(current: dict, previous: dict | None,
                 previous_event_id: str | None) -> tuple[dict | None, dict, str] | None:
    if previous is not None and previous == current:
        return None
    return previous, current, event_id_of("user_attributes_check", current, previous_event_id)
