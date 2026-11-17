"""Detective check of who can modify ref.user_attributes (specs/consumption.md, row_filter bullet:
"Who may change `ref.user_attributes`: ... MODIFY only for the synthetic loader's principal and the CI service
principal; nobody else." Lead: the loader runs as the CI SP; the catalog owner is the one allowed exception,
configured separately). PLAN.md Q17. Pure: no Spark, no workspace.

Fixture conventions (chosen here, not fixed by the spec):
- Grants mirror SHOW GRANTS rows (principal, actionType, objectType, objectKey).
- Failure message wording is not fixed; tests only check that a failure exists and names the principal.
- The CI SP is a UUID applicationId, because `allowed` entries must be an email (user) or a strict
  8-4-4-4-12 hex UUID (service principal).
- Visibility is per level: owning a level or holding READ METADATA / MANAGE on it covers that level and the
  levels below it (table < schema < catalog). `unseen_levels` lists the uncovered ones in order table, schema,
  catalog.
- `owner_exception` (keyword on verdict) must equal the catalog owner when set; it does not add to `allowed`.
"""

from __future__ import annotations

import dataclasses
import json

import pytest

from gates.consumption.attr_guard import (
    Grant,
    Result,
    Securable,
    audit_change,
    canonical,
    modifiers,
    unseen_levels,
    verdict,
)

TABLE = "cat.ref.user_attributes"
SCHEMA = "cat.ref"
CAT = "cat"
OWNER = "owner@example.com"
CI = "0000c1c1-0000-4000-8000-000000000000"  # synthetic CI service principal applicationId
OTHER = "someone@example.com"
GROUP = "gs_x"


def sec(*, table_type: str | None = "MANAGED", table: str | None = OWNER, schema: str | None = OWNER,
        catalog: str | None = OWNER) -> Securable:
    return Securable(table=TABLE, table_type=table_type,
                     owners={"table": table, "schema": schema, "catalog": catalog})


def g(principal: str, privilege: str, level: str = "CATALOG") -> Grant:
    key = {"TABLE": TABLE, "SCHEMA": SCHEMA, "CATALOG": CAT}[level.upper()]
    return Grant(principal=principal, privilege=privilege, object_type=level, object_key=key)


def dev_grants() -> list[Grant]:
    """Today's dev-like shape: owner owns all three, CI SP has ALL PRIVILEGES + READ METADATA on the catalog."""
    return [g(CI, "ALL PRIVILEGES", "CATALOG"), g(CI, "READ METADATA", "CATALOG")]


def failures_naming(result: Result, name: str) -> list[str]:
    return [f for f in result.failures if name.lower() in f.lower()]


# ---------------------------------------------------------------- dataclasses


def test_dataclasses_are_frozen():
    gr = g(CI, "MODIFY")
    s = sec()
    with pytest.raises(dataclasses.FrozenInstanceError):
        gr.principal = "x"  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        s.table_type = "EXTERNAL"  # type: ignore[misc]
    r = verdict(s, dev_grants(), allowed={CI, OWNER}, run_principal=CI)
    with pytest.raises(dataclasses.FrozenInstanceError):
        r.ok = False  # type: ignore[misc]


# ---------------------------------------------------------------- modifiers: ownership


@pytest.mark.parametrize("level", ["table", "schema", "catalog"])
def test_owner_at_each_level_is_a_modifier(level):
    owners = {"table": "a@example.com", "schema": "b@example.com", "catalog": "c@example.com"}
    owners[level] = "X.Owner@Example.com"
    s = Securable(table=TABLE, table_type="MANAGED", owners=owners)
    m = modifiers(s, [])
    assert m["x.owner@example.com"] == (f"owner:{level}",)


def test_owner_of_all_three_levels_gets_sorted_reasons():
    m = modifiers(sec(), [])
    assert m == {OWNER: ("owner:catalog", "owner:schema", "owner:table")}


# ---------------------------------------------------------------- modifiers: privileges


@pytest.mark.parametrize("level", ["TABLE", "SCHEMA", "CATALOG"])
@pytest.mark.parametrize("priv,reason_priv", [
    ("MODIFY", "MODIFY"),
    ("ALL PRIVILEGES", "ALL PRIVILEGES"),
    ("ALL_PRIVILEGES", "ALL PRIVILEGES"),
    ("MANAGE", "MANAGE"),
    ("modify", "MODIFY"),
    ("all privileges", "ALL PRIVILEGES"),
    ("Manage", "MANAGE"),
])
def test_modifying_privileges_at_each_level(level, priv, reason_priv):
    m = modifiers(sec(), [g(OTHER, priv, level)])
    assert m[OTHER] == (f"{reason_priv}@{level.lower()}",)


@pytest.mark.parametrize("priv", ["SELECT", "READ METADATA", "USE SCHEMA", "USE CATALOG", "BROWSE",
                                  "select", "read metadata", "EXECUTE", "CREATE TABLE"])
@pytest.mark.parametrize("level", ["TABLE", "SCHEMA", "CATALOG"])
def test_non_modifying_privileges_never_count(priv, level):
    m = modifiers(sec(), [g(OTHER, priv, level)])
    assert OTHER not in m


def test_principal_is_lowercased_and_reasons_merge_sorted():
    grants = [g("Someone@Example.com", "MODIFY", "TABLE"), g("someone@example.com", "MANAGE", "CATALOG"),
              g(OTHER, "ALL PRIVILEGES", "SCHEMA")]
    m = modifiers(sec(), grants)
    assert m[OTHER] == tuple(sorted(("MODIFY@table", "MANAGE@catalog", "ALL PRIVILEGES@schema")))


def test_duplicate_grants_do_not_duplicate_reasons():
    m = modifiers(sec(), [g(OTHER, "MODIFY", "TABLE"), g(OTHER, "modify", "TABLE")])
    assert m[OTHER] == ("MODIFY@table",)


def test_owner_with_grant_merges_ownership_and_privilege_reasons():
    m = modifiers(sec(), [g(OWNER, "MODIFY", "TABLE")])
    assert m[OWNER] == tuple(sorted(("owner:catalog", "owner:schema", "owner:table", "MODIFY@table")))


def test_group_grantee_appears_as_itself():
    m = modifiers(sec(), [g(GROUP, "MODIFY", "SCHEMA")])
    assert m[GROUP] == ("MODIFY@schema",)


# ---------------------------------------------------------------- unseen_levels


@pytest.mark.parametrize("level,expected", [
    ("table", ("schema", "catalog")),
    ("schema", ("catalog",)),
    ("catalog", ()),
])
def test_unseen_levels_by_ownership(level, expected):
    owners = {"table": OWNER, "schema": OWNER, "catalog": OWNER}
    owners[level] = CI.upper()
    s = Securable(table=TABLE, table_type="MANAGED", owners=owners)
    assert unseen_levels(CI, s, []) == expected


def test_owning_table_and_schema_leaves_catalog_unseen():
    s = sec(table=CI, schema=CI, catalog=OWNER)
    assert unseen_levels(CI, s, []) == ("catalog",)


@pytest.mark.parametrize("priv", ["READ METADATA", "read metadata", "READ_METADATA", "MANAGE", "manage"])
@pytest.mark.parametrize("level,expected", [
    ("TABLE", ("schema", "catalog")),
    ("SCHEMA", ("catalog",)),
    ("CATALOG", ()),
])
def test_unseen_levels_by_read_metadata_or_manage(priv, level, expected):
    assert unseen_levels(CI, sec(), [g(CI, priv, level)]) == expected


def test_catalog_read_metadata_covers_all_three():
    assert unseen_levels(CI, sec(), [g(CI, "READ METADATA", "CATALOG")]) == ()


def test_schema_read_metadata_covers_table_and_schema_not_catalog():
    assert unseen_levels(CI, sec(), [g(CI, "READ METADATA", "SCHEMA")]) == ("catalog",)


def test_table_and_schema_grants_together_still_leave_catalog_unseen():
    grants = [g(CI, "READ METADATA", "TABLE"), g(CI, "MANAGE", "SCHEMA")]
    assert unseen_levels(CI, sec(), grants) == ("catalog",)


def test_unseen_levels_is_case_insensitive_on_run_principal():
    assert unseen_levels(CI.upper(), sec(), [g(CI, "READ METADATA")]) == ()


@pytest.mark.parametrize("priv", ["SELECT", "MODIFY", "USE CATALOG", "BROWSE"])
def test_all_levels_unseen_without_read_metadata_manage_or_ownership(priv):
    assert unseen_levels(CI, sec(), [g(CI, priv, "CATALOG")]) == ("table", "schema", "catalog")


def test_another_principals_read_metadata_does_not_count():
    assert unseen_levels(CI, sec(), [g(OTHER, "READ METADATA")]) == ("table", "schema", "catalog")


def test_unseen_levels_returns_a_tuple():
    assert isinstance(unseen_levels(CI, sec(), []), tuple)


def _visibility_failures(result: Result, run: str) -> list[str]:
    """Failures that name the run principal and are not 'X can modify' failures."""
    return [f for f in result.failures if run.lower() in f.lower() and "can modify" not in f.lower()]


def test_verdict_names_each_unseen_level():
    """Run principal owns the table only; catalog owned by someone else; an unexpected MODIFY@catalog exists."""
    s = sec(table=CI, schema=OWNER, catalog=OWNER)
    r = verdict(s, [g(OTHER, "MODIFY", "CATALOG")], allowed={CI, OWNER}, run_principal=CI)
    assert r.ok is False
    vis = _visibility_failures(r, CI)
    assert vis, r.failures
    joined = " ".join(vis).lower()
    assert "schema" in joined
    assert "catalog" in joined
    assert OTHER in r.unexpected


def test_verdict_schema_level_visibility_fails_naming_catalog():
    grants = [g(CI, "ALL PRIVILEGES", "CATALOG"), g(CI, "READ METADATA", "SCHEMA")]
    r = verdict(sec(), grants, allowed={CI, OWNER}, run_principal=CI)
    assert r.ok is False
    vis = _visibility_failures(r, CI)
    assert vis, r.failures
    assert "catalog" in " ".join(vis).lower()


def test_verdict_catalog_read_metadata_has_no_visibility_failure():
    r = verdict(sec(), dev_grants(), allowed={CI, OWNER}, run_principal=CI)
    assert _visibility_failures(r, CI) == []


# ---------------------------------------------------------------- verdict: pass


def test_dev_shape_with_ci_and_owner_exception_is_ok():
    r = verdict(sec(), dev_grants(), allowed={CI, OWNER}, run_principal=CI)
    assert r.ok is True
    assert r.failures == ()
    assert r.unexpected == ()
    assert r.allowed == tuple(sorted((CI, OWNER)))
    assert r.modifiers[CI] == ("ALL PRIVILEGES@catalog",)
    assert r.modifiers[OWNER] == ("owner:catalog", "owner:schema", "owner:table")


def test_allowed_is_lowercased_and_matched_case_insensitively():
    r = verdict(sec(), dev_grants(), allowed={CI.upper(), "Owner@Example.COM"}, run_principal=CI)
    assert r.ok is True
    assert r.allowed == tuple(sorted((CI, OWNER)))


def test_allowed_principal_that_is_not_a_modifier_is_not_a_failure():
    r = verdict(sec(), dev_grants(), allowed={CI, OWNER, "spare@example.com"}, run_principal=CI)
    assert r.ok is True


def test_table_type_is_case_insensitive():
    r = verdict(sec(table_type="managed"), dev_grants(), allowed={CI, OWNER}, run_principal=CI)
    assert r.ok is True


# ---------------------------------------------------------------- verdict: fail closed


def test_without_owner_exception_owner_is_unexpected():
    r = verdict(sec(), dev_grants(), allowed={CI}, run_principal=CI)
    assert r.ok is False
    assert r.unexpected == (OWNER,)
    assert failures_naming(r, OWNER)


def test_unexpected_schema_modify_to_group_fails():
    r = verdict(sec(), dev_grants() + [g(GROUP, "MODIFY", "SCHEMA")], allowed={CI, OWNER}, run_principal=CI)
    assert r.ok is False
    assert GROUP in r.unexpected
    assert failures_naming(r, GROUP)
    assert r.modifiers[GROUP] == ("MODIFY@schema",)


def test_unexpected_principals_are_sorted():
    grants = dev_grants() + [g("zed@example.com", "MODIFY", "TABLE"), g("abe@example.com", "MANAGE", "CATALOG")]
    r = verdict(sec(), grants, allowed={CI, OWNER}, run_principal=CI)
    assert r.unexpected == ("abe@example.com", "zed@example.com")
    assert list(r.failures) == sorted(r.failures)


def test_read_only_grants_to_others_do_not_fail():
    grants = dev_grants() + [g(GROUP, "SELECT", "TABLE"), g(OTHER, "READ METADATA", "CATALOG"),
                             g(GROUP, "USE SCHEMA", "SCHEMA"), g(OTHER, "BROWSE", "CATALOG")]
    r = verdict(sec(), grants, allowed={CI, OWNER}, run_principal=CI)
    assert r.ok is True


def test_run_principal_without_visibility_fails():
    run = "1111aaaa-0000-4000-8000-000000000000"
    r = verdict(sec(), dev_grants() + [g(run, "SELECT", "TABLE")], allowed={CI, OWNER}, run_principal=run)
    assert r.ok is False
    assert r.failures


def test_missing_table_fails():
    r = verdict(sec(table_type=None, table=None), [], allowed={CI, OWNER}, run_principal=CI)
    assert r.ok is False
    assert r.failures


def test_missing_table_fails_even_if_owners_present():
    r = verdict(sec(table_type=None), dev_grants(), allowed={CI, OWNER}, run_principal=CI)
    assert r.ok is False


@pytest.mark.parametrize("tt", ["EXTERNAL", "VIEW", "MATERIALIZED_VIEW", "", "STREAMING_TABLE"])
def test_non_managed_table_fails(tt):
    r = verdict(sec(table_type=tt), dev_grants(), allowed={CI, OWNER}, run_principal=CI)
    assert r.ok is False


@pytest.mark.parametrize("level", ["table", "schema", "catalog"])
@pytest.mark.parametrize("value", [None, ""])
def test_missing_owner_fails(level, value):
    r = verdict(sec(**{level: value}), dev_grants(), allowed={CI, OWNER}, run_principal=CI)
    assert r.ok is False


def test_owner_key_absent_fails():
    s = Securable(table=TABLE, table_type="MANAGED", owners={"table": OWNER, "schema": OWNER})
    r = verdict(s, dev_grants(), allowed={CI, OWNER}, run_principal=CI)
    assert r.ok is False


def test_empty_allowed_fails():
    r = verdict(sec(), [], allowed=set(), run_principal=OWNER)
    assert r.ok is False


def test_allowed_with_empty_string_fails():
    r = verdict(sec(), dev_grants(), allowed={CI, OWNER, ""}, run_principal=CI)
    assert r.ok is False


@pytest.mark.parametrize("bad", [GROUP, "account users", "admins", "abc", "dead-be", "xyz-0000-0000"])
def test_group_or_non_id_in_allowed_fails(bad):
    r = verdict(sec(), dev_grants(), allowed={CI, OWNER, bad}, run_principal=CI)
    assert r.ok is False
    assert failures_naming(r, bad)


def test_group_in_allowed_fails_even_when_it_is_the_modifier():
    r = verdict(sec(), dev_grants() + [g(GROUP, "MODIFY", "TABLE")], allowed={CI, OWNER, GROUP},
                run_principal=CI)
    assert r.ok is False


@pytest.mark.parametrize("bad", ["deadbeef", "DEADBEEF-0000", "0000c1c100004000800000000000000",
                                 "0000c1c1-0000-4000-8000-0000000000000", "0000c1c1-0000-4000-8000-00000000000",
                                 "0000c1c1-0000-4000-8000-00000000000g", "0000c1c-10000-4000-8000-000000000000",
                                 "{0000c1c1-0000-4000-8000-000000000000}"])
def test_loose_hex_ids_in_allowed_are_refused(bad):
    r = verdict(sec(), dev_grants(), allowed={CI, OWNER, bad}, run_principal=CI)
    assert r.ok is False
    assert failures_naming(r, bad)


@pytest.mark.parametrize("good", ["0000c1c1-0000-4000-8000-000000000000", "ABCDEF01-2345-4678-9ABC-DEF012345678",
                                  "abcdef01-2345-4678-9abc-def012345678", "a@example.com"])
def test_email_or_strict_uuid_allowed_entries_are_accepted(good):
    r = verdict(sec(), dev_grants(), allowed={CI, OWNER, good}, run_principal=CI)
    assert r.ok is True


def test_ok_iff_failures_empty():
    for r in (verdict(sec(), dev_grants(), allowed={CI, OWNER}, run_principal=CI),
              verdict(sec(), dev_grants(), allowed={CI}, run_principal=CI),
              verdict(sec(table_type="EXTERNAL"), dev_grants(), allowed={CI, OWNER}, run_principal=CI)):
        assert r.ok is (r.failures == ())


# ---------------------------------------------------------------- verdict: owner_exception


def test_owner_exception_matching_catalog_owner_is_ok():
    r = verdict(sec(), dev_grants(), allowed={CI, OWNER}, run_principal=CI, owner_exception=OWNER)
    assert r.ok is True
    assert r.failures == ()


def test_owner_exception_is_case_insensitive():
    r = verdict(sec(), dev_grants(), allowed={CI, OWNER}, run_principal=CI, owner_exception="Owner@EXAMPLE.com")
    assert r.ok is True


def test_owner_exception_not_the_catalog_owner_fails():
    r = verdict(sec(), dev_grants(), allowed={CI, OWNER}, run_principal=CI, owner_exception=OTHER)
    assert r.ok is False
    assert failures_naming(r, "owner_exception")


def test_owner_exception_matching_only_the_table_owner_fails():
    s = sec(table=OTHER, schema=OWNER, catalog=OWNER)
    r = verdict(s, dev_grants(), allowed={CI, OWNER, OTHER}, run_principal=CI, owner_exception=OTHER)
    assert r.ok is False
    assert failures_naming(r, "owner_exception")


def test_owner_exception_does_not_add_to_allowed():
    r = verdict(sec(), dev_grants(), allowed={CI}, run_principal=CI, owner_exception=OWNER)
    assert r.ok is False
    assert r.unexpected == (OWNER,)
    assert not failures_naming(r, "owner_exception")


@pytest.mark.parametrize("kwargs", [{}, {"owner_exception": ""}])
def test_empty_owner_exception_adds_no_such_failure(kwargs):
    ok = verdict(sec(), dev_grants(), allowed={CI, OWNER}, run_principal=CI, **kwargs)
    assert ok.ok is True
    r = verdict(sec(), dev_grants(), allowed={CI}, run_principal=CI, **kwargs)
    assert not failures_naming(r, "owner_exception")
    assert r.ok is False
    assert r.unexpected == (OWNER,)


# ---------------------------------------------------------------- canonical


def test_canonical_keys_and_json_ready():
    r = verdict(sec(), dev_grants() + [g(GROUP, "MODIFY", "SCHEMA")], allowed={CI, OWNER}, run_principal=CI)
    c = canonical(r)
    assert set(c) == {"ok", "failures", "modifiers", "unexpected", "allowed", "table"}
    assert json.loads(json.dumps(c)) == c  # tuples would come back as lists and differ
    assert c["table"] == TABLE
    assert c["ok"] is False
    assert c["unexpected"] == [GROUP]
    assert c["allowed"] == sorted([CI, OWNER])
    assert c["modifiers"][OWNER] == ["owner:catalog", "owner:schema", "owner:table"]


def test_canonical_is_order_independent():
    grants = dev_grants() + [g("Zed@Example.com", "MODIFY", "TABLE"), g(GROUP, "MANAGE", "SCHEMA"),
                             g(GROUP, "MODIFY", "CATALOG"), g(OTHER, "SELECT", "TABLE")]
    a = canonical(verdict(sec(), grants, allowed={CI, OWNER}, run_principal=CI))
    b = canonical(verdict(sec(), list(reversed(grants)), allowed={OWNER.upper(), CI}, run_principal=CI.upper()))
    assert a == b
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


def test_verdict_is_idempotent():
    a = verdict(sec(), dev_grants(), allowed={CI, OWNER}, run_principal=CI)
    b = verdict(sec(), dev_grants(), allowed={CI, OWNER}, run_principal=CI)
    assert a == b
    assert canonical(a) == canonical(b)


# ---------------------------------------------------------------- audit_change


def _ok_state() -> dict:
    return canonical(verdict(sec(), dev_grants(), allowed={CI, OWNER}, run_principal=CI))


def _bad_state() -> dict:
    return canonical(verdict(sec(), dev_grants() + [g(GROUP, "MODIFY", "SCHEMA")], allowed={CI, OWNER},
                             run_principal=CI))


def test_first_run_writes_with_no_before():
    cur = _ok_state()
    out = audit_change(cur, None, None)
    assert out is not None
    before, after, eid = out
    assert before is None
    assert after == cur
    assert isinstance(eid, str) and eid


def test_second_identical_run_writes_nothing():
    cur = _ok_state()
    assert audit_change(cur, _ok_state(), "prev-id") is None


def test_change_returns_before_and_after():
    prev, cur = _ok_state(), _bad_state()
    before, after, _ = audit_change(cur, prev, "e1")
    assert before == prev
    assert after == cur


def test_event_id_is_deterministic():
    cur, prev = _bad_state(), _ok_state()
    assert audit_change(cur, prev, "e1")[2] == audit_change(_bad_state(), _ok_state(), "e1")[2]


def test_event_id_depends_on_previous_event_id():
    cur, prev = _bad_state(), _ok_state()
    assert audit_change(cur, prev, "e1")[2] != audit_change(cur, prev, "e2")[2]


def test_fail_fix_pass_chain_gets_new_ids():
    ok, bad = _ok_state(), _bad_state()
    _, _, e1 = audit_change(ok, None, None)
    assert audit_change(ok, ok, e1) is None
    _, _, e2 = audit_change(bad, ok, e1)
    _, _, e3 = audit_change(ok, bad, e2)
    assert len({e1, e2, e3}) == 3  # back to the first state, still a new event
