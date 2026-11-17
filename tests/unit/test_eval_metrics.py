"""Eval metrics (specs/eval.md). Hand-computed fixture; synthetic names only, not the real golden set."""

import pytest

from gates.eval.metrics import GoldenRow, ProposalRow, evaluate, per_column

G = GoldenRow
P = ProposalRow

# 8 golden columns over 2 tables. Truth term None = no correct term.
GOLDEN = [
    G("t1", "claim_id", "claim_reference", "internal", "easy"),         # 1
    G("t1", "clm_dt", "claim_date", "internal", "abbrev"),              # 2
    G("t1", "ph_email", "email_address", "pii", "easy"),                # 3
    G("t1", "status", "claim_status", "internal", "hard"),              # 4
    G("t1", "misc_txt", None, "confidential", "none"),                  # 5
    G("t2", "pol_no", "policy_number", "internal", "abbrev"),           # 6
    G("t2", "gwp", "gross_written_premium", "confidential", "hard"),    # 7
    G("t2", "notes", None, "internal", "none"),                         # 8
]

PROPOSALS = [
    P("t1", "claim_id", "claim_reference", "internal", 1.0, "fuzzy", 0),        # 1 correct, >= th
    P("t1", "clm_dt", "claim_date", "internal", 0.75, "llm", 100),              # 2 correct, below th
    P("t1", "ph_email", "email_address", "pii", 0.95, "llm", 100),              # 3 correct, pii
    P("t1", "status", "policy_status", "internal", 0.9, "llm", 100),            # 4 wrong term, >= th
    P("t1", "misc_txt", None, "internal", 0.9, "llm", 100),                     # 5 NO_MATCH, under-classified
    P("t2", "pol_no", None, None, 0.0, "llm", 150, rejected=True),              # 6 rejected -> missing
    # 7 t2.gwp: no proposal -> missing
    P("t2", "notes", "claim_description", "confidential", 0.82, "llm", 150),    # 8 wrong term, over-classified
    P("t9", "not_golden", "claim_reference", "internal", 1.0, "fuzzy", 999),    # not in golden -> ignored
]


@pytest.fixture
def m():
    return evaluate(GOLDEN, PROPOSALS)


def test_term_precision(m):
    # non-rejected proposals with a term: 1,2,3,4,8 = 5; correct: 1,2,3 = 3
    assert m["term_precision"] == pytest.approx(3 / 5)


def test_coverage(m):
    # true-term columns: 1,2,3,4,6,7 = 6; non-rejected TERM proposals at conf >= 0.8: 1,3,4 = 3
    # (4 is a wrong term but still counts; 2 is 0.75, 6 rejected, 7 missing)
    assert m["coverage"] == pytest.approx(3 / 6)


def test_coverage_counts_term_proposals_only():
    # spec: NO_MATCH is not coverage. A wrong term at >= threshold still is.
    golden = [G("t1", "clm_ref", "claim_reference", "internal", "abbrev"),
              G("t1", "status", "claim_status", "internal", "hard")]
    no_match = P("t1", "clm_ref", None, "internal", 0.95, "llm", 100)
    wrong_term = P("t1", "status", "policy_status", "internal", 0.95, "llm", 100)
    assert evaluate(golden, [no_match])["coverage"] == pytest.approx(0 / 2)
    assert evaluate(golden, [wrong_term])["coverage"] == pytest.approx(1 / 2)
    assert evaluate(golden, [no_match, wrong_term])["coverage"] == pytest.approx(1 / 2)


def test_no_match_accuracy(m):
    # truth-None columns: 5,8 = 2; proposed None: 5 = 1
    assert m["no_match_accuracy"] == pytest.approx(1 / 2)


def test_pii_recall(m):
    # truth pii: 3 = 1; proposed pii: 3 = 1
    assert m["pii_recall"] == pytest.approx(1.0)


def test_fuzzy_share(m):
    # spec: fuzzy / all proposals. joined proposals incl. rejected 6: 1,2,3,4,5,6,8 = 7; fuzzy: 1 (t9 ignored)
    assert m["fuzzy_share"] == pytest.approx(1 / 7)


def test_tokens_per_dataset(m):
    # joined rows incl. rejected: 0+100+100+100+100+150+150 = 700; tables t1,t2 = 2 (t9 ignored)
    assert m["tokens_per_dataset"] == pytest.approx(700 / 2)


def test_term_precision_by_difficulty(m):
    assert m["term_precision_easy"] == pytest.approx(2 / 2)    # 1,3 both correct
    assert m["term_precision_abbrev"] == pytest.approx(1 / 1)  # 2 correct; 6 rejected so not a term proposal
    assert m["term_precision_hard"] == pytest.approx(0 / 1)    # 4 wrong; 7 missing
    assert m["term_precision_none"] == pytest.approx(0 / 1)    # 8 proposed a term; 5 NO_MATCH not counted


def test_extra_recall_at_threshold(m):
    # true-term columns: 6; correct term at conf >= 0.8: 1,3 = 2
    assert m["extra_recall_at_threshold"] == pytest.approx(2 / 6)


def test_extra_sensitivity_accuracy(m):
    # equal: 1,2,3,4 = 4; wrong: 5 (under), 8 (over), 6 and 7 missing; all golden = 8
    assert m["extra_sensitivity_accuracy"] == pytest.approx(4 / 8)


def test_extra_under_classified_count(m):
    # 5: internal < confidential. 8 is over-classified; 6 and 7 are missing, not counted
    assert m["extra_under_classified_count"] == 1


def test_extra_missing_count(m):
    # 6 rejected, 7 absent
    assert m["extra_missing_count"] == 2


def test_threshold_is_a_parameter():
    # at 0.7, column 2 (0.75 term proposal) now counts: coverage 1,2,3,4 = 4/6; recall 1,2,3 = 3/6
    m = evaluate(GOLDEN, PROPOSALS, threshold=0.7)
    assert m["coverage"] == pytest.approx(4 / 6)
    assert m["extra_recall_at_threshold"] == pytest.approx(3 / 6)


def test_same_input_same_result_and_order_does_not_matter():
    assert evaluate(GOLDEN, PROPOSALS) == evaluate(GOLDEN, PROPOSALS)
    assert evaluate(GOLDEN, PROPOSALS) == evaluate(list(reversed(GOLDEN)), list(reversed(PROPOSALS)))


def test_duplicate_proposal_for_a_column_raises():
    dup = PROPOSALS + [P("t1", "claim_id", "claim_reference", "internal", 0.9, "llm", 10)]
    with pytest.raises(ValueError):
        evaluate(GOLDEN, dup)
    with pytest.raises(ValueError):
        per_column(GOLDEN, dup)


def test_join_is_exact_on_table_and_column():
    golden = [G("t1", "claim_id", "claim_reference", "internal", "easy")]
    props = [P("t1", "Claim_ID", "claim_reference", "internal", 1.0, "fuzzy", 0),
             P("t2", "claim_id", "claim_reference", "internal", 1.0, "fuzzy", 0)]
    m = evaluate(golden, props)
    assert m["extra_missing_count"] == 1
    assert m["term_precision"] is None


def test_zero_denominators_give_none():
    golden = [G("t1", "misc_txt", None, "internal", "none")]
    m = evaluate(golden, [])
    for key in ("term_precision", "coverage", "pii_recall", "fuzzy_share", "tokens_per_dataset",
                "extra_recall_at_threshold", "term_precision_none"):
        assert m[key] is None, key
    assert m["no_match_accuracy"] == 0.0           # 0 / 1, missing counts as wrong
    assert m["extra_sensitivity_accuracy"] == 0.0  # 0 / 1
    assert m["extra_missing_count"] == 1
    assert m["extra_under_classified_count"] == 0


def test_metric_types_are_float_int_or_none(m):
    assert all(v is None or isinstance(v, int | float) for v in m.values())


PER_COLUMN_KEYS = {"table_name", "column_name", "difficulty", "expected_term_id", "proposed_term_id", "term_correct",
                   "expected_sensitivity", "proposed_sensitivity", "method", "confidence", "missing"}


def _rows():
    return {(r["table_name"], r["column_name"]): r for r in per_column(GOLDEN, PROPOSALS)}


def test_per_column_one_row_per_golden_column():
    rows = per_column(GOLDEN, PROPOSALS)
    assert len(rows) == len(GOLDEN)
    assert all(set(r) == PER_COLUMN_KEYS for r in rows)
    assert ("t9", "not_golden") not in _rows()


def test_per_column_proposed_row():
    r = _rows()[("t1", "status")]
    assert r["difficulty"] == "hard"
    assert (r["expected_term_id"], r["proposed_term_id"], r["term_correct"]) == ("claim_status", "policy_status", False)
    assert (r["expected_sensitivity"], r["proposed_sensitivity"]) == ("internal", "internal")
    assert (r["method"], r["confidence"], r["missing"]) == ("llm", 0.9, False)
    assert _rows()[("t1", "claim_id")]["term_correct"] is True


def test_per_column_missing_row():
    r = _rows()[("t2", "gwp")]
    assert r["missing"] is True
    assert r["expected_term_id"] == "gross_written_premium" and r["expected_sensitivity"] == "confidential"
    assert r["proposed_term_id"] is None and r["proposed_sensitivity"] is None
    assert r["term_correct"] is False
    assert r["method"] is None and r["confidence"] is None


def test_per_column_rejected_row_counts_as_missing():
    r = _rows()[("t2", "pol_no")]
    assert r["missing"] is True
    assert r["proposed_term_id"] is None and r["proposed_sensitivity"] is None
    assert r["term_correct"] is False


# --- extra_rule_share: rule / all joined proposals (same denominator as fuzzy_share) ---------------------------

def _rule_fixture():
    golden = [G("t1", "tmp_col", None, "internal", "none"),
              G("t1", "Col17", None, "internal", "none"),
              G("t1", "claim_id", "claim_reference", "internal", "easy"),
              G("t1", "clm_dt", "claim_date", "internal", "abbrev"),
              G("t1", "pol_no", "policy_number", "internal", "abbrev"),
              G("t1", "gwp", "gross_written_premium", "confidential", "hard")]  # no proposal -> not joined
    props = [P("t1", "tmp_col", None, "internal", 1.0, "rule", 0),
             P("t1", "Col17", None, "internal", 1.0, "rule", 0),
             P("t1", "claim_id", "claim_reference", "internal", 1.0, "fuzzy", 0),
             P("t1", "clm_dt", "claim_date", "internal", 0.9, "llm", 100),
             P("t1", "pol_no", None, None, 0.0, "llm", 100, rejected=True),  # joined, counts in denominator
             P("t9", "x_1", None, "internal", 1.0, "rule", 0)]                # not golden -> ignored
    return golden, props


def test_extra_rule_share():
    m = evaluate(*_rule_fixture())
    # joined: tmp_col, Col17, claim_id, clm_dt, pol_no = 5; rule: 2
    assert m["extra_rule_share"] == pytest.approx(2 / 5)


def test_rule_proposals_do_not_count_in_fuzzy_share():
    m = evaluate(*_rule_fixture())
    assert m["fuzzy_share"] == pytest.approx(1 / 5)


def test_extra_rule_share_zero_without_rule_proposals(m):
    assert m["extra_rule_share"] == pytest.approx(0 / 7)


def test_extra_rule_share_none_with_nothing_joined():
    m = evaluate([G("t1", "misc_txt", None, "internal", "none")], [])
    assert m["extra_rule_share"] is None
