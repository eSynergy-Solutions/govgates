"""Change-data-feed attribution rules, pure (no Spark, no hydration imports, so any job can use them).

Who committed an owner-input row, when, and whether it changed after its insert. The Spark edge is
gates.common.attribution; gates.hydration.approve re-exports these names.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta


def committers(changes: list[tuple[str, int, str]], history: dict[int, str]) -> dict[str, str]:
    """decision_id -> principal that committed its insert. Only 'insert' changes count. A version missing from
    history, or a decision_id inserted by two different principals, gives no entry (unknown: refused)."""
    seen: dict[str, set[str | None]] = {}
    for decision_id, version, change in changes:
        if change != "insert":
            continue
        seen.setdefault(decision_id, set()).add(history.get(version))  # None marks a missing version
    out: dict[str, str] = {}
    for d, users in seen.items():
        if len(users) == 1:
            user = next(iter(users))
            if user is not None:
                out[d] = user
    return out


def commit_times(changes: list[tuple[str, int, str]], history_ts: dict[int, datetime]) -> dict[str, datetime]:
    """decision_id -> timestamp of the commit that inserted it (its latest insert version). The only trusted time
    for an owner row: the typed decided_at is the owner's own claim."""
    out: dict[str, datetime] = {}
    for d, v in insert_versions(changes).items():
        if v in history_ts:
            out[d] = history_ts[v]
    return out


def insert_versions(changes: list[tuple[str, int, str]]) -> dict[str, int]:
    """decision_id -> the latest commit version that inserted it."""
    out: dict[str, int] = {}
    for d, v, change in changes:
        if change == "insert":
            out[d] = max(v, out.get(d, v))
    return out


def modified_after_insert(changes: list[tuple[str, int, str]]) -> set[str]:
    """Decision ids with any update or delete. A pending decision is never touched by the job, so any change
    after the insert means the applied contents may not be what the committer inserted."""
    return {d for d, _v, change in changes if change in ("update_preimage", "update_postimage", "delete")}


_CDF_KEY = "delta.enablechangedatafeed"
_ENABLE_OPS = ("SET TBLPROPERTIES", "CREATE TABLE", "CREATE OR REPLACE TABLE", "CREATE TABLE AS SELECT")


def _parse(v):
    if isinstance(v, str) and v.lstrip()[:1] in ("{", "["):
        try:
            return json.loads(v)
        except ValueError:
            return v
    return v


def _set_value(params) -> bool | None:
    """True/False if these operation parameters set the CDF key, else None."""
    params = _parse(params)
    if isinstance(params, dict):
        for k, v in params.items():
            if str(k).strip().casefold() == _CDF_KEY:
                return str(v).strip().casefold() == "true"
            found = _set_value(v)
            if found is not None:
                return found
    return None


def _unsets_key(params) -> bool:
    params = _parse(params)
    if isinstance(params, dict):
        return any(_unsets_key(v) for v in params.values())
    if isinstance(params, list):
        return any(str(x).strip().casefold() == _CDF_KEY for x in params)
    return False


def _toggle(operation: str, params: dict) -> bool | None:
    op = str(operation).strip().upper()
    if op == "UNSET TBLPROPERTIES":
        return False if _unsets_key(params) else None
    if op in _ENABLE_OPS:
        return _set_value(params or {})
    return None


def cdf_start_version(history: list[tuple[int, datetime, str, dict]], now: datetime,
                      window: timedelta, enabled_now: bool | None = None) -> int | None:
    """Version to read table_changes from: the first enable after the latest disable (repeat enables do not move
    it), clamped to the oldest version committed within `window` (change files older than that may be vacuumed).
    None when CDF is off, no toggle is visible, or nothing was committed within the window."""
    rows = sorted(history, key=lambda h: h[0])
    toggles = []
    for v, _ts, op, params in rows:
        t = _toggle(op, params)
        if t is not None:
            toggles.append((v, t))
    recent = [v for v, ts, _op, _p in rows if ts >= now - window]
    if not toggles:  # enabling commit expired from history: trust the table's current property
        return min(recent) if enabled_now and recent else None
    if not toggles[-1][1]:
        return None
    last_off = max((i for i, (_v, on) in enumerate(toggles) if not on), default=-1)
    first_on = toggles[last_off + 1][0]
    if not recent:
        return None
    return max(first_on, min(recent))
