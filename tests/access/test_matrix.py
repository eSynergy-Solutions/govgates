"""Persona access matrix (specs/consumption.md, "Access test harness"; C-AC3).

Runs every persona in tests/access/matrix.yaml as its own service principal (CLI profile persona-<key>, OAuth M2M),
never as an admin, an owner or the CI principal: an identity preflight must pass before any data query. Compares
visible / masked / denied per column (visible = clear values, the same checksum for every persona that sees the
column) and row counts per region, and writes the pass/fail grid to build/access/persona_matrix.md (the UAT evidence
format). The grid is deleted first and always rewritten, so a crashed run never leaves an old PASS behind.

Needs the dev workspace, so it is outside `testpaths`: run it with `pytest tests/access`.
Known limit: metastore and account admin can't be seen from SQL as the persona.
"""

from __future__ import annotations

import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import harness  # noqa: E402
import runner  # noqa: E402

REGION_COLUMN = "rgn_cd"
OUT = HERE.parents[1] / "build" / "access" / "persona_matrix.md"


def _bool(v) -> bool:
    return str(v).strip().lower() == "true"


def _commit() -> str:
    out = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, cwd=HERE)
    return out.stdout.strip()


def _query(profile, sql, *, warehouse, catalog):
    try:
        return runner.run_as(profile, sql, warehouse=warehouse, catalog=catalog)
    except Exception as e:  # a CLI failure is an error result, never a pass
        return None, f"{type(e).__name__}: {e}"


def _preflight(matrix, facts):
    groups = sorted({p.group for p in matrix.personas.values() if p.group})
    identities, warehouses, failures = {}, {}, []
    tables = sorted(matrix.tables)
    for key, p in matrix.personas.items():
        try:
            warehouses[key] = runner.warehouse_id(p.profile)
        except Exception as e:
            failures.append(f"{key}: no warehouse for {p.profile}: {e}")
            continue
        checks = ", ".join(f"is_account_group_member('{g}') AS `g__{g}`" for g in groups)
        sql = f"SELECT current_user() AS user, is_member('admins') AS is_admin, {checks}"
        rows, err = _query(p.profile, sql, warehouse=warehouses[key], catalog=facts["catalog"])
        if err:
            failures.append(f"{key}: identity query failed: {err}")
            continue
        r = rows[0]
        identities[key] = {"user": r["user"] or "", "is_admin": _bool(r["is_admin"]),
                           "groups": {g: _bool(r[f"g__{g}"]) for g in groups}}
        own_rows, err = _query(p.profile, harness.build_ownership_query(facts["catalog"], tables),
                               warehouse=warehouses[key], catalog=facts["catalog"])
        if err and not harness.classify(None, err, [], target=facts["catalog"]).denied:
            failures.append(f"{key}: ownership query failed: {err}")
        failures += harness.ownership_failures(key, own_rows or [])
    failures = harness.preflight_failures(identities, matrix, facts.get("ci_sp", "")) + failures
    return identities, warehouses, failures


@pytest.fixture(scope="module")
def run():
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.unlink(missing_ok=True)
    matrix = harness.load_matrix((HERE / "matrix.yaml").read_text(encoding="utf-8"))
    observations, diffs, preflight, meta = {}, [], [], {"run_at": datetime.now(UTC).isoformat(timespec="seconds"),
                                                        "commit": _commit()}
    try:
        facts = runner.bundle_facts()
        meta["catalog"] = facts["catalog"]
        identities, warehouses, preflight = _preflight(matrix, facts)
        meta["users"] = {k: v["user"] for k, v in identities.items()}
        meta["warehouse"] = ", ".join(sorted(set(warehouses.values())))
        if not preflight:  # never query data as an identity that failed the preflight
            for tname, spec in matrix.tables.items():
                columns = list(spec.columns)
                fqn = f"{facts['catalog']}.{tname}"
                sql = harness.build_query(fqn, columns, REGION_COLUMN, matrix.mask_literal)
                for key, p in matrix.personas.items():
                    rows, err = _query(p.profile, sql, warehouse=warehouses[key], catalog=facts["catalog"])
                    obs = harness.classify(rows, err, columns, target=fqn)
                    observations[(key, tname)] = obs
                    diffs += harness.compare(matrix, key, tname, obs)
                diffs += harness.checksum_diffs(matrix, tname, observations)
    except Exception as e:
        preflight.append(f"harness error before grading: {type(e).__name__}: {e}")
    finally:
        OUT.write_text(harness.render_grid(matrix, observations, diffs, preflight, meta=meta), encoding="utf-8")
    return matrix, preflight, diffs


def test_preflight_every_persona_is_itself_never_admin_owner_or_ci(run):
    _, preflight, _ = run
    assert preflight == [], "\n".join(preflight)


def test_matrix_matches(run):
    _, preflight, diffs = run
    assert not preflight, "preflight failed; no data was graded:\n" + "\n".join(preflight)
    assert diffs == [], "\n".join(f"{d.persona} | {d.table}.{d.item} | {d.expected} -> {d.actual}" for d in diffs)
