"""Row-filter generator planner (specs/consumption.md, `row_filter` bullet). Pure: no Spark, no workspace.

gov.access_policies `row_filter` rows + gov.approved_metadata are the truth; current UC tags, policies and
functions are read only to find the difference. The generator owns `<tag_prefix>_rls_attribute` only.

Fixture conventions (not fixed by the spec, chosen here):
- `decided` is keyed by (table_name.lower(), column_name.lower()) because one plan spans a whole schema.
- `current_functions` values are the function body as information_schema.routines shows it: the text after
  `RETURN` in the CREATE statement. `body_of()` extracts it from the planner's own function_set statement.
- `current_policies` is keyed by (securable.lower(), policy_name); `last_events` keys are taken from the planner's
  own `Action.object` (lowercased), so these tests do not fix the exact string form of a policy's object.
"""

from __future__ import annotations

import dataclasses
import re

import pytest

from gates.consumption.row_filter import (
    Action,
    PolicyRow,
    filter_sql,
    helper_sql,
    plan,
    policy_name,
    region_visible,
    validate_rows,
)
from gates.hydration.apply import Approved, quote_ident, quote_str, quote_table

CAT = "cat"
SCHEMA = "cat.curated"
OTHER_SCHEMA = "cat.claims"
FN = "cat.gov.rls_region_filter"
RLS = "gg_rls_attribute"
TERM_ID = "region_code"
HP = "ci-sp-0000"  # synthetic CI service principal applicationId
GOV = "gov"
PREFIX = "gg"
T1 = "2026-01-01T09:00:00"
T2 = "2026-01-02T09:00:00"
T3 = "2026-01-03T09:00:00"
UA_TABLE = "cat.ref.user_attributes"  # passed in from cfg.ref(...); the planner must not build it
KIND_ORDER = ["function_set", "policy_set", "tag_set", "tag_refused", "tag_unset", "policy_drop"]


def prow(policy_id: str = "rf_curated", securable: str = SCHEMA, *, active: bool = True,
         except_principals: tuple[str, ...] = (HP,), function_name: str = FN, match_tag_key: str = RLS,
         match_tag_value: str = "region", policy_type: str = "row_filter") -> PolicyRow:
    return PolicyRow(policy_id=policy_id, policy_type=policy_type, securable=securable,
                     match_tag_key=match_tag_key, match_tag_value=match_tag_value, function_name=function_name,
                     except_principals=except_principals, active=active)


def appr(table: str, col: str, term_id: str | None = TERM_ID, *, sensitivity: str = "internal",
         approved_at: str | None = T1) -> Approved:
    return Approved(table_name=table, column_name=col, term_id=term_id, description="d", sensitivity=sensitivity,
                    approved_by="owner.a", approved_at=approved_at, applied_to_uc_at=None)


def decided_for(*rows: Approved) -> dict:
    return {(r.table_name.lower(), r.column_name.lower()): {
        "table_name": r.table_name, "column_name": r.column_name, "term_id": r.term_id,
        "description": r.description, "sensitivity": r.sensitivity, "approved_by": r.approved_by} for r in rows}


def run(rows, approved=(), *, decided=None, tags=None, policies=None, functions=None, rls_tag=RLS,
        hydration_principal=HP, catalog=CAT, rls_term_id=TERM_ID, gov_schema=GOV, tag_prefix=PREFIX,
        last_events=None, user_attributes_table=UA_TABLE) -> list[Action]:
    approved = list(approved)
    return plan(list(rows), approved, decided_for(*approved) if decided is None else decided,
                set() if tags is None else tags, {} if policies is None else policies,
                {} if functions is None else functions, catalog=catalog, rls_tag=rls_tag,
                rls_term_id=rls_term_id, hydration_principal=hydration_principal, gov_schema=gov_schema,
                tag_prefix=tag_prefix, last_events=last_events, user_attributes_table=user_attributes_table)


def pname(policy_id: str) -> str:
    return policy_name(policy_id, PREFIX)


def pkey(r: PolicyRow) -> tuple[str, str]:
    """current_policies key: (securable lowercased, policy name)."""
    return (r.securable.lower(), pname(r.policy_id))


def body_of(statement: str) -> str:
    m = re.search(r"\bRETURN\b(.*)$", statement, re.IGNORECASE | re.DOTALL)
    assert m, statement
    return m.group(1).strip().rstrip(";").strip()


def policy_state(r: PolicyRow) -> dict:
    """A policy as UC would describe the generator's own CREATE POLICY for row `r`."""
    return {"securable": r.securable, "function_name": r.function_name, "match_tag_key": r.match_tag_key,
            "match_tag_value": r.match_tag_value, "match_alias": "r", "using_columns": ("r",),
            "to_principals": ("account users",), "for_securable_type": "TABLE",
            "except_principals": tuple(r.except_principals)}


def converged_objects(rows) -> tuple[dict, dict]:
    """(current_policies, current_functions) as they would be after a clean first run."""
    first = run(rows)
    functions = {a.object.lower(): body_of(a.statement) for a in first if a.kind == "function_set"}
    policies = {pkey(r): policy_state(r) for r in rows if r.active}
    return policies, functions


def of(actions, kind: str) -> list[Action]:
    return [a for a in actions if a.kind == kind]


def kinds(actions) -> list[str]:
    return [a.kind for a in actions]


def names(obj: str) -> str:
    return obj.lower()


def tag_cols(actions, kind: str) -> set[tuple[str, str]]:
    """(table, column) mentioned by each tag action's object, lowercased, as a set of object strings."""
    return {names(a.object) for a in of(actions, kind)}


def obj_for(table: str, col: str) -> str:
    return f"{table}.{col}".lower()


# ---------------------------------------------------------------- types and names

class TestTypes:
    def test_policy_row_is_frozen(self):
        with pytest.raises(dataclasses.FrozenInstanceError):
            prow().active = False  # type: ignore[misc]

    def test_action_is_frozen(self):
        a = Action(kind="tag_set", object="o", statement="s", event_id="e", before={}, after={})
        with pytest.raises(dataclasses.FrozenInstanceError):
            a.kind = "x"  # type: ignore[misc]


class TestPolicyName:
    def test_prefix_and_charset(self):
        for pid in ("rf_curated", "RF-Curated.01", "a b/c", "ÄÖ-x"):
            n = pname(pid)
            assert n.startswith("gg_rls_"), n
            assert re.fullmatch(r"[a-z0-9_]+", n), n

    def test_prefix_is_config(self):
        n = policy_name("rf_curated", "xx")
        assert n.startswith("xx_rls_") and not n.startswith("gg_"), n
        assert re.fullmatch(r"[a-z0-9_]+", n), n

    def test_deterministic(self):
        assert pname("RF-Curated.01") == pname("RF-Curated.01")

    def test_distinct_ids_distinct_names(self):
        assert pname("rf_curated") != pname("rf_claims")

    def test_ids_that_sanitise_alike_still_differ(self):
        # policies are found by name when retiring: two rows must never share one policy
        assert pname("a-b") != pname("a_b")
        assert policy_name("a-b", "xx") != policy_name("a_b", "xx")


# ---------------------------------------------------------------- validate_rows

class TestValidateRows:
    def v(self, rows, hp=HP, rls_tag=RLS, catalog=CAT, gov_schema=GOV):
        return validate_rows(rows, catalog, rls_tag, hp, gov_schema)

    def test_valid_row_ok(self):
        assert self.v([prow()]) == []

    def test_no_rows_ok(self):
        assert self.v([]) == []

    def test_two_active_rows_same_securable_refused(self):
        assert self.v([prow("a"), prow("b")]) != []

    def test_two_active_rows_same_securable_case_insensitive(self):
        assert self.v([prow("a", "cat.curated"), prow("b", "cat.Curated")]) != []

    def test_one_active_one_inactive_same_securable_ok(self):
        assert self.v([prow("a"), prow("b", active=False)]) == []

    def test_active_rows_on_different_securables_ok(self):
        assert self.v([prow("a", SCHEMA), prow("b", OTHER_SCHEMA)]) == []

    @pytest.mark.parametrize("securable", ["cat", "cat.curated.claims", "other.curated", "", "cat."])
    def test_securable_must_be_schema_in_catalog(self, securable):
        assert self.v([prow(securable=securable)]) != []

    @pytest.mark.parametrize("fn", ["rls_region_filter", "gov.rls_region_filter", "other.gov.fn",
                                    "cat.gov.fn.extra", ""])
    def test_function_name_must_be_three_part_in_catalog(self, fn):
        assert self.v([prow(function_name=fn)]) != []

    @pytest.mark.parametrize("fn", ["cat.raw.fn", "cat.gov2.fn", "cat.curated.rls_region_filter"])
    def test_function_must_live_in_gov_schema(self, fn):
        assert self.v([prow(function_name=fn)]) != []

    def test_function_gov_schema_case_insensitive(self):
        assert self.v([prow(function_name="cat.GOV.rls_region_filter")]) == []

    def test_gov_schema_is_config(self):
        assert self.v([prow(function_name="cat.gov_x.fn")], gov_schema="gov_x") == []
        assert self.v([prow()], gov_schema="gov_x") != []

    def test_inactive_row_function_must_also_be_in_gov(self):
        assert self.v([prow(active=False, function_name="cat.raw.fn")]) != []

    def test_match_tag_key_must_be_rls_tag(self):
        assert self.v([prow(match_tag_key="gg_sensitivity")]) != []
        assert self.v([prow()], rls_tag="xx_rls_attribute") != []

    def test_match_tag_value_must_be_region(self):
        assert self.v([prow(match_tag_value="line_of_business")]) != []

    def test_active_row_must_except_hydration_principal(self):
        assert self.v([prow(except_principals=())]) != []
        assert self.v([prow(except_principals=("gs_data_owner",))]) != []

    def test_extra_except_principals_ok(self):
        assert self.v([prow(except_principals=("gs_data_owner", HP))]) == []

    def test_inactive_row_need_not_except_hydration_principal(self):
        assert self.v([prow(active=False, except_principals=())]) == []

    def test_empty_hydration_principal_refused(self):
        assert self.v([prow(except_principals=("",))], hp="") != []

    def test_non_row_filter_rows_ignored(self):
        g = prow("g1", securable="whatever", policy_type="grant", function_name="", match_tag_key="",
                 match_tag_value="", except_principals=())
        m = prow("m1", securable=SCHEMA, policy_type="mask", match_tag_key="gg_sensitivity",
                 match_tag_value="pii", except_principals=())
        assert self.v([g, m, prow()]) == []

    def test_returns_list_of_str(self):
        errs = self.v([prow(match_tag_value="x")])
        assert isinstance(errs, list) and errs and all(isinstance(e, str) for e in errs)


# ---------------------------------------------------------------- region rule (reference)

class TestRegionVisible:
    def test_no_matched_rows_fail_closed(self):
        assert region_visible("UK", []) is False
        assert region_visible(None, []) is False

    def test_single_region(self):
        assert region_visible("UK", ["UK"]) is True
        assert region_visible("US", ["UK"]) is False

    def test_null_row_region_never_matched_by_regions(self):
        assert region_visible(None, ["UK"]) is False
        assert region_visible(None, ["UK", "US"]) is False

    def test_two_regions(self):
        assert region_visible("UK", ["UK", "US"]) is True
        assert region_visible("US", ["UK", "US"]) is True
        assert region_visible("FR", ["UK", "US"]) is False

    def test_all_sees_everything_including_null(self):
        assert region_visible("UK", ["ALL"]) is True
        assert region_visible("FR", ["ALL"]) is True
        assert region_visible(None, ["ALL"]) is True

    def test_all_plus_region_is_all(self):
        assert region_visible("US", ["ALL", "UK"]) is True
        assert region_visible(None, ["UK", "ALL"]) is True

    def test_none_sees_nothing(self):
        assert region_visible("UK", ["NONE"]) is False
        assert region_visible(None, ["NONE"]) is False

    def test_none_beats_all(self):
        assert region_visible("UK", ["NONE", "ALL"]) is False
        assert region_visible(None, ["ALL", "NONE"]) is False

    def test_none_beats_region(self):
        assert region_visible("UK", ["NONE", "UK"]) is False
        assert region_visible("UK", ["UK", "NONE"]) is False


# ---------------------------------------------------------------- SQL text (light; logic proven on dev)

def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).upper()


class TestHelperSql:
    def test_shape(self):
        s = helper_sql("cat.gov.rls_region_helper")
        n = _norm(s)
        assert "CREATE OR REPLACE FUNCTION" in n
        assert quote_table("cat.gov.rls_region_helper") in s
        assert "RETURNS BOOLEAN" in n
        assert re.search(r"ARRAY\s*<\s*STRING\s*>", n)
        assert "'NONE'" in n and "'ALL'" in n
        assert "RETURN" in n

    def test_pure_no_table_or_identity(self):
        s = helper_sql("cat.gov.h").lower()
        assert "session_user" not in s and "is_account_group_member" not in s and " from " not in s

    def test_deterministic(self):
        assert helper_sql("cat.gov.h") == helper_sql("cat.gov.h")

    def test_backtick_escaped(self):
        assert quote_table("cat.gov.we`ird") in helper_sql("cat.gov.we`ird")

    def test_none_branch_before_all_branch(self):
        # spec: any NONE -> no rows, even with ALL; a CASE picks the first true branch
        n = _norm(helper_sql("cat.gov.h"))
        assert n.index("'NONE'") < n.index("'ALL'"), n

    def test_null_r_branch_before_final_region_match(self):
        # spec: a NULL region is never matched by a region list
        n = _norm(helper_sql("cat.gov.h"))
        null_r = re.search(r"\b`?R`?\s+IS\s+NULL\b", n)
        matches = list(re.finditer(r"ARRAY_CONTAINS\(\s*`?REGIONS`?\s*,\s*`?R`?\s*\)", n))
        assert null_r and matches, n
        assert null_r.start() < matches[-1].start(), n


class TestFilterSql:
    UA = "c2.r2.ua_custom"

    def test_shape(self):
        s = filter_sql("cat.gov.rls_region_filter", "cat.gov.rls_region_helper", self.UA)
        n = _norm(s)
        assert "CREATE OR REPLACE FUNCTION" in n
        assert quote_table("cat.gov.rls_region_filter") in s
        assert quote_table("cat.gov.rls_region_helper") in s
        assert quote_table(self.UA) in s
        assert "RETURNS BOOLEAN" in n
        assert re.search(r"\(\s*`?R`?\s+STRING\s*\)", n)
        assert "SESSION_USER()" in n
        assert "IS_ACCOUNT_GROUP_MEMBER" in n

    def test_table_name_not_hard_coded(self):
        s = filter_sql("cat.gov.f", "cat.gov.h", self.UA)
        assert "user_attributes" not in s.lower()

    def test_deterministic(self):
        args = ("cat.gov.f", "cat.gov.h", self.UA)
        assert filter_sql(*args) == filter_sql(*args)


# ---------------------------------------------------------------- plan: first run

TBL = f"{SCHEMA}.claims"
TBL2 = f"{SCHEMA}.policies"
RAW = "cat.raw.claims"


class TestFirstRun:
    def test_empty_state_creates_function_policy_and_tag(self):
        a = run([prow()], [appr(TBL, "region")])
        assert {"function_set", "policy_set", "tag_set"} <= set(kinds(a))
        assert any(names(x.object) == FN.lower() for x in of(a, "function_set"))
        (p,) = of(a, "policy_set")
        assert pname("rf_curated") in names(p.object)
        assert tag_cols(a, "tag_set") == {obj_for(TBL, "region")}

    def test_only_region_term_columns_tagged(self):
        a = run([prow()], [appr(TBL, "region"), appr(TBL, "claim_ref", "claim_reference"),
                           appr(TBL2, "sales_region"), appr(TBL2, "notes", None)])
        assert tag_cols(a, "tag_set") == {obj_for(TBL, "region"), obj_for(TBL2, "sales_region")}

    def test_columns_outside_securable_not_tagged(self):
        a = run([prow()], [appr(RAW, "region"), appr("cat.curated2.x", "region"), appr(TBL, "region")])
        assert tag_cols(a, "tag_set") == {obj_for(TBL, "region")}

    def test_table_match_is_case_insensitive(self):
        a = run([prow(securable="cat.Curated")], [appr("CAT.curated.Claims", "Region")])
        assert tag_cols(a, "tag_set") == {obj_for(TBL, "region")}

    def test_rls_term_id_is_config(self):
        a = run([prow()], [appr(TBL, "region"), appr(TBL, "zone", "sales_zone")], rls_term_id="sales_zone")
        assert tag_cols(a, "tag_set") == {obj_for(TBL, "zone")}

    def test_no_rows_no_actions(self):
        assert run([], [appr(TBL, "region")]) == []

    def test_non_row_filter_rows_produce_nothing(self):
        g = prow("g1", policy_type="grant", function_name="", match_tag_key="", match_tag_value="",
                 except_principals=())
        assert run([g], [appr(TBL, "region")]) == []

    def test_invalid_rows_raise(self):
        with pytest.raises(ValueError):
            run([prow("a"), prow("b")], [appr(TBL, "region")])
        with pytest.raises(ValueError):
            run([prow(except_principals=())], [])
        with pytest.raises(ValueError):
            run([prow()], [], hydration_principal="")
        with pytest.raises(ValueError):
            run([prow(function_name="cat.raw.rls_region_filter")], [])


# ---------------------------------------------------------------- statements

class TestStatements:
    def test_tag_set_statement(self):
        (t,) = of(run([prow()], [appr(TBL, "region")]), "tag_set")
        assert quote_table(TBL) in t.statement
        assert quote_ident("region") in t.statement
        assert "SET TAGS" in t.statement.upper()
        assert quote_str(RLS) in t.statement and quote_str("region") in t.statement

    def test_tag_unset_statement(self):
        policies, functions = converged_objects([prow()])
        (t,) = of(run([prow()], [], tags={(TBL, "old_region")}, policies=policies, functions=functions),
                  "tag_unset")
        assert quote_table(TBL) in t.statement and quote_ident("old_region") in t.statement
        assert "UNSET TAGS" in t.statement.upper() and quote_str(RLS) in t.statement

    def test_identifiers_backtick_escaped(self):
        (t,) = of(run([prow()], [appr(TBL, "reg`ion")]), "tag_set")
        assert quote_ident("reg`ion") in t.statement  # `reg``ion`

    def test_policy_statement(self):
        r = prow(except_principals=("gs_data_owner", HP))
        (p,) = of(run([r], []), "policy_set")
        s = p.statement
        assert "POLICY" in s.upper() and "ROW FILTER" in _norm(s)
        assert quote_ident(pname("rf_curated")) in s
        assert quote_table(SCHEMA) in s
        assert quote_table(FN) in s
        assert quote_ident(HP) in s and quote_ident("gs_data_owner") in s
        assert quote_str(RLS) in s and quote_str("region") in s

    def test_function_statements_are_create_or_replace(self):
        for f in of(run([prow()], []), "function_set"):
            assert "CREATE OR REPLACE FUNCTION" in _norm(f.statement)
            assert "RETURNS BOOLEAN" in _norm(f.statement)

    def test_tag_refused_has_no_statement(self):
        r = appr(TBL, "region")
        (t,) = of(run([prow()], [r], decided={}), "tag_refused")
        assert t.statement is None

    def test_only_rls_tag_is_touched(self):
        policies, functions = converged_objects([prow()])
        a = run([prow()], [appr(TBL, "region"), appr(TBL, "claim_ref", "claim_reference", sensitivity="pii")],
                tags={(TBL, "stale")}, policies=policies, functions=functions)
        assert of(a, "tag_set") and of(a, "tag_unset")
        for x in run([prow()], [appr(TBL, "region")]) + a:
            if x.statement:
                assert "gg_sensitivity" not in x.statement and "gg_business_term" not in x.statement

    def test_rls_tag_follows_config(self):
        r = prow(match_tag_key="xx_rls_attribute")
        (t,) = of(run([r], [appr(TBL, "region")], rls_tag="xx_rls_attribute"), "tag_set")
        assert quote_str("xx_rls_attribute") in t.statement and RLS not in t.statement


# ---------------------------------------------------------------- idempotency and drift

class TestIdempotent:
    def converged(self, rows, approved):
        policies, functions = converged_objects(rows)
        tags = {(r.table_name.lower(), r.column_name.lower()) for r in approved if r.term_id == TERM_ID
                and r.table_name.lower().startswith(SCHEMA + ".")}
        return policies, functions, tags

    def test_converged_state_is_noop(self):
        rows, approved = [prow()], [appr(TBL, "region"), appr(TBL, "claim_ref", "claim_reference")]
        policies, functions, tags = self.converged(rows, approved)
        assert run(rows, approved, tags=tags, policies=policies, functions=functions) == []

    def test_same_input_same_plan(self):
        rows, approved = [prow()], [appr(TBL, "region"), appr(TBL2, "region")]
        assert run(rows, approved) == run(rows, approved)

    def test_function_body_whitespace_and_case_ignored(self):
        rows, approved = [prow()], [appr(TBL, "region")]
        policies, functions, tags = self.converged(rows, approved)
        messy = {k: "  " + re.sub(r"\s+", "\n    ", v).upper() + "\n" for k, v in functions.items()}
        assert run(rows, approved, tags=tags, policies=policies, functions=messy) == []

    def test_changed_function_body_reemitted(self):
        rows, approved = [prow()], [appr(TBL, "region")]
        policies, functions, tags = self.converged(rows, approved)
        functions[FN.lower()] = "true"
        a = run(rows, approved, tags=tags, policies=policies, functions=functions)
        assert kinds(a) == ["function_set"]
        assert names(a[0].object) == FN.lower()

    def test_except_list_order_and_case_ignored(self):
        r = prow(except_principals=("gs_data_owner", HP))
        policies, functions, _ = self.converged([r], [])
        policies[pkey(r)]["except_principals"] = (HP.upper(), "GS_Data_Owner")
        assert run([r], [], policies=policies, functions=functions) == []

    def test_to_principals_and_securable_type_case_and_order_ignored(self):
        r = prow()
        policies, functions, _ = self.converged([r], [])
        policies[pkey(r)]["to_principals"] = ("Account Users",)
        policies[pkey(r)]["for_securable_type"] = "table"
        assert run([r], [], policies=policies, functions=functions) == []

    @pytest.mark.parametrize("field,value", [
        ("except_principals", (HP, "gs_analyst_uk")),
        ("function_name", "cat.gov.other_fn"),
        ("match_tag_value", "line_of_business"),
        ("match_tag_key", "gg_sensitivity"),
        ("to_principals", ("gs_analyst_uk",)),  # narrowed to one group: everyone else unfiltered
        ("to_principals", ("account users", "gs_analyst_uk")),
        ("to_principals", ()),
        ("for_securable_type", "SCHEMA"),
        ("match_alias", "x"),
        ("using_columns", ("x",)),
        ("using_columns", ("r", "x")),
        ("using_columns", ()),
    ])
    def test_policy_drift_reemitted(self, field, value):
        r = prow()
        policies, functions, _ = self.converged([r], [])
        policies[pkey(r)][field] = value
        a = run([r], [], policies=policies, functions=functions)
        assert kinds(a) == ["policy_set"]

    def test_policy_under_another_securable_key_is_not_this_one(self):
        # same name, keyed to another schema: the row's own policy is missing, so it is (re)created here
        r = prow()
        policies, functions, _ = self.converged([r], [])
        moved = {(OTHER_SCHEMA, pname(r.policy_id)): policy_state(prow(securable=OTHER_SCHEMA))}
        a = run([r], [], policies=moved, functions=functions)
        assert of(a, "policy_set")
        (d,) = of(a, "policy_drop")
        assert quote_table(OTHER_SCHEMA) in d.statement

    def test_missing_policy_reemitted(self):
        policies, functions, _ = self.converged([prow()], [])
        assert kinds(run([prow()], [], functions=functions)) == ["policy_set"]


class TestTagDrift:
    def setup_method(self):
        self.policies, self.functions = converged_objects([prow()])

    def r(self, approved, tags, decided=None, rows=None):
        return run(rows or [prow()], approved, tags=tags, decided=decided, policies=self.policies,
                   functions=self.functions)

    def test_tag_without_approved_row_unset(self):
        a = self.r([], {(TBL, "region")})
        assert kinds(a) == ["tag_unset"]
        assert tag_cols(a, "tag_unset") == {obj_for(TBL, "region")}

    def test_tag_on_column_no_longer_region_term_unset(self):
        a = self.r([appr(TBL, "region", "country_code", approved_at=T2)], {(TBL, "region")})
        assert kinds(a) == ["tag_unset"]

    def test_tag_outside_any_securable_left_alone(self):
        assert self.r([], {(RAW, "region"), ("cat.curated2.x", "region")}) == []

    def test_present_tag_case_insensitive(self):
        assert self.r([appr("CAT.Curated.Claims", "Region")], {(TBL, "region")}) == []

    def test_refused_region_row_emits_tag_refused_and_no_set(self):
        r = appr(TBL, "region")
        a = self.r([r], set(), decided={})
        assert kinds(a) == ["tag_refused"]
        assert tag_cols(a, "tag_refused") == {obj_for(TBL, "region")}

    def test_refused_row_keeps_existing_tag(self):
        # hand-edited approved_metadata now says another term: the edit is not trusted, the tag stays
        r = appr(TBL, "region", "country_code")
        decided = decided_for(appr(TBL, "region"))
        a = self.r([r], {(TBL, "region")}, decided=decided)
        assert kinds(a) == ["tag_refused"]
        assert of(a, "tag_unset") == []

    def test_refused_for_bad_sensitivity(self):
        r = appr(TBL, "region", sensitivity="secret")
        a = self.r([r], {(TBL, "region")})
        assert kinds(a) == ["tag_refused"]


class TestRetiring:
    def test_inactive_row_with_policy_drops_and_unsets(self):
        r = prow(active=False)
        policies, _ = converged_objects([prow()])
        a = run([r], [appr(TBL, "region")], tags={(TBL, "region"), (TBL2, "region"), (RAW, "region")},
                policies=policies)
        assert kinds(a) == ["tag_unset", "tag_unset", "policy_drop"]
        assert tag_cols(a, "tag_unset") == {obj_for(TBL, "region"), obj_for(TBL2, "region")}
        (d,) = of(a, "policy_drop")
        assert pname(r.policy_id) in names(d.object)
        assert "DROP POLICY" in _norm(d.statement)
        assert quote_ident(pname(r.policy_id)) in d.statement
        assert quote_table(SCHEMA) in d.statement

    def test_inactive_row_without_policy_still_unsets_tags(self):
        # REVERSED (review fix D): was test_inactive_row_without_policy_does_nothing. The spec's retiring rule
        # ("unsets <tag_prefix>_rls_attribute on that securable's columns") is unconditional: a policy dropped
        # by hand, or a crash between drop and unset, must not leave stale tags behind.
        r = prow(active=False)
        a = run([r], [appr(TBL, "region")], tags={(TBL, "region"), (TBL2, "x"), (RAW, "region")})
        assert kinds(a) == ["tag_unset", "tag_unset"]
        assert tag_cols(a, "tag_unset") == {obj_for(TBL, "region"), obj_for(TBL2, "x")}

    def test_inactive_and_active_row_same_securable(self):
        # old row retired, new row active on the same schema: old policy dropped, wanted tag kept
        old, new = prow("rf_old", active=False), prow("rf_new")
        policies, functions = converged_objects([new])
        policies[pkey(old)] = policy_state(old)
        a = run([old, new], [appr(TBL, "region")], tags={(TBL, "region")}, policies=policies,
                functions=functions)
        assert kinds(a) == ["policy_drop"]
        assert pname("rf_old") in names(a[0].object)

    def test_inactive_row_sets_nothing(self):
        r = prow(active=False)
        policies, _ = converged_objects([prow()])
        a = run([r], [appr(TBL, "region")], policies=policies)
        assert not set(kinds(a)) & {"function_set", "policy_set", "tag_set"}

    def test_retire_rerun_is_noop(self):
        r = prow(active=False)
        assert run([r], [appr(TBL, "region")]) == []


# ---------------------------------------------------------------- ordering and event ids

class TestStrayPolicies:
    """Policies named <prefix>_rls_ that match no row_filter row on their securable are dropped."""

    def setup_method(self):
        self.policies, self.functions = converged_objects([prow()])

    def test_stray_on_active_securable_dropped_tags_kept(self):
        stray = (SCHEMA, f"{PREFIX}_rls_stray_0000abcd")
        self.policies[stray] = policy_state(prow("stray"))
        a = run([prow()], [appr(TBL, "region")], tags={(TBL, "region")}, policies=self.policies,
                functions=self.functions)
        assert kinds(a) == ["policy_drop"]
        assert f"{PREFIX}_rls_stray_0000abcd" in names(a[0].object)
        assert quote_ident(stray[1]) in a[0].statement and quote_table(SCHEMA) in a[0].statement

    def test_stray_on_uncovered_securable_dropped_and_tags_unset(self):
        stray = (OTHER_SCHEMA, f"{PREFIX}_rls_stray_0000abcd")
        self.policies[stray] = policy_state(prow("stray", OTHER_SCHEMA))
        tags = {(TBL, "region"), (f"{OTHER_SCHEMA}.c", "region"), (RAW, "region")}
        a = run([prow()], [appr(TBL, "region")], tags=tags, policies=self.policies, functions=self.functions)
        assert kinds(a) == ["tag_unset", "policy_drop"]
        assert tag_cols(a, "tag_unset") == {obj_for(f"{OTHER_SCHEMA}.c", "region")}
        assert quote_table(OTHER_SCHEMA) in of(a, "policy_drop")[0].statement

    def test_row_moved_securable(self):
        # rf_curated now points at OTHER_SCHEMA: new policy there, old one dropped, old schema's tags unset
        moved = prow(securable=OTHER_SCHEMA)
        old_key = pkey(prow())
        policies = {old_key: policy_state(prow())}
        tags = {(TBL, "region")}
        a = run([moved], [appr(TBL, "region"), appr(f"{OTHER_SCHEMA}.c", "region")], tags=tags,
                policies=policies, functions=self.functions)
        assert kinds(a) == ["policy_set", "tag_set", "tag_unset", "policy_drop"]
        assert quote_table(OTHER_SCHEMA) in of(a, "policy_set")[0].statement
        assert tag_cols(a, "tag_set") == {obj_for(f"{OTHER_SCHEMA}.c", "region")}
        assert tag_cols(a, "tag_unset") == {obj_for(TBL, "region")}
        assert quote_table(SCHEMA) in of(a, "policy_drop")[0].statement

    def test_policies_without_prefix_never_touched(self):
        for key in [(SCHEMA, "hand_made_policy"), (OTHER_SCHEMA, "xx_rls_other_tool"),
                    (OTHER_SCHEMA, "gg_mask_pii"), (OTHER_SCHEMA, "rls_gg_x")]:
            self.policies[key] = policy_state(prow("x", key[0]))
        a = run([prow()], [appr(TBL, "region")], tags={(TBL, "region")}, policies=self.policies,
                functions=self.functions)
        assert a == []

    def test_prefix_follows_config(self):
        # with tag_prefix xx, a gg_rls_ policy is someone else's and an xx_rls_ stray is ours
        r = prow(match_tag_key="xx_rls_attribute")
        policies = {(SCHEMA, policy_name(r.policy_id, "xx")): policy_state(r),
                    (SCHEMA, f"gg_rls_{'0' * 8}"): policy_state(r),
                    (SCHEMA, f"xx_rls_stray_{'0' * 8}"): policy_state(r)}
        a = run([r], [], policies=policies, functions=self.functions, rls_tag="xx_rls_attribute",
                tag_prefix="xx")
        assert kinds(a) == ["policy_drop"]
        assert "xx_rls_stray_" in names(a[0].object)


class TestOrderAndEvents:
    def everything(self, approved_at=T1):
        rows = [prow("rf_curated", SCHEMA), prow("rf_claims", OTHER_SCHEMA, active=False)]
        policies = {pkey(prow("rf_claims", OTHER_SCHEMA)): policy_state(prow("rf_claims", OTHER_SCHEMA))}
        approved = [appr(TBL, "region", approved_at=approved_at),
                    appr(TBL2, "region", "country_code")]  # refused below
        decided = decided_for(approved[0], appr(TBL2, "region"))
        tags = {(TBL, "stale"), (TBL2, "region"), (f"{OTHER_SCHEMA}.c", "region")}
        return run(rows, approved, decided=decided, tags=tags, policies=policies)

    def test_kinds_ordered(self):
        a = self.everything()
        assert set(kinds(a)) == set(KIND_ORDER)
        idx = [KIND_ORDER.index(k) for k in kinds(a)]
        assert idx == sorted(idx)

    def test_event_ids_deterministic_and_unique(self):
        a, b = self.everything(), self.everything()
        assert [x.event_id for x in a] == [x.event_id for x in b]
        ids = [x.event_id for x in a]
        assert len(ids) == len(set(ids)) and all(isinstance(i, str) and i for i in ids)

    def test_set_unset_set_gives_distinct_ids(self):
        policies, functions = converged_objects([prow()])
        kw = dict(policies=policies, functions=functions)
        (s1,) = of(run([prow()], [appr(TBL, "region", approved_at=T1)], **kw), "tag_set")
        (u,) = of(run([prow()], [appr(TBL, "region", "country_code", approved_at=T2)],
                      tags={(TBL, "region")}, **kw), "tag_unset")
        (s2,) = of(run([prow()], [appr(TBL, "region", approved_at=T3)], **kw), "tag_set")
        assert len({s1.event_id, u.event_id, s2.event_id}) == 3

    def test_tag_set_id_stable_for_same_approval(self):
        policies, functions = converged_objects([prow()])
        a = of(run([prow()], [appr(TBL, "region")], policies=policies, functions=functions), "tag_set")
        b = of(run([prow()], [appr(TBL, "region")], policies=policies, functions=functions), "tag_set")
        assert a[0].event_id == b[0].event_id

    def test_policy_change_has_distinct_id_per_definition(self):
        r1, r2 = prow(), prow(except_principals=(HP, "gs_data_owner"))
        (p1,) = of(run([r1], []), "policy_set")
        (p2,) = of(run([r2], []), "policy_set")
        assert p1.event_id != p2.event_id


class TestLastEvents:
    """Event ids carry the newest completed audit event for the object, so set -> unset -> set is three events."""

    def test_set_hand_unset_reset_new_id(self):
        policies, functions = converged_objects([prow()])
        kw = dict(policies=policies, functions=functions)
        (s1,) = of(run([prow()], [appr(TBL, "region", approved_at=T1)], **kw), "tag_set")
        # tag removed by hand; nothing in gov changed (same approved_at); last completed event is s1
        (s2,) = of(run([prow()], [appr(TBL, "region", approved_at=T1)],
                       last_events={s1.object.lower(): s1.event_id}, **kw), "tag_set")
        assert s2.event_id != s1.event_id

    def test_crash_replay_same_ids(self):
        rows = [prow(), prow("rf_claims", OTHER_SCHEMA, active=False)]
        policies = {pkey(rows[1]): policy_state(rows[1])}
        approved = [appr(TBL, "region")]
        tags = {(TBL, "stale"), (f"{OTHER_SCHEMA}.c", "region")}
        first = run(rows, approved, tags=tags, policies=policies)
        last = {a.object.lower(): f"ev-{i}" for i, a in enumerate(first)}
        a = run(rows, approved, tags=tags, policies=policies, last_events=last)
        b = run(rows, approved, tags=tags, policies=policies, last_events=dict(last))
        assert [x.event_id for x in a] == [x.event_id for x in b]
        # no completed event yet (only *_started): None and {} give the same ids
        assert [x.event_id for x in run(rows, approved, tags=tags, policies=policies, last_events={})] == \
            [x.event_id for x in first]

    def test_every_action_id_includes_its_last_event(self):
        rows = [prow("rf_curated", SCHEMA), prow("rf_claims", OTHER_SCHEMA, active=False)]
        policies = {pkey(rows[1]): policy_state(rows[1])}
        approved = [appr(TBL, "region"), appr(TBL2, "region", "country_code")]
        decided = decided_for(approved[0], appr(TBL2, "region"))
        tags = {(TBL, "stale"), (TBL2, "region"), (f"{OTHER_SCHEMA}.c", "region")}
        base = run(rows, approved, decided=decided, tags=tags, policies=policies)
        assert set(kinds(base)) == set(KIND_ORDER)
        for act in base:
            again = run(rows, approved, decided=decided, tags=tags, policies=policies,
                        last_events={act.object.lower(): "ev-prev"})
            ids = {(x.kind, x.object): x.event_id for x in again}
            for x in base:
                if x.kind == "tag_refused":
                    # record-only events never chain on last_events (review fix: they would re-log every run)
                    assert ids[(x.kind, x.object)] == x.event_id, (act.kind, x.kind)
                elif (x.kind, x.object) == (act.kind, act.object):
                    assert ids[(x.kind, x.object)] != x.event_id, x.kind
                elif x.object.lower() != act.object.lower():
                    assert ids[(x.kind, x.object)] == x.event_id, (act.kind, x.kind)

    def test_unrelated_last_events_change_nothing(self):
        a = run([prow()], [appr(TBL, "region")])
        b = run([prow()], [appr(TBL, "region")], last_events={"cat.other.t.c": "ev-x"})
        assert a == b

    def test_function_recreated_after_drift_new_id(self):
        policies, _ = converged_objects([prow()])
        first = of(run([prow()], [], policies=policies), "function_set")
        f1 = next(f for f in first if names(f.object) == FN.lower())
        drifted = {f.object.lower(): body_of(f.statement) for f in first}
        drifted[FN.lower()] = "true"  # hand edit
        a = run([prow()], [], policies=policies, functions=drifted,
                last_events={f.object.lower(): f.event_id for f in first})
        (f2,) = of(a, "function_set")
        assert names(f2.object) == FN.lower()
        assert f2.event_id != f1.event_id

    def test_policy_recreated_after_hand_drop_new_id(self):
        _, functions = converged_objects([prow()])
        (p1,) = of(run([prow()], [], functions=functions), "policy_set")
        (p2,) = of(run([prow()], [], functions=functions, last_events={p1.object.lower(): p1.event_id}),
                   "policy_set")
        assert p2.event_id != p1.event_id

    def test_policy_reset_after_drift_new_id(self):
        r = prow()
        policies, functions = converged_objects([r])
        policies[pkey(r)]["to_principals"] = ("gs_analyst_uk",)
        (p1,) = of(run([r], [], policies=policies, functions=functions), "policy_set")
        (p2,) = of(run([r], [], policies=policies, functions=functions,
                       last_events={p1.object.lower(): "ev-earlier-set"}), "policy_set")
        assert p1.event_id != p2.event_id


class TestTagRefusedStable:
    """tag_refused is record-only: its id must not chain on last_events, or a refused row is re-logged every run."""

    def setup_method(self):
        self.policies, self.functions = converged_objects([prow()])
        self.approved = [appr(TBL, "region", "country_code")]
        self.decided = decided_for(appr(TBL, "region"))  # hand edit: refused
        self.tags = {(TBL, "region")}

    def r(self, last_events=None):
        return run([prow()], self.approved, decided=self.decided, tags=self.tags, policies=self.policies,
                   functions=self.functions, last_events=last_events)

    def test_rerun_with_all_run1_ids_as_last_events_same_id(self):
        first = self.r()
        (t1,) = of(first, "tag_refused")
        last = {a.object.lower(): a.event_id for a in first}
        (t2,) = of(self.r(last_events=last), "tag_refused")
        assert t2.event_id == t1.event_id

    @pytest.mark.parametrize("prev", ["ev-other", "ev-earlier-set", ""])
    def test_any_last_event_for_object_leaves_id_unchanged(self, prev):
        (t1,) = of(self.r(), "tag_refused")
        (t2,) = of(self.r(last_events={t1.object.lower(): prev}), "tag_refused")
        assert t2.event_id == t1.event_id


class TestUserAttributesTable:
    def call(self, **extra):
        return plan([prow()], [], {}, set(), {}, {}, catalog=CAT, rls_tag=RLS, rls_term_id=TERM_ID,
                    hydration_principal=HP, gov_schema=GOV, tag_prefix=PREFIX, **extra)

    def test_required_keyword(self):
        with pytest.raises(TypeError):
            self.call()

    def filter_stmt(self, actions) -> str:
        (f,) = [a for a in of(actions, "function_set") if names(a.object) == FN.lower()]
        return f.statement

    def test_filter_references_exactly_the_passed_table(self):
        ua = "c2.r2.ua_custom"
        s = self.filter_stmt(self.call(user_attributes_table=ua))
        assert quote_table(ua) in s
        assert "user_attributes" not in s.lower()
        assert ".ref." not in s.lower() and "`ref`" not in s.lower()

    def test_run_helper_table_used(self):
        s = self.filter_stmt(run([prow()], []))
        assert quote_table(UA_TABLE) in s
