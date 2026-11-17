from gates.common.config import GateConfig
from gates.hydration.profile import ColumnProfile
from gates.hydration.propose import Proposal
from gates.hydration.records import (
    PROFILE_SCHEMA,
    PROPOSAL_SCHEMA,
    profile_rows,
    proposal_rows,
    qualify_table,
    run_summary,
)


def _schema_cols(ddl):
    return [part.strip().split(" ")[0] for part in ddl.split(",")]


def test_qualify_table():
    cfg = GateConfig("cat", raw_schema="landing")
    assert qualify_table("claims", cfg) == "cat.landing.claims"
    assert qualify_table("other.sch.tbl", cfg) == "other.sch.tbl"


def test_profile_rows_match_schema():
    p = ColumnProfile("a", "string", 0.0, 100.0, ["email"], "h" * 64, ["email", "phone"], 18.5)
    rows = profile_rows("run1", "c.s.t", "hash", [p])
    assert len(rows[0]) == len(_schema_cols(PROFILE_SCHEMA))
    assert rows[0][:3] == ("run1", "c.s.t", "a")


def test_proposal_rows_status_and_ids():
    props = [Proposal("a", "t1", "d", "pii", 0.9, "r", "fuzzy"),
             Proposal("b", None, None, None, 0.0, "REJECTED: x", "llm", "m", "propose_v1", 12)]
    rows = proposal_rows("run1", "c.s.t", props)
    cols = _schema_cols(PROPOSAL_SCHEMA)
    assert len(rows[0]) == len(cols)
    rec = dict(zip(cols, rows[1]))
    assert rec["status"] == "proposed" and rec["tokens"] == 12 and rec["term_id"] is None
    assert len({r[0] for r in rows}) == 2  # unique proposal ids


def test_run_summary_counts():
    props = [Proposal("a", "t1", "d", "pii", 0.9, "r", "fuzzy"),
             Proposal("b", None, None, "internal", 0.6, "r", "llm", "m", "propose_v1", 10),
             Proposal("c", "t2", None, "internal", 0.0, "REJECTED: x", "llm", "m", "propose_v1", 5)]
    s = run_summary("run1", "c.s.t", "hash", props, llm_calls=1, rejections=1, model="m")
    assert s["fuzzy"] == 1 and s["llm"] == 2 and s["no_match"] == 1 and s["tokens"] == 15
    assert s["llm_calls"] == 1 and s["rejected"] == 1


def test_run_summary_records_the_passed_prompt_version():
    props = [Proposal("b", None, None, "internal", 0.6, "r", "llm", "m", "propose_v7", 10)]
    s = run_summary("run1", "c.s.t", "hash", props, llm_calls=1, rejections=0, model="m", prompt_version="propose_v7")
    assert s["prompt_version"] == "propose_v7"


def test_run_summary_prompt_version_is_idempotent():
    props = [Proposal("b", None, None, "internal", 0.6, "r", "llm", "m", "propose_v7", 10)]
    kw = dict(llm_calls=1, rejections=0, model="m", prompt_version="propose_v7")
    assert run_summary("r", "c.s.t", "h", props, **kw) == run_summary("r", "c.s.t", "h", props, **kw)


def test_run_summary_counts_rule_proposals_separately():
    props = [Proposal("a", "t1", "d", "pii", 0.9, "r", "fuzzy"),
             Proposal("b", None, "noise", "internal", 1.0, "noise tokens: tmp, col", "rule"),
             Proposal("c", None, "noise", "internal", 1.0, "noise tokens: col, 17", "rule"),
             Proposal("d", None, None, "internal", 0.6, "r", "llm", "m", "propose_v2", 10)]
    s = run_summary("run1", "c.s.t", "hash", props, llm_calls=1, rejections=0, model="m")
    assert s["rule"] == 2 and s["fuzzy"] == 1 and s["llm"] == 1
    assert s["columns_proposed"] == 4


def test_run_summary_rule_is_zero_without_rule_proposals():
    props = [Proposal("a", "t1", "d", "pii", 0.9, "r", "fuzzy")]
    s = run_summary("run1", "c.s.t", "hash", props, llm_calls=0, rejections=0, model=None)
    assert s["rule"] == 0
