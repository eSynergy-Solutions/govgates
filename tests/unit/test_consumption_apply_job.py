"""Row-filter apply loop (specs/consumption.md, `row_filter` bullet; "Generator records every statement it runs in
`gov.audit_log`"), with a fake Spark that records every statement and a captured `_merge` for audit rows.

Per statement action: `<kind>_started` audit row (carrying the statement), the statement, then `<kind>` or
`<kind>_failed`. `tag_refused` is record-only. Nothing runs after a failed function or policy; a policy drop waits
for every tag unset to succeed. `_last_events` is Spark SQL and is out of scope here.
"""

from __future__ import annotations

import json

import pytest

import jobs.consumption_apply as job
from gates.common.config import GateConfig
from gates.consumption.row_filter import Action

CFG = GateConfig("cat")
ACTOR = "svc"


class FakeSpark:
    def __init__(self, log: list, fail_on: tuple[str, ...] = (),
                 message: str = "[PERMISSION_DENIED] no req=abc"):
        self.log, self.fail_on, self.message = log, fail_on, message

    def sql(self, stmt, args=None):
        if any(f == stmt for f in self.fail_on):
            self.log.append(("sql_failed", stmt))
            raise RuntimeError(self.message)
        self.log.append(("sql", stmt))


@pytest.fixture
def log(monkeypatch):
    entries: list[tuple] = []

    def fake_merge(spark, rows, schema, target, sql):
        assert target == CFG.gov("audit_log")
        for r in rows:
            entries.append(("audit", r))

    monkeypatch.setattr(job, "_merge", fake_merge)
    return entries


def act(kind: str, obj: str, statement: str | None = "default") -> Action:
    stmt = f"STMT {kind} {obj}" if statement == "default" else statement
    return Action(kind, obj, stmt, f"ev-{kind}-{obj}", {"b": 1}, {"a": 1})


def apply(log, actions, fail_on=()):
    return job._apply(FakeSpark(log, tuple(fail_on)), CFG, actions, ACTOR)


def audits(log) -> list[tuple]:
    return [e[1] for e in log if e[0] == "audit"]


def actions_of(log) -> list[str]:
    """Audit action names (row[2]) and ran statements, in order."""
    out = []
    for e in log:
        out.append(e[1][2] if e[0] == "audit" else f"{e[0]}:{e[1]}")
    return out


def ran(log) -> list[str]:
    return [e[1] for e in log if e[0] in ("sql", "sql_failed")]


# ---------------------------------------------------------------- per-action order

@pytest.mark.parametrize("kind", ["function_set", "policy_set", "tag_set", "tag_unset", "policy_drop"])
def test_started_then_statement_then_done(log, kind):
    a = act(kind, "o1")
    assert apply(log, [a]) == []
    assert actions_of(log) == [f"{kind}_started", f"sql:{a.statement}", kind]
    started, done = audits(log)
    assert started[0] == a.event_id
    assert done[0] != started[0]


@pytest.mark.parametrize("kind", ["function_set", "policy_set", "tag_set", "tag_unset", "policy_drop"])
def test_started_then_statement_then_failed(log, kind):
    a = act(kind, "o1")
    failed = apply(log, [a], fail_on=[a.statement])
    assert [(o, k) for o, k, _ in failed] == [("o1", kind)]
    assert actions_of(log) == [f"{kind}_started", f"sql_failed:{a.statement}", f"{kind}_failed"]
    started, fail = audits(log)
    assert fail[0] != started[0]


def test_done_and_failed_ids_differ(log):
    a = act("tag_set", "o1")
    apply(log, [a])
    done_id = audits(log)[-1][0]
    log.clear()
    apply(log, [a], fail_on=[a.statement])
    started_id, failed_id = audits(log)[0][0], audits(log)[-1][0]
    assert len({started_id, done_id, failed_id}) == 3


def test_audit_rows_carry_gate_actor_object(log):
    a = act("tag_set", "cat.curated.claims.region")
    apply(log, [a])
    for r in audits(log):
        assert r[1] == "consumption" and r[3] == ACTOR and r[4] == a.object


# ---------------------------------------------------------------- the statement is recorded

@pytest.mark.parametrize("kind", ["function_set", "policy_set", "tag_set", "tag_unset", "policy_drop"])
def test_started_row_records_exact_statement(log, kind):
    stmt = "ALTER TABLE `cat`.`curated`.`claims` ALTER COLUMN `region` SET TAGS ('gg_rls_attribute' = 'region')"
    a = act(kind, "o1", stmt)
    apply(log, [a])
    started = audits(log)[0]
    assert started[2] == f"{kind}_started"
    assert json.loads(started[6])["statement"] == stmt


def test_started_row_records_statement_even_when_it_fails(log):
    a = act("policy_set", "p1")
    apply(log, [a], fail_on=[a.statement])
    assert json.loads(audits(log)[0][6])["statement"] == a.statement


# ---------------------------------------------------------------- tag_refused

def test_tag_refused_one_audit_row_no_sql(log):
    a = act("tag_refused", "cat.curated.claims.region", None)
    assert apply(log, [a]) == []
    assert ran(log) == []
    (row,) = audits(log)
    assert row[2] == "tag_refused" and row[0] == a.event_id


# ---------------------------------------------------------------- failure propagation

@pytest.mark.parametrize("first", ["function_set", "policy_set"])
def test_failed_function_or_policy_stops_everything_after(log, first):
    head = act(first, "x")
    later = [act("tag_set", "t1"), act("tag_unset", "t2"), act("policy_drop", "p_old")]
    failed = apply(log, [head, *later], fail_on=[head.statement])
    assert [(o, k) for o, k, _ in failed] == [("x", first)]
    assert ran(log) == [head.statement]
    assert not any(r[2].endswith("_started") and r[4] != "x" for r in audits(log))


def test_failed_function_skips_later_policy(log):
    f, p = act("function_set", "fn"), act("policy_set", "pol")
    apply(log, [f, p], fail_on=[f.statement])
    assert p.statement not in ran(log)
    assert "policy_set_started" not in actions_of(log)


def test_failed_tag_unset_skips_policy_drop_but_not_other_unsets(log):
    u1, u2, d = act("tag_unset", "t1"), act("tag_unset", "t2"), act("policy_drop", "p_old")
    failed = apply(log, [u1, u2, d], fail_on=[u1.statement])
    assert [(o, k) for o, k, _ in failed] == [("t1", "tag_unset")]
    assert u2.statement in ran(log)
    assert d.statement not in ran(log)
    assert "policy_drop_started" not in actions_of(log)


def test_failed_tag_set_does_not_stop_later_tag_set(log):
    s1, s2 = act("tag_set", "t1"), act("tag_set", "t2")
    failed = apply(log, [s1, s2], fail_on=[s1.statement])
    assert [(o, k) for o, k, _ in failed] == [("t1", "tag_set")]
    assert ran(log) == [s1.statement, s2.statement]
    assert actions_of(log)[-1] == "tag_set"


def test_tag_refused_still_recorded_after_policy_failure(log):
    p, r = act("policy_set", "pol"), act("tag_refused", "t1", None)
    apply(log, [p, r], fail_on=[p.statement])
    assert "tag_refused" in actions_of(log)
