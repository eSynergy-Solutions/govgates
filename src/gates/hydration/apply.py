"""Apply step planner (specs/hydration.md step 7). Pure: no Spark.

gov.approved_metadata is the truth. The current UC state is read only to find the difference, never as truth.
Only two keys are managed: `<prefix>_sensitivity` (governed) and `<prefix>_business_term` (plain). Every other
tag on a column is left alone and never recorded.

A row is refused (no UC change, no stamp, an `apply_refused` audit row) when it has no approve audit event or
differs from its newest one: approved_metadata is only ever written by approve, so a difference means hand SQL.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime

from gates.common.contract import SENSITIVITIES, tag_keys

_NS = uuid.UUID("6f1d3c52-8a0e-4f3b-9a51-2f6c0b7e4d19")  # apply-step namespace for deterministic event ids
_DECIDED_FIELDS = ("term_id", "description", "sensitivity", "approved_by")


@dataclass(frozen=True)
class UcColumn:
    comment: str | None
    tags: dict[str, str]


@dataclass(frozen=True)
class Approved:
    table_name: str
    column_name: str
    term_id: str | None
    description: str | None
    sensitivity: str
    approved_by: str
    approved_at: str | None
    applied_to_uc_at: str | None


@dataclass(frozen=True)
class ColumnChange:
    column: str
    set_tags: dict[str, str]
    unset_tags: tuple[str, ...]
    comment_change: bool
    comment: str | None
    statements: tuple[str, ...]
    before: dict
    after: dict
    event_id: str


@dataclass(frozen=True)
class Refusal:
    column: str
    reason: str
    event_id: str
    row: dict | None = None  # the approved_metadata values that were refused
    decided: dict | None = None  # the newest approve audit event's values they were compared with


@dataclass
class TablePlan:
    changes: list[ColumnChange] = field(default_factory=list)
    refused: list[Refusal] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    untracked: list[tuple[str, dict[str, str]]] = field(default_factory=list)
    stamp: list[Approved] = field(default_factory=list)

    @property
    def statement_count(self) -> int:
        return sum(len(c.statements) for c in self.changes)


def quote_ident(name: str) -> str:
    return "`" + name.replace("`", "``") + "`"


def quote_table(table: str) -> str:
    return ".".join(quote_ident(p) for p in table.split("."))


def quote_str(s: str) -> str:
    """Databricks SQL string literal; the backslash is the documented escape."""
    return "'" + s.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _norm_comment(c: str | None) -> str | None:
    return c or None


def event_id_of(*parts) -> str:
    return str(uuid.uuid5(_NS, json.dumps(parts, sort_keys=True, default=str)))


def _ts(s: str | None) -> datetime | None:
    return datetime.fromisoformat(s.replace("Z", "+00:00")) if s else None


def _needs_stamp(r: Approved) -> bool:
    applied, approved = _ts(r.applied_to_uc_at), _ts(r.approved_at)
    if applied is None:
        return True
    if approved is None:
        return False
    if (applied.tzinfo is None) != (approved.tzinfo is None):  # compare naive with naive
        applied, approved = applied.replace(tzinfo=None), approved.replace(tzinfo=None)
    return applied < approved


def refusal_reason(r: Approved, decided: dict | None) -> str | None:
    if r.sensitivity not in SENSITIVITIES:
        return f"sensitivity {r.sensitivity!r} not in {SENSITIVITIES}"
    if decided is None:
        return "no approve audit event for this column"
    diff = [f for f in _DECIDED_FIELDS if getattr(r, f) != decided.get(f)]
    if diff:
        # neutral wording: an approve run between its audit write and its MERGE looks the same as a hand edit
        return (f"approved_metadata differs from its newest approve audit event on {', '.join(diff)} "
                "(approve in progress, or a hand edit)")
    return None


def statements(table: str, column: str, set_tags: dict[str, str], unset_tags: tuple[str, ...],
               comment_change: bool, comment: str | None) -> tuple[str, ...]:
    """One statement per kind: SET TAGS and UNSET TAGS can't be combined, and each takes one column."""
    t, c = quote_table(table), quote_ident(column)
    out = []
    if set_tags:
        pairs = ", ".join(f"{quote_str(k)} = {quote_str(v)}" for k, v in sorted(set_tags.items()))
        out.append(f"ALTER TABLE {t} ALTER COLUMN {c} SET TAGS ({pairs})")
    if unset_tags:
        out.append(f"ALTER TABLE {t} ALTER COLUMN {c} UNSET TAGS ({', '.join(quote_str(k) for k in unset_tags)})")
    if comment_change:
        out.append(f"COMMENT ON COLUMN {t}.{c} IS {'NULL' if comment is None else quote_str(comment)}")
    return tuple(out)


def _change(table: str, r: Approved, uc: UcColumn, keys: tuple[str, str]) -> ColumnChange | None:
    sens_key, term_key = keys
    current = {k: v for k, v in uc.tags.items() if k in keys}
    desired = {sens_key: r.sensitivity, **({term_key: r.term_id} if r.term_id is not None else {})}
    set_tags = {k: v for k, v in desired.items() if current.get(k) != v}
    unset_tags = tuple(k for k in (term_key,) if k in current and k not in desired)
    have, want = _norm_comment(uc.comment), _norm_comment(r.description)
    comment_change = have != want
    if not set_tags and not unset_tags and not comment_change:
        return None
    before = {"column": r.column_name, "comment": have, "tags": dict(sorted(current.items()))}
    after = {"column": r.column_name, "comment": want, "tags": dict(sorted(desired.items())),
             "approved_at": r.approved_at}
    return ColumnChange(
        column=r.column_name, set_tags=set_tags, unset_tags=unset_tags, comment_change=comment_change,
        comment=want, statements=statements(table, r.column_name, set_tags, unset_tags, comment_change, want),
        before=before, after=after,
        # the current stamp is in the id: repeating the same drift after a successful stamp is a new event,
        # while a crash replay or a retried failure (neither stamps) keeps the same id
        event_id=event_id_of("applied_to_uc", table, r.column_name, before, after, r.applied_to_uc_at))


def plan_table(table: str, approved: list[Approved], decided: dict[str, dict], uc: dict[str, UcColumn],
               prefix: str = "gg") -> TablePlan:
    """decided: column -> the `approved` dict of that column's newest approve audit event.
    uc: every column of the UC table -> its current comment and tags."""
    keys = tag_keys(prefix)
    cols = [r.column_name for r in approved]
    if len(cols) != len(set(cols)):  # e.g. one column approved under two casings of the table name
        raise ValueError(f"{table}: more than one approved row for column(s) "
                         f"{sorted({c for c in cols if cols.count(c) > 1})}")
    plan = TablePlan()
    approved_cols = {r.column_name for r in approved}
    for r in sorted(approved, key=lambda x: x.column_name):
        if r.column_name not in uc:
            plan.missing.append(r.column_name)
            continue
        reason = refusal_reason(r, decided.get(r.column_name))
        if reason:
            row_state = {f: getattr(r, f) for f in (*_DECIDED_FIELDS, "approved_at")}
            seen = decided.get(r.column_name)
            plan.refused.append(Refusal(r.column_name, reason, event_id_of(
                "apply_refused", table, r.column_name, row_state, seen), row=row_state, decided=seen))
            continue
        change = _change(table, r, uc[r.column_name], keys)
        if change:
            plan.changes.append(change)
        if change or _needs_stamp(r):
            plan.stamp.append(r)
    for col in sorted(set(uc) - approved_cols):
        ours = {k: v for k, v in uc[col].tags.items() if k in keys}
        if ours:
            plan.untracked.append((col, ours))
    return plan


APPLY_ACTIONS = ("apply_started", "applied_to_uc", "apply_failed", "apply_refused")


def audit_rows(plan: TablePlan, table: str, actor: str) -> list[tuple]:
    """(event_id, gate, action, actor, object, before_json, after_json), written before any UC statement: one
    `apply_started` per change (its id is the change's event_id) and one `apply_refused` per refusal. The outcome
    of each change is a separate row (applied_row / failed_row), so a success after a failed attempt is recorded."""
    out = [(c.event_id, "hydration", "apply_started", actor, table, json.dumps(c.before), json.dumps(c.after))
           for c in plan.changes]
    out += [(f.event_id, "hydration", "apply_refused", actor, table,
             json.dumps({"column": f.column, "row": f.row}, default=str),
             json.dumps({"column": f.column, "reason": f.reason, "decided": f.decided}, default=str))
            for f in plan.refused]
    return out


def applied_row(change: ColumnChange, table: str, actor: str) -> tuple:
    """Outcome row after the change's statements all succeeded."""
    return (event_id_of("applied_to_uc", change.event_id), "hydration", "applied_to_uc", actor, table,
            json.dumps(change.before), json.dumps(change.after))


def error_class(message: str, exc_type: str) -> str:
    """Stable part of an error: the first [ERROR_CLASS] in the message, else the exception type. Request ids,
    timestamps and statement text vary per run and must not reach the event id."""
    m = re.search(r"\[([A-Z][A-Z0-9_.]+)\]", message)
    return m.group(1) if m else exc_type


def failed_row(change: ColumnChange, table: str, actor: str, exc_type: str, message: str) -> tuple:
    """Outcome row when a statement failed. Same change + same error class -> same id, so retrying a persistent
    failure adds no rows; the full message is kept in after_json."""
    cls = error_class(message, exc_type)
    return (event_id_of("apply_failed", change.event_id, cls), "hydration", "apply_failed", actor, table,
            json.dumps(change.before), json.dumps({**change.after, "error_class": cls, "error": message[:1000]}))
