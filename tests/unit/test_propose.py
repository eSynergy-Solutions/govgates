import json
import re

import pytest

import gates.hydration.propose as propose_mod
from gates.common.llm_client import LLMResponse
from gates.hydration.match import Candidate, MatchResult
from gates.hydration.profile import ColumnProfile
from gates.hydration.propose import (
    NO_MATCH,
    PROMPT_VERSION,
    ColumnInput,
    build_messages,
    enforce_floor,
    fuzzy_proposal,
    load_prompt,
    parse_response,
    propose_llm,
    split_tokens,
)


def _profile(col, hits=(), dtype="string", any_hits=None, avg_length=12.0, distinct_pct=99.0):
    return ColumnProfile(col, dtype, 1.5, distinct_pct, list(hits), "f" * 64,
                         list(any_hits if any_hits is not None else hits), avg_length)


def _cands(*ids):
    return [Candidate(i, i.replace("_", " ").title(), f"Definition of {i}.", "internal", 80.0 - n)
            for n, i in enumerate(ids)]


INPUTS = [
    ColumnInput("clm_ref", "claim reference", _profile("clm_ref"), _cands("claim_reference", "policy_reference")),
    ColumnInput("ph_email", "phone email", _profile("ph_email", ["email"]), _cands("email_address", "phone_number")),
    ColumnInput("misc_txt", "misc txt", _profile("misc_txt"), _cands("claim_description")),
]


def _entry(col, term, sens="internal", conf=0.8):
    return {"column": col, "term_id": term, "description": f"{col} desc", "sensitivity": sens,
            "confidence": conf, "rationale": "because"}


def _good():
    return json.dumps({"columns": [_entry("clm_ref", "claim_reference"),
                                   _entry("ph_email", "email_address", "pii", 0.95),
                                   _entry("misc_txt", NO_MATCH, "internal", 0.6)]})


class FakeClient:
    model = "fake-model"

    def __init__(self, text):
        self.text, self.calls, self.messages = text, 0, None

    def chat(self, messages, temperature=0.0, max_tokens=4000):
        assert temperature == 0.0
        self.calls += 1
        self.messages = messages
        return LLMResponse(self.text, prompt_tokens=700, completion_tokens=301, model=self.model)


def test_prompt_file_has_both_sections():
    system, user = load_prompt(PROMPT_VERSION)
    assert "NO_MATCH" in system and "{{columns_json}}" in user and "{{table}}" in user


def test_messages_carry_names_stats_and_candidates_only():
    msgs = build_messages("gate_sandbox.raw.claims", INPUTS, version="propose_v1")
    payload = msgs[1]["content"]
    assert "Table: claims" in payload and "gate_sandbox" not in payload
    cols = json.loads(payload.split("Columns:\n", 1)[1])
    assert [c["column"] for c in cols] == ["clm_ref", "ph_email", "misc_txt"]
    assert set(cols[0]) == {"column", "expanded_name", "data_type", "null_pct", "distinct_pct", "pattern_hits",
                            "any_pattern_hits", "avg_length", "candidates"}
    assert "sample_hash" not in payload  # not useful to the model
    assert set(cols[0]["candidates"][0]) == {"term_id", "name", "definition", "sensitivity", "fuzzy_score"}


def test_parse_good_response():
    props, rejects = parse_response(_good(), INPUTS)
    assert not rejects
    by = {p.column: p for p in props}
    assert by["clm_ref"].term_id == "claim_reference"
    assert by["misc_txt"].term_id is None  # NO_MATCH stored as null, like golden_set
    assert by["ph_email"].confidence == 0.95


def test_parse_accepts_fenced_json():
    props, rejects = parse_response("```json\n" + _good() + "\n```", INPUTS)
    assert len(props) == 3 and not rejects


def test_term_outside_candidates_is_rejected():
    bad = json.dumps({"columns": [_entry("clm_ref", "policy_status"),
                                  _entry("ph_email", "email_address", "pii"), _entry("misc_txt", NO_MATCH)]})
    props, rejects = parse_response(bad, INPUTS)
    assert [r.column for r in rejects] == ["clm_ref"] and "not a candidate" in rejects[0].reason
    assert {p.column for p in props} == {"ph_email", "misc_txt"}


def test_another_columns_candidate_is_rejected():
    bad = json.dumps({"columns": [_entry("clm_ref", "email_address"),
                                  _entry("ph_email", "email_address", "pii"), _entry("misc_txt", NO_MATCH)]})
    _, rejects = parse_response(bad, INPUTS)
    assert [r.column for r in rejects] == ["clm_ref"]


@pytest.mark.parametrize("field,value", [("sensitivity", "secret"), ("confidence", 1.5), ("confidence", "high")])
def test_invalid_fields_are_rejected(field, value):
    e = _entry("clm_ref", "claim_reference")
    e[field] = value
    bad = json.dumps({"columns": [e, _entry("ph_email", "email_address", "pii"), _entry("misc_txt", NO_MATCH)]})
    _, rejects = parse_response(bad, INPUTS)
    assert [r.column for r in rejects] == ["clm_ref"]


def test_missing_unknown_and_duplicate_columns():
    bad = json.dumps({"columns": [_entry("clm_ref", "claim_reference"), _entry("clm_ref", "claim_reference"),
                                  _entry("nope", NO_MATCH)]})
    props, rejects = parse_response(bad, INPUTS)
    assert [p.column for p in props] == ["clm_ref"]
    reasons = {r.column: r.reason for r in rejects}
    assert "duplicate" in reasons["clm_ref"] and "unknown" in reasons["nope"]
    assert "missing" in reasons["ph_email"] and "missing" in reasons["misc_txt"]


def test_unparseable_response_rejects_every_column():
    props, rejects = parse_response("Sure! Here you go: {", INPUTS)
    assert not props and len(rejects) == 3


def test_pattern_floor():
    assert enforce_floor("internal", ["email"]) == "pii"
    assert enforce_floor("public", ["iso_date", "postcode"]) == "pii"
    assert enforce_floor("confidential", ["currency_amount"]) == "confidential"
    assert enforce_floor(None, ["sort_code"]) == "pii"
    assert enforce_floor(None, []) is None


def test_term_floor_model_may_raise_never_lower():
    assert enforce_floor("public", [], term_sensitivity="confidential") == "confidential"  # lowered -> restored
    assert enforce_floor("pii", [], term_sensitivity="internal") == "pii"  # raised -> kept
    assert enforce_floor("internal", ["email"], term_sensitivity="confidential") == "pii"  # max of all three
    assert enforce_floor(None, [], term_sensitivity="internal") == "internal"


def test_llm_cannot_lower_the_chosen_terms_sensitivity():
    cands = [Candidate("gwp", "Gross Written Premium", "Premium.", "confidential", 70.0)]
    inputs = [ColumnInput("gwp_amt", "gwp amount", _profile("gwp_amt"), cands)]
    text = json.dumps({"columns": [_entry("gwp_amt", "gwp", "public", 0.9)]})
    out = propose_llm("t", inputs, FakeClient(text), version="propose_v1")
    assert out.proposals[0].sensitivity == "confidential"


def test_no_match_keeps_model_sensitivity_without_term_floor():
    cands = [Candidate("gwp", "Gross Written Premium", "Premium.", "confidential", 40.0)]
    inputs = [ColumnInput("note", "note", _profile("note"), cands)]
    text = json.dumps({"columns": [_entry("note", NO_MATCH, "internal", 0.85)]})
    assert propose_llm("t", inputs, FakeClient(text), version="propose_v1").proposals[0].sensitivity == "internal"


def test_split_tokens_sums_to_total():
    assert split_tokens(1001, 3) == [334, 334, 333]
    assert split_tokens(0, 2) == [0, 0]
    assert split_tokens(5, 0) == []


def test_fuzzy_proposal_uses_term_and_pii_floor():
    cand = Candidate("postcode", "Postcode", "Postal code of a client address.", "public", 100.0)
    m = MatchResult("post_cd", "post code", cand, [cand])
    p = fuzzy_proposal(m, _profile("post_cd", ["postcode"]))
    assert (p.term_id, p.method, p.confidence, p.sensitivity, p.tokens) == ("postcode", "fuzzy", 1.0, "pii", 0)
    assert p.description == cand.definition and p.model is None


def test_propose_llm_one_call_for_all_columns():
    client = FakeClient(_good())
    out = propose_llm("gate_sandbox.raw.claims", INPUTS, client, version="propose_v1")
    assert client.calls == 1 and out.calls == 1
    assert {p.column for p in out.proposals} == {"clm_ref", "ph_email", "misc_txt"}
    assert sum(p.tokens for p in out.proposals) == 1001
    assert all(p.method == "llm" and p.model == "fake-model" and p.prompt_version == "propose_v1"
               for p in out.proposals)


def test_propose_llm_gives_rejected_columns_a_zero_confidence_placeholder():
    bad = json.dumps({"columns": [_entry("clm_ref", "made_up"), _entry("ph_email", "email_address", "internal"),
                                  _entry("misc_txt", NO_MATCH)]})
    out = propose_llm("t", INPUTS, FakeClient(bad), version="propose_v1")
    by = {p.column: p for p in out.proposals}
    assert len(out.rejections) == 1
    assert by["clm_ref"].term_id is None and by["clm_ref"].confidence == 0.0
    assert by["clm_ref"].rationale.startswith("REJECTED")
    assert by["ph_email"].sensitivity == "pii"  # floor applied to LLM output too


def test_propose_llm_makes_no_call_with_nothing_to_resolve():
    client = FakeClient(_good())
    out = propose_llm("t", [], client)
    assert client.calls == 0 and out.calls == 0 and out.proposals == []


# Spec (hydration.md, Sensitivity classes): "`public` only if the chosen term is `public`;
# otherwise the floor is `internal` (so a `NO_MATCH` proposal is never `public`)."
def test_public_needs_a_public_term():
    assert enforce_floor("public", [], None) == "internal"  # no term -> internal floor
    assert enforce_floor("public", [], "public") == "public"
    assert enforce_floor("public", [], "internal") == "internal"
    assert enforce_floor(None, [], "public") == "public"


def test_no_sensitivity_and_no_term_stays_none():
    # Rejected placeholders carry no sensitivity; the floor does not invent one.
    assert enforce_floor(None, [], None) is None


def test_no_match_is_never_public():
    cands = [Candidate("gwp", "Gross Written Premium", "Premium.", "confidential", 40.0)]
    inputs = [ColumnInput("note", "note", _profile("note"), cands)]
    text = json.dumps({"columns": [_entry("note", NO_MATCH, "public", 0.85)]})
    assert propose_llm("t", inputs, FakeClient(text), version="propose_v1").proposals[0].sensitivity == "internal"


# --- prompt version + table column names (specs/hydration.md step 4: "The prompt version is a job parameter
# (`prompt_version`, `propose_v<N>`); proposals record it. A prompt whose USER section contains
# `{{table_columns}}` also gets every column name in the table (names only, no values or stats)") ---

REAL_PROMPTS_DIR = propose_mod.PROMPTS_DIR
SHIPPED_PROMPTS = sorted(REAL_PROMPTS_DIR.glob("propose_v*.md"))
TABLE_COLUMNS = ["clm_ref", "ph_email", "misc_txt", "loss_dt", "insured_nm"]

V7 = "## SYSTEM\nFixture system NO_MATCH.\n\n## USER\n\nTable: {{table}}\n\nColumns:\n{{columns_json}}\n"
V8 = ("## SYSTEM\nFixture system NO_MATCH.\n\n## USER\n\nTable: {{table}}\n\nAll columns: {{table_columns}}"
      "\nEND_NAMES\n\nColumns:\n{{columns_json}}\n")
V9 = "## SYSTEM\nFixture system.\n\n## USER\n\nTable: {{table}}\n{{unknown}}\nColumns:\n{{columns_json}}\n"


@pytest.fixture
def prompt_dir(tmp_path, monkeypatch):
    d = tmp_path / "prompts"
    d.mkdir()
    for name, text in {"propose_v7": V7, "propose_v8": V8, "propose_v9": V9}.items():
        (d / f"{name}.md").write_text(text, encoding="utf-8")
    monkeypatch.setattr(propose_mod, "PROMPTS_DIR", d)
    return d


def _names_in(user: str) -> list:
    return json.loads(user.split("All columns: ", 1)[1].split("\nEND_NAMES", 1)[0])


def test_build_messages_loads_the_named_version(prompt_dir):
    msgs = build_messages("cat.raw.claims", INPUTS, version="propose_v7")
    assert msgs[0]["content"] == "Fixture system NO_MATCH."
    assert msgs[1]["content"].startswith("Table: claims")


@pytest.mark.parametrize("version", ["../propose_v1", "propose_v1.md", "v1", "", "propose_vx"])
def test_invalid_version_is_rejected_before_any_read(prompt_dir, version):
    # Make every bad name resolvable on disk, so only up-front validation can produce the ValueError.
    (prompt_dir.parent / "propose_v1.md").write_text(V7, encoding="utf-8")
    for name in ["propose_v1.md", "v1", "", "propose_vx"]:
        (prompt_dir / f"{name}.md").write_text(V7, encoding="utf-8")
    with pytest.raises(ValueError):
        build_messages("t", INPUTS, version=version)


def test_invalid_version_makes_no_llm_call(prompt_dir):
    client = FakeClient(_good())
    with pytest.raises(ValueError):
        propose_llm("t", INPUTS, client, version="../propose_v7")
    assert client.calls == 0


def test_valid_but_missing_version_raises_file_not_found(prompt_dir):
    with pytest.raises(FileNotFoundError):
        build_messages("t", INPUTS, version="propose_v999")


def test_propose_llm_stamps_the_chosen_version(prompt_dir):
    out = propose_llm("cat.raw.claims", INPUTS, FakeClient(_good()), version="propose_v7")
    assert len(out.proposals) == 3
    assert all(p.prompt_version == "propose_v7" for p in out.proposals)


def test_rejected_placeholders_are_stamped_too(prompt_dir):
    bad = json.dumps({"columns": [_entry("clm_ref", "made_up"), _entry("ph_email", "email_address", "pii")]})
    out = propose_llm("t", INPUTS, FakeClient(bad), version="propose_v7")
    assert {p.prompt_version for p in out.proposals} == {"propose_v7"}


def test_table_columns_placeholder_gets_names_only(prompt_dir):
    msgs = build_messages("cat.raw.claims", INPUTS, version="propose_v8", table_columns=TABLE_COLUMNS)
    assert _names_in(msgs[1]["content"]) == TABLE_COLUMNS
    assert "{{" not in msgs[1]["content"]


def test_table_columns_are_ignored_without_placeholder_v1():
    without = build_messages("cat.raw.claims", INPUTS, version="propose_v1")
    with_cols = build_messages("cat.raw.claims", INPUTS, version="propose_v1", table_columns=TABLE_COLUMNS)
    assert with_cols == without
    assert "insured_nm" not in with_cols[1]["content"]


def test_table_columns_are_ignored_without_placeholder_fixture(prompt_dir):
    assert (build_messages("t", INPUTS, version="propose_v7", table_columns=TABLE_COLUMNS)
            == build_messages("t", INPUTS, version="propose_v7"))


def test_leftover_placeholder_raises(prompt_dir):
    with pytest.raises(ValueError):
        build_messages("t", INPUTS, version="propose_v9", table_columns=TABLE_COLUMNS)


def test_leftover_placeholder_makes_no_llm_call(prompt_dir):
    client = FakeClient(_good())
    with pytest.raises(ValueError):
        propose_llm("t", INPUTS, client, version="propose_v9", table_columns=TABLE_COLUMNS)
    assert client.calls == 0


def test_propose_llm_sends_table_columns_to_the_client(prompt_dir):
    client = FakeClient(_good())
    propose_llm("cat.raw.claims", INPUTS, client, version="propose_v8", table_columns=TABLE_COLUMNS)
    assert _names_in(client.messages[1]["content"]) == TABLE_COLUMNS


def test_build_messages_with_table_columns_is_idempotent(prompt_dir):
    a = build_messages("t", INPUTS, version="propose_v8", table_columns=TABLE_COLUMNS)
    assert a == build_messages("t", INPUTS, version="propose_v8", table_columns=TABLE_COLUMNS)


@pytest.mark.parametrize("path", SHIPPED_PROMPTS, ids=lambda p: p.stem)
def test_shipped_prompts_render_without_leftover_placeholders(path):
    msgs = build_messages("cat.raw.claims", INPUTS[:1], version=path.stem, table_columns=TABLE_COLUMNS)
    for m in msgs:
        assert not re.search(r"\{\{[^{}]*\}\}", m["content"]), (path.stem, m["role"])


def test_there_are_shipped_prompts():
    assert REAL_PROMPTS_DIR.joinpath(f"{PROMPT_VERSION}.md") in SHIPPED_PROMPTS


def test_table_columns_placeholder_without_columns_raises(prompt_dir):
    # Coordinator ruling: a {{table_columns}} prompt called with table_columns=None is a leftover placeholder.
    with pytest.raises(ValueError):
        build_messages("t", INPUTS, version="propose_v8")
    with pytest.raises(ValueError):
        build_messages("t", INPUTS, version="propose_v8", table_columns=None)


# --- check_prompt + single-pass rendering ---

def test_prompt_version_default_is_v3():
    assert PROMPT_VERSION == "propose_v3"


def test_check_prompt_returns_placeholder_names(prompt_dir):
    from gates.hydration.propose import check_prompt
    assert check_prompt("propose_v7") == {"table", "columns_json"}
    assert check_prompt("propose_v8") == {"table", "columns_json", "table_columns"}


def test_check_prompt_rejects_unknown_placeholder(prompt_dir):
    from gates.hydration.propose import check_prompt
    with pytest.raises(ValueError):
        check_prompt("propose_v9")


@pytest.mark.parametrize("version", ["../propose_v1", "propose_v1.md", "v1", "", "propose_vx"])
def test_check_prompt_rejects_bad_version(prompt_dir, version):
    from gates.hydration.propose import check_prompt
    with pytest.raises(ValueError):
        check_prompt(version)


def test_check_prompt_missing_version_raises_file_not_found(prompt_dir):
    from gates.hydration.propose import check_prompt
    with pytest.raises(FileNotFoundError):
        check_prompt("propose_v999")


@pytest.mark.parametrize("path", SHIPPED_PROMPTS, ids=lambda p: p.stem)
def test_check_prompt_accepts_shipped_prompts(path):
    from gates.hydration.propose import check_prompt
    assert check_prompt(path.stem) <= {"table", "columns_json", "table_columns"}


TRICKY = "see {{table_columns}} and {{x}}"


def _tricky_inputs():
    cands = [Candidate("claim_reference", "Claim Reference", TRICKY, "internal", 80.0)]
    return [ColumnInput("clm_ref", "claim reference", _profile("clm_ref"), cands)]


def test_placeholder_text_in_candidate_definition_is_not_rendered_or_rejected(prompt_dir):
    msgs = build_messages("cat.raw.claims", _tricky_inputs(), version="propose_v8", table_columns=TABLE_COLUMNS)
    user = msgs[1]["content"]
    assert _names_in(user) == TABLE_COLUMNS
    cols = json.loads(user.split("Columns:\n", 1)[1])
    assert cols[0]["candidates"][0]["definition"] == TRICKY


def test_placeholder_text_in_column_name_is_kept_verbatim(prompt_dir):
    name = "odd_{{x}}_{{table_columns}}"
    inputs = [ColumnInput(name, "odd", _profile(name), _cands("claim_reference"))]
    msgs = build_messages("t", inputs, version="propose_v8", table_columns=[name])
    user = msgs[1]["content"]
    assert _names_in(user) == [name]
    assert json.loads(user.split("Columns:\n", 1)[1])[0]["column"] == name


def test_placeholder_text_in_content_with_v7_fixture(prompt_dir):
    msgs = build_messages("t", _tricky_inputs(), version="propose_v7")
    assert json.loads(msgs[1]["content"].split("Columns:\n", 1)[1])[0]["candidates"][0]["definition"] == TRICKY


def test_tricky_content_rendering_is_idempotent(prompt_dir):
    a = build_messages("t", _tricky_inputs(), version="propose_v8", table_columns=TABLE_COLUMNS)
    assert a == build_messages("t", _tricky_inputs(), version="propose_v8", table_columns=TABLE_COLUMNS)


# --- noise-name rule proposal ----------------------------------------------------------------------------------

def test_noise_proposal_is_a_rule_with_no_term():
    from gates.hydration.propose import noise_proposal

    p = noise_proposal("tmp_col", _profile("tmp_col"), ["tmp", "col"])
    assert p.column == "tmp_col"
    assert p.method == "rule"
    assert p.term_id is None
    assert p.confidence == 1.0
    assert isinstance(p.description, str) and p.description.strip()


def test_noise_proposal_rationale_mentions_the_tokens():
    from gates.hydration.propose import noise_proposal

    p = noise_proposal("user_def_3", _profile("user_def_3"), ["user", "def", "3"])
    assert p.rationale
    for t in ("user", "def", "3"):
        assert t in p.rationale


def test_noise_proposal_sensitivity_is_internal_without_hits():
    from gates.hydration.propose import noise_proposal

    prof = _profile("Col17")
    p = noise_proposal("Col17", prof, ["col", "17"])
    assert p.sensitivity == enforce_floor("internal", prof.pattern_hits, None) == "internal"


def test_noise_proposal_is_idempotent():
    from gates.hydration.propose import noise_proposal

    prof = _profile("tmp_col")
    assert noise_proposal("tmp_col", prof, ["tmp", "col"]) == noise_proposal("tmp_col", prof, ["tmp", "col"])


# --- rule_proposal: noise name + exceptions (specs/hydration.md, noise names first) ------------------------------

RP_NOISE = frozenset({"tmp", "temp", "test", "unused", "col", "field", "filler", "legacy", "ref", "user", "def",
                      "udf", "misc", "attr", "x"})
RP_WEAK = frozenset({"ref", "user"})


def _rule(column, profile, weak=RP_WEAK):
    from gates.hydration.propose import rule_proposal
    return rule_proposal(column, profile, RP_NOISE, weak)


def test_rule_proposal_on_clean_noise_profile_is_noise_proposal():
    from gates.hydration.propose import noise_proposal

    prof = _profile("user_def_3")
    p = _rule("user_def_3", prof)
    assert p is not None
    assert p == noise_proposal("user_def_3", prof, ["user", "def", "3"])
    assert p.method == "rule" and p.term_id is None and p.confidence == 1.0


def test_rule_proposal_value_pattern_sends_column_on():
    assert _rule("user_def_3", _profile("user_def_3", hits=["iso_date"])) is None


@pytest.mark.parametrize("pii", ["email", "uk_ni_number", "phone", "postcode", "sort_code"])
def test_rule_proposal_any_force_pii_hit_sends_column_on(pii):
    assert _rule("user_def_3", _profile("user_def_3", any_hits=[pii])) is None


def test_rule_proposal_non_pii_any_hit_keeps_the_rule():
    p = _rule("user_def_3", _profile("user_def_3", any_hits=["currency_amount"]))
    assert p is not None and p.method == "rule"


def test_rule_proposal_free_text_sends_column_on():
    assert _rule("user_def_3", _profile("user_def_3", avg_length=80.0, distinct_pct=95.0)) is None


def test_rule_proposal_long_but_low_distinct_keeps_the_rule():
    p = _rule("user_def_3", _profile("user_def_3", avg_length=80.0, distinct_pct=40.0))
    assert p is not None and p.method == "rule"


def test_rule_proposal_free_text_bounds_are_strict():
    assert _rule("user_def_3", _profile("user_def_3", avg_length=30.0, distinct_pct=95.0)) is not None
    assert _rule("user_def_3", _profile("user_def_3", avg_length=80.0, distinct_pct=50.0)) is not None


def test_rule_proposal_avg_length_none_is_not_free_text():
    p = _rule("user_def_3", _profile("user_def_3", avg_length=None, distinct_pct=95.0))
    assert p is not None and p.method == "rule"


@pytest.mark.parametrize("column", ["col_cd", "user_ref", "ref"])
def test_rule_proposal_none_for_non_noise_names(column):
    assert _rule(column, _profile(column)) is None


def test_rule_proposal_weak_defaults_to_empty():
    from gates.hydration.propose import rule_proposal

    assert rule_proposal("user_ref", _profile("user_ref"), RP_NOISE) is not None


def test_rule_proposal_is_idempotent():
    prof = _profile("LEGACY_REF_2")
    assert _rule("LEGACY_REF_2", prof) == _rule("LEGACY_REF_2", prof)


def test_iban_pattern_hit_floors_to_pii():
    assert enforce_floor("internal", ["iban"], term_sensitivity=None) == "pii"
    assert enforce_floor("public", ["iban"], term_sensitivity="public") == "pii"
