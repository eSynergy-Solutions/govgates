"""Apply job write order (specs/hydration.md step 7), with a fake Spark that records every call.

Per column: apply_started audit row, then its UC statements, then the applied_to_uc outcome row and the stamp.
A failing column gets an apply_failed row and no stamp; later columns still apply.
"""

from __future__ import annotations

import json

import pytest

from gates.common.config import GateConfig
from gates.hydration.apply import Approved, UcColumn, applied_row, error_class, failed_row, plan_table
from jobs.apply import _write

TABLE = "cat.raw.claims"
T = "2026-01-01T09:00:00"


class FakeDF:
    def __init__(self, spark, rows):
        self.spark, self.rows = spark, rows

    def createOrReplaceTempView(self, name):  # noqa: N802 (Spark API)
        self.spark.views[name] = self.rows


class FakeSpark:
    def __init__(self, fail_on: str | None = None, message: str = "[UNAUTHORIZED_ACCESS] denied req=abc"):
        self.fail_on, self.message = fail_on, message
        self.views: dict[str, list] = {}
        self.log: list[tuple] = []  # ("audit", action, column) | ("sql", stmt) | ("stamp", column)

    def createDataFrame(self, rows, schema):  # noqa: N802 (Spark API)
        return FakeDF(self, rows)

    def sql(self, stmt, args=None):
        if stmt.lstrip().startswith("MERGE INTO"):
            view = next(v for v in self.views if v in stmt)
            for r in self.views[view]:
                if "audit_log" in stmt:
                    self.log.append(("audit", r[2], json.loads(r[5]).get("column")))
                else:
                    self.log.append(("stamp", r[1]))
            return
        if self.fail_on and self.fail_on in stmt:
            raise RuntimeError(self.message)
        self.log.append(("sql", stmt))


def approved(col: str) -> Approved:
    return Approved(TABLE, col, "claim_reference", "Claim reference.", "internal", "owner.a", T, None)


def decided(*rows: Approved) -> dict:
    return {r.column_name: {"term_id": r.term_id, "description": r.description, "sensitivity": r.sensitivity,
                            "approved_by": r.approved_by} for r in rows}


def run(spark: FakeSpark, cols=("a", "b", "c"), refused: tuple[str, ...] = ()):
    rows = [approved(c) for c in cols]
    d = decided(*[r for r in rows if r.column_name not in refused])
    plan = plan_table(TABLE, rows, d, {c: UcColumn(None, {}) for c in cols})
    failed = _write(spark, GateConfig("cat"), TABLE, plan, {c: T for c in cols}, "svc")
    return plan, failed


def test_per_column_order_started_statements_applied_stamp():
    spark = FakeSpark()
    _, failed = run(spark, cols=("a",))
    assert failed == []
    kinds = [e[0] if e[0] != "audit" else e[1] for e in spark.log]
    assert kinds == ["apply_started", "sql", "sql", "applied_to_uc", "stamp"]


def test_failed_column_is_audited_unstamped_and_others_still_apply():
    spark = FakeSpark(fail_on="`b`")
    _, failed = run(spark)
    assert failed == [("b", "UNAUTHORIZED_ACCESS")]
    audits = [(e[1], e[2]) for e in spark.log if e[0] == "audit"]
    assert ("apply_failed", "b") in audits and ("applied_to_uc", "b") not in audits
    assert [e[1] for e in spark.log if e[0] == "stamp"] == ["a", "c"]
    assert ("applied_to_uc", "c") in audits


def test_audit_row_precedes_its_statements():
    spark = FakeSpark()
    run(spark, cols=("a", "b"))
    for col in ("a", "b"):
        started = spark.log.index(("audit", "apply_started", col))
        first_sql = next(i for i, e in enumerate(spark.log) if e[0] == "sql" and f"`{col}`" in e[1])
        assert started < first_sql


def test_refusals_are_audited_and_never_run_or_stamped():
    spark = FakeSpark()
    run(spark, cols=("a", "b"), refused=("b",))
    assert ("audit", "apply_refused", "b") in spark.log
    assert not any(e[0] == "sql" and "`b`" in e[1] for e in spark.log)
    assert ("stamp", "b") not in spark.log


def test_success_after_failure_gets_its_own_row():
    plan = plan_table(TABLE, [approved("a")], decided(approved("a")), {"a": UcColumn(None, {})})
    [change] = plan.changes
    started = change.event_id
    ok = applied_row(change, TABLE, "svc")[0]
    bad = failed_row(change, TABLE, "svc", "RuntimeError", "[UNAUTHORIZED_ACCESS] x")[0]
    assert len({started, ok, bad}) == 3


@pytest.mark.parametrize("m1,m2", [("[UNAUTHORIZED_ACCESS] req=1 at 10:00", "[UNAUTHORIZED_ACCESS] req=2 at 10:05"),
                                   ("timeout after 31s", "timeout after 47s")])
def test_failure_id_ignores_varying_message_text(m1, m2):
    plan = plan_table(TABLE, [approved("a")], decided(approved("a")), {"a": UcColumn(None, {})})
    [change] = plan.changes
    assert failed_row(change, TABLE, "svc", "RuntimeError", m1)[0] == failed_row(change, TABLE, "svc",
                                                                                 "RuntimeError", m2)[0]
    assert json.loads(failed_row(change, TABLE, "svc", "RuntimeError", m1)[6])["error"] == m1


def test_error_class():
    assert error_class("[UNAUTHORIZED_ACCESS] Unauthorized access: PERMISSION_DENIED", "X") == "UNAUTHORIZED_ACCESS"
    assert error_class("no class here", "RuntimeError") == "RuntimeError"
