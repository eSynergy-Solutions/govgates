"""Apply step planner (specs/hydration.md step 7 Apply). Pure: no Spark, no workspace.

gov.approved_metadata is the truth; UC is read only to find the difference. Only `<prefix>_sensitivity` and
`<prefix>_business_term` are managed. A row that differs from its newest approve audit event is refused.
"""

from __future__ import annotations

import dataclasses
import json
import re

import pytest

from gates.hydration.apply import (
    Approved,
    ColumnChange,
    Refusal,
    TablePlan,
    UcColumn,
    audit_rows,
    plan_table,
)

TABLE = "cat.raw.claims"
ACTOR = "svc-ci"
T_EARLY = "2026-01-01T08:00:00"
T_APPROVED = "2026-01-01T09:00:00"
T_LATE = "2026-01-01T10:00:00"
SENS = "gg_sensitivity"
TERM = "gg_business_term"


def row(col: str, term_id: str | None = "claim_reference", description: str | None = "Claim reference.",
        sensitivity: str = "internal", *, approved_at: str | None = T_APPROVED,
        applied: str | None = None, approved_by: str = "owner.a") -> Approved:
    return Approved(table_name=TABLE, column_name=col, term_id=term_id, description=description,
                    sensitivity=sensitivity, approved_by=approved_by, approved_at=approved_at,
                    applied_to_uc_at=applied)


def decided_for(*rows: Approved) -> dict[str, dict]:
    return {r.column_name: {"table_name": r.table_name, "column_name": r.column_name, "term_id": r.term_id,
                            "description": r.description, "sensitivity": r.sensitivity,
                            "approved_by": r.approved_by} for r in rows}


def uc_matching(r: Approved, prefix: str = "gg", **extra_tags: str) -> UcColumn:
    tags = {f"{prefix}_sensitivity": r.sensitivity, **extra_tags}
    if r.term_id is not None:
        tags[f"{prefix}_business_term"] = r.term_id
    return UcColumn(comment=r.description, tags=tags)


def plan1(r: Approved, uc: UcColumn, **kw) -> TablePlan:
    return plan_table(TABLE, [r], decided_for(r), {r.column_name: uc}, **kw)


def only_change(p: TablePlan) -> ColumnChange:
    assert len(p.changes) == 1, p.changes
    return p.changes[0]


def stmts(p: TablePlan) -> list[str]:
    return [s for c in p.changes for s in c.statements]


def values(obj) -> list:
    """Every scalar inside a parsed JSON structure (keys and values)."""
    if isinstance(obj, dict):
        return [x for k, v in obj.items() for x in (k, *values(v))]
    if isinstance(obj, list):
        return [x for v in obj for x in values(v)]
    return [obj]


# ---------------------------------------------------------------- dataclasses

class TestTypes:
    def test_uc_column_is_frozen(self):
        c = UcColumn(comment=None, tags={})
        with pytest.raises(dataclasses.FrozenInstanceError):
            c.comment = "x"  # type: ignore[misc]

    def test_approved_is_frozen(self):
        r = row("a")
        with pytest.raises(dataclasses.FrozenInstanceError):
            r.term_id = "x"  # type: ignore[misc]

    def test_refusal_fields(self):
        f = Refusal(column="a", reason="r", event_id="e")
        assert (f.column, f.reason, f.event_id) == ("a", "r", "e")


# ---------------------------------------------------------------- desired state

class TestDesiredState:
    def test_blank_column_gets_both_tags_and_comment(self):
        r = row("claim_ref")
        c = only_change(plan1(r, UcColumn(comment=None, tags={})))
        assert c.column == "claim_ref"
        assert c.set_tags == {SENS: "internal", TERM: "claim_reference"}
        assert c.unset_tags == ()
        assert c.comment_change is True
        assert c.comment == "Claim reference."

    def test_matching_column_has_no_change(self):
        r = row("claim_ref")
        p = plan1(r, uc_matching(r))
        assert p.changes == []
        assert p.statement_count == 0

    def test_other_tags_ignored_for_matching(self):
        r = row("claim_ref")
        p = plan1(r, uc_matching(r, owner_team="claims-ops"))
        assert p.changes == []

    def test_null_term_unsets_stale_business_term(self):
        r = row("notes", term_id=None, sensitivity="confidential", description="Free notes.")
        uc = UcColumn(comment="Free notes.", tags={SENS: "confidential", TERM: "claim_reference"})
        c = only_change(plan1(r, uc))
        assert TERM in c.unset_tags
        assert SENS not in c.unset_tags
        assert c.set_tags == {}
        assert c.comment_change is False

    def test_null_term_without_business_term_tag_is_no_change(self):
        r = row("notes", term_id=None, sensitivity="confidential", description="Free notes.")
        p = plan1(r, UcColumn(comment="Free notes.", tags={SENS: "confidential"}))
        assert p.changes == []

    def test_stale_pii_lowered_to_internal_by_set_not_unset(self):
        r = row("claim_ref", sensitivity="internal")
        uc = UcColumn(comment="Claim reference.", tags={SENS: "pii", TERM: "claim_reference"})
        c = only_change(plan1(r, uc))
        assert c.set_tags == {SENS: "internal"}
        assert c.unset_tags == ()

    @pytest.mark.parametrize("uc_tags", [{}, {SENS: "pii"}, {SENS: "internal", TERM: "x"}, {TERM: "x"}])
    @pytest.mark.parametrize("term_id", ["claim_reference", None])
    def test_sensitivity_never_unset_while_gov_has_a_value(self, uc_tags, term_id):
        r = row("c", term_id=term_id)
        p = plan1(r, UcColumn(comment=None, tags=dict(uc_tags)))
        for c in p.changes:
            assert SENS not in c.unset_tags
        for s in stmts(p):
            if "UNSET TAGS" in s.upper():
                assert SENS not in s

    def test_null_description_clears_comment(self):
        r = row("claim_ref", description=None)
        uc = UcColumn(comment="old text", tags={SENS: "internal", TERM: "claim_reference"})
        c = only_change(plan1(r, uc))
        assert c.comment_change is True
        assert c.comment is None
        comment_stmts = [s for s in c.statements if "COMMENT" in s.upper()]
        assert comment_stmts and any("NULL" in s.upper() for s in comment_stmts)

    @pytest.mark.parametrize("uc_comment", [None, ""])
    def test_empty_uc_comment_counts_as_none(self, uc_comment):
        r = row("claim_ref", description=None)
        p = plan1(r, UcColumn(comment=uc_comment, tags={SENS: "internal", TERM: "claim_reference"}))
        assert p.changes == []

    def test_changed_description_only_changes_comment(self):
        r = row("claim_ref", description="New text.")
        uc = UcColumn(comment="Old text.", tags={SENS: "internal", TERM: "claim_reference"})
        c = only_change(plan1(r, uc))
        assert c.set_tags == {} and c.unset_tags == ()
        assert c.comment_change is True and c.comment == "New text."
        assert not any("SET TAGS" in s.upper() for s in c.statements)

    def test_tag_only_change_has_no_comment_statement(self):
        r = row("claim_ref")
        uc = UcColumn(comment="Claim reference.", tags={TERM: "claim_reference"})
        c = only_change(plan1(r, uc))
        assert c.comment_change is False
        assert not any("COMMENT" in s.upper() for s in c.statements)


# ---------------------------------------------------------------- only our two keys

class TestOnlyOurKeys:
    def test_foreign_tags_never_set_unset_or_recorded(self):
        r = row("claim_ref", term_id=None)
        uc = UcColumn(comment=None, tags={"owner_team": "claims-ops", "sensitivity": "secret",
                                          TERM: "old_term"})
        c = only_change(plan1(r, uc))
        assert set(c.set_tags) <= {SENS, TERM}
        assert set(c.unset_tags) <= {SENS, TERM}
        for blob in (c.before, c.after):
            flat = values(json.loads(json.dumps(blob, default=str)))
            assert "owner_team" not in flat and "claims-ops" not in flat
            assert "secret" not in flat
        for s in c.statements:
            assert "owner_team" not in s and "claims-ops" not in s and "secret" not in s

    def test_prefix_is_honoured(self):
        r = row("claim_ref")
        uc = UcColumn(comment=None, tags={SENS: "pii", TERM: "other"})  # gg_* are foreign under prefix xx
        c = only_change(plan1(r, uc, prefix="xx"))
        assert c.set_tags == {"xx_sensitivity": "internal", "xx_business_term": "claim_reference"}
        assert c.unset_tags == ()
        for s in c.statements:
            assert "gg_" not in s
        assert any("xx_sensitivity" in s and "xx_business_term" in s for s in c.statements)

    def test_prefix_matching_column_is_no_change(self):
        r = row("claim_ref")
        assert plan1(r, uc_matching(r, prefix="xx"), prefix="xx").changes == []


# ---------------------------------------------------------------- untracked and missing

class TestUntrackedAndMissing:
    def test_untracked_reports_only_our_keys(self):
        r = row("claim_ref")
        uc = {"claim_ref": uc_matching(r),
              "orphan": UcColumn(comment="x", tags={SENS: "pii", "owner_team": "ops"}),
              "orphan2": UcColumn(comment=None, tags={TERM: "t", SENS: "internal"}),
              "plain": UcColumn(comment="y", tags={"owner_team": "ops"})}
        p = plan_table(TABLE, [r], decided_for(r), uc)
        assert dict(p.untracked) == {"orphan": {SENS: "pii"}, "orphan2": {TERM: "t", SENS: "internal"}}
        assert p.changes == []  # reported, not removed
        assert all(c.column not in ("orphan", "orphan2", "plain") for c in p.changes)

    def test_untracked_uses_prefix(self):
        uc = {"orphan": UcColumn(comment=None, tags={SENS: "pii", "xx_sensitivity": "pii"})}
        p = plan_table(TABLE, [], {}, uc, prefix="xx")
        assert dict(p.untracked) == {"orphan": {"xx_sensitivity": "pii"}}

    def test_missing_column_is_reported_not_changed_or_stamped(self):
        here, gone = row("claim_ref"), row("dropped_col")
        p = plan_table(TABLE, [here, gone], decided_for(here, gone), {"claim_ref": UcColumn(None, {})})
        assert p.missing == ["dropped_col"]
        assert [c.column for c in p.changes] == ["claim_ref"]
        assert [s.column_name for s in p.stamp] == ["claim_ref"]
        assert p.refused == []


# ---------------------------------------------------------------- tamper refusal

class TestRefusal:
    def assert_refused_only(self, p: TablePlan, col: str):
        assert [f.column for f in p.refused] == [col]
        assert p.refused[0].reason
        assert p.refused[0].event_id
        assert all(c.column != col for c in p.changes)
        assert all(s.column_name != col for s in p.stamp)

    def test_no_decided_event_is_refused(self):
        r = row("claim_ref")
        p = plan_table(TABLE, [r], {}, {"claim_ref": UcColumn(None, {})})
        self.assert_refused_only(p, "claim_ref")
        assert p.statement_count == 0

    @pytest.mark.parametrize("field,value", [("term_id", "email_address"), ("term_id", None),
                                             ("description", "hand edited"), ("description", None),
                                             ("sensitivity", "public"), ("approved_by", "someone.else")])
    def test_row_differing_from_decided_is_refused(self, field, value):
        audited = row("claim_ref")
        tampered = dataclasses.replace(audited, **{field: value})
        p = plan_table(TABLE, [tampered], decided_for(audited), {"claim_ref": UcColumn(None, {})})
        self.assert_refused_only(p, "claim_ref")

    def test_sensitivity_outside_contract_is_refused(self):
        r = row("claim_ref", sensitivity="secret")
        p = plan_table(TABLE, [r], decided_for(r), {"claim_ref": UcColumn(None, {})})
        self.assert_refused_only(p, "claim_ref")

    def test_refusal_does_not_block_other_columns(self):
        ok, bad = row("a"), row("b")
        decided = decided_for(ok, bad)
        decided["b"] = {**decided["b"], "sensitivity": "pii"}
        p = plan_table(TABLE, [ok, bad], decided, {"a": UcColumn(None, {}), "b": UcColumn(None, {})})
        assert [f.column for f in p.refused] == ["b"]
        assert [c.column for c in p.changes] == ["a"]
        assert [s.column_name for s in p.stamp] == ["a"]

    def test_refusal_event_id_is_deterministic(self):
        r = row("claim_ref")
        args = (TABLE, [r], {}, {"claim_ref": UcColumn(None, {})})
        assert plan_table(*args).refused[0].event_id == plan_table(*args).refused[0].event_id


# ---------------------------------------------------------------- stamping and idempotency

class TestStamp:
    def test_fully_applied_table_plans_nothing(self):
        rows = [row("a", applied=T_LATE), row("b", term_id=None, description=None, applied=T_LATE),
                row("c", sensitivity="pii", applied=T_APPROVED)]
        uc = {r.column_name: uc_matching(r) for r in rows}
        p = plan_table(TABLE, rows, decided_for(*rows), uc)
        assert p.changes == []
        assert p.stamp == []
        assert p.refused == [] and p.missing == [] and p.untracked == []
        assert p.statement_count == 0
        assert audit_rows(p, TABLE, ACTOR) == []

    def test_matching_but_never_stamped_is_stamped_without_change(self):
        r = row("a", applied=None)
        p = plan1(r, uc_matching(r))
        assert p.changes == []
        assert p.stamp == [r]

    def test_matching_but_stamp_older_than_approval_is_stamped(self):
        r = row("a", applied=T_EARLY)
        p = plan1(r, uc_matching(r))
        assert p.changes == []
        assert p.stamp == [r]

    def test_changed_row_is_stamped(self):
        r = row("a", applied=None)
        p = plan1(r, UcColumn(None, {}))
        assert p.stamp == [r]

    def test_drift_on_stamped_row_is_repaired(self):
        # stamped after approval, but someone removed the tag by hand: diff runs regardless of stamp
        r = row("a", applied=T_LATE)
        uc = UcColumn(comment="Claim reference.", tags={TERM: "claim_reference"})
        p = plan1(r, uc)
        c = only_change(p)
        assert c.set_tags == {SENS: "internal"}
        assert r in p.stamp

    def test_statement_count_is_total(self):
        rows = [row("a"), row("b", term_id=None, description=None)]
        uc = {"a": UcColumn("old", {}), "b": UcColumn("old", {TERM: "stale", SENS: "pii"})}
        p = plan_table(TABLE, rows, decided_for(*rows), uc)
        assert p.statement_count == sum(len(c.statements) for c in p.changes) > 0

    def test_same_input_same_plan(self):
        rows = [row("a"), row("b", term_id=None)]
        uc = {"a": UcColumn(None, {}), "b": UcColumn("x", {TERM: "old"})}
        assert plan_table(TABLE, rows, decided_for(*rows), uc) == plan_table(TABLE, rows, decided_for(*rows), uc)

    def test_applying_the_plan_then_replanning_is_empty(self):
        rows = [row("a"), row("b", term_id=None, description=None, sensitivity="confidential")]
        uc = {"a": UcColumn("old", {"owner_team": "ops"}), "b": UcColumn("x", {TERM: "old", SENS: "pii"})}
        first = plan_table(TABLE, rows, decided_for(*rows), uc)
        assert first.changes
        stamped = [dataclasses.replace(r, applied_to_uc_at=T_LATE) for r in rows]
        after = {r.column_name: uc_matching(r, owner_team="ops") if r.column_name == "a" else uc_matching(r)
                 for r in rows}
        second = plan_table(TABLE, stamped, decided_for(*stamped), after)
        assert second.changes == [] and second.stamp == [] and second.statement_count == 0


# ---------------------------------------------------------------- statements

class TestStatements:
    def test_table_and_column_backtick_quoted(self):
        r = row("my col")
        p = plan_table(TABLE, [r], decided_for(r), {"my col": UcColumn(None, {})})
        ss = stmts(p)
        assert ss
        for s in ss:
            assert "`cat`.`raw`.`claims`" in s
            assert "`my col`" in s

    def test_backtick_in_column_name_is_doubled(self):
        r = row("we`ird")
        p = plan_table(TABLE, [r], decided_for(r), {"we`ird": UcColumn(None, {})})
        for s in stmts(p):
            assert "`we``ird`" in s

    def test_set_tags_includes_both_keys_when_both_differ(self):
        r = row("claim_ref", sensitivity="pii", term_id="email_address")
        p = plan1(r, UcColumn("Claim reference.", {SENS: "internal", TERM: "claim_reference"}))
        set_stmts = " ".join(s for s in stmts(p) if "SET TAGS" in s.upper() and "UNSET TAGS" not in s.upper())
        assert SENS in set_stmts and "'pii'" in set_stmts
        assert TERM in set_stmts and "'email_address'" in set_stmts

    def test_unset_tags_statement(self):
        r = row("notes", term_id=None, description="Free notes.")
        p = plan1(r, UcColumn("Free notes.", {SENS: "internal", TERM: "claim_reference"}))
        [s] = stmts(p)
        assert "UNSET TAGS" in s.upper()
        assert TERM in s

    @staticmethod
    def _assert_escaped(s: str, text: str):
        escaped = [text.replace("'", "''"), text.replace("'", "\\'")]
        assert any(e in s for e in escaped), s
        stripped = s
        for e in escaped:
            stripped = stripped.replace(e, "")
        assert text not in stripped, f"unescaped {text!r} in {s!r}"

    def test_single_quote_in_comment_is_escaped(self):
        r = row("claim_ref", description="Name as O'Brien wrote it")
        p = plan1(r, UcColumn(None, {SENS: "internal", TERM: "claim_reference"}))
        [s] = [s for s in stmts(p) if "COMMENT" in s.upper()]
        self._assert_escaped(s, "O'Brien")

    def test_single_quote_in_tag_value_is_escaped(self):
        r = row("claim_ref", term_id="o'brien_term")
        p = plan1(r, UcColumn("Claim reference.", {SENS: "internal"}))
        [s] = [s for s in stmts(p) if "SET TAGS" in s.upper()]
        self._assert_escaped(s, "o'brien_term")

    def test_comment_statement_contains_text(self):
        r = row("claim_ref", description="Unique claim reference.")
        p = plan1(r, UcColumn(None, {SENS: "internal", TERM: "claim_reference"}))
        [s] = stmts(p)
        assert "COMMENT" in s.upper() and "Unique claim reference." in s
        assert re.search(r"\bNULL\b", s.upper()) is None


# ---------------------------------------------------------------- event ids

class TestEventId:
    def test_deterministic(self):
        r = row("a")
        uc = UcColumn("old", {})
        assert only_change(plan1(r, uc)).event_id == only_change(plan1(r, uc)).event_id

    def test_differs_with_uc_before_state(self):
        r = row("a")
        e1 = only_change(plan1(r, UcColumn("old", {}))).event_id
        e2 = only_change(plan1(r, UcColumn("older", {}))).event_id
        e3 = only_change(plan1(r, UcColumn("old", {SENS: "pii"}))).event_id
        assert len({e1, e2, e3}) == 3

    def test_differs_with_desired_state(self):
        uc = UcColumn(None, {})
        e1 = only_change(plan1(row("a", sensitivity="internal"), uc)).event_id
        e2 = only_change(plan1(row("a", sensitivity="pii"), uc)).event_id
        e3 = only_change(plan1(row("a", description="other"), uc)).event_id
        assert len({e1, e2, e3}) == 3

    def test_differs_between_columns(self):
        rows = [row("a"), row("b")]
        p = plan_table(TABLE, rows, decided_for(*rows), {"a": UcColumn(None, {}), "b": UcColumn(None, {})})
        assert len({c.event_id for c in p.changes}) == 2


# ---------------------------------------------------------------- audit rows

class TestAuditRows:
    def test_change_and_refusal_rows(self):
        ok, bad = row("a"), row("b")
        decided = decided_for(ok)  # b has no approve event -> refused
        p = plan_table(TABLE, [ok, bad], decided, {"a": UcColumn("old", {}), "b": UcColumn(None, {})})
        rows = audit_rows(p, TABLE, ACTOR)
        assert len(rows) == 2
        by_action = {r[2]: r for r in rows}
        assert set(by_action) == {"apply_started", "apply_refused"}
        for r in rows:
            assert len(r) == 7
            event_id, gate, _action, actor, obj, _before, _after = r
            assert gate == "hydration" and actor == ACTOR and obj == TABLE
        assert by_action["apply_started"][0] == p.changes[0].event_id
        assert by_action["apply_refused"][0] == p.refused[0].event_id

    def test_before_after_json_name_the_column(self):
        r = row("claim_ref")
        p = plan1(r, UcColumn("old", {SENS: "pii"}))
        [(_, _, _, _, _, before_json, after_json)] = audit_rows(p, TABLE, ACTOR)
        before, after = json.loads(before_json), json.loads(after_json)
        assert isinstance(before, dict) and isinstance(after, dict)
        assert "claim_ref" in values(before) and "claim_ref" in values(after)
        assert "internal" in values(after) and "pii" in values(before)

    def test_empty_plan_has_no_rows(self):
        r = row("a", applied=T_LATE)
        assert audit_rows(plan1(r, uc_matching(r)), TABLE, ACTOR) == []

    def test_crash_replay_adds_no_duplicate(self):
        # run 1 wrote its audit row and ran the ALTERs, then crashed before stamping
        rows = [row("a"), row("b", term_id=None)]
        original = {"a": UcColumn("old", {}), "b": UcColumn(None, {TERM: "stale", SENS: "internal"})}
        first = plan_table(TABLE, rows, decided_for(*rows), original)
        ids = [r[0] for r in audit_rows(first, TABLE, ACTOR)]
        assert len(ids) == 2
        # a replay that still sees the original UC state reproduces the same ids (MERGE on event_id dedupes)
        again = plan_table(TABLE, rows, decided_for(*rows), original)
        assert [r[0] for r in audit_rows(again, TABLE, ACTOR)] == ids
        # a replay after the ALTERs landed sees UC already matching: no change rows, still stamps
        landed = {r.column_name: uc_matching(r) for r in rows}
        replay = plan_table(TABLE, rows, decided_for(*rows), landed)
        assert replay.changes == []
        assert audit_rows(replay, TABLE, ACTOR) == []
        assert {s.column_name for s in replay.stamp} == {"a", "b"}


# ---------------------------------------------------------------- refusal evidence and the approve audit contract

class TestRefusalEvidence:
    def test_refusal_audit_records_row_and_decided_values(self):
        audited = row("claim_ref", sensitivity="pii")
        tampered = dataclasses.replace(audited, sensitivity="internal")
        p = plan_table(TABLE, [tampered], decided_for(audited), {"claim_ref": UcColumn(None, {})})
        [(_, _, action, _, _, before_json, after_json)] = audit_rows(p, TABLE, ACTOR)
        assert action == "apply_refused"
        before, after = json.loads(before_json), json.loads(after_json)
        assert before["row"]["sensitivity"] == "internal"
        assert after["decided"]["sensitivity"] == "pii"
        assert after["reason"]


class TestApproveAuditContract:
    """The job feeds plan_table the `approved` dict of approve's own audit_after(); prove the two agree."""

    @staticmethod
    def _decided(r: Approved) -> dict:
        from gates.hydration.approve import Action, ApprovedRow, audit_after
        a = Action(proposal_id="p1", table_name=r.table_name, column_name=r.column_name, kind="accepted",
                   new_status="accepted", approved=ApprovedRow(r.table_name, r.column_name, r.term_id,
                                                               r.description, r.sensitivity, r.approved_by),
                   golden=None, actor="svc")
        return json.loads(json.dumps(audit_after(a)))["approved"]

    def test_approve_audit_payload_is_accepted(self):
        r = row("claim_ref")
        p = plan_table(TABLE, [r], {"claim_ref": self._decided(r)}, {"claim_ref": UcColumn(None, {})})
        assert p.refused == [] and [c.column for c in p.changes] == ["claim_ref"]

    @pytest.mark.parametrize("field,value", [("term_id", "other"), ("description", "x"), ("sensitivity", "pii"),
                                             ("approved_by", "x")])
    def test_approve_audit_payload_detects_each_field(self, field, value):
        r = row("claim_ref")
        tampered = dataclasses.replace(r, **{field: value})
        p = plan_table(TABLE, [tampered], {"claim_ref": self._decided(r)}, {"claim_ref": UcColumn(None, {})})
        assert [f.column for f in p.refused] == ["claim_ref"]


class TestRepeatedDrift:
    def test_same_drift_repaired_twice_gets_two_audit_events(self):
        drifted = UcColumn("Claim reference.", {TERM: "claim_reference"})  # gg_sensitivity removed by hand
        first = plan1(row("a", applied=T_LATE), drifted)
        second = plan1(row("a", applied="2026-01-02T10:00:00"), drifted)  # stamped again by the first repair
        assert only_change(first).event_id != only_change(second).event_id

    def test_unstamped_replay_keeps_the_id(self):
        drifted = UcColumn(None, {})
        assert only_change(plan1(row("a"), drifted)).event_id == only_change(plan1(row("a"), drifted)).event_id

    def test_duplicate_column_rows_raise(self):
        r = row("a")
        with pytest.raises(ValueError):
            plan_table(TABLE, [r, dataclasses.replace(r, table_name="CAT.raw.claims")], decided_for(r),
                       {"a": UcColumn(None, {})})


class TestRlsTagIgnored:
    """specs/consumption.md: hydration apply never touches `<tag_prefix>_rls_attribute` (consumption owns it)."""
    RLS = "gg_rls_attribute"

    def test_matching_column_with_rls_tag_is_no_change(self):
        r = row("region", term_id="region_code")
        p = plan1(r, uc_matching(r, **{self.RLS: "region"}))
        assert p.changes == [] and p.statement_count == 0

    def test_changes_never_mention_rls_tag(self):
        r = row("region", term_id=None, sensitivity="confidential")
        uc = UcColumn(comment=None, tags={SENS: "pii", TERM: "region_code", self.RLS: "region"})
        c = only_change(plan1(r, uc))
        assert self.RLS not in c.set_tags and self.RLS not in c.unset_tags
        assert c.unset_tags == (TERM,)
        assert all(self.RLS not in s for s in c.statements)
        assert self.RLS not in c.before["tags"] and self.RLS not in c.after["tags"]

    def test_untracked_column_with_only_rls_tag_not_reported(self):
        p = plan_table(TABLE, [], {}, {"region": UcColumn(comment=None, tags={self.RLS: "region"})})
        assert p.untracked == [] and p.changes == []
