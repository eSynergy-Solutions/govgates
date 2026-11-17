"""Pure core of the persona access harness (specs/consumption.md, "Access test harness"). No workspace calls.

Reads the human-written matrix, builds one query per persona per table, classifies what the persona saw, compares
it with the matrix and renders the markdown grid. A masked column is one where every value equals the mask literal
(the mask function returns it even for NULL input); a visible column has no such value; anything else fails. Only a
permission error counts as `denied`: any other error (missing table or column) fails, so it can never pass as denied.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import yaml

MASK_DEFAULT = "***MASKED***"
EXPECTED = ("visible", "masked", "denied")
_DENIED = re.compile(r"42501|INSUFFICIENT_PERMISSIONS|PERMISSION_DENIED")
_TARGET = re.compile(r"does not have (SELECT|USE SCHEMA|USE CATALOG) on (Table|Schema|Catalog) '([^']+)'", re.I)


@dataclass(frozen=True)
class Persona:
    key: str
    group: str | None
    profile: str
    regions: object
    pii: str


@dataclass(frozen=True)
class TableSpec:
    name: str
    columns: dict[str, dict[str, str]]
    rows: dict[str, dict[str, int] | str]


@dataclass(frozen=True)
class Matrix:
    version: int
    mask_literal: str
    regions: tuple[str, ...]
    personas: dict[str, Persona]
    tables: dict[str, TableSpec]


@dataclass(frozen=True)
class Observation:
    denied: bool
    error: str | None
    cells: dict[str, str]
    rows: dict[str | None, int]
    checksums: dict[str, dict[str | None, tuple[int, int, str | None]]] = field(default_factory=dict)


@dataclass(frozen=True)
class Diff:
    persona: str
    table: str
    item: str
    expected: str
    actual: str


# ---------------------------------------------------------------- matrix

def load_matrix(text: str) -> Matrix:
    data = yaml.safe_load(text)
    if not isinstance(data, dict):
        raise ValueError("matrix must be a mapping")
    errors: list[str] = []
    regions = tuple(data.get("regions") or ())
    personas: dict[str, Persona] = {}
    for key, p in (data.get("personas") or {}).items():
        p = p or {}
        personas[key] = Persona(key, p.get("group"), p.get("profile") or f"persona-{key}", p.get("regions"),
                                p.get("pii") or "")
    tables: dict[str, TableSpec] = {}
    for name, spec in (data.get("tables") or {}).items():
        if not isinstance(name, str) or len(name.split(".")) != 2 or not all(name.split(".")):
            errors.append(f"table {name!r}: name must be schema.table")
            continue
        spec = spec or {}
        columns: dict[str, dict[str, str]] = {}
        for col, cells in (spec.get("columns") or {}).items():
            cells = cells or {}
            for persona, value in cells.items():
                if persona not in personas:
                    errors.append(f"{name}.{col}: persona {persona!r} is not declared")
                if value not in EXPECTED:
                    errors.append(f"{name}.{col}: {persona}: {value!r} is not one of {EXPECTED}")
            for persona in sorted(set(personas) - set(cells)):
                errors.append(f"{name}.{col}: no expectation for persona {persona!r}")
            columns[col] = dict(cells)
        rows_in = spec.get("rows") or {}
        rows: dict[str, dict[str, int] | str] = {}
        for persona in sorted(set(personas) - set(rows_in)):
            errors.append(f"{name} rows: no expectation for persona {persona!r}")
        for persona, value in rows_in.items():
            if persona not in personas:
                errors.append(f"{name} rows: persona {persona!r} is not declared")
            if value == "denied":
                rows[persona] = "denied"
            elif isinstance(value, dict):
                bad = sorted(str(r) for r in set(value) - set(regions))
                if bad:
                    errors.append(f"{name} rows: {persona}: region(s) {bad} not in regions {list(regions)}")
                rows[persona] = {str(r): int(n) for r, n in value.items()}
            else:
                errors.append(f"{name} rows: {persona}: {value!r} is neither a region mapping nor 'denied'")
        for persona in personas:
            states = {cells.get(persona) for cells in columns.values()}
            all_denied = bool(columns) and states == {"denied"}
            if rows.get(persona) == "denied" and not all_denied:
                errors.append(f"{name}: {persona}: rows are denied but some columns are not")
            if isinstance(rows.get(persona), dict) and all_denied:
                errors.append(f"{name}: {persona}: every column is denied but rows expect counts")
        tables[name] = TableSpec(name, columns, rows)
    if errors:
        raise ValueError("; ".join(errors))
    return Matrix(int(data.get("version") or 0), data.get("mask_literal") or MASK_DEFAULT, regions, personas,
                  tables)


# ---------------------------------------------------------------- query and classification

def _ident(name: str) -> str:
    return "`" + name.replace("`", "``") + "`"


def _lit(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def build_query(table_fqn: str, columns: list[str], region_col: str, literal: str) -> str:
    table = ".".join(_ident(p) for p in table_fqn.split("."))
    region = _ident(region_col)
    parts = [f"{region} AS region", "count(*) AS n"]
    for c in columns:
        col = _ident(c)
        parts += [f"count_if(CAST({col} AS STRING) <=> {_lit(literal)}) AS {_ident('m__' + c)}",
                  f"count({col}) AS {_ident('c__' + c)}",
                  f"count(DISTINCT {col}) AS {_ident('d__' + c)}",
                  # a sum does not cancel in pairs (bit_xor does); DECIMAL(38,0) can't overflow at our row counts
                  f"CAST(sum(CAST(xxhash64({col}) AS DECIMAL(38,0))) AS STRING) AS {_ident('h__' + c)}"]
    return f"SELECT {', '.join(parts)} FROM {table} GROUP BY {region}"


def _on_target(error: str, target: str) -> bool:
    """The permission error is about reading the target itself (SELECT on it, USE SCHEMA / USE CATALOG above it),
    not about something the query touched on the way (a mask or filter function, ref.user_attributes)."""
    parts = target.lower().split(".")
    allowed = {".".join(parts), ".".join(parts[:2]), parts[0]}
    return any(m.group(3).replace("`", "").lower() in allowed for m in _TARGET.finditer(error))


def classify(result_rows: list[dict] | None, error: str | None, columns: list[str], *,
             target: str | None = None) -> Observation:
    """With `target`, a permission error only counts as denied when it is about reading the target."""
    if error is not None:
        if _DENIED.search(error) and (target is None or _on_target(error, target)):
            return Observation(True, None, {c: "denied" for c in columns}, {})
        return Observation(False, error, {c: "error" for c in columns}, {})
    rows: dict[str | None, int] = {}
    total = 0
    masked = dict.fromkeys(columns, 0)
    nonnull: dict[str, int | None] = dict.fromkeys(columns, None)
    sums: dict[str, dict[str | None, tuple[int, int, str | None]]] = {}
    for r in result_rows or []:
        n = int(r["n"])
        total += n
        rows[r["region"]] = rows.get(r["region"], 0) + n
        for c in columns:
            masked[c] += int(r[f"m__{c}"])
            if f"c__{c}" in r:
                cnt = int(r[f"c__{c}"] or 0)
                nonnull[c] = (nonnull[c] or 0) + cnt
                h = r.get(f"h__{c}")
                distinct = int(r.get(f"d__{c}") or 0)
                sums.setdefault(c, {})[r["region"]] = (cnt, distinct, None if h is None else str(h))
    cells = {}
    for c in columns:
        if total == 0:
            cells[c] = "no_rows"
        elif masked[c] == total:
            cells[c] = "masked"
        elif masked[c] == 0:
            cells[c] = "all_null" if nonnull[c] == 0 else "visible"
        else:
            cells[c] = "mixed"
    return Observation(False, None, cells, rows, sums)


# ---------------------------------------------------------------- comparison

def compare(matrix: Matrix, persona: str, table: str, obs: Observation) -> list[Diff]:
    spec = matrix.tables[table]
    expected_rows = spec.rows[persona]
    expect_denied = expected_rows == "denied"

    def d(item, expected, actual) -> Diff:
        return Diff(persona, table, item, str(expected), str(actual))

    if obs.error is not None:
        return [d("query", "denied" if expect_denied else "query succeeds", obs.error)]
    if expect_denied:
        if obs.denied:
            return []
        return [d(c, "denied", obs.cells.get(c, "missing")) for c in spec.columns]
    if obs.denied:
        return [d(c, spec.columns[c][persona], "denied") for c in spec.columns]
    diffs = [d(c, spec.columns[c][persona], obs.cells.get(c, "missing")) for c in spec.columns
             if obs.cells.get(c) != spec.columns[c][persona]]
    for region in matrix.regions:
        want, got = expected_rows.get(region, 0), obs.rows.get(region, 0)
        if want != got:
            diffs.append(d(f"rows:{region}", want, got))
    if obs.rows.get(None, 0):
        diffs.append(d("rows:NULL", 0, obs.rows[None]))
    for region in sorted(k for k in obs.rows if k is not None and k not in matrix.regions):
        if obs.rows[region]:
            diffs.append(d(f"rows:{region}", 0, obs.rows[region]))
    return diffs


def checksum_diffs(matrix: Matrix, table: str, observations: dict[tuple[str, str], Observation]) -> list[Diff]:
    """Personas that see a column `visible` must see the same values: per region, the same non-null count and
    checksum. A mask that returns NULL, a hash or a partial value for one persona then fails."""
    spec = matrix.tables[table]
    diffs: list[Diff] = []
    for col, expected in spec.columns.items():
        seen: dict[str | None, dict[str, tuple[int, int, str | None]]] = {}
        for persona in sorted(matrix.personas):
            obs = observations.get((persona, table))
            if expected.get(persona) != "visible" or obs is None or obs.cells.get(col) != "visible":
                continue
            for region, value in (obs.checksums.get(col) or {}).items():
                seen.setdefault(region, {})[persona] = value
        for region, by_persona in seen.items():
            ref_persona = sorted(by_persona)[0]
            ref = by_persona[ref_persona]
            for persona, value in sorted(by_persona.items()):
                if value != ref:
                    diffs.append(Diff(persona, table, col, f"{region}: {ref} (as {ref_persona})", f"{region}: {value}"))
    return diffs


def preflight_failures(identities: dict[str, dict], matrix: Matrix, ci_principal: str) -> list[str]:
    """Every persona must be itself: a distinct, non-admin, non-CI user in exactly the group the matrix names."""
    out: list[str] = []
    if not (ci_principal or "").strip():
        out.append("CI principal is not resolved (ci_sp empty); cannot prove no persona is the CI principal")
    seen: dict[str, str] = {}
    persona_groups = {p.group for p in matrix.personas.values() if p.group}
    for key, p in matrix.personas.items():
        ident = identities.get(key)
        if ident is None:
            out.append(f"{key}: no identity result")
            continue
        user = (ident.get("user") or "").strip()
        if not user:
            out.append(f"{key}: current_user() is empty")
            continue
        if ci_principal and user.lower() == ci_principal.strip().lower():
            out.append(f"{key}: runs as the CI principal {user}")
        if ident.get("is_admin"):
            out.append(f"{key}: {user} is a workspace admin")
        if user.lower() in seen:
            out.append(f"{key}: same user {user} as persona {seen[user.lower()]}")
        seen[user.lower()] = key
        groups = ident.get("groups") or {}
        if p.group and not groups.get(p.group):
            out.append(f"{key}: {user} is not a member of its group {p.group}")
        if p.group:
            others = sorted(g for g in persona_groups if g != p.group and groups.get(g))
            if others:
                out.append(f"{key}: {user} is also a member of other persona group(s) {others}")
        if p.group is None:
            inside = sorted(g for g in persona_groups if groups.get(g))
            if inside:
                out.append(f"{key}: groupless persona {user} is a member of {inside}")
    return out


def build_ownership_query(catalog: str, tables: list[str]) -> str:
    """Run as a persona: ownership, ALL PRIVILEGES or MANAGE it (or a group it is in) holds on the catalog, a matrix
    schema or a matrix table. An owner reads without any agreement grant, so the matrix would prove nothing."""
    cat = _ident(catalog)
    mine = "({x} = current_user() OR is_account_group_member({x}))"
    schemas = sorted({t.split(".")[0] for t in tables})
    in_schemas = ", ".join(_lit(x) for x in schemas)
    in_tables = ", ".join(_lit(t) for t in tables)
    priv = "privilege_type IN ('ALL PRIVILEGES', 'MANAGE')"
    return " UNION ALL ".join([
        f"SELECT 'catalog' AS lvl, catalog_name AS obj, 'owner' AS what, catalog_owner AS who "
        f"FROM {cat}.information_schema.catalogs WHERE catalog_name = {_lit(catalog)} "
        f"AND {mine.format(x='catalog_owner')}",
        f"SELECT 'schema', schema_name, 'owner', schema_owner FROM {cat}.information_schema.schemata "
        f"WHERE schema_name IN ({in_schemas}) AND {mine.format(x='schema_owner')}",
        f"SELECT 'table', concat(table_schema, '.', table_name), 'owner', table_owner "
        f"FROM {cat}.information_schema.tables WHERE concat(table_schema, '.', table_name) IN ({in_tables}) "
        f"AND {mine.format(x='table_owner')}",
        f"SELECT 'catalog', catalog_name, privilege_type, grantee FROM {cat}.information_schema.catalog_privileges "
        f"WHERE catalog_name = {_lit(catalog)} AND {priv} AND {mine.format(x='grantee')}",
        f"SELECT 'schema', schema_name, privilege_type, grantee FROM {cat}.information_schema.schema_privileges "
        f"WHERE schema_name IN ({in_schemas}) AND {priv} AND {mine.format(x='grantee')}",
        f"SELECT 'table', concat(table_schema, '.', table_name), privilege_type, grantee "
        f"FROM {cat}.information_schema.table_privileges "
        f"WHERE concat(table_schema, '.', table_name) IN ({in_tables}) AND {priv} AND {mine.format(x='grantee')}",
    ])


def ownership_failures(persona: str, rows: list[dict]) -> list[str]:
    return [f"{persona}: {r['who']} holds {r['what']} on {r['lvl']} {r['obj']} (bypasses agreement grants)"
            for r in rows]


# ---------------------------------------------------------------- grid

def _cell(actual: str, expected: str) -> str:
    return f"{actual} ✓" if actual == expected else f"{actual} ✗ (expected {expected})"


def render_grid(matrix: Matrix, observations: dict[tuple[str, str], Observation], diffs: list[Diff],
                preflight: list[str], meta: dict | None = None) -> str:
    keys = list(matrix.personas)
    missing = [(k, t) for t in matrix.tables for k in keys if (k, t) not in observations]
    # a run that did not grade every persona x table never reads PASS (interrupted, timed out, preflight stopped it)
    ok = not diffs and not preflight and not missing
    lines = [f"# Persona access matrix: {'PASS' if ok else 'FAIL'}", "",
             f"{len(keys)} personas, {len(matrix.tables)} tables, {len(diffs)} differences, "
             f"{len(preflight)} preflight failures.", ""]
    meta = meta or {}
    for k in ("run_at", "commit", "catalog", "warehouse"):
        if meta.get(k):
            lines.append(f"- {k}: {meta[k]}")
    if meta.get("users"):
        lines.append("- ran as: " + ", ".join(f"{k} = {u}" for k, u in sorted(meta["users"].items())))
    if meta:
        lines.append("")
    if preflight:
        lines += ["## Preflight failures (no data was queried)", ""] + [f"- {f}" for f in preflight] + [""]
    if missing:
        lines += [f"Incomplete run: {len(missing)} persona x table pairs not run.", ""]
    for tname, spec in matrix.tables.items():
        lines += [f"## {tname}", "", "| column | " + " | ".join(keys) + " |",
                  "| --- |" + " --- |" * len(keys)]
        for col, expected in spec.columns.items():
            cells = []
            for k in keys:
                obs = observations.get((k, tname))
                actual = obs.cells.get(col, "not run") if obs else "not run"
                cells.append(_cell(actual, expected[k]))
            lines.append(f"| {col} | " + " | ".join(cells) + " |")
        lines += ["", f"Row counts by region ({tname}):", "",
                  "| persona | " + " | ".join(matrix.regions) + " | NULL |",
                  "| --- |" + " --- |" * (len(matrix.regions) + 1)]
        for k in keys:
            want = spec.rows[k]
            obs = observations.get((k, tname))
            if want == "denied":
                actual = "denied" if obs and obs.denied else ("not run" if obs is None else "not denied")
                lines.append(f"| {k} | " + " | ".join([_cell(actual, "denied")] * len(matrix.regions)) + " | |")
                continue
            got = obs.rows if obs else {}
            cells = [_cell(str(got.get(r, 0)) if obs else "not run", str(want.get(r, 0))) for r in matrix.regions]
            lines.append(f"| {k} | " + " | ".join(cells) + f" | {got.get(None, 0)} |")
        lines.append("")
    if diffs:
        lines += ["## Differences", ""] + [f"- {x.persona} | {x.table}.{x.item} | expected {x.expected} -> "
                                           f"{x.actual}" for x in diffs] + [""]
    lines += ["Note: the matrix's `joinup` block (before/after a hydration revise) is not executed by this harness; "
              "the columns above are graded in the current state."]
    return "\n".join(lines) + "\n"
