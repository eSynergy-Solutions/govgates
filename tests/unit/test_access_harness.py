"""Unit tests for the persona access harness (tests/access/harness.py).

Spec: specs/consumption.md "Access test harness" plus the agreed harness interface.
Fixtures are small synthetic matrices; the real tests/access/matrix.yaml is only
loaded to check that it parses. No workspace, no Spark, no network.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ACCESS_DIR = Path(__file__).resolve().parents[1] / "access"
REAL_MATRIX = ACCESS_DIR / "matrix.yaml"


@pytest.fixture
def harness():
    if str(ACCESS_DIR) not in sys.path:
        sys.path.insert(0, str(ACCESS_DIR))
    import harness as h  # noqa: PLC0415

    return h


# Tiny synthetic matrix: 2 personas, 1 table, 2 columns, regions [UK, US].
GOOD = """
version: 1
mask_literal: "***MASKED***"
regions: [UK, US]
personas:
  reader:  {group: gs_reader, profile: persona-reader, regions: [UK], pii: masked}
  outsider: {group: null,     profile: persona-outsider, regions: NONE, pii: none}
given:
  reader: {intended_use: reporting}
tables:
  raw.t1:
    columns:
      id:    {reader: visible, outsider: denied}
      notes: {reader: masked,  outsider: denied}
    rows:
      reader:   {UK: 3, US: 0}
      outsider: denied
joinup:
  column: raw.t1.notes
"""


def _replace(text: str, old: str, new: str) -> str:
    assert old in text, f"fixture edit target missing: {old!r}"
    return text.replace(old, new, 1)


@pytest.fixture
def matrix(harness):
    return harness.load_matrix(GOOD)


def _obs(harness, *, denied=False, error=None, cells=None, rows=None):
    return harness.Observation(denied=denied, error=error, cells=cells or {}, rows=rows or {})


# --------------------------------------------------------------------------- constants


def test_constants(harness):
    assert harness.MASK_DEFAULT == "***MASKED***"
    assert tuple(harness.EXPECTED) == ("visible", "masked", "denied")


# --------------------------------------------------------------------------- load_matrix


def test_load_good_matrix(harness, matrix):
    assert matrix.version == 1
    assert matrix.mask_literal == "***MASKED***"
    assert tuple(matrix.regions) == ("UK", "US")
    assert set(matrix.personas) == {"reader", "outsider"}
    assert matrix.personas["reader"].group == "gs_reader"
    assert matrix.personas["outsider"].group is None
    assert matrix.personas["reader"].profile == "persona-reader"
    assert matrix.personas["reader"].pii == "masked"
    t = matrix.tables["raw.t1"]
    assert isinstance(t, harness.TableSpec)
    assert t.name == "raw.t1"
    assert t.columns["notes"]["reader"] == "masked"
    assert t.columns["id"]["outsider"] == "denied"
    assert t.rows["reader"] == {"UK": 3, "US": 0}
    assert t.rows["outsider"] == "denied"


def test_load_is_deterministic(harness):
    assert harness.load_matrix(GOOD) == harness.load_matrix(GOOD)


def test_extra_top_level_keys_ignored(harness):
    text = GOOD + "\nsomething_else: {a: 1}\n"
    m = harness.load_matrix(text)
    assert "raw.t1" in m.tables


def test_real_matrix_parses(harness):
    m = harness.load_matrix(REAL_MATRIX.read_text(encoding="utf-8"))
    assert m.personas
    assert m.tables


def test_reject_bad_expected_value(harness):
    bad = _replace(GOOD, "id:    {reader: visible", "id:    {reader: hidden")
    with pytest.raises(ValueError, match="hidden"):
        harness.load_matrix(bad)


def test_reject_undeclared_persona_in_cell(harness):
    bad = _replace(GOOD, "outsider: denied}\n      notes", "outsider: denied, ghost: visible}\n      notes")
    with pytest.raises(ValueError, match="ghost"):
        harness.load_matrix(bad)


def test_reject_column_missing_persona(harness):
    bad = _replace(GOOD, "notes: {reader: masked,  outsider: denied}", "notes: {reader: masked}")
    with pytest.raises(ValueError, match="outsider"):
        harness.load_matrix(bad)


def test_reject_rows_missing_persona(harness):
    bad = _replace(GOOD, "      outsider: denied\njoinup", "joinup")
    with pytest.raises(ValueError, match="outsider"):
        harness.load_matrix(bad)


def test_reject_rows_region_not_declared(harness):
    bad = _replace(GOOD, "{UK: 3, US: 0}", "{UK: 3, US: 0, FR: 1}")
    with pytest.raises(ValueError, match="FR"):
        harness.load_matrix(bad)


def test_reject_rows_value_not_mapping_or_denied(harness):
    bad = _replace(GOOD, "reader:   {UK: 3, US: 0}", "reader:   everything")
    with pytest.raises(ValueError, match="reader"):
        harness.load_matrix(bad)


def test_reject_rows_denied_but_column_not_denied(harness):
    # reader rows say denied, but its columns are visible/masked.
    bad = _replace(GOOD, "reader:   {UK: 3, US: 0}", "reader:   denied")
    with pytest.raises(ValueError, match="reader"):
        harness.load_matrix(bad)


def test_reject_all_columns_denied_but_rows_mapping(harness):
    bad = _replace(GOOD, "      outsider: denied\njoinup", "      outsider: {UK: 0, US: 0}\njoinup")
    with pytest.raises(ValueError, match="outsider"):
        harness.load_matrix(bad)


@pytest.mark.parametrize("name", ["t1", "cat.raw.t1"])
def test_reject_table_name_not_schema_dot_table(harness, name):
    bad = _replace(GOOD, "  raw.t1:", f"  {name}:")
    with pytest.raises(ValueError, match="t1"):
        harness.load_matrix(bad)


def test_error_lists_every_problem(harness):
    bad = _replace(GOOD, "id:    {reader: visible", "id:    {reader: hidden")
    bad = _replace(bad, "{UK: 3, US: 0}", "{UK: 3, US: 0, FR: 1}")
    with pytest.raises(ValueError) as exc:
        harness.load_matrix(bad)
    msg = str(exc.value)
    assert "hidden" in msg
    assert "FR" in msg


# --------------------------------------------------------------------------- build_query


def test_build_query_shape(harness):
    q = harness.build_query("cat.raw.t1", ["id", "notes"], "rgn_cd", "***MASKED***")
    assert "m__id" in q
    assert "m__notes" in q
    assert "GROUP BY" in q.upper()
    assert "count_if" in q.lower()
    assert "'***MASKED***'" in q
    assert q.count(";") <= 1  # one statement


def test_build_query_quotes_fqn_per_part(harness):
    q = harness.build_query("cat.raw.t1", ["id"], "rgn_cd", "x")
    assert "`cat`.`raw`.`t1`" in q
    assert "`rgn_cd`" in q


def test_build_query_escapes_backtick_in_column(harness):
    q = harness.build_query("cat.raw.t1", ["we`ird"], "rgn_cd", "x")
    assert "`we``ird`" in q


def test_build_query_escapes_quote_in_literal(harness):
    q = harness.build_query("cat.raw.t1", ["id"], "rgn_cd", "it's")
    assert "'it''s'" in q
    assert "'it's'" not in q


def test_build_query_is_deterministic(harness):
    a = harness.build_query("cat.raw.t1", ["id", "notes"], "rgn_cd", "M")
    b = harness.build_query("cat.raw.t1", ["id", "notes"], "rgn_cd", "M")
    assert a == b


# --------------------------------------------------------------------------- classify


@pytest.mark.parametrize(
    "err",
    [
        "[INSUFFICIENT_PERMISSIONS] User does not have SELECT on table. SQLSTATE: 42501",
        "SQLSTATE: 42501",
        "PERMISSION_DENIED: no access",
    ],
)
def test_classify_permission_error_is_denied(harness, err):
    o = harness.classify(None, err, ["id", "notes"])
    assert o.denied is True
    assert o.cells == {"id": "denied", "notes": "denied"}
    assert o.rows == {}


@pytest.mark.parametrize(
    "err",
    ["[UNRESOLVED_COLUMN] cannot resolve `x`", "[TABLE_OR_VIEW_NOT_FOUND] raw.t9"],
)
def test_classify_other_error(harness, err):
    o = harness.classify(None, err, ["id", "notes"])
    assert o.denied is False
    assert o.error == err or (o.error and err in o.error)
    assert o.cells == {"id": "error", "notes": "error"}


def test_classify_visible_masked_mixed(harness):
    rows = [
        {"region": "UK", "n": 3, "m__id": 0, "m__notes": 3, "m__x": 1},
        {"region": "US", "n": 2, "m__id": 0, "m__notes": 2, "m__x": 0},
    ]
    o = harness.classify(rows, None, ["id", "notes", "x"])
    assert o.denied is False
    assert o.error is None
    assert o.cells == {"id": "visible", "notes": "masked", "x": "mixed"}
    assert o.rows == {"UK": 3, "US": 2}


def test_classify_no_rows(harness):
    o = harness.classify([], None, ["id"])
    assert o.cells == {"id": "no_rows"}
    assert o.rows == {}


def test_classify_sums_per_region_and_keeps_null(harness):
    rows = [
        {"region": "UK", "n": 1, "m__id": 0},
        {"region": "UK", "n": 2, "m__id": 0},
        {"region": None, "n": 4, "m__id": 0},
    ]
    o = harness.classify(rows, None, ["id"])
    assert o.rows == {"UK": 3, None: 4}
    assert o.cells == {"id": "visible"}


# --------------------------------------------------------------------------- compare


def _good_reader_obs(harness):
    return _obs(harness, cells={"id": "visible", "notes": "masked"}, rows={"UK": 3, "US": 0})


def test_compare_all_match(harness, matrix):
    assert harness.compare(matrix, "reader", "raw.t1", _good_reader_obs(harness)) == []


def test_compare_missing_region_counts_as_zero(harness, matrix):
    obs = _obs(harness, cells={"id": "visible", "notes": "masked"}, rows={"UK": 3})
    assert harness.compare(matrix, "reader", "raw.t1", obs) == []


def test_compare_expected_denied_and_denied(harness, matrix):
    obs = _obs(harness, denied=True, cells={"id": "denied", "notes": "denied"})
    assert harness.compare(matrix, "outsider", "raw.t1", obs) == []


def test_compare_expected_denied_but_succeeded(harness, matrix):
    obs = _obs(harness, cells={"id": "visible", "notes": "masked"}, rows={"UK": 3})
    diffs = harness.compare(matrix, "outsider", "raw.t1", obs)
    by_item = {d.item: d for d in diffs}
    assert {"id", "notes"} <= set(by_item)
    assert by_item["id"].expected == "denied" and by_item["id"].actual == "visible"
    assert by_item["notes"].expected == "denied" and by_item["notes"].actual == "masked"
    assert all(d.persona == "outsider" and d.table == "raw.t1" for d in diffs)


def test_compare_expected_access_but_denied(harness, matrix):
    obs = _obs(harness, denied=True, cells={"id": "denied", "notes": "denied"})
    diffs = harness.compare(matrix, "reader", "raw.t1", obs)
    assert diffs
    assert all(d.actual == "denied" for d in diffs)
    items = {d.item for d in diffs}
    assert {"id", "notes"} <= items


def test_compare_query_error(harness, matrix):
    obs = _obs(harness, error="[UNRESOLVED_COLUMN] notes", cells={"id": "error", "notes": "error"})
    diffs = harness.compare(matrix, "reader", "raw.t1", obs)
    q = [d for d in diffs if d.item == "query"]
    assert len(q) == 1
    assert "UNRESOLVED_COLUMN" in q[0].actual
    # no false passes: nothing in the result claims the expected state was met
    assert all(d.expected != d.actual for d in diffs)


def test_compare_query_error_on_expected_denied_is_not_a_pass(harness, matrix):
    obs = _obs(harness, error="[TABLE_OR_VIEW_NOT_FOUND] raw.t1", cells={"id": "error", "notes": "error"})
    diffs = harness.compare(matrix, "outsider", "raw.t1", obs)
    assert any(d.item == "query" for d in diffs)


def test_compare_cell_mismatch(harness, matrix):
    obs = _obs(harness, cells={"id": "visible", "notes": "visible"}, rows={"UK": 3, "US": 0})
    diffs = harness.compare(matrix, "reader", "raw.t1", obs)
    assert len(diffs) == 1
    d = diffs[0]
    assert (d.item, d.expected, d.actual) == ("notes", "masked", "visible")


@pytest.mark.parametrize("state", ["mixed", "no_rows"])
def test_compare_mixed_or_no_rows_never_pass(harness, matrix, state):
    obs = _obs(harness, cells={"id": "visible", "notes": state}, rows={"UK": 3, "US": 0})
    diffs = harness.compare(matrix, "reader", "raw.t1", obs)
    assert [(d.item, d.actual) for d in diffs] == [("notes", state)]


def test_compare_row_count_mismatch(harness, matrix):
    obs = _obs(harness, cells={"id": "visible", "notes": "masked"}, rows={"UK": 3, "US": 5})
    diffs = harness.compare(matrix, "reader", "raw.t1", obs)
    assert len(diffs) == 1
    d = diffs[0]
    assert d.item == "rows:US"
    assert str(d.expected) == "0"
    assert str(d.actual) == "5"


def test_compare_null_region_rows(harness, matrix):
    obs = _obs(harness, cells={"id": "visible", "notes": "masked"}, rows={"UK": 3, "US": 0, None: 2})
    diffs = harness.compare(matrix, "reader", "raw.t1", obs)
    assert len(diffs) == 1
    assert diffs[0].item == "rows:NULL"
    assert str(diffs[0].expected) == "0"
    assert str(diffs[0].actual) == "2"


def test_compare_null_region_zero_is_fine(harness, matrix):
    obs = _obs(harness, cells={"id": "visible", "notes": "masked"}, rows={"UK": 3, None: 0})
    assert harness.compare(matrix, "reader", "raw.t1", obs) == []


# --------------------------------------------------------------------------- preflight


def _identities():
    return {
        "reader": {"user": "sp-reader", "is_admin": False, "groups": {"gs_reader": True}},
        "outsider": {"user": "sp-outsider", "is_admin": False, "groups": {"gs_reader": False}},
    }


def test_preflight_ok(harness, matrix):
    assert harness.preflight_failures(_identities(), matrix, "sp-ci") == []


def test_preflight_missing_persona(harness, matrix):
    ids = _identities()
    del ids["outsider"]
    fails = harness.preflight_failures(ids, matrix, "sp-ci")
    assert fails and any("outsider" in f for f in fails)


def test_preflight_empty_user(harness, matrix):
    ids = _identities()
    ids["reader"]["user"] = ""
    fails = harness.preflight_failures(ids, matrix, "sp-ci")
    assert fails and any("reader" in f for f in fails)


def test_preflight_user_is_ci_principal_case_insensitive(harness, matrix):
    ids = _identities()
    ids["reader"]["user"] = "SP-CI"
    fails = harness.preflight_failures(ids, matrix, "sp-ci")
    assert fails and any("reader" in f for f in fails)


def test_preflight_admin(harness, matrix):
    ids = _identities()
    ids["reader"]["is_admin"] = True
    fails = harness.preflight_failures(ids, matrix, "sp-ci")
    assert fails and any("reader" in f for f in fails)


def test_preflight_duplicate_user(harness, matrix):
    ids = _identities()
    ids["outsider"]["user"] = "sp-reader"
    fails = harness.preflight_failures(ids, matrix, "sp-ci")
    assert fails and any("sp-reader" in f for f in fails)


def test_preflight_not_member_of_own_group(harness, matrix):
    ids = _identities()
    ids["reader"]["groups"]["gs_reader"] = False
    fails = harness.preflight_failures(ids, matrix, "sp-ci")
    assert fails and any("gs_reader" in f for f in fails)


def test_preflight_groupless_persona_in_persona_group(harness, matrix):
    ids = _identities()
    ids["outsider"]["groups"]["gs_reader"] = True
    fails = harness.preflight_failures(ids, matrix, "sp-ci")
    assert fails and any("outsider" in f for f in fails)


# --------------------------------------------------------------------------- render_grid


def _all_pass_obs(harness):
    return {
        ("reader", "raw.t1"): _good_reader_obs(harness),
        ("outsider", "raw.t1"): _obs(harness, denied=True, cells={"id": "denied", "notes": "denied"}),
    }


def test_render_pass(harness, matrix):
    out = harness.render_grid(matrix, _all_pass_obs(harness), [], [])
    first = out.splitlines()[0]
    assert "PASS" in first and "FAIL" not in first
    assert "reader" in out and "outsider" in out
    assert "raw.t1" in out
    assert "id" in out and "notes" in out


def test_render_fail_on_diff(harness, matrix):
    obs = _all_pass_obs(harness)
    obs[("reader", "raw.t1")] = _obs(
        harness, cells={"id": "visible", "notes": "visible"}, rows={"UK": 3, "US": 0}
    )
    diffs = harness.compare(matrix, "reader", "raw.t1", obs[("reader", "raw.t1")])
    out = harness.render_grid(matrix, obs, diffs, [])
    assert "FAIL" in out.splitlines()[0]
    # the differing cell shows actual and expected together
    lines_with_both = [ln for ln in out.splitlines() if "visible" in ln and "expected masked" in ln]
    assert lines_with_both


def test_render_fail_on_preflight(harness, matrix):
    msg = "reader: user is an admin (synthetic)"
    out = harness.render_grid(matrix, _all_pass_obs(harness), [], [msg])
    assert "FAIL" in out.splitlines()[0]
    assert msg in out


def test_render_row_counts_section(harness, matrix):
    out = harness.render_grid(matrix, _all_pass_obs(harness), [], [])
    assert "UK" in out and "US" in out
    assert "3" in out


def test_render_joinup_note(harness, matrix):
    out = harness.render_grid(matrix, _all_pass_obs(harness), [], [])
    assert "joinup" in out.lower()


def test_render_is_deterministic(harness, matrix):
    a = harness.render_grid(matrix, _all_pass_obs(harness), [], [])
    b = harness.render_grid(matrix, _all_pass_obs(harness), [], [])
    assert a == b


# --------------------------------------------------------------------------- review round 1 (false passes)


def test_build_query_has_count_and_checksum(harness):
    q = harness.build_query("cat.raw.t1", ["id"], "rgn_cd", "M")
    assert "AS `c__id`" in q and "AS `h__id`" in q and "AS `d__id`" in q
    assert "xxhash64" in q
    # review round 2: an aggregate that does not cancel in pairs (bit_xor does), plus a distinct count
    assert "bit_xor" not in q
    assert "sum(" in q.lower() and "decimal(38" in q.lower()
    assert "count(distinct" in q.lower()


def test_classify_all_null_is_not_visible(harness):
    rows = [{"region": "UK", "n": 3, "m__id": 0, "c__id": 0, "h__id": None}]
    o = harness.classify(rows, None, ["id"])
    assert o.cells == {"id": "all_null"}


def test_classify_keeps_checksums_per_region(harness):
    rows = [{"region": "UK", "n": 3, "m__id": 0, "c__id": 3, "d__id": 2, "h__id": "77"},
            {"region": "US", "n": 1, "m__id": 0, "c__id": 1, "d__id": 1, "h__id": "5"}]
    o = harness.classify(rows, None, ["id"])
    assert o.cells == {"id": "visible"}
    assert o.checksums["id"] == {"UK": (3, 2, "77"), "US": (1, 1, "5")}


def test_checksum_mismatch_between_visible_personas_is_a_diff(harness):
    m = harness.load_matrix(GOOD.replace("outsider: denied}\n      notes", "outsider: visible}\n      notes")
                            .replace("      outsider: denied\njoinup", "      outsider: {UK: 3, US: 0}\njoinup")
                            .replace("notes: {reader: masked,  outsider: denied}",
                                     "notes: {reader: masked,  outsider: masked}"))
    a = _obs(harness, cells={"id": "visible", "notes": "masked"}, rows={"UK": 3})
    b = _obs(harness, cells={"id": "visible", "notes": "masked"}, rows={"UK": 3})
    a = harness.Observation(a.denied, a.error, a.cells, a.rows, {"id": {"UK": (3, "1")}})
    b = harness.Observation(b.denied, b.error, b.cells, b.rows, {"id": {"UK": (3, "2")}})
    diffs = harness.checksum_diffs(m, "raw.t1", {("reader", "raw.t1"): a, ("outsider", "raw.t1"): b})
    assert diffs and any(d.item == "id" for d in diffs)
    b2 = harness.Observation(b.denied, b.error, b.cells, b.rows, {"id": {"UK": (3, "1")}})
    assert harness.checksum_diffs(m, "raw.t1", {("reader", "raw.t1"): a, ("outsider", "raw.t1"): b2}) == []


def test_compare_unexpected_region_key_is_a_diff(harness, matrix):
    obs = _obs(harness, cells={"id": "visible", "notes": "masked"}, rows={"UK": 3, "US": 0, "GB": 4})
    diffs = harness.compare(matrix, "reader", "raw.t1", obs)
    assert [(d.item, d.actual) for d in diffs] == [("rows:GB", "4")]


def test_preflight_empty_ci_principal_fails(harness, matrix):
    fails = harness.preflight_failures(_identities(), matrix, "")
    assert any("CI principal" in f for f in fails)


def test_preflight_member_of_another_persona_group_fails(harness):
    two = GOOD.replace("outsider: {group: null,     profile: persona-outsider",
                       "outsider: {group: gs_out,   profile: persona-outsider")
    m = harness.load_matrix(two)
    ids = {"reader": {"user": "r", "is_admin": False, "groups": {"gs_reader": True, "gs_out": True}},
           "outsider": {"user": "o", "is_admin": False, "groups": {"gs_reader": False, "gs_out": True}}}
    fails = harness.preflight_failures(ids, m, "ci")
    assert any("reader" in f and "gs_out" in f for f in fails)


def test_ownership_query_and_failures(harness):
    q = harness.build_ownership_query("cat", ["raw.t1"])
    assert "information_schema" in q and "table_owner" in q and "ALL PRIVILEGES" in q and "MANAGE" in q
    rows = [{"lvl": "table", "obj": "raw.t1", "what": "owner", "who": "gs_reader"},
            {"lvl": "schema", "obj": "raw", "what": "MANAGE", "who": "sp-reader"}]
    fails = harness.ownership_failures("reader", rows)
    assert len(fails) == 2 and all("reader" in f for f in fails)
    assert harness.ownership_failures("reader", []) == []


@pytest.mark.parametrize("err,denied", [
    ("[INSUFFICIENT_PERMISSIONS] Principal 'x' does not have USE CATALOG on Catalog 'cat'. SQLSTATE: 42501", True),
    ("[INSUFFICIENT_PERMISSIONS] Principal 'x' does not have SELECT on Table 'cat.raw.t1'. SQLSTATE: 42501", True),
    ("[INSUFFICIENT_PERMISSIONS] User does not have USE SCHEMA on Schema 'cat.raw'. SQLSTATE: 42501", True),
    ("[INSUFFICIENT_PERMISSIONS] User does not have EXECUTE on Routine 'cat.gov.mask_pii'. SQLSTATE: 42501", False),
    ("[INSUFFICIENT_PERMISSIONS] does not have SELECT on Table 'cat.ref.user_attributes'. SQLSTATE: 42501", False),
])
def test_classify_with_target_only_denies_on_the_target(harness, err, denied):
    o = harness.classify(None, err, ["id"], target="cat.raw.t1")
    assert o.denied is denied
    if not denied:
        assert o.error and o.cells == {"id": "error"}


def test_render_includes_meta(harness, matrix):
    out = harness.render_grid(matrix, _all_pass_obs(harness), [], [],
                              meta={"run_at": "2026-11-09T22:30Z", "users": {"reader": "sp-reader"}})
    assert "2026-11-09T22:30Z" in out and "sp-reader" in out


def test_distinct_count_separates_a_constant_from_a_two_value_column(harness):
    const = harness.classify([{"region": "UK", "n": 4, "m__x": 0, "c__x": 4, "d__x": 1, "h__x": "0"}], None, ["x"])
    two = harness.classify([{"region": "UK", "n": 4, "m__x": 0, "c__x": 4, "d__x": 2, "h__x": "0"}], None, ["x"])
    assert const.checksums["x"]["UK"] != two.checksums["x"]["UK"]


def test_render_incomplete_run_is_fail(harness, matrix):
    obs = _all_pass_obs(harness)
    del obs[("outsider", "raw.t1")]
    out = harness.render_grid(matrix, obs, [], [])
    assert "FAIL" in out.splitlines()[0]
    assert "not run" in out
