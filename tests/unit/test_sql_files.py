from pathlib import Path

import pytest

from gates.common.config import GateConfig
from gates.common.sql_files import render, split_statements, statements_from_dir

DDL = Path(__file__).resolve().parents[2] / "sql" / "ddl"


def test_render_and_missing_placeholder():
    assert render("CREATE SCHEMA ${catalog}.x", {"catalog": "c"}) == "CREATE SCHEMA c.x"
    with pytest.raises(KeyError):
        render("${nope}", {})


def test_split_ignores_comments():
    sql = "-- header\nCREATE A;\n\n-- note\nCREATE B;\n"
    assert split_statements(sql) == ["CREATE A", "CREATE B"]


def test_split_keeps_semicolon_inside_a_line():
    sql = "CREATE T (a INT) COMMENT 'one; two';\nCREATE U;\n"
    assert split_statements(sql) == ["CREATE T (a INT) COMMENT 'one; two'", "CREATE U"]


def test_all_ddl_renders_with_no_leftover_placeholders():
    cfg = GateConfig(catalog="test_cat")
    stmts = statements_from_dir(DDL, cfg.substitutions())
    assert len(stmts) >= 13
    for _, s in stmts:
        assert "${" not in s
        assert "test_cat." in s


def test_ddl_matches_contract_tables():
    text = " ".join(s for _, s in statements_from_dir(DDL, GateConfig("c").substitutions()))
    for table in ("business_terms", "golden_set", "column_profiles", "term_proposals", "approved_metadata",
                  "access_policies", "access_requests", "audit_log", "eval_runs", "user_attributes",
                  "review_decisions", "golden_candidates", "table_approvers"):
        assert f".{table} (" in text, table


def test_config_fq_names():
    cfg = GateConfig(catalog="c", gov_schema="g")
    assert cfg.gov("audit_log") == "c.g.audit_log"
    assert cfg.raw("claims") == "c.raw.claims"


# ---------------------------------------------------------------- ADD COLUMNS made idempotent (no IF NOT EXISTS in SQL)

def test_add_columns_parsed():
    from gates.common.sql_files import add_columns
    stmt = "ALTER TABLE c.gov.access_requests ADD COLUMNS (consumer STRING, intended_use STRING COMMENT 'a, b')"
    assert add_columns(stmt) == ("c.gov.access_requests", [("consumer", "consumer STRING"),
                                                           ("intended_use", "intended_use STRING COMMENT 'a, b'")])


def test_add_columns_none_for_other_statements():
    from gates.common.sql_files import add_columns
    assert add_columns("CREATE TABLE IF NOT EXISTS c.gov.t (a INT)") is None
    assert add_columns("ALTER TABLE c.gov.t SET TBLPROPERTIES (x = 1)") is None


def test_only_missing_columns_are_added():
    from gates.common.sql_files import missing_columns_statement
    stmt = "ALTER TABLE c.gov.t ADD COLUMNS (a STRING, B STRING)"
    assert missing_columns_statement(stmt, {"a"}) == "ALTER TABLE c.gov.t ADD COLUMNS (B STRING)"
    assert missing_columns_statement(stmt, {"a", "b"}) is None  # case-insensitive: nothing to add
    assert missing_columns_statement(stmt, set()) == "ALTER TABLE c.gov.t ADD COLUMNS (a STRING, B STRING)"
