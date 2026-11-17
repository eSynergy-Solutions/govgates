import pytest

from gates.hydration.match import (
    IgnoreRules,
    Term,
    expand,
    expand_variants,
    load_abbreviations,
    load_ignore,
    match_column,
    tokenize,
)

TERMS = [
    Term("client_identifier", "Client Identifier", "Unique id of a client.",
         ["customer id", "client number"], "internal"),
    Term("policy_status", "Policy Status", "Status of a policy.", ["policy state"], "internal"),
    Term("quote_status", "Quote Status", "Status of a quote.", ["quotation status"], "internal"),
    Term("claim_status", "Claim Status", "Status of a claim.", [], "internal"),
    Term("date_of_birth", "Date of Birth", "Birth date.", ["dob", "birth date"], "pii"),
    Term("email_address", "Email Address", "Email.", ["email", "e-mail"], "pii"),
    Term("loss_date", "Loss Date", "Date of loss.", ["date of loss"], "internal"),
    Term("policy_expiry_date", "Policy Expiry Date", "Date cover ends.", ["expiry date"], "internal"),
    Term("policyholder_name", "Policyholder Name", "Name of the policyholder.", [], "pii"),
]
ABBR = {"cust": ["customer"], "id": ["identifier"], "dt": ["date"], "pol": ["policy"], "sts": ["status"],
        "exp": ["expiry", "exposure", "expense"], "ph": ["phone", "policyholder"], "nm": ["name"]}


@pytest.mark.parametrize("name,tokens", [
    ("cust_id", ["cust", "id"]),
    ("CustID", ["cust", "id"]),
    ("polStatusCd", ["pol", "status", "cd"]),
    ("addr_line1", ["addr", "line", "1"]),
    ("  Email-Addr ", ["email", "addr"]),
    ("HTTPCode", ["http", "code"]),
])
def test_tokenize(name, tokens):
    assert tokenize(name) == tokens


def test_expand_keeps_unknown_tokens():
    assert expand(["cust", "id", "xyz"], ABBR) == "customer identifier xyz"


def test_expand_shows_every_reading_of_an_ambiguous_token():
    assert expand(["pol", "exp", "dt"], ABBR) == "policy (expiry/exposure/expense) date"


def test_expand_variants_is_the_cartesian_product():
    assert expand_variants(["exp", "dt"], ABBR) == {"expiry date", "exposure date", "expense date"}
    assert len(expand_variants(["exp", "ph", "exp"], ABBR)) == 3 * 2 * 3


def test_string_values_still_load_as_single_expansions():
    assert expand(["cust"], {"cust": "customer"}) == "customer"


def test_best_expansion_wins_the_score():
    r = match_column("pol_exp_dt", TERMS, ABBR)
    assert r.candidates[0].term_id == "policy_expiry_date" and r.candidates[0].score == 100


def test_ambiguous_token_never_auto_accepts():
    r = match_column("pol_exp_dt", TERMS, ABBR)
    assert r.ambiguous_tokens == ["exp"]
    assert r.accepted is None  # goes to the LLM even with a perfect, unique score
    assert r.expanded == "policy (expiry/exposure/expense) date"


def test_unambiguous_column_has_no_ambiguous_tokens():
    assert match_column("cust_id", TERMS, ABBR).ambiguous_tokens == []


def test_exact_synonym_after_expansion_is_accepted():
    r = match_column("cust_id", TERMS, ABBR)
    assert r.expanded == "customer identifier"
    assert r.accepted is not None and r.accepted.term_id == "client_identifier"
    assert r.accepted.score >= 90


def test_abbreviation_hits_synonym():
    r = match_column("dob", TERMS, ABBR)
    assert r.accepted and r.accepted.term_id == "date_of_birth"


def test_tie_at_the_top_is_not_accepted():
    # "status" is a subset of three terms: token_set_ratio gives each 100, so fuzzy must not pick one.
    r = match_column("status", TERMS, ABBR)
    assert r.accepted is None
    assert {c.term_id for c in r.candidates[:3]} == {"policy_status", "quote_status", "claim_status"}


def test_expansion_breaks_the_tie():
    r = match_column("pol_sts", TERMS, ABBR)
    assert r.accepted and r.accepted.term_id == "policy_status"


def test_below_threshold_keeps_top_five_sorted():
    r = match_column("wibble_factor", TERMS, ABBR)
    assert r.accepted is None
    assert len(r.candidates) == 5
    scores = [c.score for c in r.candidates]
    assert scores == sorted(scores, reverse=True)


def test_threshold_is_configurable():
    r = match_column("cust_id", TERMS, ABBR, fuzzy_accept=101)
    assert r.accepted is None and r.candidates[0].term_id == "client_identifier"


def test_candidate_order_is_deterministic():
    a = match_column("status", TERMS, ABBR)
    b = match_column("status", list(reversed(TERMS)), ABBR)
    assert [c.term_id for c in a.candidates] == [c.term_id for c in b.candidates]


def test_shipped_dictionary_is_well_formed():
    d = load_abbreviations()
    assert len(d) > 50
    for k, vs in d.items():
        assert k == k.lower() and k.isalnum(), k
        assert vs and len(vs) == len(set(vs)), k
        for v in vs:
            assert v == v.lower() and v.strip() == v, v
        assert tokenize(k) == [k], f"key {k!r} would never be produced by the tokenizer"
    assert d["exp"] == ["expiry", "exposure", "expense"]
    assert d["rev"] == ["reversal", "revenue", "review"]
    assert d["ph"] == ["phone", "policyholder"]
    assert d["ba"] == ["binding authority", "bank account"]
    assert d["co"] == ["company", "cover", "contact"]


# --- column-token coverage (specs/hydration.md step 3: "Fuzzy accept also needs full column-token coverage") ---

COVERAGE_TERMS = [
    Term("cause_of_loss", "Cause of Loss", "What caused the loss.", ["peril", "loss cause", "col"], "internal"),
    Term("policy_status", "Policy Status", "Status of a policy.", [], "internal"),
]


def test_uncovered_column_token_blocks_fuzzy_accept():
    # "tmp col" vs synonym "col": token_set_ratio is 100 (subset), but "tmp" is not explained by the target.
    r = match_column("tmp_col", COVERAGE_TERMS, {})
    assert r.accepted is None
    assert r.candidates[0].term_id == "cause_of_loss"


def test_full_coverage_after_expansion_is_still_accepted():
    terms = [Term("customer_identifier", "Customer Identifier", "Unique id of a customer.", [], "internal")]
    r = match_column("cust_id", terms, {"cust": ["customer"], "id": ["identifier"]})
    assert r.accepted is not None and r.accepted.term_id == "customer_identifier"


def test_target_may_have_extra_tokens():
    # Measured: token_set_ratio("claim status", "claim status code") == 100, so default fuzzy_accept (90) holds.
    terms = [Term("claim_status_code", "Claim Status Code", "Coded status of a claim.", [], "internal")]
    r = match_column("claim_status", terms, {})
    assert r.accepted is not None and r.accepted.term_id == "claim_status_code"


def test_extra_column_token_is_not_accepted():
    terms = [Term("claim_status", "Claim Status", "Status of a claim.", [], "internal")]
    r = match_column("claim_status_flag", terms, {})  # "flag" is unexplained
    assert r.accepted is None
    assert r.candidates[0].term_id == "claim_status"


def test_coverage_is_checked_on_the_expanded_reading():
    terms = [Term("claim_status", "Claim Status", "Status of a claim.", [], "internal")]
    r = match_column("clm_sts", terms, {"clm": ["claim"], "sts": ["status"]})
    assert r.accepted is not None and r.accepted.term_id == "claim_status"


# --- table-name tie-break (specs/hydration.md step 3: "Candidates with equal scores are ordered by overlap
# with the table name's tokens (singularised: `payments` -> `payment`), then term_id") ---

AMOUNT_TERMS = [
    Term(f"{x.replace(' ', '_')}_amount", f"{x.title()} Amount", f"Amount of {x}.", [], "confidential")
    for x in ["brokerage", "claim", "claim paid", "claim reserve", "deductible", "payment", "subrogation"]
]
AMT = {"amt": "amount"}


def test_table_tie_break_puts_matching_entity_first():
    r = match_column("amt", AMOUNT_TERMS, AMT, table="cat.raw.payments")
    assert r.candidates[0].term_id == "payment_amount"
    assert "payment_amount" in [c.term_id for c in r.candidates[:5]]


def test_table_tie_break_never_accepts_a_tie():
    r = match_column("amt", AMOUNT_TERMS, AMT, table="cat.raw.payments")
    assert len({c.score for c in r.candidates}) == 1  # still a genuine tie
    assert r.accepted is None


def test_no_table_keeps_term_id_order():
    r = match_column("amt", AMOUNT_TERMS, AMT, table=None)
    assert [c.term_id for c in r.candidates] == [
        "brokerage_amount", "claim_amount", "claim_paid_amount", "claim_reserve_amount", "deductible_amount"]
    assert r.accepted is None
    assert [c.term_id for c in match_column("amt", AMOUNT_TERMS, AMT).candidates] == [
        c.term_id for c in r.candidates]  # table defaults to None


def test_table_tokens_are_tokenized_and_singularised():
    r = match_column("amt", AMOUNT_TERMS, AMT, table="cat.raw.claims_extract_au")
    assert r.candidates[0].term_id.startswith("claim_")
    assert r.accepted is None


def test_only_last_dotted_segment_of_table_counts():
    # "payment" in the catalog name must not lift payment_amount; only the table segment ("claims") counts.
    r = match_column("amt", AMOUNT_TERMS, AMT, table="payment.raw.claims")
    assert r.candidates[0].term_id.startswith("claim_")


def test_equal_overlap_falls_back_to_term_id():
    # claim, claim_paid and claim_reserve each overlap "claim" once -> term_id order among them.
    r = match_column("amt", AMOUNT_TERMS, AMT, table="cat.raw.claims")
    assert [c.term_id for c in r.candidates[:3]] == ["claim_amount", "claim_paid_amount", "claim_reserve_amount"]


def test_score_order_beats_table_overlap():
    terms = [*AMOUNT_TERMS, Term("payment_method", "Payment Method", "How it was paid.", [], "internal")]
    r = match_column("payment_amt", terms, AMT, table="cat.raw.claims")
    assert r.candidates[0].term_id == "payment_amount"
    scores = [c.score for c in r.candidates]
    assert scores == sorted(scores, reverse=True)
    assert all(c.score < r.candidates[0].score for c in r.candidates[1:])
    assert r.accepted is not None and r.accepted.term_id == "payment_amount"


def test_table_tie_break_is_deterministic():
    a = match_column("amt", AMOUNT_TERMS, AMT, table="cat.raw.payments")
    b = match_column("amt", list(reversed(AMOUNT_TERMS)), AMT, table="cat.raw.payments")
    assert a == b == match_column("amt", AMOUNT_TERMS, AMT, table="cat.raw.payments")


# --- ignore_tokens: currency suffixes and attached trailing digits (approved; in specs/hydration.md step 3) ---

IGNORE_ABBR = {"addr": "address", "ln": "line", "acct": "account", "lname": "last name", "ref": "reference",
               "def": ["default", "definition"]}
RULES = IgnoreRules(currencies=frozenset({"gbp", "aud", "usd", "eur"}), strip_trailing_digits=True)


def test_ignore_rules_defaults_are_empty_and_frozen():
    r = IgnoreRules()
    assert r.currencies == frozenset() and r.strip_trailing_digits is False
    with pytest.raises(AttributeError):
        r.strip_trailing_digits = True


def test_load_ignore_reads_the_block(tmp_path):
    p = tmp_path / "abbr.yaml"
    p.write_text("abbreviations:\n  cust: customer\nignore_tokens:\n  currencies: [GBP, aud]\n"
                 "  strip_trailing_digits: true\n", encoding="utf-8")
    r = load_ignore(p)
    assert r.currencies == frozenset({"gbp", "aud"}) and r.strip_trailing_digits is True


def test_load_ignore_without_block_is_empty(tmp_path):
    p = tmp_path / "abbr.yaml"
    p.write_text("abbreviations:\n  cust: customer\n", encoding="utf-8")
    assert load_ignore(p) == IgnoreRules()


def test_shipped_ignore_rules():
    r = load_ignore()
    assert r.currencies == frozenset({"gbp", "aud", "usd", "eur"}) and r.strip_trailing_digits is True


@pytest.mark.parametrize("name,tokens", [
    ("addr_line1", ["addr", "line"]),  # "line" is the single-word expansion of "ln" -> known
    ("addr2", ["addr"]),  # abbreviation key -> known
    ("acct_last4", ["acct", "last", "4"]),  # "last" only appears inside the multi-word "last name"
    ("Col17", ["col", "17"]),  # "col" unknown
    ("LEGACY_REF_2", ["legacy", "ref", "2"]),  # digit is its own part
    ("user_def_3", ["user", "def", "3"]),  # digit is its own part, even after a known token
    ("filler_01", ["filler", "01"]),
    ("gwp_gbp", ["gwp"]),
    ("IncurredAUD", ["incurred"]),  # currency is case-insensitive
    ("gbp", ["gbp"]),  # never return an empty list
])
def test_tokenize_with_ignore_rules(name, tokens):
    assert tokenize(name, IGNORE_ABBR, RULES) == tokens


@pytest.mark.parametrize("name", ["addr_line1", "addr2", "gwp_gbp", "IncurredAUD", "Col17"])
def test_tokenize_without_ignore_is_unchanged(name):
    assert tokenize(name, IGNORE_ABBR) == tokenize(name) == tokenize(name, IGNORE_ABBR, None)


def test_empty_rules_drop_nothing():
    assert tokenize("addr_line1_gbp", IGNORE_ABBR, IgnoreRules()) == ["addr", "line", "1", "gbp"]


def test_digits_kept_when_stripping_disabled():
    rules = IgnoreRules(currencies=frozenset({"gbp"}), strip_trailing_digits=False)
    assert tokenize("addr_line1_gbp", IGNORE_ABBR, rules) == ["addr", "line", "1"]


def test_ignore_does_not_rescue_an_unknown_digit_column():
    terms = [Term("cause_of_loss", "Cause of Loss", "What caused the loss.", ["col"], "internal")]
    r = match_column("Col17", terms, IGNORE_ABBR, ignore=RULES)
    assert r.accepted is None and r.candidates[0].term_id == "cause_of_loss"


def test_currency_suffix_is_ignored_for_coverage():
    terms = [Term("gwp", "Gross Written Premium", "Premium written.", ["gwp"], "confidential")]
    assert match_column("gwp_gbp", terms, IGNORE_ABBR).accepted is None  # "gbp" unexplained without rules
    r = match_column("gwp_gbp", terms, IGNORE_ABBR, ignore=RULES)
    # single-token guard: a token was ignored and one is left -> the LLM decides; the term stays first
    assert r.accepted is None and r.candidates[0].term_id == "gwp"


def test_attached_digit_is_ignored_for_coverage():
    terms = [Term("postal_address", "Postal Address", "Postal address.", ["address line"], "pii")]
    assert match_column("addr_line1", terms, IGNORE_ABBR).accepted is None  # "1" unexplained without rules
    r = match_column("addr_line1", terms, IGNORE_ABBR, ignore=RULES)
    assert r.accepted is not None and r.accepted.term_id == "postal_address"


def test_camel_case_currency_is_ignored_for_coverage():
    terms = [Term("claim_amount", "Claim Amount", "Amount of a claim.", ["incurred"], "confidential")]
    r = match_column("IncurredAUD", terms, IGNORE_ABBR, ignore=RULES)
    # single-token guard: a token was ignored and one is left -> the LLM decides; the term stays first
    assert r.accepted is None and r.candidates[0].term_id == "claim_amount"


# --- single-token guard (lead): "if the ignore rules removed any token AND exactly one token is left, never
# fuzzy-accept". The column goes to the LLM; candidates are still computed. Uses the SHIPPED dictionary. ---

GUARD_TERMS = [
    Term("sum_insured", "Sum Insured", "Sum insured.", ["value", "sum insured"], "confidential"),
    Term("cause_of_loss", "Cause of Loss", "What caused the loss.", ["col", "peril"], "internal"),
    Term("deductible_amount", "Deductible Amount", "Deductible.", ["excess", "xs"], "confidential"),
    Term("gwp", "Gross Written Premium", "Premium written.", ["gwp"], "confidential"),
    Term("claim_amount", "Claim Amount", "Amount of a claim.", ["incurred"], "confidential"),
    Term("postal_address", "Postal Address", "Postal address.", ["address", "address line"], "pii"),
]


@pytest.fixture(scope="module")
def shipped():
    return load_abbreviations(), load_ignore()


@pytest.mark.parametrize("name,term", [
    ("Value17", "sum_insured"), ("val1", "sum_insured"), ("value_gbp", "sum_insured"),
    ("col_gbp", "cause_of_loss"), ("col_usd", "cause_of_loss"), ("excess1", "deductible_amount"),
    ("gwp_gbp", "gwp"), ("IncurredAUD", "claim_amount"),
])
def test_single_token_left_after_ignore_is_not_accepted(shipped, name, term):
    abbr, rules = shipped
    r = match_column(name, GUARD_TERMS, abbr, ignore=rules)
    assert r.accepted is None
    assert r.candidates and r.candidates[0].term_id == term


def test_two_tokens_left_after_ignore_is_still_accepted(shipped):
    abbr, rules = shipped
    r = match_column("addr_line1", GUARD_TERMS, abbr, ignore=rules)
    assert r.accepted is not None and r.accepted.term_id == "postal_address"


@pytest.mark.parametrize("name", ["Col17", "tmp_col"])
def test_uninformative_names_stay_unaccepted_with_shipped_rules(shipped, name):
    abbr, rules = shipped
    assert match_column(name, GUARD_TERMS, abbr, ignore=rules).accepted is None


def test_single_token_guard_is_idempotent(shipped):
    abbr, rules = shipped
    assert (match_column("gwp_gbp", GUARD_TERMS, abbr, ignore=rules)
            == match_column("gwp_gbp", GUARD_TERMS, abbr, ignore=rules))


# --- noise-name rule (abbreviations.yaml noise_tokens) ---------------------------------------------------------
# New names are imported inside the tests so the existing tests in this file keep running before they exist.

SHIPPED_NOISE = frozenset({"tmp", "temp", "test", "unused", "col", "field", "filler", "legacy", "ref", "user", "def",
                           "udf", "misc", "attr", "x"})


def _noise_api():
    from gates.hydration.match import load_noise, noise_tokens_of
    return load_noise, noise_tokens_of


def test_load_noise_reads_and_lowercases(tmp_path):
    load_noise, _ = _noise_api()
    p = tmp_path / "abbr.yaml"
    p.write_text("abbreviations: {id: identifier}\nnoise_tokens: [TMP, Col, x]\n", encoding="utf-8")
    assert load_noise(p) == frozenset({"tmp", "col", "x"})
    assert isinstance(load_noise(p), frozenset)


def test_load_noise_missing_key_gives_empty_frozenset(tmp_path):
    load_noise, _ = _noise_api()
    p = tmp_path / "abbr.yaml"
    p.write_text("abbreviations: {id: identifier}\n", encoding="utf-8")
    assert load_noise(p) == frozenset()


def test_load_noise_shipped_file_is_the_approved_15_tokens():
    load_noise, _ = _noise_api()
    assert load_noise() == SHIPPED_NOISE


@pytest.mark.parametrize("column, tokens", [
    ("Col17", ["col", "17"]),
    ("user_def_3", ["user", "def", "3"]),
    ("LEGACY_REF_2", ["legacy", "ref", "2"]),
    ("tmp_col", ["tmp", "col"]),
    ("filler_01", ["filler", "01"]),
    ("TEMP_FIELD", ["temp", "field"]),
    ("x_1", ["x", "1"]),
    ("udf", ["udf"]),
    ("test_col", ["test", "col"]),
    ("unused_1", ["unused", "1"]),
])
def test_noise_names_are_caught_and_return_their_tokens(column, tokens):
    _, noise_tokens_of = _noise_api()
    assert noise_tokens_of(column, SHIPPED_NOISE) == tokens


@pytest.mark.parametrize("column", [
    "col_cd",  # cd means code
    "x_flag",
    "misc_txt",
    "etl_batch_id",
    "chk_sum",
    "rowhash",
    "STATE_CD",
    "pol_ref",
    "user_id",
])
def test_names_with_a_meaningful_token_are_not_noise(column):
    _, noise_tokens_of = _noise_api()
    assert noise_tokens_of(column, SHIPPED_NOISE) is None


def test_bare_number_is_not_noise_a_name_needs_at_least_one_noise_token():
    _, noise_tokens_of = _noise_api()
    assert noise_tokens_of("17", SHIPPED_NOISE) is None
    assert noise_tokens_of("01_2", SHIPPED_NOISE) is None


def test_empty_name_is_not_noise():
    _, noise_tokens_of = _noise_api()
    assert noise_tokens_of("", SHIPPED_NOISE) is None
    assert noise_tokens_of("__", SHIPPED_NOISE) is None


def test_empty_noise_set_catches_nothing():
    _, noise_tokens_of = _noise_api()
    assert noise_tokens_of("tmp_col", frozenset()) is None


def test_noise_tokens_of_is_idempotent():
    _, noise_tokens_of = _noise_api()
    assert noise_tokens_of("user_def_3", SHIPPED_NOISE) == noise_tokens_of("user_def_3", SHIPPED_NOISE)


def test_noise_rule_never_catches_a_golden_column_with_a_true_term():
    from gates.hydration.match import load_weak_noise
    from synthetic.generate_data import golden_set

    load_noise, noise_tokens_of = _noise_api()
    noise, weak = load_noise(), load_weak_noise()
    rows = golden_set()
    caught = {r["column_name"] for r in rows if noise_tokens_of(r["column_name"], noise, weak) is not None}
    wrongly = [r["column_name"] for r in rows
               if r["expected_term_id"] and noise_tokens_of(r["column_name"], noise, weak) is not None]
    assert wrongly == []
    assert caught == {"tmp_col", "Col17", "user_def_3", "LEGACY_REF_2", "filler_01"}


# --- weak noise tokens (abbreviations.yaml weak_noise_tokens) ----------------------------------------------------

SHIPPED_WEAK = frozenset({"ref", "user"})


def test_load_weak_noise_reads_and_lowercases(tmp_path):
    from gates.hydration.match import load_weak_noise

    p = tmp_path / "abbr.yaml"
    p.write_text("noise_tokens: [ref, user]\nweak_noise_tokens: [REF, User]\n", encoding="utf-8")
    assert load_weak_noise(p) == frozenset({"ref", "user"})
    assert isinstance(load_weak_noise(p), frozenset)


def test_load_weak_noise_missing_key_gives_empty_frozenset(tmp_path):
    from gates.hydration.match import load_weak_noise

    p = tmp_path / "abbr.yaml"
    p.write_text("noise_tokens: [tmp]\n", encoding="utf-8")
    assert load_weak_noise(p) == frozenset()


def test_load_weak_noise_shipped_file():
    from gates.hydration.match import load_weak_noise

    assert load_weak_noise() == SHIPPED_WEAK


@pytest.mark.parametrize("column", ["ref", "user", "user_ref", "ref_1", "User_Ref_2"])
def test_names_of_only_weak_noise_are_not_caught(column):
    _, noise_tokens_of = _noise_api()
    assert noise_tokens_of(column, SHIPPED_NOISE, SHIPPED_WEAK) is None


@pytest.mark.parametrize("column, tokens", [
    ("LEGACY_REF_2", ["legacy", "ref", "2"]),
    ("user_def_3", ["user", "def", "3"]),
    ("x_ref", ["x", "ref"]),
    ("Col17", ["col", "17"]),
    ("tmp_col", ["tmp", "col"]),
    ("filler_01", ["filler", "01"]),
    ("TEMP_FIELD", ["temp", "field"]),
    ("udf", ["udf"]),
])
def test_weak_tokens_still_caught_with_a_strong_noise_token(column, tokens):
    _, noise_tokens_of = _noise_api()
    assert noise_tokens_of(column, SHIPPED_NOISE, SHIPPED_WEAK) == tokens


@pytest.mark.parametrize("column", ["col_cd", "pol_ref", "user_id", "17"])
def test_weak_set_does_not_make_meaningful_names_noise(column):
    _, noise_tokens_of = _noise_api()
    assert noise_tokens_of(column, SHIPPED_NOISE, SHIPPED_WEAK) is None


def test_weak_default_is_empty_so_weak_only_names_are_caught_without_it():
    _, noise_tokens_of = _noise_api()
    assert noise_tokens_of("user_ref", SHIPPED_NOISE) == ["user", "ref"]


def test_noise_tokens_of_with_weak_is_idempotent():
    _, noise_tokens_of = _noise_api()
    a = noise_tokens_of("User_Ref_2", SHIPPED_NOISE, SHIPPED_WEAK)
    assert a == noise_tokens_of("User_Ref_2", SHIPPED_NOISE, SHIPPED_WEAK)
    assert a is None
