from synthetic.fields import TABLES
from synthetic.generate_data import generate, golden_set, load_glossary

SMALL = 0.05


def test_deterministic():
    a = generate(seed=7, scale=SMALL)
    b = generate(seed=7, scale=SMALL)
    assert a == b


def test_deterministic_across_processes():
    # Same seed in a fresh interpreter must give identical data (catches clock- or hash-dependent values).
    import json
    import subprocess
    import sys

    code = ("import json,sys; from synthetic.generate_data import generate; "
            "json.dump(generate(seed=7, scale=0.02), sys.stdout, default=str, sort_keys=True)")
    runs = [subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout
            for _ in range(2)]
    assert runs[0] == runs[1]
    assert json.loads(runs[0])["clients"]["cust_id"]


def test_every_table_and_column_generated():
    data = generate(scale=SMALL)
    for t in TABLES:
        assert t.name in data
        assert list(data[t.name]) == [c.name for c in t.cols]
        lengths = {len(v) for v in data[t.name].values()}
        assert len(lengths) == 1 and lengths.pop() >= 1


def test_foreign_keys_resolve():
    data = generate(scale=SMALL)
    clients = set(data["clients"]["cust_id"])
    policies = set(data["policies"]["pol_ref"])
    assert set(data["policies"]["cust_id"]) <= clients
    assert set(data["claims"]["pol_ref"]) <= policies
    assert set(data["payments"]["pol_ref"]) <= policies
    assert set(data["policies"]["brk_cd"]) <= set(data["brokers"]["brk_cd"])


def test_policy_dates_consistent():
    data = generate(scale=SMALL)
    p = data["policies"]
    assert all(e > s for s, e in zip(p["incept_dt"], p["exp_dt"]))


def test_golden_set_terms_exist_in_glossary():
    terms = {t["term_id"] for t in load_glossary()["terms"]}
    for row in golden_set():
        assert row["expected_term_id"] is None or row["expected_term_id"] in terms


def test_golden_set_shape():
    gs = golden_set()
    assert len(gs) == sum(len(t.cols) for t in TABLES)
    assert sum(r["expected_term_id"] is None for r in gs) >= 10, "need no-match cases or the eval flatters itself"
    assert {r["difficulty"] for r in gs} == {"easy", "abbrev", "hard", "none"}


def test_glossary_has_distractors():
    used = {r["expected_term_id"] for r in golden_set() if r["expected_term_id"]}
    terms = {t["term_id"] for t in load_glossary()["terms"]}
    assert len(terms - used) >= 10


def test_glossary_ids_unique():
    ids = [t["term_id"] for t in load_glossary()["terms"]]
    assert len(ids) == len(set(ids))


def test_claim_notes_contain_planted_pii():
    data = generate(scale=0.2)
    notes = data["claims"]["notes_txt"]
    assert any(n.startswith("Insured ") for n in notes)
