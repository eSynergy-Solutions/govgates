from gates.hydration.guard import decide, schema_hash

COLS = [("claim_id", "string"), ("loss_dt", "date"), ("paid_amt", "double")]


def test_hash_is_order_independent():
    assert schema_hash(COLS) == schema_hash(list(reversed(COLS)))


def test_hash_ignores_name_case():
    assert schema_hash([("Claim_ID", "STRING")]) == schema_hash([("claim_id", "string")])


def test_hash_changes_when_a_type_changes():
    changed = [("claim_id", "string"), ("loss_dt", "string"), ("paid_amt", "double")]
    assert schema_hash(COLS) != schema_hash(changed)


def test_hash_changes_when_a_column_is_added():
    assert schema_hash(COLS) != schema_hash(COLS + [("status", "string")])


def test_skip_when_hash_seen_and_every_column_covered():
    h = schema_hash(COLS)
    names = [c for c, _ in COLS]
    d = decide(h, {h}, names, approved={"claim_id"}, pending_same_hash={"loss_dt", "paid_amt"})
    assert d.skip
    assert d.to_propose == []


def test_run_when_table_is_new():
    h = schema_hash(COLS)
    d = decide(h, set(), [c for c, _ in COLS], approved=set(), pending_same_hash=set())
    assert not d.skip
    assert d.to_propose == ["claim_id", "loss_dt", "paid_amt"]


def test_hash_seen_but_a_column_uncovered_runs_for_that_column_only():
    h = schema_hash(COLS)
    d = decide(h, {h}, [c for c, _ in COLS], approved={"claim_id"}, pending_same_hash={"loss_dt"})
    assert not d.skip
    assert d.to_propose == ["paid_amt"]


def test_schema_change_keeps_approved_columns_out_of_the_llm():
    new_cols = COLS + [("status", "string")]
    h = schema_hash(new_cols)
    d = decide(h, {schema_hash(COLS)}, [c for c, _ in new_cols],
               approved={"claim_id", "loss_dt", "paid_amt"}, pending_same_hash=set())
    assert not d.skip
    assert d.to_propose == ["status"]


def test_force_proposes_every_column_even_when_covered():
    h = schema_hash(COLS)
    names = [c for c, _ in COLS]
    d = decide(h, {h}, names, approved={"claim_id"}, pending_same_hash={"loss_dt", "paid_amt"}, force=True)
    assert not d.skip
    assert d.to_propose == ["claim_id", "loss_dt", "paid_amt"]
    assert "force" in d.reason


def test_force_keeps_the_given_column_order():
    h = schema_hash(COLS)
    names = ["paid_amt", "claim_id", "loss_dt"]
    d = decide(h, {h}, names, approved=set(names), pending_same_hash=set(), force=True)
    assert d.to_propose == names


def test_force_false_is_the_default_behaviour():
    h = schema_hash(COLS)
    names = [c for c, _ in COLS]
    d = decide(h, {h}, names, approved={"claim_id"}, pending_same_hash={"loss_dt", "paid_amt"}, force=False)
    assert d.skip and d.to_propose == []
