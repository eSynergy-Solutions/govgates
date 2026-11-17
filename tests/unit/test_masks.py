"""Column-mask generator planner (specs/consumption.md: Policy model `mask` rows, Join-up, EXCEPT). Pure: no Spark.

gov.access_policies `mask` rows + gov.approved_metadata are the truth; current UC policies and function bodies are
read only to find the difference. Masks are schema-level ABAC policies matched on `<tag_prefix>_sensitivity`, so a
column hydration newly approves as pii is masked with no consumption-side action (the join-up).

Fixture conventions (mirroring tests/unit/test_row_filter.py, not fixed by the spec):
- `current_functions` values are the function body as information_schema.routines shows it: the text after `RETURN`.
- `current_policies` is keyed by (securable.lower(), policy_name); the state dict uses the column-mask fields
  match_alias / on_column instead of the row filter's using_columns.
- `column_types` is keyed by (table_name.lower(), column_name.lower()) -> UC data type.
- `last_events` keys are taken from the planner's own `Action.object` (lowercased).
"""

from __future__ import annotations

import dataclasses
import json
import re
from pathlib import Path

import pytest
import yaml

from gates.common.contract import SENSITIVITIES
from gates.consumption.masks import (
    MaskPlan,
    mask_policy_name,
    mask_sql,
    plan_masks,
    tampered,
    validate_mask_rows,
)
from gates.consumption.policy_rows import row_dict
from gates.consumption.row_filter import Action, PolicyRow, policy_name
from gates.hydration.apply import Approved, quote_ident, quote_str, quote_table

CAT = "cat"
RAW = "cat.raw"
CURATED = "cat.curated"
FN = "cat.gov.mask_pii"
FN_CONF = "cat.gov.mask_confidential"
SENS = "gg_sensitivity"
RLS = "gg_rls_attribute"
TERM_ID = "region_code"
HP = "ci-sp-0000"  # synthetic CI service principal applicationId
GOV = "gov"
PREFIX = "gg"
T1 = "2026-01-01T09:00:00"
TBL = f"{RAW}.claims"
TBL2 = f"{CURATED}.policies"
KIND_ORDER = ["function_set", "policy_set", "policy_drop"]
MATRIX = Path(__file__).resolve().parents[1] / "access" / "matrix.yaml"


def mrow(policy_id: str = "mk_raw_pii", securable: str = RAW, *, active: bool = True,
         except_principals: tuple[str, ...] = (HP, "gs_claims_handler"), function_name: str = FN,
         match_tag_key: str = SENS, match_tag_value: str = "pii", policy_type: str = "mask") -> PolicyRow:
    return PolicyRow(policy_id=policy_id, policy_type=policy_type, securable=securable,
                     match_tag_key=match_tag_key, match_tag_value=match_tag_value, function_name=function_name,
                     except_principals=except_principals, active=active)


def rfrow(policy_id: str = "rf_raw", securable: str = RAW, *, active: bool = True) -> PolicyRow:
    return PolicyRow(policy_id=policy_id, policy_type="row_filter", securable=securable, match_tag_key=RLS,
                     match_tag_value="region", function_name="cat.gov.rls_region_filter",
                     except_principals=(HP,), active=active)


def appr(table: str, col: str, term_id: str | None = "claim_notes", *, sensitivity: str = "pii",
         approved_at: str | None = T1) -> Approved:
    return Approved(table_name=table, column_name=col, term_id=term_id, description="d", sensitivity=sensitivity,
                    approved_by="owner.a", approved_at=approved_at, applied_to_uc_at=None)


def string_types(approved) -> dict:
    return {(a.table_name.lower(), a.column_name.lower()): "STRING" for a in approved}


def run(rows, approved=(), *, policies=None, functions=None, column_types=None, rls_rows=(), catalog=CAT,
        sensitivity_tag=SENS, rls_term_id=TERM_ID, hydration_principal=HP, gov_schema=GOV, tag_prefix=PREFIX,
        last_events=None) -> MaskPlan:
    approved = list(approved)
    return plan_masks(list(rows), approved, {} if policies is None else policies,
                      {} if functions is None else functions, catalog=catalog, sensitivity_tag=sensitivity_tag,
                      rls_term_id=rls_term_id, rls_rows=list(rls_rows), hydration_principal=hydration_principal,
                      gov_schema=gov_schema, tag_prefix=tag_prefix, last_events=last_events,
                      column_types=string_types(approved) if column_types is None else column_types)


def pname(policy_id: str) -> str:
    return mask_policy_name(policy_id, PREFIX)


def pkey(r: PolicyRow) -> tuple[str, str]:
    return (r.securable.lower(), pname(r.policy_id))


def body_of(statement: str) -> str:
    m = re.search(r"\bRETURN\b(.*)$", statement, re.IGNORECASE | re.DOTALL)
    assert m, statement
    return m.group(1).strip().rstrip(";").strip()


def policy_state(r: PolicyRow) -> dict:
    """A policy as UC would describe the generator's own CREATE POLICY ... COLUMN MASK for row `r`."""
    return {"securable": r.securable, "function_name": r.function_name, "match_tag_key": r.match_tag_key,
            "match_tag_value": r.match_tag_value, "match_alias": "c", "on_column": "c",
            "to_principals": ("account users",), "for_securable_type": "TABLE",
            "except_principals": tuple(r.except_principals)}


def converged_objects(rows) -> tuple[dict, dict]:
    first = run(rows).actions
    functions = {a.object.lower(): body_of(a.statement) for a in first if a.kind == "function_set"}
    policies = {pkey(r): policy_state(r) for r in rows if r.active and r.policy_type == "mask"}
    return policies, functions


def of(actions, kind: str) -> list[Action]:
    return [a for a in actions if a.kind == kind]


def kinds(actions) -> list[str]:
    return [a.kind for a in actions]


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).upper()


# ---------------------------------------------------------------- types and names

class TestTypes:
    def test_mask_plan_is_frozen(self):
        p = run([])
        with pytest.raises(dataclasses.FrozenInstanceError):
            p.failures = ("x",)  # type: ignore[misc]

    def test_mask_plan_shape(self):
        p = run([])
        assert p.actions == [] and p.failures == () and p.warnings == ()
        assert isinstance(p.failures, tuple) and isinstance(p.warnings, tuple)


class TestMaskPolicyName:
    def test_prefix_and_charset(self):
        for pid in ("mk_raw_pii", "MK-Raw.01", "a b/c", "ÄÖ-x"):
            n = pname(pid)
            assert n.startswith("gg_mask_"), n
            assert re.fullmatch(r"[a-z0-9_]+", n), n

    def test_prefix_is_config(self):
        n = mask_policy_name("mk_raw_pii", "xx")
        assert n.startswith("xx_mask_") and not n.startswith("gg_"), n
        assert re.fullmatch(r"[a-z0-9_]+", n), n

    def test_deterministic(self):
        assert pname("MK-Raw.01") == pname("MK-Raw.01")

    def test_distinct_ids_distinct_names(self):
        assert pname("mk_raw_pii") != pname("mk_curated_pii")

    def test_ids_that_sanitise_alike_still_differ(self):
        assert pname("a-b") != pname("a_b")
        assert mask_policy_name("a-b", "xx") != mask_policy_name("a_b", "xx")

    def test_never_in_row_filter_namespace(self):
        n = pname("rf_raw")
        assert n != policy_name("rf_raw", PREFIX) and not n.startswith("gg_rls_")


# ---------------------------------------------------------------- validate_mask_rows

class TestValidateMaskRows:
    def v(self, rows, hp=HP, sensitivity_tag=SENS, catalog=CAT, gov_schema=GOV):
        return validate_mask_rows(rows, catalog=catalog, sensitivity_tag=sensitivity_tag, hydration_principal=hp,
                                  gov_schema=gov_schema)

    def test_valid_row_ok(self):
        assert self.v([mrow()]) == []

    def test_no_rows_ok(self):
        assert self.v([]) == []

    def test_returns_list_of_str(self):
        errs = self.v([mrow(except_principals=("gs_x",)), mrow("b", securable="other.raw")])
        assert isinstance(errs, list) and errs and all(isinstance(e, str) for e in errs)

    def test_non_mask_rows_ignored(self):
        junk = mrow("rf_junk", "x", policy_type="row_filter", function_name="", match_tag_key="k",
                    match_tag_value="v", except_principals=())
        assert self.v([junk, rfrow()]) == []

    @pytest.mark.parametrize("securable", ["cat", "cat.raw.claims", "other.raw", "", "cat."])
    def test_securable_must_be_schema_in_catalog(self, securable):
        assert self.v([mrow(securable=securable)]) != []

    @pytest.mark.parametrize("fn", ["mask_pii", "gov.mask_pii", "other.gov.mask_pii", "cat.gov.mask_pii.extra", "",
                                    "cat.raw.mask_pii", "cat.gov2.mask_pii"])
    def test_function_must_be_three_part_in_catalog_gov(self, fn):
        assert self.v([mrow(function_name=fn)]) != []

    def test_function_gov_schema_case_insensitive(self):
        assert self.v([mrow(function_name="cat.GOV.mask_pii")]) == []

    def test_gov_schema_is_config(self):
        assert self.v([mrow(function_name="cat.gov_x.mask_pii")], gov_schema="gov_x") == []
        assert self.v([mrow()], gov_schema="gov_x") != []

    def test_inactive_row_function_must_also_be_in_gov(self):
        assert self.v([mrow(active=False, function_name="cat.raw.mask_pii")]) != []

    def test_match_tag_key_must_be_sensitivity_tag(self):
        assert self.v([mrow(match_tag_key=RLS)]) != []
        assert self.v([mrow(match_tag_key="xx_sensitivity")], sensitivity_tag="xx_sensitivity") == []
        assert self.v([mrow()], sensitivity_tag="xx_sensitivity") != []

    @pytest.mark.parametrize("value", ["secret", "", "region", "restricted"])
    def test_match_tag_value_must_be_a_sensitivity(self, value):
        assert self.v([mrow(match_tag_value=value)]) != []

    @pytest.mark.parametrize("value", SENSITIVITIES)
    def test_every_sensitivity_class_accepted(self, value):
        assert self.v([mrow(match_tag_value=value)]) == []

    def test_ci_principal_must_be_in_every_mask_except(self):
        # THE done-when: profiling as the CI SP must read unmasked values, or the pii floor silently disappears
        assert self.v([mrow(except_principals=("gs_claims_handler",))]) != []
        assert self.v([mrow(except_principals=())]) != []
        ok = mrow("mk_raw_pii")
        bad = mrow("mk_curated_pii", CURATED, except_principals=("gs_claims_handler", "gs_data_owner"))
        errs = self.v([ok, bad])
        assert errs != []
        assert any("mk_curated_pii" in e for e in errs)

    def test_ci_principal_match_is_case_insensitive(self):
        assert self.v([mrow(except_principals=(HP.upper(), "gs_claims_handler"))]) == []
        assert self.v([mrow()], hp=HP.upper()) == []

    def test_inactive_row_need_not_except_ci_principal(self):
        assert self.v([mrow(active=False, except_principals=("gs_claims_handler",))]) == []

    def test_empty_hydration_principal_refused(self):
        assert self.v([mrow()], hp="") != []

    @pytest.mark.parametrize("bad", ["account users", "users", "", "Account Users"])
    def test_except_must_not_exempt_everyone_or_blank(self, bad):
        assert self.v([mrow(except_principals=(HP, bad))]) != []

    @pytest.mark.parametrize("bad", ["admins", "Admins", "ADMINS", " admins "])
    def test_except_must_not_exempt_admins(self, bad):
        # spec: admins and owners are filtered too; a workspace-admin group in EXCEPT would unmask them
        errs = self.v([mrow(except_principals=(HP, bad))])
        assert errs != [] and any("mk_raw_pii" in e for e in errs)

    def test_two_active_rows_same_schema_and_class_refused(self):
        assert self.v([mrow("a"), mrow("b")]) != []

    def test_two_active_rows_same_class_schema_case_insensitive(self):
        assert self.v([mrow("a", "cat.raw"), mrow("b", "cat.RAW")]) != []

    def test_same_schema_different_class_ok(self):
        conf = mrow("b", match_tag_value="confidential", function_name=FN_CONF)
        assert self.v([mrow("a"), conf]) == []

    def test_one_active_one_inactive_same_schema_and_class_ok(self):
        assert self.v([mrow("a"), mrow("b", active=False)]) == []

    def test_same_class_different_schemas_ok(self):
        assert self.v([mrow("a", RAW), mrow("b", CURATED)]) == []


# ---------------------------------------------------------------- mask_sql

class TestMaskSql:
    def test_body_is_exactly_the_matrix_mask_literal(self):
        literal = yaml.safe_load(MATRIX.read_text(encoding="utf-8"))["mask_literal"]
        assert body_of(mask_sql(FN)) == quote_str(literal)

    def test_shape(self):
        n = _norm(mask_sql(FN))
        assert n.startswith("CREATE OR REPLACE FUNCTION ")
        assert _norm(quote_table(FN) + "(v STRING)") in n
        assert "RETURNS STRING" in n

    def test_pure_no_table_or_identity(self):
        n = _norm(mask_sql(FN))
        for word in ("SELECT", "SESSION_USER", "CURRENT_USER", "IS_ACCOUNT_GROUP_MEMBER", "FROM "):
            assert word not in n

    def test_deterministic(self):
        assert mask_sql(FN) == mask_sql(FN)

    def test_backtick_escaped(self):
        assert quote_table("cat.gov.ma`sk") in mask_sql("cat.gov.ma`sk")


# ---------------------------------------------------------------- first run

class TestFirstRun:
    def test_empty_state_creates_function_and_policy(self):
        p = run([mrow()])
        assert kinds(p.actions) == ["function_set", "policy_set"]
        assert p.actions[0].object.lower() == FN.lower()
        assert p.actions[1].object == pname("mk_raw_pii")
        assert p.failures == () and p.warnings == ()

    def test_one_function_per_distinct_function(self):
        p = run([mrow("a", RAW), mrow("b", CURATED)])
        assert len(of(p.actions, "function_set")) == 1
        assert len(of(p.actions, "policy_set")) == 2

    def test_function_name_match_case_insensitive_for_distinct(self):
        p = run([mrow("a", RAW), mrow("b", CURATED, function_name="cat.GOV.mask_pii")])
        assert len(of(p.actions, "function_set")) == 1

    def test_one_function_per_class(self):
        conf = mrow("b", match_tag_value="confidential", function_name=FN_CONF)
        fns = {a.object.lower() for a in of(run([mrow("a"), conf]).actions, "function_set")}
        assert fns == {FN.lower(), FN_CONF.lower()}

    def test_inactive_row_creates_nothing(self):
        assert run([mrow(active=False)]).actions == []

    def test_no_rows_no_actions(self):
        assert run([]).actions == []

    def test_non_mask_rows_produce_nothing(self):
        assert run([rfrow()]).actions == []

    def test_invalid_rows_raise(self):
        with pytest.raises(ValueError):
            run([mrow(except_principals=("gs_claims_handler",))])
        with pytest.raises(ValueError):
            run([mrow("a"), mrow("b")])
        with pytest.raises(ValueError):
            run([mrow()], hydration_principal="")


class TestStatements:
    def test_policy_statement(self):
        r = mrow(except_principals=("gs_data_owner", HP, "gs_claims_handler"))
        (p,) = of(run([r]).actions, "policy_set")
        n = _norm(p.statement)
        assert n.startswith(_norm(f"CREATE OR REPLACE POLICY {quote_ident(pname(r.policy_id))} "
                                  f"ON SCHEMA {quote_table(RAW)}"))
        assert "COMMENT " in n
        assert _norm(f"COLUMN MASK {quote_table(FN)}") in n
        assert _norm("TO `account users`") in n
        excepts = ", ".join(quote_ident(x) for x in sorted({"gs_data_owner", HP, "gs_claims_handler"}))
        assert _norm(f"EXCEPT {excepts}") in n
        assert "FOR TABLES" in n
        assert _norm(f"MATCH COLUMNS has_tag_value({quote_str(SENS)}, {quote_str('pii')}) AS c") in n
        assert _norm("ON COLUMN c") in n

    def test_policy_clauses_in_order(self):
        n = _norm(of(run([mrow()]).actions, "policy_set")[0].statement)
        idx = [n.index(k) for k in ("ON SCHEMA", "COLUMN MASK", "TO ", "EXCEPT", "FOR TABLES", "MATCH COLUMNS",
                                    "ON COLUMN")]
        assert idx == sorted(idx)

    def test_function_statement_is_mask_sql(self):
        (f,) = of(run([mrow()]).actions, "function_set")
        assert f.statement == mask_sql(FN)

    def test_sensitivity_tag_follows_config(self):
        r = mrow(match_tag_key="xx_sensitivity")
        (p,) = of(run([r], sensitivity_tag="xx_sensitivity").actions, "policy_set")
        assert quote_str("xx_sensitivity") in p.statement and SENS not in p.statement

    def test_never_touches_tags(self):
        p = run([mrow()], [appr(TBL, "notes_txt")])
        for a in p.actions:
            assert "SET TAGS" not in _norm(a.statement) and "UNSET TAGS" not in _norm(a.statement)

    def test_drop_statement(self):
        stray = mrow("mk_old")
        p = run([], policies={pkey(stray): policy_state(stray)})
        (d,) = of(p.actions, "policy_drop")
        n = _norm(d.statement)
        assert "DROP POLICY" in n and quote_ident(pname("mk_old")) in d.statement
        assert quote_table(RAW) in d.statement


# ---------------------------------------------------------------- idempotency and drift

class TestIdempotent:
    def test_converged_state_is_noop(self):
        rows = [mrow("a", RAW), mrow("b", CURATED)]
        approved = [appr(TBL, "notes_txt"), appr(TBL2, "holder_nm"), appr(TBL, "amt", "amount",
                                                                         sensitivity="internal")]
        policies, functions = converged_objects(rows)
        p = run(rows, approved, policies=policies, functions=functions)
        assert p.actions == [] and p.failures == () and p.warnings == ()

    def test_same_input_same_plan(self):
        rows, approved = [mrow("a", RAW), mrow("b", CURATED)], [appr(TBL, "notes_txt")]
        assert run(rows, approved) == run(rows, approved)

    def test_joinup_new_pii_column_needs_no_mask_action(self):
        # C-AC5: hydration approves a column as pii; the schema policy already covers it
        rows = [mrow()]
        policies, functions = converged_objects(rows)
        before = run(rows, [appr(TBL, "loss_loc", "loss_location", sensitivity="confidential")],
                     policies=policies, functions=functions)
        after = run(rows, [appr(TBL, "loss_loc", "loss_location", sensitivity="pii")],
                    policies=policies, functions=functions)
        assert before.actions == [] and after.actions == []
        assert after.failures == ()

    def test_function_body_surrounding_whitespace_ignored(self):
        policies, functions = converged_objects([mrow()])
        messy = {k: "\n   " + v + "  \n" for k, v in functions.items()}
        assert run([mrow()], policies=policies, functions=messy).actions == []

    def test_changed_function_body_reemitted(self):
        policies, functions = converged_objects([mrow()])
        functions[FN.lower()] = "v"  # hand edit: unmasked
        a = run([mrow()], policies=policies, functions=functions).actions
        assert kinds(a) == ["function_set"] and a[0].object.lower() == FN.lower()

    def test_missing_function_reemitted(self):
        policies, _ = converged_objects([mrow()])
        assert kinds(run([mrow()], policies=policies).actions) == ["function_set"]

    def test_missing_policy_reemitted(self):
        _, functions = converged_objects([mrow()])
        assert kinds(run([mrow()], functions=functions).actions) == ["policy_set"]

    def test_except_list_order_and_case_ignored(self):
        r = mrow(except_principals=("gs_data_owner", HP))
        policies, functions = converged_objects([r])
        policies[pkey(r)]["except_principals"] = (HP.upper(), "GS_Data_Owner")
        assert run([r], policies=policies, functions=functions).actions == []

    def test_to_principals_and_securable_type_case_ignored(self):
        policies, functions = converged_objects([mrow()])
        policies[pkey(mrow())]["to_principals"] = ("Account Users",)
        policies[pkey(mrow())]["for_securable_type"] = "table"
        assert run([mrow()], policies=policies, functions=functions).actions == []

    @pytest.mark.parametrize("field,value", [
        ("securable", CURATED),
        ("function_name", "cat.gov.other_fn"),
        ("match_tag_key", RLS),
        ("match_tag_value", "confidential"),
        ("match_alias", "x"),
        ("on_column", "x"),
        ("to_principals", ("gs_analyst_uk",)),  # narrowed: everyone else unmasked
        ("to_principals", ("account users", "gs_analyst_uk")),
        ("to_principals", ()),
        ("for_securable_type", "SCHEMA"),
        ("except_principals", (HP, "gs_claims_handler", "gs_analyst_uk")),  # hand-widened
        ("except_principals", ("gs_claims_handler",)),  # CI SP removed by hand
    ])
    def test_policy_drift_reemitted(self, field, value):
        r = mrow()
        policies, functions = converged_objects([r])
        policies[pkey(r)][field] = value
        assert kinds(run([r], policies=policies, functions=functions).actions) == ["policy_set"]


# ---------------------------------------------------------------- drops

class TestDrops:
    def test_inactive_row_with_policy_dropped(self):
        r = mrow()
        policies, functions = converged_objects([r])
        a = run([mrow(active=False)], policies=policies, functions=functions).actions
        assert kinds(a) == ["policy_drop"] and a[0].object == pname(r.policy_id)

    def test_retire_rerun_is_noop(self):
        _, functions = converged_objects([mrow()])
        assert run([mrow(active=False)], functions=functions).actions == []

    def test_stray_prefixed_policy_dropped(self):
        policies, functions = converged_objects([mrow()])
        stray = mrow("mk_stray")
        policies[pkey(stray)] = policy_state(stray)
        a = run([mrow()], policies=policies, functions=functions).actions
        assert kinds(a) == ["policy_drop"] and a[0].object == pname("mk_stray")

    def test_row_moved_schema_creates_new_and_drops_old(self):
        r = mrow()
        _, functions = converged_objects([r])
        old = {(CURATED, pname(r.policy_id)): policy_state(mrow(securable=CURATED))}
        a = run([r], policies=old, functions=functions).actions
        assert kinds(a) == ["policy_set", "policy_drop"]
        assert quote_table(CURATED) in a[1].statement

    def test_other_policies_never_touched(self):
        policies, functions = converged_objects([mrow()])
        policies[(RAW, policy_name("rf_raw", PREFIX))] = policy_state(mrow("rf_raw"))  # gg_rls_*
        policies[(RAW, "hand_made_mask")] = policy_state(mrow("hm"))
        policies[(CURATED, "xx_mask_other_team")] = policy_state(mrow("xx", CURATED))
        assert run([mrow()], policies=policies, functions=functions).actions == []

    def test_prefix_follows_config(self):
        stray = mrow("mk_stray")
        policies = {(RAW, mask_policy_name("mk_stray", "xx")): policy_state(stray),
                    pkey(stray): policy_state(stray)}
        a = run([], policies=policies, tag_prefix="xx").actions
        assert [x.object for x in a] == [mask_policy_name("mk_stray", "xx")]

    def test_functions_never_dropped(self):
        _, functions = converged_objects([mrow()])
        a = run([mrow(active=False)], functions=functions).actions
        assert all("DROP FUNCTION" not in _norm(x.statement) for x in a)


# ---------------------------------------------------------------- failures and warnings

class TestFailures:
    def test_pii_column_in_unmasked_schema_fails(self):
        p = run([mrow("a", RAW)], [appr(TBL2, "holder_nm")])
        assert len(p.failures) == 1 and "holder_nm" in p.failures[0].lower()

    def test_pii_column_where_mask_row_inactive_fails(self):
        assert run([mrow(active=False)], [appr(TBL, "notes_txt")]).failures != ()

    def test_pii_column_with_no_rows_at_all_fails(self):
        assert run([], [appr(TBL, "notes_txt")]).failures != ()

    def test_pii_column_in_masked_schema_ok(self):
        assert run([mrow()], [appr(TBL, "notes_txt")]).failures == ()

    def test_schema_match_case_insensitive(self):
        assert run([mrow(securable="cat.RAW")], [appr("cat.raw.Claims", "notes_txt")]).failures == ()

    @pytest.mark.parametrize("sensitivity", ["public", "internal", "confidential"])
    def test_only_pii_is_default_deny(self, sensitivity):
        assert run([], [appr(TBL, "c", "t", sensitivity=sensitivity)]).failures == ()

    def test_pii_mask_does_not_cover_other_class_mask_needed(self):
        # a confidential mask on the schema is not a pii mask
        conf = mrow("mk_conf", match_tag_value="confidential", function_name=FN_CONF)
        assert run([conf], [appr(TBL, "notes_txt")]).failures != ()

    def test_masked_rls_input_column_fails(self):
        # a masked column cannot be the row-filter input: the filter would compare against the mask literal
        p = run([mrow()], [appr(TBL, "rgn_cd", TERM_ID, sensitivity="pii")], rls_rows=[rfrow()])
        assert len(p.failures) >= 1 and any("rgn_cd" in f.lower() for f in p.failures)

    def test_rls_input_in_unmasked_class_ok(self):
        p = run([mrow()], [appr(TBL, "rgn_cd", TERM_ID, sensitivity="internal")], rls_rows=[rfrow()])
        assert p.failures == ()

    def test_rls_input_check_needs_active_rls_row(self):
        p = run([mrow()], [appr(TBL, "rgn_cd", TERM_ID, sensitivity="pii")], rls_rows=[rfrow(active=False)])
        assert p.failures == ()
        assert run([mrow()], [appr(TBL, "rgn_cd", TERM_ID, sensitivity="pii")]).failures == ()

    def test_rls_input_check_only_for_rls_term(self):
        p = run([mrow()], [appr(TBL, "rgn_cd", "country_code", sensitivity="pii")], rls_rows=[rfrow()])
        assert p.failures == ()

    def test_rls_term_id_is_config(self):
        p = run([mrow()], [appr(TBL, "lob", "line_of_business")], rls_rows=[rfrow()],
                rls_term_id="line_of_business")
        assert p.failures != ()

    def test_failures_deterministic_and_sorted_stable(self):
        approved = [appr(TBL2, "b_col"), appr(TBL2, "a_col")]
        assert run([mrow()], approved).failures == run([mrow()], list(reversed(approved))).failures


class TestWarnings:
    def test_non_string_masked_column_warns(self):
        approved = [appr(TBL, "dob")]
        p = run([mrow()], approved, column_types={(TBL, "dob"): "DATE"})
        assert len(p.warnings) == 1 and "dob" in p.warnings[0].lower()
        assert p.failures == ()

    def test_string_type_case_insensitive(self):
        assert run([mrow()], [appr(TBL, "notes_txt")], column_types={(TBL, "notes_txt"): "string"}).warnings == ()

    def test_unmasked_class_non_string_no_warning(self):
        p = run([mrow()], [appr(TBL, "amt", "amount", sensitivity="internal")], column_types={(TBL, "amt"): "INT"})
        assert p.warnings == ()

    def test_other_masked_class_warns(self):
        conf = mrow("mk_conf", match_tag_value="confidential", function_name=FN_CONF)
        p = run([mrow(), conf], [appr(TBL, "amt", "amount", sensitivity="confidential")],
                column_types={(TBL, "amt"): "DECIMAL(10,2)"})
        assert len(p.warnings) == 1

    def test_column_types_key_case_insensitive(self):
        approved = [appr("cat.raw.Claims", "DOB")]
        assert run([mrow()], approved, column_types={(TBL, "dob"): "DATE"}).warnings != ()

    def test_column_types_is_required(self):
        with pytest.raises(TypeError):
            plan_masks([mrow()], [], {}, {}, catalog=CAT, sensitivity_tag=SENS, rls_term_id=TERM_ID,  # type: ignore[call-arg]
                       rls_rows=[], hydration_principal=HP, gov_schema=GOV, tag_prefix=PREFIX)


# ---------------------------------------------------------------- order and events

class TestOrderAndEvents:
    def everything(self, last_events=None) -> list[Action]:
        rows = [mrow("a", RAW), mrow("old", CURATED, active=False)]
        policies = {pkey(rows[1]): policy_state(rows[1])}
        return run(rows, policies=policies, last_events=last_events).actions

    def test_kinds_ordered(self):
        a = self.everything()
        assert kinds(a) == KIND_ORDER

    def test_event_ids_deterministic_and_unique(self):
        a, b = self.everything(), self.everything()
        assert [x.event_id for x in a] == [x.event_id for x in b]
        ids = [x.event_id for x in a]
        assert len(ids) == len(set(ids)) and all(isinstance(i, str) and i for i in ids)

    def test_policy_change_has_distinct_id_per_definition(self):
        (p1,) = of(run([mrow()]).actions, "policy_set")
        (p2,) = of(run([mrow(except_principals=(HP, "gs_data_owner"))]).actions, "policy_set")
        assert p1.event_id != p2.event_id

    def test_crash_replay_same_ids(self):
        first = self.everything()
        last = {a.object.lower(): f"ev-{i}" for i, a in enumerate(first)}
        assert [x.event_id for x in self.everything(last)] == [x.event_id for x in self.everything(dict(last))]
        assert [x.event_id for x in self.everything({})] == [x.event_id for x in first]

    def test_every_action_id_includes_its_last_event(self):
        base = self.everything()
        for act in base:
            again = {(x.kind, x.object): x.event_id for x in self.everything({act.object.lower(): "ev-prev"})}
            for x in base:
                if (x.kind, x.object) == (act.kind, act.object):
                    assert again[(x.kind, x.object)] != x.event_id, x.kind
                elif x.object.lower() != act.object.lower():
                    assert again[(x.kind, x.object)] == x.event_id, (act.kind, x.kind)

    def test_unrelated_last_events_change_nothing(self):
        assert self.everything() == self.everything({"cat.other.t.c": "ev-x"})

    def test_policy_reset_after_drift_new_id(self):
        r = mrow()
        policies, functions = converged_objects([r])
        (p1,) = of(run([r], functions=functions).actions, "policy_set")
        policies[pkey(r)]["except_principals"] = (HP, "gs_claims_handler", "gs_analyst_uk")
        (p2,) = of(run([r], policies=policies, functions=functions,
                       last_events={p1.object.lower(): p1.event_id}).actions, "policy_set")
        assert p2.event_id != p1.event_id


# ---------------------------------------------------------------- tampered

class TestTampered:
    def test_clean_rows(self):
        rows = [mrow(), rfrow()]
        assert tampered(rows, {r.policy_id: row_dict(r) for r in rows}) == []

    def test_clean_after_json_round_trip(self):
        r = mrow(except_principals=("gs_data_owner", HP))
        assert tampered([r], {r.policy_id: json.loads(json.dumps(row_dict(r)))}) == []

    def test_hand_widened_except(self):
        audited = mrow()
        stored = mrow(except_principals=(*audited.except_principals, "gs_analyst_uk"))
        assert tampered([stored], {audited.policy_id: row_dict(audited)}) == [audited.policy_id]

    def test_hand_reactivated(self):
        audited = mrow(active=False)
        assert tampered([mrow()], {audited.policy_id: row_dict(audited)}) == [audited.policy_id]

    def test_no_audit_after(self):
        assert tampered([mrow()], {}) == ["mk_raw_pii"]
        assert tampered([mrow()], {"mk_raw_pii": None}) == ["mk_raw_pii"]

    def test_any_policy_type(self):
        r = rfrow()
        assert tampered([r], {}) == [r.policy_id]

    def test_sorted(self):
        rows = [mrow("zz", CURATED), mrow("aa", RAW)]
        assert tampered(rows, {}) == ["aa", "zz"]


# ---------------------------------------------------------------- review fixes

class TestReviewFixes:
    def test_mask_function_may_not_be_a_row_filter_function_or_helper(self):
        from gates.consumption.masks import function_clashes
        rf = rfrow()
        for fn in (rf.function_name, rf.function_name + "_helper", rf.function_name.upper()):
            assert function_clashes([mrow(function_name=fn), rf]) != []
        assert function_clashes([mrow(), rf]) == []

    def test_plan_masks_raises_on_function_clash(self):
        with pytest.raises(ValueError):
            run([mrow(function_name="cat.gov.rls_region_filter")], rls_rows=[rfrow()])

    def test_one_function_per_class(self):
        a, b = mrow("a", RAW), mrow("b", CURATED, function_name="cat.gov.mask_pii_2")
        assert validate_mask_rows([a, b], catalog=CAT, sensitivity_tag=SENS, hydration_principal=HP,
                                  gov_schema=GOV) != []

    def test_two_classes_may_not_share_a_function(self):
        a, b = mrow("a"), mrow("b", match_tag_value="confidential")  # both FN
        assert validate_mask_rows([a, b], catalog=CAT, sensitivity_tag=SENS, hydration_principal=HP,
                                  gov_schema=GOV) != []

    def test_deleted_audited_row_is_tampered(self):
        after = {"gone": row_dict(mrow("gone")), "mk_raw_pii": row_dict(mrow())}
        assert tampered([mrow()], after) == ["gone"]

    def test_retyped_audited_row_is_tampered(self):
        # hand SQL changed policy_type so the mask/row_filter readers no longer see it: same as deleted
        after = {"mk_raw_pii": row_dict(mrow())}
        assert tampered([], after) == ["mk_raw_pii"]

    def test_audited_rows_of_other_types_are_not_expected(self):
        g = row_dict(mrow("g1", policy_type="grant"))
        assert tampered([], {"g1": g}) == []

    def test_on_failure_only_masking_actions_survive(self):
        from gates.consumption.masks import safe_on_failure
        policies, functions = converged_objects([mrow()])
        stray = mrow("mk_stray")
        policies[pkey(stray)] = policy_state(stray)
        functions[FN.lower()] = "v"  # hand-edited body: unmasked
        p = run([mrow()], [appr(TBL2, "holder_nm")], policies=policies, functions=functions)
        assert p.failures != ()
        assert {a.kind for a in p.actions} == {"function_set", "policy_drop"}
        assert [a.kind for a in safe_on_failure(p.actions)] == ["function_set"]

    def test_on_failure_policy_set_also_survives(self):
        from gates.consumption.masks import safe_on_failure
        _, functions = converged_objects([mrow()])
        p = run([mrow()], [appr(TBL2, "holder_nm")], policies={}, functions=functions)  # policy missing
        assert p.failures != ()
        assert [a.kind for a in safe_on_failure(p.actions)] == ["policy_set"]
