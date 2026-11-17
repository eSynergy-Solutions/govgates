"""Audited writer for gov.access_policies row_filter rows (specs/consumption.md)."""

from __future__ import annotations

import pytest

from gates.consumption.policy_rows import row_change, row_dict
from gates.consumption.row_filter import PolicyRow
from gates.hydration.apply import event_id_of

HP = "ci-sp-0000"
KW = dict(catalog="cat", rls_tag="gg_rls_attribute", sensitivity_tag="gg_sensitivity", hydration_principal=HP,
          gov_schema="gov")


def row(**kw) -> PolicyRow:
    base = dict(policy_id="rf_raw", policy_type="row_filter", securable="cat.raw", match_tag_key="gg_rls_attribute",
                match_tag_value="region", function_name="cat.gov.rls_region_filter", except_principals=(HP,),
                active=True)
    base.update(kw)
    return PolicyRow(**base)


def change(existing, new, *, others=(), last_event_id=None, **kw):
    return row_change(existing, new, others=list(others), last_event_id=last_event_id, **{**KW, **kw})


def test_new_row_has_no_before():
    before, after, eid = change(None, row())
    assert before is None and after["policy_id"] == "rf_raw" and eid


def test_same_row_is_noop():
    assert change(row_dict(row()), row()) is None


def test_same_row_is_noop_whatever_last_event():
    assert change(row_dict(row()), row(), last_event_id="ev-1") is None


def test_except_order_ignored():
    stored = row_dict(row(except_principals=("gs_x", HP)))
    stored["except_principals"] = [HP, "gs_x"]
    assert change(stored, row(except_principals=("gs_x", HP))) is None


def test_retire_is_a_change_with_before():
    before, after, _ = change(row_dict(row()), row(active=False))
    assert before["active"] is True and after["active"] is False


def test_event_id_deterministic_per_after():
    a = change(None, row())[2]
    assert a == change(None, row())[2]
    assert a != change(None, row(active=False))[2]


def test_event_id_includes_before():
    a = change(None, row())[2]
    b = change(row_dict(row(active=False)), row())[2]
    assert a != b


def test_event_id_includes_last_event_id():
    a = change(row_dict(row()), row(active=False), last_event_id="ev-1")[2]
    b = change(row_dict(row()), row(active=False), last_event_id="ev-2")[2]
    assert a != b
    # replay with the same last event dedupes
    assert a == change(row_dict(row()), row(active=False), last_event_id="ev-1")[2]


def test_retire_reactivate_cycle_distinct_ids():
    # create -> retire -> reactivate -> retire -> reactivate: steps 2/4 and 3/5 have identical before/after,
    # only the advancing last_event_id tells them apart
    stored, last, ids = None, None, []
    for active in (True, False, True, False, True):
        new = row(active=active)
        _, after, eid = change(stored, new, last_event_id=last)
        ids.append(eid)
        stored, last = after, eid
    assert len(set(ids)) == 5


def test_invalid_row_refused():
    with pytest.raises(ValueError):
        change(None, row(except_principals=("gs_x",)))
    with pytest.raises(ValueError):
        change(None, row(securable="other.raw"))


@pytest.mark.parametrize("fn", ["cat.raw.fn", "cat.gov2.fn"])
def test_function_outside_gov_schema_refused(fn):
    with pytest.raises(ValueError):
        change(None, row(function_name=fn))


def test_function_gov_schema_case_insensitive():
    assert change(None, row(function_name="cat.GOV.rls_region_filter")) is not None


def test_second_active_row_on_same_securable_refused():
    other = row(policy_id="rf_raw_2")
    with pytest.raises(ValueError):
        change(None, row(), others=[other])


def test_second_active_row_refused_case_insensitive():
    other = row(policy_id="rf_raw_2", securable="cat.RAW")
    with pytest.raises(ValueError):
        change(None, row(), others=[other])


def test_reactivating_refused_while_another_is_active():
    other = row(policy_id="rf_raw_2")
    with pytest.raises(ValueError):
        change(row_dict(row(active=False)), row(), others=[other])


def test_inactive_other_on_same_securable_ok():
    other = row(policy_id="rf_raw_old", active=False)
    assert change(None, row(), others=[other]) is not None


def test_retiring_while_another_is_active_ok():
    other = row(policy_id="rf_raw_2")
    assert change(row_dict(row()), row(active=False), others=[other]) is not None


def test_active_other_on_another_securable_ok():
    other = row(policy_id="rf_curated", securable="cat.curated")
    assert change(None, row(), others=[other]) is not None


def test_invalid_other_row_refuses_write():
    # rows are validated together: a write is refused while the stored set would be invalid
    bad = row(policy_id="rf_bad", securable="cat.curated", function_name="cat.raw.fn")
    with pytest.raises(ValueError):
        change(None, row(), others=[bad])


def test_securable_change_for_existing_policy_refused():
    with pytest.raises(ValueError):
        change(row_dict(row()), row(securable="cat.curated"))


def test_securable_change_refused_even_when_retiring():
    with pytest.raises(ValueError):
        change(row_dict(row()), row(securable="cat.curated", active=False))


# ---------------------------------------------------------------- mask rows (validated by masks.validate_mask_rows)

def mask(**kw) -> PolicyRow:
    base = dict(policy_id="mk_raw_pii", policy_type="mask", securable="cat.raw", match_tag_key="gg_sensitivity",
                match_tag_value="pii", function_name="cat.gov.mask_pii", except_principals=(HP, "gs_claims_handler"),
                active=True)
    base.update(kw)
    return PolicyRow(**base)


def test_valid_mask_row_is_a_change():
    before, after, eid = change(None, mask())
    assert before is None and after["policy_type"] == "mask" and eid


def test_same_mask_row_is_noop():
    assert change(row_dict(mask()), mask()) is None


def test_mask_row_without_ci_sp_in_except_refused():
    with pytest.raises(ValueError):
        change(None, mask(except_principals=("gs_claims_handler",)))


def test_mask_row_tag_key_must_be_sensitivity_tag():
    with pytest.raises(ValueError):
        change(None, mask(match_tag_key="gg_rls_attribute"))
    assert change(None, mask(match_tag_key="xx_sensitivity"), sensitivity_tag="xx_sensitivity") is not None


def test_second_active_pii_mask_on_same_schema_refused():
    with pytest.raises(ValueError):
        change(None, mask(), others=[mask(policy_id="mk_raw_pii_2")])


def test_mask_rows_of_different_classes_on_same_schema_ok():
    other = mask(policy_id="mk_raw_conf", match_tag_value="confidential", function_name="cat.gov.mask_confidential")
    assert change(None, mask(), others=[other]) is not None


def test_mask_and_row_filter_on_same_schema_ok():
    assert change(None, mask(), others=[row()]) is not None
    assert change(None, row(), others=[mask()]) is not None


def test_invalid_mask_other_refuses_row_filter_write():
    bad = mask(policy_id="mk_bad", except_principals=("gs_x",))
    with pytest.raises(ValueError):
        change(None, row(), others=[bad])


def test_mask_securable_change_refused():
    with pytest.raises(ValueError):
        change(row_dict(mask()), mask(securable="cat.curated"))


def test_sensitivity_tag_is_required():
    kw = {k: v for k, v in KW.items() if k != "sensitivity_tag"}
    with pytest.raises(TypeError):
        row_change(None, mask(), others=[], last_event_id=None, **kw)


def test_mask_row_may_not_reuse_the_row_filter_function():
    rf = row()
    m = row(policy_id="mk_raw_pii", policy_type="mask", match_tag_key="gg_sensitivity", match_tag_value="pii",
            function_name=rf.function_name + "_helper")
    with pytest.raises(ValueError):
        change(None, m, others=[rf])


# ---------------------------------------------------------------- grant rows are never written by set_policy_row

def grant_row(**kw) -> PolicyRow:
    base = dict(policy_id="gr_ag-0001", policy_type="grant", securable="cat.raw.claims", match_tag_key="",
                match_tag_value="", function_name="", except_principals=(), active=True)
    base.update(kw)
    return PolicyRow(**base)


def test_grant_row_refused():
    # grant rows come only from live agreements via the grants generator (specs/consumption.md, Policy model)
    with pytest.raises(ValueError):
        change(None, grant_row())


def test_grant_row_refused_even_when_retiring_or_already_stored():
    with pytest.raises(ValueError):
        change(row_dict(grant_row()), grant_row(active=False))
    with pytest.raises(ValueError):
        change(row_dict(grant_row()), grant_row())


def test_turning_a_row_filter_into_a_grant_refused():
    with pytest.raises(ValueError):
        change(row_dict(row()), row(policy_type="grant"))


def test_gr_prefix_reserved_for_the_grants_generator():
    with pytest.raises(ValueError, match="gr_"):
        change(None, row(policy_id="gr_sneaky"))


# ---------------------------------------------------------------- rows_change: batch writer (access-model adapter)
# Imported per test so a missing name fails these tests only, not the whole file.

def batch(stored, new, *, last_event_ids=None, **kw):
    from gates.consumption.policy_rows import rows_change
    return rows_change([row_dict(s) if isinstance(s, PolicyRow) else s for s in stored], list(new),
                       last_event_ids=last_event_ids or {}, **{**KW, **kw})


def test_batch_new_row_matches_single_row_change():
    out = batch([], [row()])
    assert out == [change(None, row())]


def test_batch_unchanged_is_empty():
    assert batch([row(), mask()], [row(), mask()]) == []


def test_batch_except_order_ignored():
    stored = row_dict(mask(except_principals=("gs_claims_handler", HP)))
    stored["except_principals"] = ["gs_claims_handler", HP]
    assert batch([stored], [mask(except_principals=(HP, "gs_claims_handler"))]) == []


def test_batch_returns_only_changed_rows():
    new_mask = mask(except_principals=(HP, "gs_claims_handler", "gs_data_owner"))
    (before, after, eid), = batch([row(), mask()], [row(), new_mask])
    assert before["policy_id"] == "mk_raw_pii" and after == row_dict(new_mask)
    assert (before, after, eid) == change(row_dict(mask()), new_mask, others=[row()])


def test_batch_event_id_identical_to_row_change():
    new_mask = mask(active=False)
    (_, _, eid), = batch([mask()], [new_mask], last_event_ids={"mk_raw_pii": "ev-7"})
    assert eid == change(row_dict(mask()), new_mask, last_event_id="ev-7")[2]
    before = row_dict(mask())
    assert eid == event_id_of("policy_row_set", "mk_raw_pii", before, row_dict(new_mask), "ev-7")


def test_batch_last_event_of_other_ids_ignored():
    a = batch([mask()], [mask(active=False)], last_event_ids={"other": "ev-1"})[0][2]
    b = batch([mask()], [mask(active=False)])[0][2]
    assert a == b


def test_batch_swap_accepted_where_single_writes_refused():
    # one pii function per class: moving both pii masks to a new function is only valid as a whole
    a, b = mask(), mask(policy_id="mk_cur_pii", securable="cat.curated")
    a2 = mask(function_name="cat.gov.mask_pii_v2")
    b2 = mask(policy_id="mk_cur_pii", securable="cat.curated", function_name="cat.gov.mask_pii_v2")
    with pytest.raises(ValueError):
        change(row_dict(a), a2, others=[b])
    with pytest.raises(ValueError):
        change(row_dict(b), b2, others=[a])
    out = batch([a, b], [a2, b2])
    assert sorted(x[1]["policy_id"] for x in out) == ["mk_cur_pii", "mk_raw_pii"]


def test_batch_replace_row_filter_in_one_write():
    old, new = row(), row(policy_id="rf_raw_v2", function_name="cat.gov.rls_region_filter_raw")
    with pytest.raises(ValueError):
        change(None, new, others=[old])
    out = batch([old], [new, row(active=False)])
    assert len(out) == 2


def test_batch_invalid_end_state_refused():
    with pytest.raises(ValueError):
        batch([row()], [row(policy_id="rf_raw_2")])


def test_batch_invalid_untouched_stored_row_refuses_write():
    bad = row(policy_id="rf_bad", securable="cat.curated", function_name="cat.raw.fn")
    with pytest.raises(ValueError):
        batch([bad], [mask()])


def test_batch_one_bad_row_refuses_all():
    with pytest.raises(ValueError):
        batch([], [mask(), row(except_principals=("gs_x",))])


def test_batch_securable_change_refused():
    with pytest.raises(ValueError):
        batch([mask()], [mask(securable="cat.curated")])


def test_batch_gr_prefix_refused():
    with pytest.raises(ValueError, match="gr_"):
        batch([], [row(policy_id="gr_sneaky")])


def test_batch_am_prefix_reserved_for_adapter():
    with pytest.raises(ValueError, match="am_"):
        batch([], [row(policy_id="am_rf_raw")])
    assert len(batch([], [row(policy_id="am_rf_raw")], allow_reserved=True)) == 1


@pytest.mark.parametrize("ptype", ["grant", "other", ""])
def test_batch_policy_type_must_be_mask_or_row_filter(ptype):
    with pytest.raises(ValueError):
        batch([], [row(policy_type=ptype, policy_id="x_1")])


def test_row_change_refuses_am_prefix():
    with pytest.raises(ValueError, match="am_"):
        change(None, row(policy_id="am_rf_raw"))
