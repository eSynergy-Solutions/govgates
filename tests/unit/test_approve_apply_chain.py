"""Approve + apply as one job (Q20: "Approve doesn't trigger apply"); specs/hydration.md steps 6-7.

A. Bundle shape (pure YAML inspection, `${...}` stays a literal string): the `approve` job runs task `approve` then
   task `apply` (only on success), the standalone `apply` job is gone (it could race the chained apply), and a
   failure notifies the addresses in the `notify_on_failure` bundle variable (no literal address in the repo).
B. Missing / not-visible table: reject and revise still land (spec step 6), accept/edit, permitted uses and the
   auto-rule wait, then the approve task fails so apply does not run. There is no fake-Spark harness for job
   mains, so main()'s wiring is checked on its AST.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
RESOURCES = ROOT / "resources"
APPROVE_SRC = ROOT / "src" / "jobs" / "approve.py"
TABLE_PARAM = "{{job.parameters.table}}"
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")

EXPECTED_APPLY_PARAMS = {
    "--catalog": "${var.catalog}",
    "--raw-schema": "${var.raw_schema}",
    "--gov-schema": "${var.gov_schema}",
    "--ref-schema": "${var.ref_schema}",
    "--curated-schema": "${var.curated_schema}",
    "--tag-prefix": "${var.tag_prefix}",
    "--table": TABLE_PARAM,
}


# ---------- helpers ----------

def _load_yaml(path: Path):
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _all_jobs() -> list[tuple[str, str, dict]]:
    out = []
    for f in sorted(RESOURCES.glob("*.yml")):
        doc = _load_yaml(f) or {}
        for name, job in ((doc.get("resources") or {}).get("jobs") or {}).items():
            out.append((f.name, name, job or {}))
    return out


def _approve_job() -> dict:
    found = [job for _, name, job in _all_jobs() if name == "approve"]
    assert len(found) == 1, f"expected exactly one job keyed 'approve', found {len(found)}"
    return found[0]


def _task(job: dict, key: str) -> dict:
    tasks = [t for t in job.get("tasks") or [] if t.get("task_key") == key]
    assert len(tasks) == 1, f"expected exactly one task '{key}', found {len(tasks)}"
    return tasks[0]


def _flag_pairs(params: list) -> dict[str, str]:
    """['--a', 'x', '--b', 'y'] -> {'--a': 'x', '--b': 'y'}; every flag must take one value, no repeats."""
    params = [str(p) for p in params]
    assert len(params) % 2 == 0, f"odd parameter list: {params}"
    flags = params[0::2]
    assert all(f.startswith("--") for f in flags), f"not --flag value pairs: {params}"
    assert len(flags) == len(set(flags)), f"repeated flag: {params}"
    return dict(zip(flags, params[1::2], strict=True))


def _main_calls_in_order() -> list[tuple[int, int, str]]:
    """(lineno, col, called name) for every call inside main() of src/jobs/approve.py, in source order."""
    tree = ast.parse(APPROVE_SRC.read_text(encoding="utf-8"))
    mains = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "main"]
    assert len(mains) == 1, "src/jobs/approve.py has no single top-level main()"
    calls = []
    for node in ast.walk(mains[0]):
        if isinstance(node, ast.Call):
            f = node.func
            name = f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else None
            if name:
                calls.append((node.lineno, node.col_offset, name))
    return sorted(calls)


# ---------- A1-A2. approve job = approve then apply ----------

def test_approve_job_has_two_tasks_approve_then_apply():
    keys = [t.get("task_key") for t in _approve_job().get("tasks") or []]
    assert keys == ["approve", "apply"]


def test_apply_task_depends_only_on_approve():
    apply = _task(_approve_job(), "apply")
    assert apply.get("depends_on") == [{"task_key": "approve"}]


def test_apply_task_runs_only_when_approve_succeeds():
    apply = _task(_approve_job(), "apply")
    assert apply.get("run_if", "ALL_SUCCESS") == "ALL_SUCCESS"


# ---------- A3. apply task parameters ----------

def test_apply_task_runs_apply_py():
    apply = _task(_approve_job(), "apply")
    assert (apply.get("spark_python_task") or {}).get("python_file") == "../src/jobs/apply.py"


def test_apply_task_parameters_exact():
    apply = _task(_approve_job(), "apply")
    params = (apply.get("spark_python_task") or {}).get("parameters") or []
    assert _flag_pairs(params) == EXPECTED_APPLY_PARAMS


def test_apply_and_approve_tasks_get_the_same_table():
    job = _approve_job()
    approve = _flag_pairs((_task(job, "approve").get("spark_python_task") or {}).get("parameters") or [])
    apply = _flag_pairs((_task(job, "apply").get("spark_python_task") or {}).get("parameters") or [])
    assert approve.get("--table") == TABLE_PARAM
    assert apply.get("--table") == approve.get("--table")


# ---------- A4. job-level settings kept ----------

def test_approve_job_runs_as_ci_sp():
    assert (_approve_job().get("run_as") or {}).get("service_principal_name") == "${var.ci_sp}"


def test_approve_job_max_concurrent_runs_is_one():
    assert _approve_job().get("max_concurrent_runs") == 1


def test_approve_job_queues_overlapping_runs():
    assert _approve_job().get("queue") == {"enabled": True}


# ---------- A5. no standalone apply job ----------

def test_no_job_keyed_apply():
    where = [f"{fname}:{name}" for fname, name, _ in _all_jobs() if name == "apply"]
    assert not where, f"standalone apply job still defined: {where}"


def test_only_the_approve_job_runs_apply_py():
    where = []
    for fname, name, job in _all_jobs():
        if name == "approve":
            continue
        for t in job.get("tasks") or []:
            pf = str((t.get("spark_python_task") or {}).get("python_file") or "")
            if pf.replace("\\", "/").endswith("jobs/apply.py"):
                where.append(f"{fname}:{name}:{t.get('task_key')}")
    assert not where, f"apply.py run outside the approve job: {where}"


# ---------- A6. failure notification ----------

def test_databricks_yml_declares_notify_on_failure_variable():
    var = (_load_yaml(ROOT / "databricks.yml").get("variables") or {}).get("notify_on_failure")
    assert var is not None, "databricks.yml: variable notify_on_failure is missing"
    assert var.get("type") == "complex"
    assert var.get("default") == []


def test_approve_job_notifies_on_failure_from_variable():
    notes = _approve_job().get("email_notifications") or {}
    assert notes.get("on_failure") == "${var.notify_on_failure}"


def test_email_regex_catches_addresses_and_ignores_bundle_tokens():
    assert EMAIL_RE.search("someone" + "@" + "example.org")
    assert not EMAIL_RE.search('"@golden_set"')
    assert not EMAIL_RE.search("${var.notify_on_failure}")


def test_no_literal_email_address_in_bundle_files():
    hits = []
    for path in [ROOT / "databricks.yml", *sorted(RESOURCES.glob("*.yml"))]:
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if EMAIL_RE.search(line):
                hits.append(f"{path.relative_to(ROOT).as_posix()}:{i}")
    # paths and line numbers only; never the address
    assert not hits, "literal email address at: " + ", ".join(hits)


# ---------- B. missing / not-visible table in approve ----------
# Spec step 6: reject and revise are never blocked. So a missing table does not stop corrections: they land in gov
# (the revised row is unstamped, so grants fail closed); accept/edit and permitted-use inputs stay pending, the
# auto-rule plans nothing, and the approve task then fails so apply does not run.

def _decision(action: str, did: str):
    return SimpleNamespace(decision_id=did, action=action)


def test_corrections_only_keeps_reject_and_revise_in_order():
    from jobs.approve import corrections_only

    ds = [_decision("accept", "a"), _decision("revise", "b"), _decision("edit", "c"), _decision("reject", "d")]
    assert [d.decision_id for d in corrections_only(ds)] == ["b", "d"]


def test_corrections_only_empty():
    from jobs.approve import corrections_only

    assert corrections_only([]) == []


def test_missing_table_exit_passes_when_table_exists():
    from jobs.approve import missing_table_exit

    assert missing_table_exit("cat.raw.claims", True) is None


def test_missing_table_exit_fails_when_table_missing():
    from jobs.approve import missing_table_exit

    with pytest.raises(SystemExit) as e:
        missing_table_exit("cat.raw.no_such_table", False)
    msg = str(e.value.code)
    assert "cat.raw.no_such_table" in msg
    assert "does not exist or is not visible" in msg


def _main() -> ast.FunctionDef:
    tree = ast.parse(APPROVE_SRC.read_text(encoding="utf-8"))
    return next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "main")


def _is_name(node, name: str) -> bool:
    return isinstance(node, ast.Name) and node.id == name


def test_main_exists_comes_from_table_exists():
    assigns = [n for n in ast.walk(_main()) if isinstance(n, ast.Assign)
               and any(_is_name(t, "exists") for t in n.targets)]
    assert len(assigns) == 1, "main() must assign `exists` exactly once"
    v = assigns[0].value
    assert isinstance(v, ast.Call) and isinstance(v.func, ast.Attribute) and v.func.attr == "tableExists"


def test_main_sets_permitted_uses_only_when_table_exists():
    ifexps = [n for n in ast.walk(_main()) if isinstance(n, ast.IfExp) and _is_name(n.test, "exists")]
    guarded = [n for n in ifexps if isinstance(n.body, ast.Call) and _is_name(n.body.func, "_set_permitted_uses")]
    assert len(guarded) == 1, "_set_permitted_uses must be the `if exists` branch of a conditional expression"
    orelse = guarded[0].orelse
    assert isinstance(orelse, ast.Tuple) and isinstance(orelse.elts[0], ast.Constant) and orelse.elts[0].value is False
    calls = [n for n in ast.walk(_main()) if isinstance(n, ast.Call) and _is_name(n.func, "_set_permitted_uses")]
    assert len(calls) == 1, "_set_permitted_uses called outside the exists guard"


def test_main_filters_decisions_to_corrections_when_missing():
    ifs = [n for n in ast.walk(_main()) if isinstance(n, ast.If) and isinstance(n.test, ast.UnaryOp)
           and isinstance(n.test.op, ast.Not) and _is_name(n.test.operand, "exists")]
    assert ifs, "main() has no `if not exists:` block"
    filtered = [s for i in ifs for s in i.body if isinstance(s, ast.Assign)
                and any(_is_name(t, "decisions") for t in s.targets)
                and isinstance(s.value, ast.Call) and _is_name(s.value.func, "corrections_only")]
    assert filtered, "decisions = corrections_only(decisions) missing under `if not exists:`"


def test_main_order_read_filter_plan_write_then_exit():
    names = [n for _, _, n in _main_calls_in_order()]
    for fn in ("_read", "corrections_only", "plan", "_write", "missing_table_exit"):
        assert fn in names, f"main() no longer calls {fn}"
    assert names.index("_read") < names.index("corrections_only") < names.index("plan") < names.index("_write")
    assert names.index("_write") < names.index("missing_table_exit"), "the exit must come after the corrections land"
    exit_call = next(n for n in ast.walk(_main()) if isinstance(n, ast.Call) and _is_name(n.func, "missing_table_exit"))
    assert _is_name(exit_call.args[1], "exists")
