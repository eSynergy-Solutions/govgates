"""Row-filter generator planner (specs/consumption.md, `row_filter` bullet). Pure: no Spark.

gov.access_policies `row_filter` rows and gov.approved_metadata are the truth. Current UC column tags, policies and
function bodies are read only to find the difference. The generator owns `<tag_prefix>_rls_attribute` and nothing
else; hydration apply owns the sensitivity and business-term keys.

A column is tagged when it sits in an active row's schema and its approved term is `rls_term_id`. An approved row
that fails hydration apply's tamper check (`refusal_reason`) is not trusted: its column's tag is left as it is and a
`tag_refused` action is recorded, because unsetting the tag would switch the filter off for that table.

Statement order is chosen so a crash part-way never widens access: functions, policy, tag sets first; tag unsets
and policy drops last.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from gates.hydration.apply import Approved, event_id_of, quote_ident, quote_str, quote_table, refusal_reason

KINDS = ("function_set", "policy_set", "tag_set", "tag_refused", "tag_unset", "policy_drop")
REGION = "region"
HELPER_SUFFIX = "_helper"


@dataclass(frozen=True)
class PolicyRow:
    policy_id: str
    policy_type: str
    securable: str
    match_tag_key: str
    match_tag_value: str
    function_name: str
    except_principals: tuple[str, ...]
    active: bool


@dataclass(frozen=True)
class Action:
    kind: str
    object: str
    statement: str | None
    event_id: str
    before: dict | None
    after: dict | None


# ---------------------------------------------------------------- names and validation

def policy_prefix(prefix: str) -> str:
    return f"{prefix.lower()}_rls_"


def policy_name(policy_id: str, prefix: str) -> str:
    """<prefix>_rls_<readable>_<hash>: the hash keeps ids that sanitise alike (a-b, a_b) on separate policies."""
    readable = re.sub(r"[^a-z0-9]+", "_", policy_id.lower()).strip("_")
    digest = hashlib.sha256(policy_id.encode("utf-8")).hexdigest()[:8]
    return f"{policy_prefix(prefix)}{readable}_{digest}" if readable else f"{policy_prefix(prefix)}{digest}"


def _parts(name: str) -> list[str]:
    return name.split(".") if name else []


def _in_catalog(name: str, n_parts: int, catalog: str) -> bool:
    p = _parts(name)
    return len(p) == n_parts and all(p) and p[0].lower() == catalog.lower()


def validate_rows(rows: list[PolicyRow], catalog: str, rls_tag: str, hydration_principal: str,
                  gov_schema: str) -> list[str]:
    errors: list[str] = []
    active_by_securable: dict[str, list[str]] = {}
    for r in rows:
        if r.policy_type != "row_filter":
            continue
        who = f"row_filter {r.policy_id!r}"
        if not _in_catalog(r.securable, 2, catalog):
            errors.append(f"{who}: securable {r.securable!r} is not a schema in catalog {catalog!r}")
        if not _in_catalog(r.function_name, 3, catalog) or _parts(r.function_name)[1].lower() != gov_schema.lower():
            errors.append(f"{who}: function_name {r.function_name!r} is not a function in {catalog}.{gov_schema}")
        if r.match_tag_key != rls_tag:
            errors.append(f"{who}: match_tag_key {r.match_tag_key!r} is not {rls_tag!r}")
        if r.match_tag_value != REGION:
            errors.append(f"{who}: match_tag_value {r.match_tag_value!r} is not {REGION!r}")
        if r.active:
            active_by_securable.setdefault(r.securable.lower(), []).append(r.policy_id)
            if not hydration_principal:
                errors.append(f"{who}: hydration principal is not configured")
            elif hydration_principal.lower() not in {p.lower() for p in r.except_principals}:
                errors.append(f"{who}: except_principals must contain the hydration principal")
    for securable, ids in sorted(active_by_securable.items()):
        if len(ids) > 1:
            errors.append(f"more than one active row_filter on {securable}: {sorted(ids)}")
    return errors


# ---------------------------------------------------------------- region rule

def region_visible(row_region: str | None, caller_regions: list[str]) -> bool:
    """Reference of the spec rule; helper_sql implements the same thing in SQL."""
    if "NONE" in caller_regions:
        return False
    if "ALL" in caller_regions:
        return True
    return row_region is not None and row_region in caller_regions


def _helper_body() -> str:
    return ("CASE WHEN regions IS NULL THEN false "
            "WHEN array_contains(regions, 'NONE') THEN false "
            "WHEN array_contains(regions, 'ALL') THEN true "
            "WHEN r IS NULL THEN false "
            "ELSE array_contains(regions, r) END")


def _filter_body(helper_name: str, user_attributes_table: str) -> str:
    return (f"{quote_table(helper_name)}(r, (SELECT collect_set(region) FROM {quote_table(user_attributes_table)} "
            "WHERE principal = session_user() OR is_account_group_member(principal)))")


def helper_sql(name: str) -> str:
    return (f"CREATE OR REPLACE FUNCTION {quote_table(name)}(r STRING, regions ARRAY<STRING>)\n"
            f"RETURNS BOOLEAN\nRETURN {_helper_body()}")


def filter_sql(name: str, helper_name: str, user_attributes_table: str) -> str:
    return (f"CREATE OR REPLACE FUNCTION {quote_table(name)}(r STRING)\n"
            f"RETURNS BOOLEAN\nRETURN {_filter_body(helper_name, user_attributes_table)}")


# ---------------------------------------------------------------- statements

def _norm_body(body: str | None) -> str:
    return re.sub(r"\s+", " ", (body or "")).strip().rstrip(";").strip().lower()


MATCH_ALIAS = "r"
TO_PRINCIPALS = ("account users",)


def _lower_set(xs) -> tuple[str, ...]:
    return tuple(sorted({str(x).lower() for x in xs or ()}))


def _policy_def(r: PolicyRow) -> dict:
    """Everything that decides who is filtered and how; a difference in any of it re-emits the policy."""
    return {"securable": r.securable.lower(), "function_name": r.function_name.lower(),
            "match_tag_key": r.match_tag_key, "match_tag_value": r.match_tag_value, "match_alias": MATCH_ALIAS,
            "using_columns": (MATCH_ALIAS,), "to_principals": _lower_set(TO_PRINCIPALS),
            "for_securable_type": "table", "except_principals": _lower_set(r.except_principals)}


def _current_def(state: dict) -> dict:
    return {"securable": str(state.get("securable") or "").lower(),
            "function_name": str(state.get("function_name") or "").lower(),
            "match_tag_key": state.get("match_tag_key"), "match_tag_value": state.get("match_tag_value"),
            "match_alias": state.get("match_alias"), "using_columns": tuple(state.get("using_columns") or ()),
            "to_principals": _lower_set(state.get("to_principals")),
            "for_securable_type": str(state.get("for_securable_type") or "").lower(),
            "except_principals": _lower_set(state.get("except_principals"))}


def policy_sql(r: PolicyRow, prefix: str) -> str:
    excepts = ", ".join(quote_ident(p) for p in sorted(set(r.except_principals)))
    to = ", ".join(quote_ident(p) for p in TO_PRINCIPALS)
    return (f"CREATE OR REPLACE POLICY {quote_ident(policy_name(r.policy_id, prefix))} "
            f"ON SCHEMA {quote_table(r.securable)}\n"
            f"COMMENT {quote_str('Generated from gov.access_policies ' + r.policy_id)}\n"
            f"ROW FILTER {quote_table(r.function_name)}\n"
            f"TO {to}\nEXCEPT {excepts}\n"
            "FOR TABLES\n"
            f"MATCH COLUMNS has_tag_value({quote_str(r.match_tag_key)}, {quote_str(r.match_tag_value)}) "
            f"AS {MATCH_ALIAS}\n"
            f"USING COLUMNS ({MATCH_ALIAS})")


def drop_policy_sql(name: str, securable: str) -> str:
    return f"DROP POLICY {quote_ident(name)} ON SCHEMA {quote_table(securable)}"


def tag_sql(table: str, column: str, rls_tag: str, *, unset: bool) -> str:
    t, c = quote_table(table), quote_ident(column)
    if unset:
        return f"ALTER TABLE {t} ALTER COLUMN {c} UNSET TAGS ({quote_str(rls_tag)})"
    return f"ALTER TABLE {t} ALTER COLUMN {c} SET TAGS ({quote_str(rls_tag)} = {quote_str(REGION)})"


# ---------------------------------------------------------------- plan

def _in_schema(table: str, securable: str) -> bool:
    p = table.lower().split(".")
    return len(p) == 3 and f"{p[0]}.{p[1]}" == securable.lower()


def _obj(table: str, column: str) -> str:
    return f"{table}.{column}".lower()


def plan(rows: list[PolicyRow], approved: list[Approved], decided: dict, current_tags: set[tuple[str, str]],
         current_policies: dict[tuple[str, str], dict], current_functions: dict[str, str], *, catalog: str,
         rls_tag: str, rls_term_id: str, hydration_principal: str, gov_schema: str, tag_prefix: str,
         user_attributes_table: str, last_events: dict[str, str] | None = None) -> list[Action]:
    """Actions to converge UC on gov. `current_policies` is keyed by (schema_lower, policy_name).

    `last_events[object]` (object = Action.object lowercased) is the newest completed consumption audit event for
    that object. It is part of every event_id, so a return to an earlier state (drift repaired, tag re-set) is a
    new audited event, while a replay of a crashed run (same last event) dedupes.
    """
    errors = validate_rows(rows, catalog, rls_tag, hydration_principal, gov_schema)
    if errors:
        raise ValueError("; ".join(errors))
    last = {k.lower(): v for k, v in (last_events or {}).items()}

    def eid(kind: str, obj: str, *parts) -> str:
        return event_id_of(kind, obj.lower(), *parts, last.get(obj.lower()))

    ua_table = user_attributes_table
    rf = [r for r in rows if r.policy_type == "row_filter"]
    active = [r for r in rf if r.active]
    active_securables = {r.securable.lower() for r in active}
    tags = {(t.lower(), c.lower()) for t, c in current_tags}
    ours = policy_prefix(tag_prefix)
    out: list[Action] = []

    # functions: one helper + one filter per distinct function_name of an active row
    for fn in sorted({r.function_name for r in active}, key=str.lower):
        helper = fn + HELPER_SUFFIX
        for name, sql in ((helper, helper_sql(helper)), (fn, filter_sql(fn, helper, ua_table))):
            body = sql.split("\nRETURN ", 1)[1]
            current = current_functions.get(name.lower())
            if current is None or _norm_body(current) != _norm_body(body):
                out.append(Action("function_set", name, sql, eid("function_set", name, _norm_body(body)),
                                  {"body": current}, {"body": body}))

    # policies of active rows
    wanted_keys = set()
    for r in active:
        name = policy_name(r.policy_id, tag_prefix)
        key = (r.securable.lower(), name)
        wanted_keys.add(key)
        want = _policy_def(r)
        have = current_policies.get(key)
        if have is None or _current_def(have) != want:
            out.append(Action("policy_set", name, policy_sql(r, tag_prefix), eid("policy_set", name, want),
                              None if have is None else _current_def(have), want))

    # tags in active securables
    desired: set[tuple[str, str]] = set()
    protected: set[tuple[str, str]] = set()
    approved_at: dict[tuple[str, str], str | None] = {}
    for a in sorted(approved, key=lambda x: (x.table_name.lower(), x.column_name.lower())):
        key = (a.table_name.lower(), a.column_name.lower())
        if not any(_in_schema(key[0], s) for s in active_securables):
            continue
        approved_at[key] = a.approved_at
        reason = refusal_reason(a, decided.get(key))
        if reason:
            protected.add(key)
            out.append(Action("tag_refused", _obj(*key), None,
                              # record-only: never chained on last_events, or each run would add a row
                              event_id_of("tag_refused", _obj(*key), rls_tag, reason, a.approved_at),
                              {"tagged": key in tags}, {"reason": reason}))
            continue
        if a.term_id == rls_term_id:
            desired.add(key)
    for key in sorted(desired - tags):
        out.append(Action("tag_set", _obj(*key), tag_sql(key[0], key[1], rls_tag, unset=False),
                          eid("tag_set", _obj(*key), rls_tag, approved_at.get(key)),
                          {"tagged": False}, {"tagged": True, rls_tag: REGION}))

    # unsets: not desired in an active schema, or any tag in a schema with no active row that a row or one of our
    # policies names. Retiring is unconditional: it does not depend on the policy still existing.
    retired = ({r.securable.lower() for r in rf} | {s for s, n in current_policies if n.startswith(ours)}) \
        - active_securables
    unset = {k for k in tags if k not in desired and k not in protected
             and any(_in_schema(k[0], s) for s in active_securables | retired)}

    # drops: our policies (by prefix) that no active row wants on that schema; others are never touched
    drops: list[Action] = []
    for (securable, name), state in sorted(current_policies.items()):
        if not name.startswith(ours) or (securable, name) in wanted_keys:
            continue
        before = _current_def(state)
        drops.append(Action("policy_drop", name, drop_policy_sql(name, securable),
                            eid("policy_drop", name, securable, before), before, None))

    for key in sorted(unset):
        out.append(Action("tag_unset", _obj(*key), tag_sql(key[0], key[1], rls_tag, unset=True),
                          eid("tag_unset", _obj(*key), rls_tag, approved_at.get(key)),
                          {"tagged": True}, {"tagged": False}))
    out.extend(drops)
    return sorted(out, key=lambda x: KINDS.index(x.kind))
