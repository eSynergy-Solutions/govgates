"""Review-queue contract (C8, H2): sql/views/*.sql and ddl_then_views. Pure Python, no workspace."""

import re
from pathlib import Path

import pytest

from gates.common.config import GateConfig
from gates.common.sql_files import render, split_statements, statements_from_dir

ROOT = Path(__file__).resolve().parents[2]
DDL = ROOT / "sql" / "ddl"
VIEWS = ROOT / "sql" / "views"
VIEW_FILES = ["v_approved_columns.sql", "v_consumption_queue.sql", "v_decision_outcomes.sql", "v_hydration_queue.sql",
              "v_permitted_uses.sql", "v_review_queue.sql"]


def _raw(name: str) -> str:
    path = VIEWS / name
    assert path.is_file(), f"missing {path}"
    return path.read_text()


def _rendered(name: str, catalog: str = "cat") -> str:
    return render(_raw(name), GateConfig(catalog).substitutions())


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()


# ---------------------------------------------------------------- the four files, one view each

def test_views_dir_holds_exactly_the_view_files():
    assert VIEWS.is_dir(), f"missing {VIEWS}"
    assert sorted(p.name for p in VIEWS.glob("*.sql")) == VIEW_FILES


@pytest.mark.parametrize("name", VIEW_FILES)
def test_view_renders_with_no_leftover_placeholders(name):
    assert "${" not in _rendered(name)


@pytest.mark.parametrize("name", VIEW_FILES)
def test_view_file_is_exactly_one_statement(name):
    assert len(split_statements(_rendered(name))) == 1


@pytest.mark.parametrize("name", VIEW_FILES)
def test_view_creates_or_replaces_view_named_after_file(name):
    stmt = split_statements(_rendered(name))[0]
    stem = Path(name).stem
    assert _norm(stmt).startswith(f"create or replace view cat.gov.{stem}"), _norm(stmt)[:120]


# ---------------------------------------------------------------- no hard-coded names, no product data

@pytest.mark.parametrize("name", VIEW_FILES)
def test_no_literal_catalog_name(name):
    raw = _raw(name)
    assert "gate_sandbox" not in raw.lower()
    rendered = _rendered(name, catalog="zzcat")
    refs = re.findall(r"([\w`]+)\.gov\.", rendered)
    assert refs, "expected three-part gov references"
    for prefix in refs:
        assert prefix.strip("`") == "zzcat", f"{name}: three-part gov reference not built from ${{catalog}}: {prefix}"


@pytest.mark.parametrize("name", VIEW_FILES)
def test_no_view_reads_product_data_schemas(name):
    raw = _raw(name)
    assert "${raw_schema}" not in raw
    assert "${curated_schema}" not in raw


# ---------------------------------------------------------------- v_review_queue: shared shape over both gates

def test_review_queue_exposes_contract_columns():
    text = _rendered("v_review_queue.sql").lower()
    for col in ("gate", "item_id", "object", "proposed", "context", "waiting_since", "approvers", "can_decide"):
        assert re.search(rf"\b{col}\b", text), col


def test_review_queue_unions_both_gate_queues():
    text = _rendered("v_review_queue.sql").lower()
    assert "v_hydration_queue" in text
    assert "v_consumption_queue" in text


def test_review_queue_created_after_the_views_it_reads():
    names = [n for n, _ in statements_from_dir(VIEWS, GateConfig("cat").substitutions())]
    i = names.index("v_review_queue.sql")
    assert names.index("v_hydration_queue.sql") < i
    assert names.index("v_consumption_queue.sql") < i


# ---------------------------------------------------------------- v_decision_outcomes

def test_decision_outcomes_reads_all_three_decision_tables():
    text = _rendered("v_decision_outcomes.sql").lower()
    for table in ("review_decisions", "request_decisions", "permitted_use_inputs"):
        assert table in text, table


def test_decision_outcomes_exposes_contract_columns():
    text = _rendered("v_decision_outcomes.sql").lower()
    for col in ("gate", "decision_id", "item_id", "action", "decided_by", "decided_at", "applied_at", "outcome"):
        assert re.search(rf"\b{col}\b", text), col


# ---------------------------------------------------------------- per-gate queues

def test_hydration_queue_reads_proposed_terms_profiles_and_approvers():
    text = _rendered("v_hydration_queue.sql").lower()
    assert "term_proposals" in text
    assert "'proposed'" in text
    assert "column_profiles" in text
    assert "table_approvers" in text


def test_hydration_queue_does_not_expose_sample_hash():
    assert "sample_hash" not in _rendered("v_hydration_queue.sql").lower()


def test_consumption_queue_reads_pending_requests_uses_and_approvers():
    text = _rendered("v_consumption_queue.sql").lower()
    assert "access_requests" in text
    assert "'pending_owner'" in text
    assert "permitted_uses" in text
    assert "table_approvers" in text


def test_can_decide_matches_caller_identity_not_group_membership():
    # approve/decide jobs match the committer to table_approvers.principal by exact identity (case-insensitive);
    # a group-membership check would show can_decide = true for a decision the job then refuses.
    text = " ".join(_rendered(n).lower() for n in ("v_hydration_queue.sql", "v_consumption_queue.sql",
                                                    "v_review_queue.sql"))
    assert "can_decide" in text
    assert "current_user()" in text


@pytest.mark.parametrize("name", ["v_hydration_queue.sql", "v_consumption_queue.sql", "v_review_queue.sql"])
def test_can_decide_never_uses_group_membership(name):
    assert "is_account_group_member" not in _rendered(name).lower()


# ---------------------------------------------------------------- ddl_then_views (pure)

def _write(d: Path, files: dict[str, str]) -> Path:
    d.mkdir()
    for name, body in files.items():
        (d / name).write_text(body)
    return d


def test_ddl_then_views_orders_ddl_before_views(tmp_path):
    from gates.common.sql_files import ddl_then_views
    ddl = _write(tmp_path / "ddl", {"b.sql": "CREATE B;\n", "a.sql": "CREATE ${catalog}.A;\n"})
    views = _write(tmp_path / "views", {"v2.sql": "CREATE V2;\n", "v1.sql": "CREATE V1;\n"})
    out = ddl_then_views(ddl, views, {"catalog": "c"})
    assert [n for n, _ in out] == ["a.sql", "b.sql", "v1.sql", "v2.sql"]
    assert out[0][1] == "CREATE c.A"


def test_ddl_then_views_without_views_dir_is_ddl_only(tmp_path):
    from gates.common.sql_files import ddl_then_views
    ddl = _write(tmp_path / "ddl", {"b.sql": "CREATE B;\n", "a.sql": "CREATE A;\n"})
    out = ddl_then_views(ddl, None, {})
    assert [n for n, _ in out] == ["a.sql", "b.sql"]


def test_ddl_then_views_is_idempotent(tmp_path):
    from gates.common.sql_files import ddl_then_views
    ddl = _write(tmp_path / "ddl", {"a.sql": "CREATE A;\n"})
    views = _write(tmp_path / "views", {"v1.sql": "CREATE V1;\n"})
    assert ddl_then_views(ddl, views, {}) == ddl_then_views(ddl, views, {})


def test_ddl_then_views_on_real_dirs_ends_with_the_views():
    from gates.common.sql_files import ddl_then_views
    subs = GateConfig("cat").substitutions()
    out = ddl_then_views(DDL, VIEWS, subs)
    assert [n for n, _ in out[-len(VIEW_FILES):]] == VIEW_FILES
    assert out[:-len(VIEW_FILES)] == statements_from_dir(DDL, subs)


# ---------------------------------------------------------------- review round 1: views say what the jobs do


def _view(name: str) -> str:
    return " ".join(_rendered(name).lower().split())


@pytest.mark.parametrize("name", VIEW_FILES)
def test_no_view_exposes_hashes(name):
    text = _rendered(name).lower()
    assert "sample_hash" not in text and "schema_hash" not in text


@pytest.mark.parametrize("name", ["v_hydration_queue.sql", "v_approved_columns.sql"])
def test_newest_proposal_ordering_matches_the_approve_job(name):
    t = _view(name)
    assert "partition by p.table_name, p.column_name" in t or "partition by table_name, column_name" in t
    assert "created_at desc nulls last" in t and "proposal_id desc" in t


def test_hydration_queue_excludes_eval_and_keeps_only_the_newest_open_proposal():
    t = _view("v_hydration_queue.sql")
    assert "status <> 'eval'" in t
    assert "rn = 1" in t and "status = 'proposed'" in t


def test_hydration_queue_flags_permitted_uses_and_takes_newest_term_version():
    t = _view("v_hydration_queue.sql")
    assert "has_permitted_uses" in t
    assert "order by version desc" in t


def test_consumption_can_decide_excludes_requester_and_consumer():
    t = _view("v_consumption_queue.sql")
    assert "r.requester" in t.split("as can_decide")[0].split("approvers,")[-1]
    assert "r.consumer" in t.split("as can_decide")[0].split("approvers,")[-1]


def test_consumption_queue_never_repeats_a_request():
    t = _view("v_consumption_queue.sql")
    assert "group by product, use_code" in t
    assert "group by use_mode, delivery, access_mode" in t


@pytest.mark.parametrize("name", ["v_hydration_queue.sql", "v_consumption_queue.sql", "v_approved_columns.sql",
                                  "v_permitted_uses.sql"])
def test_can_decide_compares_trimmed_case_insensitive(name):
    assert "lower(trim(current_user()))" in _view(name)


def test_approved_columns_names_the_revise_target():
    t = _view("v_approved_columns.sql")
    assert "revise_proposal_id" in t and "status in ('accepted', 'edited')" in t and "pending_proposal_id" in t


def test_permitted_uses_view_shows_active_rows_only():
    assert "where u.active" in _view("v_permitted_uses.sql")
