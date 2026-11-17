"""Access-model adapter planner (specs/consumption.md, "Agreements and the access model"). Pure: no Spark.

The external authorisation service's model (groups, members, entitlements) is read from a YAML stub until the real
service can be read. It says WHAT a principal may see within an agreement, never WHETHER: grants come only from
agreements.

Whether a mask or row filter exists comes from config scope (the schemas the generator manages), never from the
model: retiring a row filter would show every region to anyone holding an agreement, and a missing mask blocks the
grants path. The model only decides who is excepted from a mask and which regions a principal sees, so a removed
entitlement shrinks an EXCEPT list or deletes a ref.user_attributes row. The adapter never retires a policy row.

Existing hand-written rows (written before the adapter) are adopted in place, keeping their policy_id and function,
so the first run changes an EXCEPT list rather than dropping and recreating a policy.
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass
from datetime import timedelta

import yaml

from gates.common.contract import SENSITIVITIES
from gates.consumption.row_filter import REGION, PolicyRow
from gates.hydration.apply import event_id_of

REGIONS = ("UK", "US", "AU")
SPECIAL_REGIONS = ("ALL", "NONE")
_TOP_KEYS = {"version", "masked_classes", "groups", "isolated_members"}
_GROUP_KEYS = {"members", "regions", "line_of_business", "schemas"}
_SCHEMA_KEYS = {"unmasked"}
_FORBIDDEN_GROUPS = {"account users", "users", "admins", ""}
ADAPTER_PREFIX = "am_"
# Classes that carry a mask in every schema in scope. Config, not the model: the model can only except groups from
# these, never remove a mask by leaving a class out.
MASKED_CLASSES: tuple[str, ...] = ("pii",)


@dataclass(frozen=True)
class Group:
    name: str
    members: tuple[str, ...]
    regions: tuple[str, ...]
    line_of_business: str | None
    unmasked: dict[str, tuple[str, ...]]


@dataclass(frozen=True)
class AccessModel:
    version: int
    masked_classes: tuple[str, ...]
    groups: tuple[Group, ...]
    isolated_members: tuple[str, ...]


@dataclass(frozen=True)
class RowPlan:
    writes: list[PolicyRow]
    failures: tuple[str, ...]


@dataclass(frozen=True)
class UserAttr:
    principal: str
    region: str
    line_of_business: str | None


@dataclass(frozen=True)
class AttrChange:
    kind: str
    principal: str
    region: str
    before: dict | None
    after: dict | None
    event_id: str


# ---------------------------------------------------------------- load and validate

def _str_list(value, where: str, errors: list[str]) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        errors.append(f"{where} must be a list of strings")
        return ()
    return tuple(v.strip() for v in value)


def _regions(value, who: str, errors: list[str]) -> tuple[str, ...]:
    if isinstance(value, str) and value in SPECIAL_REGIONS:
        return (value,)
    if (isinstance(value, list) and value and all(isinstance(v, str) for v in value)
            and all(v in REGIONS for v in value)):
        return tuple(sorted(set(value)))
    errors.append(f"{who}: regions must be a non-empty list of {list(REGIONS)} or one of {list(SPECIAL_REGIONS)}, "
                  f"got {value!r}")
    return ()


def _group(name, body, masked: tuple[str, ...], errors: list[str]) -> Group | None:
    who = f"group {name!r}"
    if not isinstance(name, str) or name.strip().lower() in _FORBIDDEN_GROUPS:
        errors.append(f"{who}: group name may not be empty or everyone (account users, users, admins)")
        return None
    body = body if body is not None else {}
    if not isinstance(body, dict):
        errors.append(f"{who}: must be a mapping")
        return None
    unknown = sorted(set(body) - _GROUP_KEYS)
    if unknown:
        errors.append(f"{who}: unknown key(s) {unknown}")
    members = _str_list(body.get("members"), f"{who}: members", errors)
    regions = _regions(body.get("regions"), who, errors)
    lob = body.get("line_of_business")
    if lob is not None and not isinstance(lob, str):
        errors.append(f"{who}: line_of_business must be a string or null")
        lob = None
    schemas = body.get("schemas") or {}
    unmasked: dict[str, tuple[str, ...]] = {}
    if not isinstance(schemas, dict):
        errors.append(f"{who}: schemas must be a mapping of schema name -> entitlement")
        schemas = {}
    for schema, ent in schemas.items():
        where = f"{who} schema {schema!r}"
        if not isinstance(schema, str) or not schema.strip() or "." in schema:
            errors.append(f"{where}: key must be a schema name, not catalog.schema or a table (entitlement is "
                          "per schema)")
            continue
        ent = ent if ent is not None else {}
        if not isinstance(ent, dict):
            errors.append(f"{where}: must be a mapping")
            continue
        bad = sorted(set(ent) - _SCHEMA_KEYS)
        if bad:
            errors.append(f"{where}: unknown key(s) {bad}")
        classes = _str_list(ent.get("unmasked"), f"{where}: unmasked", errors)
        for c in classes:
            if c not in masked:
                errors.append(f"{where}: unmasked class {c!r} is not in masked_classes {list(masked)}")
        unmasked[schema.strip()] = tuple(sorted(set(classes)))
    if regions == ("NONE",) and any(unmasked.values()):
        errors.append(f"{who}: regions NONE but holds unmasked entitlements; a principal with no rows holds nothing")
    return Group(name.strip(), tuple(sorted(set(members))), regions, lob, unmasked)


def load_model(text: str, allowed_classes: tuple[str, ...] = MASKED_CLASSES) -> AccessModel:
    """Parse and validate the access model. Raises ValueError listing every error found.

    `masked_classes` in the model may only name classes the config masks (`allowed_classes`)."""
    data = yaml.safe_load(text)
    if not isinstance(data, dict):
        raise ValueError("access model must be a mapping")
    errors: list[str] = []
    unknown = sorted(set(data) - _TOP_KEYS)
    if unknown:
        errors.append(f"unknown top-level key(s) {unknown}")
    if data.get("version") != 1:
        errors.append(f"version must be 1, got {data.get('version')!r}")
    masked = _str_list(data.get("masked_classes"), "masked_classes", errors)
    if not masked:
        errors.append("masked_classes must list at least one class")
    for c in masked:
        if c not in SENSITIVITIES:
            errors.append(f"masked_classes: {c!r} is not one of {SENSITIVITIES}")
        elif c not in allowed_classes:
            errors.append(f"masked_classes: {c!r} is not masked by config ({list(allowed_classes)})")
    masked = tuple(sorted(set(masked), key=lambda c: (SENSITIVITIES.index(c) if c in SENSITIVITIES else 99, c)))
    raw_groups = data.get("groups") or {}
    if not isinstance(raw_groups, dict):
        errors.append("groups must be a mapping of group name -> body")
        raw_groups = {}
    groups = [g for g in (_group(n, b, masked, errors) for n, b in raw_groups.items()) if g is not None]
    seen: set[str] = set()
    for g in groups:
        if g.name.lower() in seen:
            errors.append(f"group {g.name!r} is declared twice (names are case-insensitive)")
        seen.add(g.name.lower())
    isolated = tuple(sorted(set(_str_list(data.get("isolated_members"), "isolated_members", errors))))
    for g in groups:
        both = sorted(set(g.members) & set(isolated))
        if both:
            errors.append(f"group {g.name!r}: member(s) {both} are isolated_members and may be in no group")
    if errors:
        raise ValueError("; ".join(errors))
    return AccessModel(1, masked, tuple(sorted(groups, key=lambda g: g.name)), isolated)


# ---------------------------------------------------------------- policy rows

def _adopt(stored: list[PolicyRow], kind: str, securable: str, value: str | None) -> PolicyRow | None:
    hits = [r for r in stored if r.active and r.policy_type == kind and r.securable.lower() == securable.lower()
            and (value is None or r.match_tag_value == value)]
    return hits[0] if len(hits) == 1 else None


def _class_function(stored: list[PolicyRow], cls: str, catalog: str, gov_schema: str) -> str:
    """One masking function per class: a new mask row reuses the function the class's active masks already use."""
    fns = sorted({r.function_name for r in stored if r.active and r.policy_type == "mask" and r.match_tag_value == cls})
    return fns[0] if len(fns) == 1 else f"{catalog}.{gov_schema}.mask_{cls}"


def desired_rows(model: AccessModel, *, schemas: list[str], stored: list[PolicyRow], catalog: str, gov_schema: str,
                 sensitivity_tag: str, rls_tag: str, hydration_principal: str,
                 masked_classes: tuple[str, ...] = MASKED_CLASSES) -> list[PolicyRow]:
    """One active row_filter row per schema in scope and one active mask row per (schema, class in config).

    Both lists come from config; the model only shapes EXCEPT lists."""
    out: list[PolicyRow] = []
    for schema in schemas:
        securable = f"{catalog}.{schema}"
        rf = _adopt(stored, "row_filter", securable, None)
        out.append(PolicyRow(rf.policy_id if rf else f"{ADAPTER_PREFIX}rf_{schema}", "row_filter", securable,
                             rls_tag, REGION,
                             rf.function_name if rf else f"{catalog}.{gov_schema}.rls_region_filter_{schema}",
                             (hydration_principal,), True))
        for cls in masked_classes:
            mk = _adopt(stored, "mask", securable, cls)
            entitled = {g.name for g in model.groups if cls in g.unmasked.get(schema, ())}
            out.append(PolicyRow(mk.policy_id if mk else f"{ADAPTER_PREFIX}mask_{schema}_{cls}", "mask", securable,
                                 sensitivity_tag, cls,
                                 mk.function_name if mk else _class_function(stored, cls, catalog, gov_schema),
                                 tuple(sorted({hydration_principal} | entitled)), True))
    return out


def _same(a: PolicyRow, b: PolicyRow) -> bool:
    return (a.policy_type, a.securable.lower(), a.match_tag_key, a.match_tag_value, a.function_name.lower(),
            a.active, frozenset(a.except_principals)) == (
            b.policy_type, b.securable.lower(), b.match_tag_key, b.match_tag_value, b.function_name.lower(),
            b.active, frozenset(b.except_principals))


def _norm(d: dict | None) -> dict | None:
    if d is None:
        return None
    out = dict(d)
    out["except_principals"] = sorted(set(out.get("except_principals") or ()))
    out["active"] = bool(out.get("active"))
    out["securable"] = str(out.get("securable") or "").lower()
    out["function_name"] = str(out.get("function_name") or "").lower()
    return out


def unaudited(stored: list[PolicyRow], newest_after: dict[str, dict | None]) -> set[str]:
    """policy_ids whose stored row isn't its newest audited `after`: an interrupted run or a hand edit."""
    from gates.consumption.policy_rows import row_dict

    return {r.policy_id for r in stored if r.policy_type in ("mask", "row_filter")
            and _norm(row_dict(r)) != _norm(newest_after.get(r.policy_id))}


def plan_rows(desired: list[PolicyRow], stored: list[PolicyRow],
              newest_after: dict[str, dict | None] | None = None) -> RowPlan:
    """Rows to write, plus failures for active mask/row_filter rows the model doesn't cover (never retired here).

    With `newest_after` (newest policy_row_set `after` per policy_id), a desired row whose stored row differs from
    its audit is written again (re-asserted through the audited path), so an interrupted run or a hand edit is
    repaired by the next run instead of leaving the tamper check refusing forever."""
    by_id = {r.policy_id: r for r in stored}
    stale = unaudited(stored, newest_after) if newest_after is not None else set()
    writes = [d for d in desired
              if d.policy_id not in by_id or not _same(d, by_id[d.policy_id]) or d.policy_id in stale]
    wanted = {d.policy_id for d in desired}
    failures = tuple(sorted(
        f"active {r.policy_type} row {r.policy_id!r} on {r.securable} is not covered by the access model's scope; "
        "a person must retire it or bring its schema into scope"
        for r in stored if r.active and r.policy_type in ("mask", "row_filter") and r.policy_id not in wanted))
    return RowPlan(sorted(writes, key=lambda r: r.policy_id), failures)


# ---------------------------------------------------------------- ref.user_attributes

def desired_user_attributes(model: AccessModel) -> list[UserAttr]:
    return sorted((UserAttr(g.name, r, g.line_of_business) for g in model.groups for r in g.regions),
                  key=lambda a: (a.principal, a.region))


def _keyed(rows: list[UserAttr], what: str) -> dict[tuple[str, str], UserAttr]:
    out: dict[tuple[str, str], UserAttr] = {}
    for r in rows:
        k = (r.principal, r.region)
        if k in out:
            raise ValueError(f"{what} user_attributes has more than one row for {k}")
        out[k] = r
    return out


def plan_user_attributes(desired: list[UserAttr], stored: list[UserAttr],
                         last_events: dict[tuple[str, str], str],
                         last_after: dict[tuple[str, str], dict | None] | None = None) -> list[AttrChange]:
    """Changes to bring ref.user_attributes to the model; unaudited stored rows that stay are adopted (audited).

    With `last_after` (newest audited `after` per key), a row that stays but differs from its audit (an interrupted
    run, a hand edit) is re-adopted with a new chained event, so the audit always ends describing the table."""
    want, have = _keyed(desired, "desired"), _keyed(stored, "stored")
    changes: list[AttrChange] = []
    for k in sorted(set(want) | set(have)):
        before = asdict(have[k]) if k in have else None
        after = asdict(want[k]) if k in want else None
        if before is None:
            kind = "insert"
        elif after is None:
            kind = "delete"
        elif before != after:
            kind = "update"
        elif k not in last_events or (last_after is not None and last_after.get(k) != after):
            kind = "adopt"
        else:
            continue
        changes.append(AttrChange(kind, k[0], k[1], before, after,
                                  event_id_of("user_attributes_set", k[0], k[1], before, after, last_events.get(k))))
    return changes


# ---------------------------------------------------------------- membership (read-only report)

def membership_report(model: AccessModel, actual: dict[str, set[str]],
                      isolated_groups: dict[str, set[str]] | None = None
                      ) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """(warnings, failures). Membership belongs to the identity provider: reported, never changed or trusted.

    `isolated_groups` maps each isolated member to every group it is in (any group, not only the model's); a member
    missing from it could not be looked up, which fails closed."""
    warnings: list[str] = []
    failures: list[str] = []
    for g in model.groups:
        if g.name not in actual:
            failures.append(f"group {g.name!r} does not exist as an account group")
            continue
        have = actual[g.name]
        for m in sorted(set(g.members) - have):
            warnings.append(f"group {g.name!r}: declared member {m!r} is not a member")
        for m in sorted(have - set(g.members)):
            warnings.append(f"group {g.name!r}: member {m!r} is not declared in the model")
    for name, have in sorted(actual.items()):
        for m in sorted(set(model.isolated_members) & set(have)):
            failures.append(f"isolated member {m!r} is in group {name!r}")
    if isolated_groups is not None:
        for m in model.isolated_members:
            if m not in isolated_groups:
                failures.append(f"isolated member {m!r} could not be looked up")
            elif isolated_groups[m]:
                failures.append(f"isolated member {m!r} is in group(s) {sorted(isolated_groups[m])}")
    return tuple(warnings), tuple(failures)


# ---------------------------------------------------------------- membership gate (option B, 9 Nov)
# The CI SP can't read account groups (no read-only group role exists). A separate job, run as a person who can,
# records the membership report as a `membership_check` audit row; the adapter only writes on a fresh pass for the
# same model file.

# 26h, not 24h: the daily 06:00 run lands a few minutes after the previous one, and a clock change makes one gap 25h.
MEMBERSHIP_MAX_AGE = timedelta(hours=26)


def model_sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def membership_result(warnings: tuple[str, ...], failures: tuple[str, ...], sha: str) -> dict:
    return {"status": "fail" if failures else "pass", "model_sha": sha, "failures": list(failures),
            "warnings": list(warnings)}


def membership_gate(latest: dict | None, age: timedelta | None, sha: str, *, actor: str | None, self_actor: str,
                    future_rows: int = 0, max_age: timedelta = MEMBERSHIP_MAX_AGE) -> list[str]:
    """Failures that stop the adapter. `age` and `future_rows` come from SQL against the database clock, so no
    timestamp is interpreted in Python: a future-dated row (which would otherwise stay newest for ever) fails."""
    if future_rows:
        return [f"{future_rows} membership_check row(s) are dated in the future; a person must investigate"]
    if latest is None or age is None:
        return ["no membership check recorded; run job check_access_model_membership"]
    if latest.get("status") not in ("pass", "fail"):
        return [f"membership check result unreadable: {latest!r}"]
    out = []
    if actor is None or actor.strip().lower() == self_actor.strip().lower():
        out.append(f"membership check was recorded by {actor!r}, the adapter's own principal; a person must run it")
    if latest["status"] == "fail":
        out.append(f"membership check failed: {latest.get('failures')}")
    if latest.get("model_sha") != sha:
        out.append("membership check was run against a different model file; run it again")
    if age > max_age or age < timedelta(0):
        out.append(f"membership check is {age} old, outside 0..{max_age}; run it again")
    return out


def membership_write(result: dict, today: str, newest: tuple[str, dict, str] | None) -> str | None:
    """Event id for this check's audit row, or None when the newest row already says the same thing today.

    `today` and newest's day are UTC dates from SQL. `newest` = (event_id, after, day) of the newest membership_check
    row. The id chains on it, so returning to an earlier result on the same day (file A, then B, then A) is a new
    newest row, never a collision with A's."""
    if newest is not None and newest[1] == result and newest[2] == today:
        return None
    return event_id_of("membership_check", today, result, newest[0] if newest else None)
