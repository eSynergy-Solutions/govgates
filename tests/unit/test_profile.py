from gates.hydration.profile import ColumnProfile, profile_from_stats


def _p(**kw):
    base = dict(column="c", data_type="string", total=200, non_null=150, distinct=75,
                pattern_counts={"email": 150, "phone": 3, "postcode": 0}, fingerprint=12345, avg_length=21.456)
    base.update(kw)
    return profile_from_stats(**base)


def test_percentages():
    p = _p()
    assert p.null_pct == 25.0
    assert p.distinct_pct == 50.0  # distinct over non-null values


def test_pattern_hits_use_non_null_denominator():
    assert _p().pattern_hits == ["email"]


def test_any_pattern_hits_lists_every_pattern_seen_at_least_once():
    assert _p().any_pattern_hits == ["email", "phone"]


def test_avg_length_rounded_and_null_safe():
    assert _p().avg_length == 21.46
    assert _p(avg_length=None, non_null=0, distinct=0).avg_length is None


def test_empty_table_is_safe():
    p = _p(total=0, non_null=0, distinct=0, pattern_counts={}, avg_length=None)
    assert p.null_pct == 0.0 and p.distinct_pct == 0.0 and p.pattern_hits == [] and p.any_pattern_hits == []


def test_all_null_column():
    p = _p(non_null=0, distinct=0, pattern_counts={"email": 0})
    assert p.null_pct == 100.0 and p.pattern_hits == []


def test_sample_hash_is_a_hash_not_a_value_and_changes_with_data():
    a, b = _p(), _p(fingerprint=999)
    assert len(a.sample_hash) == 64 and a.sample_hash != b.sample_hash
    assert a.sample_hash == _p().sample_hash


def test_record_has_contract_fields_only():
    rec = _p().as_record()
    assert set(rec) == {"column_name", "data_type", "null_pct", "distinct_pct", "pattern_hits", "sample_hash",
                        "any_pattern_hits", "avg_length"}
    assert isinstance(_p(), ColumnProfile)
