"""Column-mask generator planner (specs/consumption.md: Policy model `mask` rows, Join-up, EXCEPT). Pure: no Spark.

gov.access_policies `mask` rows + gov.approved_metadata are the truth; current UC policies and function bodies are
read only to find the difference. One schema-level ABAC column-mask policy per active row, matched on
`<tag_prefix>_sensitivity = <class>`, so a column hydration newly approves as pii is masked with no consumption
action (the join-up). The hydration principal is in every EXCEPT, or profiling would read masked values.

The mask returns the matrix's literal `'***MASKED***'` (STRING). A masked non-STRING column makes a masked reader's
query fail (the output is cast to the column type): fails closed, reported as a warning. Failures (the job applies
nothing for masks and exits non-zero): a pii column in a schema with no active pii mask (default deny), and a masked
row-filter input column (a masked column can't feed another policy).
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass

from gates.common.contract import SENSITIVITIES
from gates.consumption.policy_rows import row_dict
from gates.consumption.row_filter import (
    HELPER_SUFFIX,
    Action,
    PolicyRow,
    _in_catalog,
    _in_schema,
    _lower_set,
    _parts,
)
from gates.hydration.apply import Approved, event_id_of, quote_ident, quote_str, quote_table

KINDS = ("function_set", "policy_set", "policy_drop")
MASK_LITERAL = "***MASKED***"
MATCH_ALIAS = "c"
TO_PRINCIPALS = ("account users",)
_FORBIDDEN_EXCEPT = {"account users", "users", "admins", ""}


@dataclass(frozen=True)
class MaskPlan:
    actions: list[Action]
    failures: tuple[str, ...]
    warnings: tuple[str, ...]


def mask_prefix(prefix: str) -> str:
    return f"{prefix.lower()}_mask_"


def mask_policy_name(policy_id: str, prefix: str) -> str:
    readable = re.sub(r"[^a-z0-9]+", "_", policy_id.lower()).strip("_")
    digest = hashlib.sha256(policy_id.encode("utf-8")).hexdigest()[:8]
    return f"{mask_prefix(prefix)}{readable}_{digest}" if readable else f"{mask_prefix(prefix)}{digest}"


def validate_mask_rows(rows: list[PolicyRow], *, catalog: str, sensitivity_tag: str, hydration_principal: str,
                       gov_schema: str) -> list[str]:
    errors: list[str] = []
    seen: dict[tuple[str, str], list[str]] = {}
    for r in rows:
        if r.policy_type != "mask":
            continue
        who = f"mask {r.policy_id!r}"
        if not _in_catalog(r.securable, 2, catalog):
            errors.append(f"{who}: securable {r.securable!r} is not a schema in catalog {catalog!r}")
        if not _in_catalog(r.function_name, 3, catalog) or _parts(r.function_name)[1].lower() != gov_schema.lower():
            errors.append(f"{who}: function_name {r.function_name!r} is not a function in {catalog}.{gov_schema}")
        if r.match_tag_key != sensitivity_tag:
            errors.append(f"{who}: match_tag_key {r.match_tag_key!r} is not {sensitivity_tag!r}")
        if r.match_tag_value not in SENSITIVITIES:
            errors.append(f"{who}: match_tag_value {r.match_tag_value!r} is not one of {SENSITIVITIES}")
        bad = sorted({p for p in r.except_principals if p.strip().lower() in _FORBIDDEN_EXCEPT})
        if bad or any(not p.strip() for p in r.except_principals):
            errors.append(f"{who}: except_principals may not contain everyone or an empty name ({bad})")
        if r.active:
            seen.setdefault((r.securable.lower(), r.match_tag_value), []).append(r.policy_id)
            if not hydration_principal:
                errors.append(f"{who}: hydration principal is not configured")
            elif hydration_principal.lower() not in {p.lower() for p in r.except_principals}:
                errors.append(f"{who}: except_principals must contain the hydration principal")
    for (securable, cls), ids in sorted(seen.items()):
        if len(ids) > 1:
            errors.append(f"more than one active {cls} mask on {securable}: {sorted(ids)}")
    # one masking function per sensitivity class (spec), and no function shared between classes
    by_class: dict[str, set[str]] = {}
    for r in rows:
        if r.policy_type == "mask" and r.active:
            by_class.setdefault(r.match_tag_value, set()).add(r.function_name.lower())
    for cls, fns in sorted(by_class.items()):
        if len(fns) > 1:
            errors.append(f"class {cls} uses more than one mask function: {sorted(fns)}")
    owners: dict[str, list[str]] = {}
    for cls, fns in by_class.items():
        for fn in fns:
            owners.setdefault(fn, []).append(cls)
    for fn, classes in sorted(owners.items()):
        if len(classes) > 1:
            errors.append(f"mask function {fn} is shared by classes {sorted(classes)}")
    return errors


def function_clashes(rows: list[PolicyRow]) -> list[str]:
    """A mask function may not be a row-filter function or its helper: the two tasks would overwrite each other."""
    rls = set()
    for r in rows:
        if r.policy_type == "row_filter":
            rls |= {r.function_name.lower(), (r.function_name + HELPER_SUFFIX).lower()}
    return sorted({f"mask {r.policy_id!r}: function {r.function_name} is a row-filter function"
                   for r in rows if r.policy_type == "mask" and r.function_name.lower() in rls})


def mask_sql(name: str) -> str:
    return (f"CREATE OR REPLACE FUNCTION {quote_table(name)}(v STRING)\n"
            f"RETURNS STRING\nRETURN {quote_str(MASK_LITERAL)}")


def _norm_body(body: str | None) -> str:
    """Whitespace only: the literal's case matters (a hand-edited '***masked***' is drift)."""
    return re.sub(r"\s+", " ", body or "").strip().rstrip(";").strip()


def _policy_def(r: PolicyRow) -> dict:
    return {"securable": r.securable.lower(), "function_name": r.function_name.lower(),
            "match_tag_key": r.match_tag_key, "match_tag_value": r.match_tag_value, "match_alias": MATCH_ALIAS,
            "on_column": MATCH_ALIAS, "to_principals": _lower_set(TO_PRINCIPALS), "for_securable_type": "table",
            "except_principals": _lower_set(r.except_principals)}


def _current_def(state: dict) -> dict:
    return {"securable": str(state.get("securable") or "").lower(),
            "function_name": str(state.get("function_name") or "").lower(),
            "match_tag_key": state.get("match_tag_key"), "match_tag_value": state.get("match_tag_value"),
            "match_alias": state.get("match_alias"), "on_column": state.get("on_column"),
            "to_principals": _lower_set(state.get("to_principals")),
            "for_securable_type": str(state.get("for_securable_type") or "").lower(),
            "except_principals": _lower_set(state.get("except_principals"))}


def mask_policy_sql(r: PolicyRow, prefix: str) -> str:
    excepts = ", ".join(quote_ident(p) for p in sorted(set(r.except_principals)))
    to = ", ".join(quote_ident(p) for p in TO_PRINCIPALS)
    return (f"CREATE OR REPLACE POLICY {quote_ident(mask_policy_name(r.policy_id, prefix))} "
            f"ON SCHEMA {quote_table(r.securable)}\n"
            f"COMMENT {quote_str('Generated from gov.access_policies ' + r.policy_id)}\n"
            f"COLUMN MASK {quote_table(r.function_name)}\n"
            f"TO {to}\nEXCEPT {excepts}\n"
            "FOR TABLES\n"
            f"MATCH COLUMNS has_tag_value({quote_str(r.match_tag_key)}, {quote_str(r.match_tag_value)}) "
            f"AS {MATCH_ALIAS}\n"
            f"ON COLUMN {MATCH_ALIAS}")


def _drop_sql(name: str, securable: str) -> str:
    return f"DROP POLICY {quote_ident(name)} ON SCHEMA {quote_table(securable)}"


def plan_masks(rows: list[PolicyRow], approved: list[Approved], current_policies: dict[tuple[str, str], dict],
               current_functions: dict[str, str], *, catalog: str, sensitivity_tag: str, rls_term_id: str,
               rls_rows: list[PolicyRow], hydration_principal: str, gov_schema: str, tag_prefix: str,
               column_types: dict[tuple[str, str], str], last_events: dict[str, str] | None = None) -> MaskPlan:
    errors = validate_mask_rows(rows, catalog=catalog, sensitivity_tag=sensitivity_tag,
                                hydration_principal=hydration_principal, gov_schema=gov_schema)
    errors += function_clashes(list(rows) + list(rls_rows))
    if errors:
        raise ValueError("; ".join(errors))
    last = {k.lower(): v for k, v in (last_events or {}).items()}

    def eid(kind: str, obj: str, *parts) -> str:
        return event_id_of(kind, obj.lower(), *parts, last.get(obj.lower()))

    active = [r for r in rows if r.policy_type == "mask" and r.active]
    masked = {(r.securable.lower(), r.match_tag_value) for r in active}
    out: list[Action] = []

    distinct: dict[str, str] = {}
    for r in sorted(active, key=lambda x: x.function_name):
        distinct.setdefault(r.function_name.lower(), r.function_name)
    for fn in [distinct[k] for k in sorted(distinct)]:
        sql = mask_sql(fn)
        body = sql.split("\nRETURN ", 1)[1]
        current = current_functions.get(fn.lower())
        if current is None or _norm_body(current) != _norm_body(body):
            out.append(Action("function_set", fn, sql, eid("function_set", fn, _norm_body(body)),
                              {"body": current}, {"body": body}))

    wanted = set()
    for r in active:
        name = mask_policy_name(r.policy_id, tag_prefix)
        key = (r.securable.lower(), name)
        wanted.add(key)
        want = _policy_def(r)
        have = current_policies.get(key)
        if have is None or _current_def(have) != want:
            out.append(Action("policy_set", name, mask_policy_sql(r, tag_prefix), eid("policy_set", name, want),
                              None if have is None else _current_def(have), want))

    for (securable, name), state in sorted(current_policies.items()):
        if not name.startswith(mask_prefix(tag_prefix)) or (securable, name) in wanted:
            continue
        before = _current_def(state)
        out.append(Action("policy_drop", name, _drop_sql(name, securable), eid("policy_drop", name, securable, before),
                          before, None))

    failures: list[str] = []
    warnings: list[str] = []
    rls_schemas = {r.securable.lower() for r in rls_rows if r.policy_type == "row_filter" and r.active}
    for a in sorted(approved, key=lambda x: (x.table_name.lower(), x.column_name.lower())):
        table, col = a.table_name.lower(), a.column_name.lower()
        schema = ".".join(table.split(".")[:2])
        covered = (schema, a.sensitivity) in masked
        if a.sensitivity == "pii" and not covered:
            failures.append(f"{table}.{col} is pii but {schema} has no active pii mask (default deny)")
        if covered and a.term_id == rls_term_id and any(_in_schema(table, s) for s in rls_schemas):
            failures.append(f"{table}.{col} is the row-filter input and would be masked ({a.sensitivity}); a masked "
                            "column can't feed another policy")
        if covered:
            dtype = column_types.get((table, col))
            if dtype is None or dtype.strip().upper() != "STRING":
                warnings.append(f"{table}.{col} is masked but its type is {dtype or 'unknown'}, not STRING: masked "
                                "readers' queries will fail (fails closed)")
    return MaskPlan(sorted(out, key=lambda x: KINDS.index(x.kind)), tuple(failures), tuple(warnings))


def tampered(rows: list[PolicyRow], newest_after: dict[str, dict | None]) -> list[str]:
    """Policy ids whose stored row isn't their newest audited write (policy_row_set `after`): hand SQL, or a
    access_model_sync run interrupted between its audit and its MERGE. Covers rows changed in place, rows with no
    audited write, and audited mask / row_filter rows that are gone or retyped (rows are never deleted)."""
    bad = []
    present = set()
    for r in rows:
        present.add(r.policy_id)
        after = newest_after.get(r.policy_id)
        if not after or json.loads(json.dumps(row_dict(r), sort_keys=True)) != _norm_after(after):
            bad.append(r.policy_id)
    for pid, after in newest_after.items():
        if after and after.get("policy_type") in ("mask", "row_filter") and pid not in present:
            bad.append(pid)
    return sorted(set(bad))


def safe_on_failure(actions: list[Action]) -> list[Action]:
    """When the plan has failures, only actions that add or restore masking still run; drops wait."""
    return [a for a in actions if a.kind in ("function_set", "policy_set")]


def _norm_after(after: dict) -> dict:
    a = dict(after)
    a["except_principals"] = sorted(set(a.get("except_principals") or ()))
    a["active"] = bool(a.get("active"))
    return json.loads(json.dumps(a, sort_keys=True))
