"""Access-model adapter (specs/consumption.md: "Agreements and the access model", "Policy model"). Pure: no Spark.

The access model says WHO is excepted from a mask / row filter, never WHETHER one exists. Existence comes from config
scope (the `schemas` list, bundle var grant_schemas). A removed entitlement shrinks an EXCEPT list or deletes a
user_attributes row; the adapter never retires a mask/row_filter row. The hydration principal is in every EXCEPT.

The module under test is imported lazily (per test) so a missing module fails each test with ImportError rather than
aborting collection of the whole unit suite.
"""

from __future__ import annotations

import importlib
import textwrap
from pathlib import Path

import pytest

from gates.consumption.row_filter import PolicyRow
from gates.hydration.apply import event_id_of


class _Lazy:
    def __getattr__(self, name):
        return getattr(importlib.import_module("gates.consumption.access_model"), name)


am = _Lazy()

STUB = Path(__file__).resolve().parents[2] / "config" / "access_model.example.yaml"
CAT, GOV, HP = "cat", "gov", "ci-sp"
SENS, RLS = "gg_sensitivity", "gg_rls_attribute"
DR = dict(catalog=CAT, gov_schema=GOV, sensitivity_tag=SENS, rls_tag=RLS, hydration_principal=HP)
RC = dict(catalog=CAT, gov_schema=GOV, sensitivity_tag=SENS, rls_tag=RLS, hydration_principal=HP)
STUB_GROUPS = ("gs_analyst_global", "gs_analyst_uk", "gs_claims_handler", "gs_data_owner", "gs_no_access")


def y(s: str) -> str:
    return textwrap.dedent(s).lstrip()


def stub_text() -> str:
    return STUB.read_text(encoding="utf-8")


def stub():
    return am.load_model(stub_text())


def model_with(groups_yaml: str, *, masked: str = "[pii]", isolated: str = "[]", allowed=("pii",)):
    body = textwrap.indent(textwrap.dedent(groups_yaml).strip(), "  ")
    return am.load_model(f"version: 1\nmasked_classes: {masked}\ngroups:\n{body}\nisolated_members: {isolated}\n",
                         allowed_classes=tuple(allowed))


def load_err(text: str) -> str:
    with pytest.raises(ValueError) as ei:
        am.load_model(text)
    return str(ei.value)


def rf(policy_id="rf_raw", securable="cat.raw", fn="cat.gov.rls_region_filter", excepts=(HP,), active=True):
    return PolicyRow(policy_id=policy_id, policy_type="row_filter", securable=securable, match_tag_key=RLS,
                     match_tag_value="region", function_name=fn, except_principals=excepts, active=active)


def mk(policy_id="mk_raw_pii", securable="cat.raw", cls="pii", fn="cat.gov.mask_pii", excepts=(HP,), active=True):
    return PolicyRow(policy_id=policy_id, policy_type="mask", securable=securable, match_tag_key=SENS,
                     match_tag_value=cls, function_name=fn, except_principals=excepts, active=active)


def grant(policy_id="gr_ag-0001"):
    return PolicyRow(policy_id=policy_id, policy_type="grant", securable="cat.raw", match_tag_key="",
                     match_tag_value="", function_name="", except_principals=("gs_x",), active=True)


STORED = [rf(), mk()]


def by_id(rows) -> dict[str, PolicyRow]:
    return {r.policy_id: r for r in rows}


# ================================================================ load_model: the stub

class TestLoadStub:
    def test_stub_loads(self):
        m = stub()
        assert m.version == 1
        assert m.masked_classes == ("pii",)
        assert tuple(g.name for g in m.groups) == STUB_GROUPS  # sorted by name
        assert m.isolated_members == ("proxied_analyst_sp",)

    def test_stub_pii_visible_on_raw_for_claims_handler_and_owner(self):
        g = {g.name: g for g in stub().groups}
        assert g["gs_claims_handler"].unmasked == {"raw": ("pii",)}
        assert g["gs_data_owner"].unmasked == {"raw": ("pii",)}
        for name in ("gs_analyst_uk", "gs_analyst_global", "gs_no_access"):
            assert g[name].unmasked == {}

    def test_stub_regions_and_members(self):
        g = {g.name: g for g in stub().groups}
        assert g["gs_no_access"].regions == ("NONE",)
        assert g["gs_analyst_uk"].regions == ("UK",)
        assert g["gs_analyst_global"].regions == ("ALL",)
        assert g["gs_data_owner"].regions == ("ALL",)
        assert g["gs_claims_handler"].members == ("claims_handler_sp",)
        assert all(x.line_of_business is None for x in g.values())

    def test_load_is_deterministic(self):
        assert stub() == stub()


# ================================================================ load_model: shapes and defaults

class TestLoadShapes:
    def test_optional_keys_default(self):
        m = am.load_model(y("""
            version: 1
            masked_classes: [pii]
            groups:
              gs_a:
                regions: [UK]
        """))
        (g,) = m.groups
        assert g.name == "gs_a"
        assert g.members == ()
        assert g.line_of_business is None
        assert g.unmasked == {}
        assert m.isolated_members == ()

    def test_regions_list_sorted_and_deduped(self):
        m = model_with("""
            gs_a:
              regions: [US, UK, US, AU]
        """)
        assert m.groups[0].regions == ("AU", "UK", "US")

    @pytest.mark.parametrize("value", ["ALL", "NONE"])
    def test_regions_string_all_or_none(self, value):
        m = model_with(f"""
            gs_a:
              regions: {value}
        """)
        assert m.groups[0].regions == (value,)

    def test_unmasked_classes_sorted(self):
        m = model_with("""
            gs_a:
              regions: [UK]
              schemas:
                raw: {unmasked: [pii, confidential]}
        """, masked="[pii, confidential]", allowed=("confidential", "pii"))
        assert m.groups[0].unmasked == {"raw": ("confidential", "pii")}

    def test_groups_sorted_by_name_and_tuples(self):
        m = model_with("""
            gs_b: {regions: [UK], members: [b2, b1]}
            gs_a: {regions: [UK], members: [a1]}
        """)
        assert [g.name for g in m.groups] == ["gs_a", "gs_b"]
        assert isinstance(m.groups, tuple) and isinstance(m.groups[1].members, tuple)
        assert set(m.groups[1].members) == {"b1", "b2"}

    def test_line_of_business_kept(self):
        m = model_with("""
            gs_a: {regions: [UK], line_of_business: motor}
        """)
        assert m.groups[0].line_of_business == "motor"


# ================================================================ load_model: errors

class TestLoadErrors:
    @pytest.mark.parametrize("text", ["- a\n- b\n", "just a string\n", "42\n"])
    def test_not_a_mapping(self, text):
        assert "mapping" in load_err(text).lower()

    @pytest.mark.parametrize("v", [0, 2, '"1"'])
    def test_version_must_be_1(self, v):
        assert "version" in load_err(f"version: {v}\nmasked_classes: [pii]\ngroups: {{}}\n")

    def test_unknown_top_level_key(self):
        assert "extras" in load_err("version: 1\nmasked_classes: [pii]\ngroups: {}\nextras: 1\n")

    def test_unknown_group_key(self):
        assert "rolez" in load_err(y("""
            version: 1
            masked_classes: [pii]
            groups:
              gs_a: {regions: [UK], rolez: [x]}
        """))

    @pytest.mark.parametrize("cls", ["secret", "PII_X", "restricted"])
    def test_masked_class_not_a_sensitivity(self, cls):
        assert cls in load_err(f"version: 1\nmasked_classes: [{cls}]\ngroups: {{}}\n")

    def test_masked_class_empty_item(self):
        assert "masked_classes" in load_err('version: 1\nmasked_classes: [""]\ngroups: {}\n')

    @pytest.mark.parametrize("name", ["account users", "Account Users", "users", "USERS", "admins", "Admins"])
    def test_forbidden_group_name(self, name):
        msg = load_err(f'version: 1\nmasked_classes: [pii]\ngroups:\n  "{name}": {{regions: [UK]}}\n')
        assert name in msg

    def test_empty_group_name(self):
        msg = load_err('version: 1\nmasked_classes: [pii]\ngroups:\n  "": {regions: [UK]}\n')
        assert "name" in msg.lower()

    @pytest.mark.parametrize("regions", ['["UK", "ALL"]', '["XX"]', "[]", "SOME", '["uk"]', '"all"'])
    def test_bad_regions(self, regions):
        msg = load_err(f"version: 1\nmasked_classes: [pii]\ngroups:\n  gs_a: {{regions: {regions}}}\n")
        assert "regions" in msg and "gs_a" in msg

    @pytest.mark.parametrize("key", ["cat.raw", "raw.claims", "cat.raw.claims"])
    def test_schema_key_with_dot_rejected(self, key):
        msg = load_err(y(f"""
            version: 1
            masked_classes: [pii]
            groups:
              gs_a:
                regions: [UK]
                schemas:
                  "{key}": {{unmasked: [pii]}}
        """))
        assert key in msg

    def test_unknown_key_under_schema_entry(self):
        assert "masked_too" in load_err(y("""
            version: 1
            masked_classes: [pii]
            groups:
              gs_a:
                regions: [UK]
                schemas:
                  raw: {unmasked: [pii], masked_too: [pii]}
        """))

    def test_unmasked_class_not_in_masked_classes(self):
        assert "confidential" in load_err(y("""
            version: 1
            masked_classes: [pii]
            groups:
              gs_a:
                regions: [UK]
                schemas:
                  raw: {unmasked: [confidential]}
        """))

    def test_regions_none_with_unmasked_entry(self):
        msg = load_err(y("""
            version: 1
            masked_classes: [pii]
            groups:
              gs_none:
                regions: NONE
                schemas:
                  raw: {unmasked: [pii]}
        """))
        assert "gs_none" in msg and "NONE" in msg

    def test_isolated_member_also_in_group(self):
        assert "proxied_sp" in load_err(y("""
            version: 1
            masked_classes: [pii]
            groups:
              gs_a: {regions: [UK], members: [a_sp, proxied_sp]}
            isolated_members: [proxied_sp]
        """))

    def test_every_error_is_listed(self):
        msg = load_err(y("""
            version: 1
            masked_classes: [pii]
            extras: 1
            groups:
              admins: {regions: [UK]}
              gs_a: {regions: [XX], rolez: 1}
        """))
        for part in ("extras", "admins", "rolez", "regions"):
            assert part in msg


# ================================================================ desired_rows

def desired(model, schemas=("raw",), stored=STORED, **kw):
    return am.desired_rows(model, schemas=list(schemas), stored=list(stored), **{**DR, **kw})


def stub_without_claims_raw():
    return am.load_model(stub_text().replace("    schemas:\n      raw: {unmasked: [pii]}\n  gs_data_owner:",
                                             "    schemas: {}\n  gs_data_owner:"))


EMPTY_GROUPS = y("""
    version: 1
    masked_classes: [pii]
    groups:
      gs_a: {regions: [UK], members: [a_sp], schemas: {}}
      gs_b: {regions: ALL, members: [b_sp], schemas: {}}
""")


class TestDesiredRows:
    def test_stub_against_stored_adopts_ids(self):
        out = by_id(desired(stub()))
        assert set(out) == {"rf_raw", "mk_raw_pii"}
        assert out["rf_raw"] == rf()  # unchanged
        m = out["mk_raw_pii"]
        assert m.active is True and m.function_name == "cat.gov.mask_pii" and m.securable == "cat.raw"
        assert m.match_tag_key == SENS and m.match_tag_value == "pii"
        assert set(m.except_principals) == {HP, "gs_claims_handler", "gs_data_owner"}
        assert m.except_principals == tuple(sorted({HP, "gs_claims_handler", "gs_data_owner"}))

    def test_fixture_helper_really_removes_claims_raw(self):
        g = {g.name: g for g in stub_without_claims_raw().groups}
        assert g["gs_claims_handler"].unmasked == {} and g["gs_data_owner"].unmasked == {"raw": ("pii",)}

    def test_removed_entitlement_shrinks_except_never_retires(self):
        out = by_id(desired(stub_without_claims_raw()))
        m = out["mk_raw_pii"]
        assert m.active is True
        assert m.except_principals == tuple(sorted({HP, "gs_data_owner"}))
        assert out["rf_raw"].active is True

    def test_no_entitlements_still_yields_active_rows_with_ci_only(self):
        out = by_id(desired(am.load_model(EMPTY_GROUPS)))
        assert set(out) == {"rf_raw", "mk_raw_pii"}
        for r in out.values():
            assert r.active is True and r.except_principals == (HP,)

    def test_new_ids_and_functions_when_nothing_stored(self):
        out = by_id(desired(stub(), schemas=["raw", "curated"], stored=[]))
        assert set(out) == {"am_rf_raw", "am_rf_curated", "am_mask_raw_pii", "am_mask_curated_pii"}
        assert out["am_rf_raw"].function_name == "cat.gov.rls_region_filter_raw"
        assert out["am_rf_curated"].function_name == "cat.gov.rls_region_filter_curated"
        assert out["am_mask_raw_pii"].function_name == "cat.gov.mask_pii"
        assert out["am_mask_curated_pii"].function_name == "cat.gov.mask_pii"
        assert out["am_rf_curated"].securable == "cat.curated"
        # gs_claims_handler / gs_data_owner unmask pii only on raw
        assert out["am_mask_curated_pii"].except_principals == (HP,)
        assert set(out["am_mask_raw_pii"].except_principals) == {HP, "gs_claims_handler", "gs_data_owner"}
        assert all(r.active for r in out.values())

    def test_row_filter_shape(self):
        r = by_id(desired(stub(), stored=[]))["am_rf_raw"]
        assert (r.policy_type, r.securable, r.match_tag_key, r.match_tag_value, r.except_principals) == \
            ("row_filter", "cat.raw", RLS, "region", (HP,))

    def test_one_mask_per_masked_class(self):
        m = model_with("""
            gs_a:
              regions: [UK]
              schemas:
                raw: {unmasked: [confidential]}
        """, masked="[pii, confidential]", allowed=("confidential", "pii"))
        # masked classes come from config (desired_rows' masked_classes), never from the model
        out = by_id(desired(m, stored=[], masked_classes=("pii", "confidential")))
        assert set(out) == {"am_rf_raw", "am_mask_raw_pii", "am_mask_raw_confidential"}
        assert out["am_mask_raw_confidential"].function_name == "cat.gov.mask_confidential"
        assert out["am_mask_raw_confidential"].match_tag_value == "confidential"
        assert set(out["am_mask_raw_confidential"].except_principals) == {HP, "gs_a"}
        assert out["am_mask_raw_pii"].except_principals == (HP,)

    def test_entitlement_on_out_of_scope_schema_creates_nothing(self):
        m = model_with("""
            gs_a:
              regions: [UK]
              schemas:
                curated: {unmasked: [pii]}
        """)
        out = by_id(desired(m, schemas=["raw"], stored=[]))
        assert set(out) == {"am_rf_raw", "am_mask_raw_pii"}
        assert out["am_mask_raw_pii"].except_principals == (HP,)

    def test_no_schemas_in_scope_no_rows(self):
        assert desired(stub(), schemas=[], stored=STORED) == []

    def test_inactive_stored_row_not_adopted(self):
        out = by_id(desired(stub(), stored=[rf(active=False), mk(active=False)]))
        assert set(out) == {"am_rf_raw", "am_mask_raw_pii"}

    def test_two_active_stored_rows_not_adopted(self):
        stored = [rf(), rf("rf_raw_2"), mk(), mk("mk_raw_pii_2")]
        out = by_id(desired(stub(), stored=stored))
        assert set(out) == {"am_rf_raw", "am_mask_raw_pii"}

    def test_mask_adoption_is_per_class(self):
        stored = [rf(), mk(cls="confidential", policy_id="mk_raw_conf", fn="cat.gov.mask_confidential")]
        out = by_id(desired(stub(), stored=stored))
        assert set(out) == {"rf_raw", "am_mask_raw_pii"}

    def test_adoption_keeps_stored_function_name(self):
        stored = [rf(fn="cat.gov.rls_custom"), mk(fn="cat.gov.mask_pii_v2")]
        out = by_id(desired(stub(), stored=stored))
        assert out["rf_raw"].function_name == "cat.gov.rls_custom"
        assert out["mk_raw_pii"].function_name == "cat.gov.mask_pii_v2"

    def test_adoption_ignores_rows_on_other_schemas(self):
        stored = [rf("rf_cur", "cat.curated"), mk("mk_cur_pii", "cat.curated")]
        out = by_id(desired(stub(), schemas=["raw"], stored=stored))
        assert set(out) == {"am_rf_raw", "am_mask_raw_pii"}

    def test_grant_rows_ignored(self):
        out = by_id(desired(stub(), stored=STORED + [grant()]))
        assert set(out) == {"rf_raw", "mk_raw_pii"}

    def test_deterministic(self):
        assert desired(stub()) == desired(stub())

    @pytest.mark.parametrize("case", ["stub", "removed", "empty", "fresh_two_schemas"])
    def test_result_passes_rows_change_validation(self, case):
        from gates.consumption.policy_rows import row_dict, rows_change
        model = {"stub": stub, "removed": stub_without_claims_raw, "empty": lambda: am.load_model(EMPTY_GROUPS),
                 "fresh_two_schemas": stub}[case]()
        schemas = ["raw", "curated"] if case == "fresh_two_schemas" else ["raw"]
        rows = desired(model, schemas=schemas)
        rows_change([row_dict(r) for r in STORED], rows, last_event_ids={}, allow_reserved=True, **RC)


# ================================================================ plan_rows

class TestPlanRows:
    def test_equal_is_noop(self):
        p = am.plan_rows([rf(), mk()], [rf(), mk()])
        assert p.writes == [] and p.failures == ()

    def test_except_order_ignored(self):
        p = am.plan_rows([mk(excepts=(HP, "gs_a"))], [mk(excepts=("gs_a", HP))])
        assert p.writes == []

    def test_changed_and_new_rows_written_sorted(self):
        want = [rf("am_rf_cur", "cat.curated", fn="cat.gov.rls_region_filter_curated"),
                mk(excepts=(HP, "gs_a")), rf()]
        p = am.plan_rows(want, [rf(), mk()])
        assert [r.policy_id for r in p.writes] == ["am_rf_cur", "mk_raw_pii"]
        assert p.failures == ()

    def test_stub_pipeline_writes_one_mask(self):
        p = am.plan_rows(desired(stub()), STORED)
        assert [r.policy_id for r in p.writes] == ["mk_raw_pii"]
        assert set(p.writes[0].except_principals) == {HP, "gs_claims_handler", "gs_data_owner"}
        assert p.failures == ()

    def test_idempotent_after_applying_writes(self):
        stored = list(STORED)
        p = am.plan_rows(desired(stub()), stored)
        applied = {**by_id(stored), **by_id(p.writes)}
        stored2 = list(applied.values())
        assert am.plan_rows(desired(stub(), stored=stored2), stored2).writes == []

    @pytest.mark.parametrize("orphan", [mk("mk_cur_pii", "cat.curated"), rf("rf_cur", "cat.curated"),
                                        mk("mk_raw_pii_2"), rf("rf_raw_2")])
    def test_active_stored_row_not_desired_is_a_failure_never_retired(self, orphan):
        p = am.plan_rows([rf(), mk()], [rf(), mk(), orphan])
        assert any(orphan.policy_id in f for f in p.failures)
        assert all(r.policy_id != orphan.policy_id for r in p.writes)
        assert all(r.active for r in p.writes)

    def test_inactive_stored_row_not_desired_ignored(self):
        p = am.plan_rows([rf(), mk()], [rf(), mk(), mk("mk_old", active=False), rf("rf_old", active=False)])
        assert p.writes == [] and p.failures == ()

    def test_grant_rows_ignored(self):
        p = am.plan_rows([rf(), mk()], [rf(), mk(), grant()])
        assert p.writes == [] and p.failures == ()

    def test_two_active_stored_rows_both_reported(self):
        stored = [rf(), rf("rf_raw_2"), mk()]
        p = am.plan_rows(desired(stub(), stored=stored), stored)
        joined = " ".join(p.failures)
        assert "rf_raw" in joined and "rf_raw_2" in joined


# ================================================================ user attributes

TODAY = [("gs_analyst_uk", "UK"), ("gs_analyst_global", "ALL"), ("gs_claims_handler", "UK"),
         ("gs_data_owner", "ALL"), ("gs_no_access", "NONE")]


def ua(principal, region, lob=None):
    return am.UserAttr(principal=principal, region=region, line_of_business=lob)


def today():
    return [ua(p, r) for p, r in TODAY]


def audited_all():
    return {(p, r): f"ev-{p}-{r}" for p, r in TODAY}


def as_dict(u) -> dict:
    return {"principal": u.principal, "region": u.region, "line_of_business": u.line_of_business}


def keys(changes):
    return sorted((c.kind, c.principal, c.region) for c in changes)


class TestDesiredUserAttributes:
    def test_stub(self):
        out = am.desired_user_attributes(stub())
        assert out == sorted(today(), key=lambda u: (u.principal, u.region))

    def test_one_row_per_region(self):
        m = model_with("""
            gs_a: {regions: [US, UK], line_of_business: motor}
        """)
        assert am.desired_user_attributes(m) == [ua("gs_a", "UK", "motor"), ua("gs_a", "US", "motor")]

    def test_sorted(self):
        out = am.desired_user_attributes(stub())
        assert out == sorted(out, key=lambda u: (u.principal, u.region))


class TestPlanUserAttributes:
    def test_stub_vs_today_unaudited_adopts_all(self):
        ch = am.plan_user_attributes(am.desired_user_attributes(stub()), today(), {})
        assert keys(ch) == sorted(("adopt", p, r) for p, r in TODAY)
        for c in ch:
            assert c.before == c.after == {"principal": c.principal, "region": c.region, "line_of_business": None}

    def test_stub_vs_today_audited_is_noop(self):
        assert am.plan_user_attributes(am.desired_user_attributes(stub()), today(), audited_all()) == []

    def test_insert(self):
        (c,) = am.plan_user_attributes([ua("gs_a", "UK")], [], {})
        assert (c.kind, c.principal, c.region, c.before, c.after) == \
            ("insert", "gs_a", "UK", None, as_dict(ua("gs_a", "UK")))

    def test_delete(self):
        (c,) = am.plan_user_attributes([], [ua("gs_a", "UK")], {("gs_a", "UK"): "ev-1"})
        assert (c.kind, c.before, c.after) == ("delete", as_dict(ua("gs_a", "UK")), None)

    def test_delete_of_unaudited_row(self):
        (c,) = am.plan_user_attributes([], [ua("gs_a", "UK")], {})
        assert c.kind == "delete"

    def test_update_line_of_business(self):
        (c,) = am.plan_user_attributes([ua("gs_a", "UK", "motor")], [ua("gs_a", "UK", None)], {("gs_a", "UK"): "e"})
        assert (c.kind, c.before["line_of_business"], c.after["line_of_business"]) == ("update", None, "motor")

    def test_removing_claims_handler_region(self):
        text = stub_text().replace("  gs_claims_handler:\n    members: [claims_handler_sp]\n    regions: [UK]\n",
                                   "  gs_claims_handler:\n    members: [claims_handler_sp]\n    regions: NONE\n")
        text = text.replace("    schemas:\n      raw: {unmasked: [pii]}\n  gs_data_owner:",
                            "    schemas: {}\n  gs_data_owner:")  # NONE may not unmask
        ch = am.plan_user_attributes(am.desired_user_attributes(am.load_model(text)), today(), audited_all())
        assert keys(ch) == [("delete", "gs_claims_handler", "UK"), ("insert", "gs_claims_handler", "NONE")]

    def test_event_id_formula(self):
        last = {("gs_a", "UK"): "ev-9"}
        (c,) = am.plan_user_attributes([ua("gs_a", "UK", "motor")], [ua("gs_a", "UK")], last)
        assert c.event_id == event_id_of("user_attributes_set", "gs_a", "UK", c.before, c.after, "ev-9")
        (n,) = am.plan_user_attributes([ua("gs_b", "UK")], [], {})
        assert n.event_id == event_id_of("user_attributes_set", "gs_b", "UK", None, n.after, None)

    def test_event_id_deterministic_and_chained(self):
        a = am.plan_user_attributes([], [ua("gs_a", "UK")], {("gs_a", "UK"): "ev-1"})[0].event_id
        assert a == am.plan_user_attributes([], [ua("gs_a", "UK")], {("gs_a", "UK"): "ev-1"})[0].event_id
        assert a != am.plan_user_attributes([], [ua("gs_a", "UK")], {("gs_a", "UK"): "ev-2"})[0].event_id

    def test_duplicate_stored_key_refused(self):
        with pytest.raises(ValueError):
            am.plan_user_attributes([ua("gs_a", "UK")], [ua("gs_a", "UK"), ua("gs_a", "UK", "motor")], {})

    def test_duplicate_desired_key_refused(self):
        with pytest.raises(ValueError):
            am.plan_user_attributes([ua("gs_a", "UK"), ua("gs_a", "UK")], [], {})


# ================================================================ membership_report

def actual_stub() -> dict[str, set[str]]:
    return {g.name: set(g.members) for g in stub().groups}


class TestMembershipReport:
    def test_matching_is_clean(self):
        assert am.membership_report(stub(), actual_stub()) == ((), ())

    def test_missing_group_is_failure(self):
        actual = actual_stub()
        del actual["gs_data_owner"]
        warnings, failures = am.membership_report(stub(), actual)
        assert any("gs_data_owner" in f for f in failures)

    def test_declared_member_missing_is_warning(self):
        actual = actual_stub()
        actual["gs_analyst_uk"] = set()
        warnings, failures = am.membership_report(stub(), actual)
        assert failures == () and any("analyst_sp" in w for w in warnings)

    def test_undeclared_member_is_warning(self):
        actual = actual_stub()
        actual["gs_analyst_uk"].add("stray_sp")
        warnings, failures = am.membership_report(stub(), actual)
        assert failures == () and any("stray_sp" in w for w in warnings)

    def test_isolated_member_in_any_group_is_failure(self):
        actual = actual_stub()
        actual["gs_analyst_global"].add("proxied_analyst_sp")
        _, failures = am.membership_report(stub(), actual)
        assert any("proxied_analyst_sp" in f for f in failures)

    def test_never_raises(self):
        warnings, failures = am.membership_report(stub(), {})
        assert isinstance(warnings, tuple) and isinstance(failures, tuple)
        assert len(failures) >= len(STUB_GROUPS)


def test_new_schema_mask_reuses_the_class_function_of_an_adopted_mask():
    from gates.consumption import access_model as am
    from gates.consumption.policy_rows import row_dict, rows_change
    from gates.consumption.row_filter import PolicyRow

    stored = [PolicyRow("rf_raw", "row_filter", "cat.raw", "gg_rls_attribute", "region", "cat.gov.rls_region_filter",
                        ("ci-sp",), True),
              PolicyRow("mk_raw_pii", "mask", "cat.raw", "gg_sensitivity", "pii", "cat.gov.mask_pii_v2", ("ci-sp",),
                        True)]
    m = am.load_model("version: 1\nmasked_classes: [pii]\ngroups: {}\n")
    out = am.desired_rows(m, schemas=["raw", "curated"], stored=stored, catalog="cat", gov_schema="gov",
                          sensitivity_tag="gg_sensitivity", rls_tag="gg_rls_attribute", hydration_principal="ci-sp")
    new_mask = next(r for r in out if r.policy_id == "am_mask_curated_pii")
    assert new_mask.function_name == "cat.gov.mask_pii_v2"
    rows_change([row_dict(r) for r in stored], out, last_event_ids={}, catalog="cat", rls_tag="gg_rls_attribute",
                sensitivity_tag="gg_sensitivity", hydration_principal="ci-sp", gov_schema="gov", allow_reserved=True)
