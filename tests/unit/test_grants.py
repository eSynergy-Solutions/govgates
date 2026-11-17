"""Grants generator (specs/consumption.md: Policy model `grant` bullet and sub-bullets Live agreement, Drift scope,
USE CATALOG, Exempt from drift; "Decisions (8 Nov)" item 3). Pure: no Spark, no workspace.

Agreements say whether; the generator only ever grants USE CATALOG / USE SCHEMA / SELECT on the agreed product to
the agreed principal, and revokes SELECT / USE SCHEMA in the managed (grant_schemas) scope that no live agreement
backs. ALL PRIVILEGES / MANAGE / ownership by anyone but the CI SP and owner_exception is a failure, not a revoke.

Fixture conventions (chosen here from the Slice B interface note, not fixed by the spec):
- `managed_schemas` are bare schema names in `catalog` (bundle variable grant_schemas, default the raw schema);
  Want / current / owners keys use the full dotted object key, lowercased (as SHOW GRANTS prints objectKey).
- SHOW GRANTS fixtures are (principal, actionType, objectType, objectKey) in the shape seen on dev: rows inherited
  from a parent show the parent's objectType/objectKey.
- `requests` rows carry `consumer` and `securable` (the requested product).
- Failure / refusal wording is not fixed: tests check presence and that the principal / agreement id is named.
- Synthetic names only: catalog gate_sandbox, group gs_analyst_uk, the CI SP uuid, placeholder uuids.
"""

from __future__ import annotations

import dataclasses
import json
import random
import re
from datetime import UTC, datetime, timedelta, timezone

import pytest

from gates.consumption.grants import (
    GRANT_PREFIX,
    Agreement,
    Grant,
    GrantPlan,
    GrantRow,
    RowChange,
    StatusChange,
    agreement_state,
    expiry_sweep,
    grant_policy_id,
    grant_row_dict,
    normalise_principal,
    plan_grants,
    product_ready,
    status_change,
    sync_grant_rows,
    tampered_grants,
    verify_agreements,
    wanted_grants,
)
from gates.consumption.row_filter import Action
from gates.hydration.apply import Approved, event_id_of, quote_ident, quote_table

CAT = "gate_sandbox"
MANAGED = ["raw"]
SCH = f"{CAT}.raw"
TBL = f"{CAT}.raw.claims"
TBL2 = f"{CAT}.raw.policies"
CUR_SCH = f"{CAT}.curated"
CUR_TBL = f"{CAT}.curated.claims_summary"
CI = "0c1a0c1a-0000-4000-8000-00000000c1c1"  # CI service principal applicationId
OWNER_EXC = "gate.owner@example.com"  # synthetic owner_exception
EXEMPT = [CI, OWNER_EXC]
ANALYST = "gs_analyst_uk"
SP_A = "00000000-0000-4000-8000-0000000000a1"  # placeholder consumer SP
SP_B = "00000000-0000-4000-8000-0000000000b2"
NOW = datetime(2026, 11, 8, 12, 0, 0, tzinfo=UTC)
T_FROM = datetime(2026, 11, 1, 9, 0, 0, tzinfo=UTC)
T_TO = T_FROM + timedelta(days=90)


# ---------------------------------------------------------------- fixtures

def agr(agreement_id: str = "ag-0001", *, request_id: str | None = None, consumer: str = SP_A, product: str = TBL,
        use_code: str = "claims_reporting", access_mode: str = "system", status: str = "active",
        valid_from: datetime = T_FROM, valid_to: datetime = T_TO, version: int = 1) -> Agreement:
    return Agreement(agreement_id=agreement_id, request_id=request_id or f"req-{agreement_id}", consumer=consumer,
                     product=product, use_code=use_code, access_mode=access_mode, status=status,
                     valid_from=valid_from, valid_to=valid_to, version=version)


def audited(*agreements) -> dict:
    """newest_audit as read back from gov.audit_log after_json (JSON round trip)."""
    return {a.agreement_id: json.loads(json.dumps(agreement_state(a))) for a in agreements}


def evidence(*agreements, steps=("pattern_check", "use_check", "agreement")) -> dict:
    return {a.request_id: set(steps) for a in agreements}


def reqs(*agreements) -> dict:
    return {a.request_id: {"request_id": a.request_id, "consumer": a.consumer, "securable": a.product}
            for a in agreements}


def verify(agreements, *, newest_audit=None, evidence_steps=None, requests=None, now=NOW):
    agreements = list(agreements)
    return verify_agreements(agreements,
                             newest_audit=audited(*agreements) if newest_audit is None else newest_audit,
                             evidence_steps=evidence(*agreements) if evidence_steps is None else evidence_steps,
                             requests=reqs(*agreements) if requests is None else requests, now=now)


def live_ids(agreements, **kw) -> set[str]:
    live, _ = verify(agreements, **kw)
    return {a.agreement_id for a in live}


def refused_ids(agreements, **kw) -> set[str]:
    _, refused = verify(agreements, **kw)
    return {aid for aid, _ in refused}


def appr(table: str = TBL, col: str = "claim_id", *, approved_at: str | None = "2026-11-01T09:00:00",
         applied: str | None = "2026-11-01T10:00:00") -> Approved:
    return Approved(table_name=table, column_name=col, term_id="claim_id", description="d", sensitivity="internal",
                    approved_by="owner.a", approved_at=approved_at, applied_to_uc_at=applied)


def g(principal: str, action: str, otype: str, okey: str) -> Grant:
    return Grant(principal=principal, action_type=action, object_type=otype, object_key=okey)


def want(consumer: str, product: str = TBL) -> set:
    schema = product.rsplit(".", 1)[0]
    p = normalise_principal(consumer)
    return {("TABLE", product.lower(), p, "SELECT"), ("SCHEMA", schema.lower(), p, "USE SCHEMA"),
            ("CATALOG", CAT, p, "USE CATALOG")}


def dev_current() -> dict:
    """SHOW GRANTS on each queried securable, as seen on dev before Slice B (analyst holds schema-wide SELECT)."""
    return {
        ("TABLE", TBL): [g(CI, "MANAGE", "SCHEMA", SCH), g(CI, "ALL PRIVILEGES", "CATALOG", CAT),
                         g(ANALYST, "SELECT", "SCHEMA", SCH)],
        ("SCHEMA", SCH): [g(ANALYST, "SELECT", "SCHEMA", SCH), g(ANALYST, "USE SCHEMA", "SCHEMA", SCH),
                          g(CI, "MANAGE", "SCHEMA", SCH), g(CI, "ALL PRIVILEGES", "CATALOG", CAT)],
        ("CATALOG", CAT): [g(ANALYST, "USE CATALOG", "CATALOG", CAT), g("account users", "BROWSE", "CATALOG", CAT),
                           g(CI, "ALL PRIVILEGES", "CATALOG", CAT)],
    }


def converged_current() -> dict:
    """dev_current after the analyst's live agreement on raw.claims has been applied."""
    return {
        ("TABLE", TBL): [g(CI, "MANAGE", "SCHEMA", SCH), g(CI, "ALL PRIVILEGES", "CATALOG", CAT),
                         g(ANALYST, "SELECT", "TABLE", TBL), g(ANALYST, "USE SCHEMA", "SCHEMA", SCH)],
        ("SCHEMA", SCH): [g(ANALYST, "USE SCHEMA", "SCHEMA", SCH), g(CI, "MANAGE", "SCHEMA", SCH),
                          g(CI, "ALL PRIVILEGES", "CATALOG", CAT)],
        ("CATALOG", CAT): [g(ANALYST, "USE CATALOG", "CATALOG", CAT), g("account users", "BROWSE", "CATALOG", CAT),
                           g(CI, "ALL PRIVILEGES", "CATALOG", CAT)],
    }


DEV_OWNERS = {("CATALOG", CAT): OWNER_EXC, ("SCHEMA", SCH): CI, ("TABLE", TBL): CI}


def plan(wanted=frozenset(), current=None, owners=None, *, exempt=None, managed=None, last_events=None) -> GrantPlan:
    return plan_grants(set(wanted), dev_current() if current is None else current,
                       dict(DEV_OWNERS) if owners is None else owners, catalog=CAT,
                       managed_schemas=MANAGED if managed is None else managed,
                       exempt=EXEMPT if exempt is None else exempt,
                       last_events={} if last_events is None else last_events)


def obj(key: str, principal: str, privilege: str) -> str:
    return f"{key}|{principal}|{privilege}".lower()


def objs(actions) -> list[str]:
    return [a.object for a in actions]


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().rstrip(";").strip()


# ---------------------------------------------------------------- types, names, normalisation

class TestTypes:
    @pytest.mark.parametrize("value", [
        lambda: agr(),
        lambda: GrantRow(policy_id="gr_x", securable=TBL, principal=SP_A, privilege="SELECT", active=True),
        lambda: g(ANALYST, "SELECT", "TABLE", TBL),
        lambda: plan(),
    ])
    def test_frozen(self, value):
        v = value()
        f = dataclasses.fields(v)[0].name
        with pytest.raises(dataclasses.FrozenInstanceError):
            setattr(v, f, "x")

    def test_grant_plan_shape(self):
        p = plan(want(ANALYST), converged_current())
        assert p.revokes == [] and p.grants == [] and p.failures == ()
        assert isinstance(p.failures, tuple)

    def test_actions_are_row_filter_actions(self):
        p = plan(want(ANALYST))
        assert p.revokes and p.grants
        assert all(isinstance(a, Action) for a in p.revokes + p.grants)
        assert {a.kind for a in p.revokes} == {"grant_revoke"} and {a.kind for a in p.grants} == {"grant_set"}


class TestNames:
    def test_grant_policy_id(self):
        assert GRANT_PREFIX == "gr_"
        assert grant_policy_id("ag-0001") == "gr_ag-0001"

    def test_grant_policy_id_distinct(self):
        assert grant_policy_id("ag-0001") != grant_policy_id("ag-0002")

    @pytest.mark.parametrize("raw,expected", [
        ("gs_analyst_uk", "gs_analyst_uk"),
        ("  GS_Analyst_UK ", "gs_analyst_uk"),
        ("`gs_analyst_uk`", "gs_analyst_uk"),
        (" `0C1A0C1A-0000-4000-8000-00000000C1C1` ", CI),
        ("Account Users", "account users"),
    ])
    def test_normalise_principal(self, raw, expected):
        assert normalise_principal(raw) == expected


# ---------------------------------------------------------------- agreement_state

class TestAgreementState:
    def test_all_fields_present(self):
        s = agreement_state(agr())
        assert set(s) == {f.name for f in dataclasses.fields(Agreement)}
        assert s["agreement_id"] == "ag-0001" and s["status"] == "active" and s["version"] == 1

    def test_json_able(self):
        s = agreement_state(agr())
        assert json.loads(json.dumps(s)) == s

    def test_datetimes_iso_utc(self):
        s = agreement_state(agr())
        for k, v in (("valid_from", T_FROM), ("valid_to", T_TO)):
            assert isinstance(s[k], str)
            parsed = datetime.fromisoformat(s[k])
            assert parsed.tzinfo is not None and parsed == v

    def test_naive_is_utc(self):
        naive = agr(valid_from=T_FROM.replace(tzinfo=None), valid_to=T_TO.replace(tzinfo=None))
        assert agreement_state(naive) == agreement_state(agr())

    def test_other_offset_normalised_to_utc(self):
        plus1 = timezone(timedelta(hours=1))
        shifted = agr(valid_from=T_FROM.astimezone(plus1), valid_to=T_TO.astimezone(plus1))
        assert agreement_state(shifted) == agreement_state(agr())

    def test_deterministic(self):
        assert agreement_state(agr()) == agreement_state(agr())


# ---------------------------------------------------------------- verify_agreements: liveness

class TestLive:
    def test_active_system_in_term_with_evidence_is_live(self):
        live, refused = verify([agr()])
        assert [a.agreement_id for a in live] == ["ag-0001"] and refused == []

    def test_valid_to_equal_now_is_dead(self):
        a = agr(valid_to=NOW)
        assert live_ids([a]) == set() and refused_ids([a]) == set()

    def test_valid_to_past_is_dead(self):
        a = agr(valid_to=NOW - timedelta(seconds=1))
        assert live_ids([a]) == set() and refused_ids([a]) == set()

    def test_valid_to_just_after_now_is_live(self):
        assert live_ids([agr(valid_to=NOW + timedelta(seconds=1))]) == {"ag-0001"}

    @pytest.mark.parametrize("status", ["revoked", "expired"])
    def test_non_active_status_is_dead_not_refused(self, status):
        a = agr(status=status, version=2)
        assert live_ids([a]) == set() and refused_ids([a]) == set()

    def test_no_agreement_evidence_is_dead(self):
        a = agr()
        ev = {a.request_id: {"pattern_check", "use_check"}}
        assert live_ids([a], evidence_steps=ev) == set() and refused_ids([a], evidence_steps=ev) == set()

    def test_no_evidence_at_all_is_dead(self):
        assert live_ids([agr()], evidence_steps={}) == set()

    def test_proxy_is_never_live(self):
        a = agr(access_mode="proxy")
        assert live_ids([a]) == set() and refused_ids([a]) == set()

    def test_naive_valid_to_is_utc(self):
        # naive 12:30 is 12:30 UTC: live at 12:00 UTC, dead at 12:30 UTC
        a = agr(valid_from=T_FROM.replace(tzinfo=None), valid_to=datetime(2026, 11, 8, 12, 30))
        assert live_ids([a]) == {"ag-0001"}
        assert live_ids([a], now=datetime(2026, 11, 8, 12, 30, tzinfo=UTC)) == set()

    def test_naive_now_is_utc(self):
        a = agr(valid_to=datetime(2026, 11, 8, 12, 30, tzinfo=UTC))
        assert live_ids([a], now=datetime(2026, 11, 8, 12, 29)) == {"ag-0001"}
        assert live_ids([a], now=datetime(2026, 11, 8, 12, 30)) == set()

    def test_aware_other_offset_compared_in_utc(self):
        # 13:00+01:00 == 12:00 UTC == now -> dead
        plus1 = timezone(timedelta(hours=1))
        a = agr(valid_to=datetime(2026, 11, 8, 13, 0, tzinfo=plus1))
        assert live_ids([a]) == set()

    def test_stored_naive_matches_audit_written_aware(self):
        # the job writes the audit from an aware row; Spark reads the stored row back naive (UTC): still verified
        stored = agr(valid_from=T_FROM.replace(tzinfo=None), valid_to=T_TO.replace(tzinfo=None))
        assert live_ids([stored], newest_audit=audited(agr())) == {"ag-0001"}

    def test_mixed_list(self):
        a, b, c = agr("ag-1"), agr("ag-2", status="revoked", version=2), agr("ag-3", valid_to=NOW)
        assert live_ids([a, b, c]) == {"ag-1"}

    def test_deterministic(self):
        items = [agr("ag-1"), agr("ag-2", consumer=SP_B)]
        assert verify(items) == verify(items)


# ---------------------------------------------------------------- verify_agreements: refusals

class TestRefused:
    def test_no_audit_at_all_refused(self):
        a = agr()
        assert refused_ids([a], newest_audit={}) == {"ag-0001"}
        assert refused_ids([a], newest_audit={"ag-0001": None}) == {"ag-0001"}
        assert live_ids([a], newest_audit={}) == set()

    def test_hand_changed_consumer_refused(self):
        audited_row = agr()
        stored = agr(consumer=SP_B)
        kw = dict(newest_audit=audited(audited_row), requests=reqs(audited_row))
        live, refused = verify([stored], **kw)
        assert live == [] and [aid for aid, _ in refused] == ["ag-0001"]
        assert all(isinstance(r, str) and r for _, r in refused)

    @pytest.mark.parametrize("field,value", [
        ("product", TBL2),
        ("valid_to", T_TO + timedelta(days=365)),  # hand-extended term
        ("status", "active"),  # hand-reactivated: audit says revoked
        ("version", 7),
        ("use_code", "marketing"),
        ("access_mode", "proxy"),
    ])
    def test_any_field_differing_from_audit_refused(self, field, value):
        audited_row = agr(status="revoked", version=2) if field == "status" else agr()
        stored = dataclasses.replace(audited_row, **{field: value})
        assert refused_ids([stored], newest_audit=audited(audited_row), requests=reqs(audited_row)) == {"ag-0001"}

    @pytest.mark.parametrize("status", ["active", "revoked", "expired"])
    def test_refused_whatever_status(self, status):
        assert refused_ids([agr(status=status)], newest_audit={}) == {"ag-0001"}

    def test_request_missing_refused(self):
        assert refused_ids([agr()], requests={}) == {"ag-0001"}

    def test_request_consumer_mismatch_refused(self):
        a = agr()
        r = {a.request_id: {"request_id": a.request_id, "consumer": SP_B, "securable": a.product}}
        assert refused_ids([a], requests=r) == {"ag-0001"}

    def test_request_product_mismatch_refused(self):
        a = agr()
        r = {a.request_id: {"request_id": a.request_id, "consumer": a.consumer, "securable": TBL2}}
        assert refused_ids([a], requests=r) == {"ag-0001"}

    def test_request_product_case_insensitive(self):
        a = agr()
        r = {a.request_id: {"request_id": a.request_id, "consumer": a.consumer, "securable": TBL.upper()}}
        assert live_ids([a], requests=r) == {"ag-0001"}

    def test_refusal_names_only_the_bad_agreement(self):
        good, bad = agr("ag-1"), agr("ag-2", consumer=SP_B)
        live, refused = verify([good, bad], newest_audit={**audited(good)})
        assert [a.agreement_id for a in live] == ["ag-1"]
        assert [aid for aid, _ in refused] == ["ag-2"]

    def test_hand_inserted_agreement_and_evidence_gets_no_grant(self):
        # someone INSERTs an agreement row and an `agreement` evidence row by hand: no audit -> refused -> no grant
        a = agr(consumer=ANALYST)
        live, refused = verify([a], newest_audit={})
        assert live == [] and [aid for aid, _ in refused] == ["ag-0001"]
        wanted, _ = wanted_grants(live, [appr()], catalog=CAT, managed_schemas=MANAGED)
        assert wanted == set()


# ---------------------------------------------------------------- expiry sweep

class TestExpirySweep:
    def test_active_past_valid_to_expires(self):
        a = agr(valid_to=NOW - timedelta(days=1))
        (c,) = expiry_sweep([a], NOW, last_events={})
        assert isinstance(c, StatusChange)
        assert c.agreement_id == a.agreement_id
        assert c.before == agreement_state(a)
        assert c.after == agreement_state(dataclasses.replace(a, status="expired", version=2))
        assert c.meta == {"reason": "valid_to passed", "triggered_by": "sweep"}

    def test_valid_to_equal_now_expires(self):
        assert len(expiry_sweep([agr(valid_to=NOW)], NOW, last_events={})) == 1

    def test_not_past_no_change(self):
        assert expiry_sweep([agr(valid_to=NOW + timedelta(seconds=1))], NOW, last_events={}) == []

    @pytest.mark.parametrize("status", ["expired", "revoked"])
    def test_non_active_never_swept(self, status):
        assert expiry_sweep([agr(status=status, valid_to=NOW - timedelta(days=1))], NOW, last_events={}) == []

    def test_version_bumped_from_current(self):
        a = agr(valid_to=NOW - timedelta(days=1), version=4)
        (c,) = expiry_sweep([a], NOW, last_events={})
        assert c.after["version"] == 5 and c.after["status"] == "expired"

    def test_naive_valid_to_is_utc(self):
        a = agr(valid_to=datetime(2026, 11, 8, 11, 59))
        assert len(expiry_sweep([a], NOW, last_events={})) == 1

    def test_deterministic_id(self):
        a = agr(valid_to=NOW - timedelta(days=1))
        (c1,) = expiry_sweep([a], NOW, last_events={})
        (c2,) = expiry_sweep([a], NOW + timedelta(hours=1), last_events={})
        assert isinstance(c1.event_id, str) and c1.event_id and c1.event_id == c2.event_id

    def test_id_includes_last_event(self):
        a = agr(valid_to=NOW - timedelta(days=1))
        (c1,) = expiry_sweep([a], NOW, last_events={})
        (c2,) = expiry_sweep([a], NOW, last_events={a.agreement_id: "ev-prev"})
        assert c1.event_id != c2.event_id

    def test_swept_state_verifies_as_not_live_not_refused(self):
        # after the sweep's audit and MERGE land, the row is verified (matches its newest audit) and simply dead
        a = agr(valid_to=NOW - timedelta(days=1))
        (c,) = expiry_sweep([a], NOW, last_events={})
        expired = dataclasses.replace(a, status="expired", version=2)
        assert verify([expired], newest_audit={a.agreement_id: c.after}) == ([], [])

    def test_only_due_ones(self):
        due, not_due = agr("ag-1", valid_to=NOW - timedelta(days=1)), agr("ag-2")
        assert [c.agreement_id for c in expiry_sweep([due, not_due], NOW, last_events={})] == ["ag-1"]


# ---------------------------------------------------------------- manual status change (set_agreement_status)

class TestStatusChange:
    def sc(self, a=None, new_status="revoked", *, reason="consumer offboarded", triggered_by="owner.a@example.com",
           last_event_id=None):
        return status_change(agr() if a is None else a, new_status, reason=reason, triggered_by=triggered_by,
                             last_event_id=last_event_id)

    def test_revoke_ok(self):
        a = agr()
        c = self.sc(a)
        assert c.agreement_id == a.agreement_id
        assert c.before == agreement_state(a)
        assert c.after == agreement_state(dataclasses.replace(a, status="revoked", version=2))
        assert c.meta == {"reason": "consumer offboarded", "triggered_by": "owner.a@example.com"}
        assert c.event_id

    def test_revoked_state_verifies_and_is_dead(self):
        a = agr()
        c = self.sc(a)
        revoked = dataclasses.replace(a, status="revoked", version=2)
        assert verify([revoked], newest_audit={a.agreement_id: c.after}) == ([], [])

    def test_manual_expired_refused(self):
        with pytest.raises(ValueError):
            self.sc(new_status="expired")

    def test_set_active_refused(self):
        with pytest.raises(ValueError):
            self.sc(new_status="active")

    @pytest.mark.parametrize("status", ["revoked", "expired"])
    def test_never_reactivate_or_re_revoke(self, status):
        with pytest.raises(ValueError):
            self.sc(agr(status=status, version=2))
        with pytest.raises(ValueError):
            self.sc(agr(status=status, version=2), "active")

    def test_unknown_agreement_refused(self):
        with pytest.raises(ValueError):
            status_change(None, "revoked", reason="r", triggered_by="owner.a@example.com", last_event_id=None)

    @pytest.mark.parametrize("blank", ["", "   "])
    def test_blank_reason_refused(self, blank):
        with pytest.raises(ValueError):
            self.sc(reason=blank)

    @pytest.mark.parametrize("blank", ["", "   "])
    def test_blank_triggered_by_refused(self, blank):
        with pytest.raises(ValueError):
            self.sc(triggered_by=blank)

    def test_event_id_deterministic_and_chained(self):
        assert self.sc().event_id == self.sc().event_id
        assert self.sc().event_id != self.sc(last_event_id="ev-prev").event_id

    def test_event_id_differs_from_sweep(self):
        a = agr(valid_to=NOW - timedelta(days=1))
        (swept,) = expiry_sweep([a], NOW, last_events={})
        assert self.sc(a).event_id != swept.event_id


# ---------------------------------------------------------------- grant rows in gov.access_policies

class TestSyncGrantRows:
    def row(self, a: Agreement, active: bool = True) -> GrantRow:
        return GrantRow(policy_id=grant_policy_id(a.agreement_id), securable=a.product, principal=a.consumer,
                        privilege="SELECT", active=active)

    def test_new_live_agreement_gets_active_row(self):
        a = agr()
        (c,) = sync_grant_rows([a], {a.agreement_id}, {}, {})
        assert isinstance(c, RowChange)
        assert c.policy_id == "gr_ag-0001" and c.before is None
        assert c.after == grant_row_dict(self.row(a))
        assert c.after["securable"] == TBL and c.after["principal"] == SP_A
        assert c.after["privilege"] == "SELECT" and c.after["active"] is True
        assert c.event_id

    def test_dead_agreement_row_set_inactive(self):
        a = agr(status="revoked", version=2)
        existing = {grant_policy_id(a.agreement_id): self.row(a)}
        (c,) = sync_grant_rows([a], set(), existing, {})
        assert c.before == grant_row_dict(self.row(a)) and c.after == grant_row_dict(self.row(a, active=False))

    def test_equal_row_no_change(self):
        a = agr()
        assert sync_grant_rows([a], {a.agreement_id}, {grant_policy_id(a.agreement_id): self.row(a)}, {}) == []

    def test_inactive_row_stays_inactive_no_change(self):
        a = agr(status="expired", version=2)
        existing = {grant_policy_id(a.agreement_id): self.row(a, active=False)}
        assert sync_grant_rows([a], set(), existing, {}) == []

    def test_never_live_no_row(self):
        assert sync_grant_rows([agr(status="revoked", version=2)], set(), {}, {}) == []

    def test_refused_agreement_never_gets_a_row(self):
        a = agr(consumer=ANALYST)
        live, _ = verify([a], newest_audit={})
        assert sync_grant_rows([a], {x.agreement_id for x in live}, {}, {}) == []

    def test_never_deleted(self):
        a, b = agr("ag-1"), agr("ag-2", status="revoked", version=2)
        existing = {grant_policy_id("ag-1"): self.row(a), grant_policy_id("ag-2"): self.row(b)}
        changes = sync_grant_rows([a, b], set(), existing, {})
        assert all(c.after is not None for c in changes)
        assert {c.policy_id for c in changes} == set(existing)
        assert all(c.after["active"] is False for c in changes)

    def test_row_whose_agreement_is_gone_is_retired(self):
        a = agr()
        (c,) = sync_grant_rows([], set(), {grant_policy_id(a.agreement_id): self.row(a)}, {})
        assert c.before == grant_row_dict(self.row(a)) and c.after == grant_row_dict(self.row(a, active=False))

    def test_reactivating_a_hand_retired_row_for_a_live_agreement(self):
        a = agr()
        existing = {grant_policy_id(a.agreement_id): self.row(a, active=False)}
        (c,) = sync_grant_rows([a], {a.agreement_id}, existing, {})
        assert c.after["active"] is True

    def test_event_id_deterministic_and_chained(self):
        a = agr()
        (c1,) = sync_grant_rows([a], {a.agreement_id}, {}, {})
        (c2,) = sync_grant_rows([a], {a.agreement_id}, {}, {})
        (c3,) = sync_grant_rows([a], {a.agreement_id}, {}, {grant_policy_id(a.agreement_id): "ev-prev"})
        assert c1.event_id == c2.event_id != c3.event_id

    def test_grant_row_dict_json_able(self):
        d = grant_row_dict(self.row(agr()))
        assert json.loads(json.dumps(d)) == d


class TestTamperedGrants:
    def row(self, pid="gr_ag-1", principal=SP_A, active=True) -> GrantRow:
        return GrantRow(policy_id=pid, securable=TBL, principal=principal, privilege="SELECT", active=active)

    def test_clean(self):
        r = self.row()
        assert tampered_grants([r], {r.policy_id: grant_row_dict(r)}) == []

    def test_clean_after_json_round_trip(self):
        r = self.row()
        assert tampered_grants([r], {r.policy_id: json.loads(json.dumps(grant_row_dict(r)))}) == []

    def test_hand_changed_principal(self):
        r = self.row()
        assert tampered_grants([self.row(principal=SP_B)], {r.policy_id: grant_row_dict(r)}) == [r.policy_id]

    def test_hand_reactivated(self):
        audited_row = self.row(active=False)
        assert tampered_grants([self.row()], {audited_row.policy_id: grant_row_dict(audited_row)}) == ["gr_ag-1"]

    def test_no_audit(self):
        assert tampered_grants([self.row()], {}) == ["gr_ag-1"]
        assert tampered_grants([self.row()], {"gr_ag-1": None}) == ["gr_ag-1"]

    def test_audited_row_gone(self):
        r = self.row("gr_gone")
        assert tampered_grants([self.row()], {"gr_ag-1": grant_row_dict(self.row()),
                                              "gr_gone": grant_row_dict(r)}) == ["gr_gone"]

    def test_sorted(self):
        assert tampered_grants([self.row("gr_zz"), self.row("gr_aa")], {}) == ["gr_aa", "gr_zz"]


# ---------------------------------------------------------------- product_ready

class TestProductReady:
    def test_all_applied_ready(self):
        assert product_ready(TBL, [appr(col="a"), appr(col="b")]) is True

    def test_applied_equal_to_approved_ready(self):
        assert product_ready(TBL, [appr(approved_at="2026-11-01T09:00:00", applied="2026-11-01T09:00:00")]) is True

    def test_one_not_applied(self):
        assert product_ready(TBL, [appr(col="a"), appr(col="b", applied=None)]) is False

    def test_applied_before_approve(self):
        assert product_ready(TBL, [appr(approved_at="2026-11-02T09:00:00", applied="2026-11-01T10:00:00")]) is False

    def test_no_approved_rows(self):
        assert product_ready(TBL, []) is False
        assert product_ready(TBL, [appr(TBL2)]) is False

    def test_other_tables_ignored(self):
        assert product_ready(TBL, [appr(), appr(TBL2, applied=None)]) is True

    def test_case_insensitive_table(self):
        assert product_ready(TBL.upper(), [appr("gate_sandbox.raw.Claims")]) is True


# ---------------------------------------------------------------- wanted_grants

class TestWanted:
    def w(self, live, approved=None):
        return wanted_grants(list(live), [appr(), appr(TBL2)] if approved is None else approved, catalog=CAT,
                             managed_schemas=MANAGED)

    def test_one_live_agreement(self):
        wanted, skipped = self.w([agr(consumer=ANALYST)])
        assert wanted == want(ANALYST) and skipped == []

    def test_exact_tuples(self):
        wanted, _ = self.w([agr(consumer=ANALYST)])
        assert wanted == {("TABLE", TBL, ANALYST, "SELECT"), ("SCHEMA", SCH, ANALYST, "USE SCHEMA"),
                          ("CATALOG", CAT, ANALYST, "USE CATALOG")}

    def test_union_same_consumer_and_product(self):
        wanted, _ = self.w([agr("ag-1", consumer=ANALYST), agr("ag-2", consumer=ANALYST, use_code="other")])
        assert wanted == want(ANALYST)

    def test_union_two_products(self):
        wanted, _ = self.w([agr("ag-1", consumer=ANALYST), agr("ag-2", consumer=ANALYST, product=TBL2)])
        assert wanted == want(ANALYST) | want(ANALYST, TBL2)
        assert len(wanted) == 4

    def test_principal_and_product_normalised(self):
        wanted, _ = self.w([agr(consumer=" `GS_Analyst_UK` ", product="Gate_Sandbox.RAW.Claims")])
        assert wanted == want(ANALYST)

    def test_out_of_scope_schema_skipped(self):
        wanted, skipped = self.w([agr(consumer=ANALYST, product=CUR_TBL)], [appr(CUR_TBL)])
        assert wanted == set() and len(skipped) == 1

    def test_other_catalog_skipped(self):
        other = "other_cat.raw.claims"
        wanted, skipped = self.w([agr(consumer=ANALYST, product=other)], [appr(other)])
        assert wanted == set() and len(skipped) == 1

    def test_not_ready_skipped(self):
        wanted, skipped = self.w([agr(consumer=ANALYST)], [appr(applied=None)])
        assert wanted == set() and len(skipped) == 1 and isinstance(skipped[0], str)

    def test_not_hydrated_skipped(self):
        wanted, skipped = self.w([agr(consumer=ANALYST)], [])
        assert wanted == set() and len(skipped) == 1

    def test_managed_schemas_is_config(self):
        wanted, _ = wanted_grants([agr(consumer=ANALYST, product=CUR_TBL)], [appr(CUR_TBL)], catalog=CAT,
                                  managed_schemas=["raw", "curated"])
        assert wanted == want(ANALYST, CUR_TBL)

    def test_no_live_no_wanted(self):
        assert self.w([]) == (set(), [])


# ---------------------------------------------------------------- plan_grants: the dev fixture

class TestPlanDev:
    def test_dev_fixture_moves_select_from_schema_to_table(self):
        p = plan(want(ANALYST))
        assert objs(p.revokes) == [obj(SCH, ANALYST, "SELECT")]
        assert objs(p.grants) == [obj(TBL, ANALYST, "SELECT")]
        assert p.failures == ()

    def test_dev_fixture_statements(self):
        p = plan(want(ANALYST))
        assert _norm(p.revokes[0].statement) == f"REVOKE SELECT ON SCHEMA {quote_table(SCH)} FROM `{ANALYST}`"
        assert _norm(p.grants[0].statement) == f"GRANT SELECT ON TABLE {quote_table(TBL)} TO `{ANALYST}`"

    def test_converged_is_noop(self):
        p = plan(want(ANALYST), converged_current())
        assert p == GrantPlan(revokes=[], grants=[], failures=())

    def test_no_agreement_revokes_select_and_use_schema_keeps_use_catalog(self):
        p = plan(set())
        assert objs(p.revokes) == [obj(SCH, ANALYST, "SELECT"), obj(SCH, ANALYST, "USE SCHEMA")]
        assert p.grants == [] and p.failures == ()
        assert all("USE CATALOG" not in a.statement.upper() for a in p.revokes)

    def test_revoked_agreement_loses_table_select_on_next_run(self):
        p = plan(set(), converged_current())
        assert objs(p.revokes) == [obj(TBL, ANALYST, "SELECT"), obj(SCH, ANALYST, "USE SCHEMA")]
        assert _norm(p.revokes[0].statement) == f"REVOKE SELECT ON TABLE {quote_table(TBL)} FROM `{ANALYST}`"
        assert _norm(p.revokes[1].statement) == f"REVOKE USE SCHEMA ON SCHEMA {quote_table(SCH)} FROM `{ANALYST}`"

    def test_inherited_rows_ignored_at_child_level(self):
        # the TABLE listing shows the analyst's schema SELECT and the CI SP's catalog/schema grants: inherited only
        cur = {("TABLE", TBL): dev_current()[("TABLE", TBL)]}
        assert plan(set(), cur, owners={}) == GrantPlan(revokes=[], grants=[], failures=())

    def test_first_grant_from_nothing(self):
        p = plan(want(SP_A), {("TABLE", TBL): [], ("SCHEMA", SCH): [], ("CATALOG", CAT): []}, owners={})
        assert objs(p.grants) == [obj(CAT, SP_A, "USE CATALOG"), obj(SCH, SP_A, "USE SCHEMA"),
                                  obj(TBL, SP_A, "SELECT")]
        assert [_norm(a.statement) for a in p.grants] == [
            f"GRANT USE CATALOG ON CATALOG {quote_ident(CAT)} TO `{SP_A}`",
            f"GRANT USE SCHEMA ON SCHEMA {quote_table(SCH)} TO `{SP_A}`",
            f"GRANT SELECT ON TABLE {quote_table(TBL)} TO `{SP_A}`",
        ]
        assert p.revokes == []

    def test_same_input_same_plan(self):
        assert plan(want(ANALYST)) == plan(want(ANALYST))

    def test_input_order_irrelevant(self):
        cur = dev_current()
        shuffled = {k: list(v) for k, v in reversed(list(cur.items()))}
        for v in shuffled.values():
            random.Random(7).shuffle(v)
        assert plan(want(ANALYST), shuffled) == plan(want(ANALYST), cur)


# ---------------------------------------------------------------- plan_grants: drift rules

class TestDrift:
    def test_catalog_level_select_revoked(self):
        cur = converged_current()
        cur[("CATALOG", CAT)] = cur[("CATALOG", CAT)] + [g(SP_B, "SELECT", "CATALOG", CAT)]
        p = plan(want(ANALYST), cur)
        assert objs(p.revokes) == [obj(CAT, SP_B, "SELECT")]
        assert _norm(p.revokes[0].statement) == f"REVOKE SELECT ON CATALOG {quote_ident(CAT)} FROM `{SP_B}`"

    def test_use_catalog_never_revoked(self):
        cur = {("CATALOG", CAT): [g(SP_B, "USE CATALOG", "CATALOG", CAT), g(ANALYST, "USE CATALOG", "CATALOG", CAT)]}
        assert plan(set(), cur, owners={}).revokes == []

    def test_unwanted_table_select_revoked(self):
        cur = converged_current()
        cur[("TABLE", TBL2)] = [g(SP_B, "SELECT", "TABLE", TBL2)]
        p = plan(want(ANALYST), cur)
        assert objs(p.revokes) == [obj(TBL2, SP_B, "SELECT")]

    def test_wanted_is_per_principal(self):
        # the analyst's agreement does not keep SP_B's direct SELECT on the same table
        cur = converged_current()
        cur[("TABLE", TBL)] = cur[("TABLE", TBL)] + [g(SP_B, "SELECT", "TABLE", TBL)]
        assert objs(plan(want(ANALYST), cur).revokes) == [obj(TBL, SP_B, "SELECT")]

    def test_out_of_scope_schema_and_tables_untouched(self):
        cur = converged_current()
        cur[("SCHEMA", CUR_SCH)] = [g(SP_B, "SELECT", "SCHEMA", CUR_SCH), g(SP_B, "USE SCHEMA", "SCHEMA", CUR_SCH),
                                    g(SP_B, "ALL PRIVILEGES", "SCHEMA", CUR_SCH)]
        cur[("TABLE", CUR_TBL)] = [g(SP_B, "SELECT", "TABLE", CUR_TBL), g(SP_B, "MANAGE", "TABLE", CUR_TBL)]
        owners = {**DEV_OWNERS, ("SCHEMA", CUR_SCH): SP_B, ("TABLE", CUR_TBL): SP_B}
        assert plan(want(ANALYST), cur, owners) == GrantPlan(revokes=[], grants=[], failures=())

    def test_database_object_type_is_schema(self):
        cur = converged_current()
        cur[("SCHEMA", SCH)] = [g(ANALYST, "USE SCHEMA", "DATABASE", SCH), g(SP_B, "SELECT", "DATABASE", SCH)]
        p = plan(want(ANALYST), cur)
        assert objs(p.revokes) == [obj(SCH, SP_B, "SELECT")]
        assert _norm(p.revokes[0].statement) == f"REVOKE SELECT ON SCHEMA {quote_table(SCH)} FROM `{SP_B}`"
        assert p.grants == []  # USE SCHEMA held directly, reported as DATABASE

    def test_object_key_case_insensitive(self):
        cur = converged_current()
        cur[("TABLE", TBL)] = [g(ANALYST, "SELECT", "TABLE", TBL.upper())]
        assert plan(want(ANALYST), cur).grants == []

    @pytest.mark.parametrize("priv", ["BROWSE", "READ VOLUME", "EXECUTE", "MODIFY", "READ METADATA", "USE CONNECTION"])
    def test_other_privileges_ignored(self, priv):
        cur = converged_current()
        cur[("TABLE", TBL)] = cur[("TABLE", TBL)] + [g(SP_B, priv, "TABLE", TBL)]
        cur[("SCHEMA", SCH)] = cur[("SCHEMA", SCH)] + [g(SP_B, priv, "SCHEMA", SCH)]
        cur[("CATALOG", CAT)] = cur[("CATALOG", CAT)] + [g(SP_B, priv, "CATALOG", CAT)]
        assert plan(want(ANALYST), cur) == GrantPlan(revokes=[], grants=[], failures=())

    def test_account_users_browse_ignored(self):
        assert plan(want(ANALYST), converged_current()).revokes == []


# ---------------------------------------------------------------- plan_grants: exempt principals

class TestExempt:
    def test_ci_sp_direct_select_never_revoked(self):
        cur = converged_current()
        cur[("SCHEMA", SCH)] = cur[("SCHEMA", SCH)] + [g(CI, "SELECT", "SCHEMA", SCH),
                                                       g(CI, "USE SCHEMA", "SCHEMA", SCH)]
        cur[("CATALOG", CAT)] = cur[("CATALOG", CAT)] + [g(CI, "SELECT", "CATALOG", CAT)]
        cur[("TABLE", TBL)] = cur[("TABLE", TBL)] + [g(CI, "SELECT", "TABLE", TBL)]
        assert plan(want(ANALYST), cur) == GrantPlan(revokes=[], grants=[], failures=())

    def test_exempt_match_case_insensitive_and_backticks(self):
        cur = converged_current()
        cur[("SCHEMA", SCH)] = cur[("SCHEMA", SCH)] + [g(CI.upper(), "SELECT", "SCHEMA", SCH),
                                                       g(CI, "MANAGE", "SCHEMA", SCH)]
        exempt = [f"`{CI.upper()}`", " Gate.Owner@Example.com "]
        assert plan(want(ANALYST), cur, exempt=exempt) == GrantPlan(revokes=[], grants=[], failures=())

    def test_owner_exception_select_never_revoked(self):
        cur = converged_current()
        cur[("CATALOG", CAT)] = cur[("CATALOG", CAT)] + [g(OWNER_EXC, "SELECT", "CATALOG", CAT)]
        assert plan(want(ANALYST), cur).revokes == []

    def test_empty_exempt_entries_ignored(self):
        cur = converged_current()
        cur[("TABLE", TBL)] = cur[("TABLE", TBL)] + [g(SP_B, "SELECT", "TABLE", TBL)]
        p = plan(want(ANALYST), cur, exempt=["", "  ", CI, OWNER_EXC])
        assert objs(p.revokes) == [obj(TBL, SP_B, "SELECT")]

    def test_ci_sp_dev_privileges_are_not_failures(self):
        # dev shape: CI SP holds ALL PRIVILEGES on the catalog and MANAGE on the schema, and owns schema + table
        assert plan(want(ANALYST), converged_current()).failures == ()

    def test_without_exempt_the_ci_sp_is_a_failure(self):
        p = plan(want(ANALYST), converged_current(), exempt=[OWNER_EXC])
        assert p.failures != () and any(CI in f for f in p.failures)
        assert p.revokes == []  # failures are never revoked

    def test_missing_wanted_grant_for_exempt_principal_is_planned(self):
        p = plan(want(CI), {("TABLE", TBL): [], ("SCHEMA", SCH): [], ("CATALOG", CAT): []}, owners={})
        assert len(p.grants) == 3


# ---------------------------------------------------------------- plan_grants: failures

class TestFailures:
    @pytest.mark.parametrize("priv", ["ALL PRIVILEGES", "MANAGE"])
    @pytest.mark.parametrize("otype,key", [("TABLE", TBL), ("SCHEMA", SCH), ("CATALOG", CAT)])
    def test_persona_with_all_privileges_or_manage_is_failure(self, priv, otype, key):
        cur = converged_current()
        cur[(otype, key)] = cur[(otype, key)] + [g(SP_B, priv, otype, key)]
        p = plan(want(ANALYST), cur)
        assert len(p.failures) == 1 and SP_B in p.failures[0]
        assert p.revokes == [] and p.grants == []

    def test_failure_principal_case_insensitive_against_exempt(self):
        cur = converged_current()
        cur[("SCHEMA", SCH)] = cur[("SCHEMA", SCH)] + [g("Gate.Owner@Example.com", "MANAGE", "SCHEMA", SCH)]
        assert plan(want(ANALYST), cur).failures == ()

    @pytest.mark.parametrize("otype,key", [("TABLE", TBL), ("SCHEMA", SCH), ("CATALOG", CAT)])
    def test_non_exempt_owner_is_failure(self, otype, key):
        owners = {**DEV_OWNERS, (otype, key): "someone@example.com"}
        p = plan(want(ANALYST), converged_current(), owners)
        assert len(p.failures) == 1 and "someone@example.com" in p.failures[0]

    def test_exempt_owner_ok(self):
        owners = {("CATALOG", CAT): f"`{OWNER_EXC.upper()}`", ("SCHEMA", SCH): CI.upper(), ("TABLE", TBL): CI}
        assert plan(want(ANALYST), converged_current(), owners).failures == ()

    def test_failure_does_not_block_other_revokes(self):
        # revokes never wait: the plan still carries the drift revokes alongside the failure
        cur = dev_current()
        cur[("TABLE", TBL)] = cur[("TABLE", TBL)] + [g(SP_B, "MANAGE", "TABLE", TBL)]
        p = plan(want(ANALYST), cur)
        assert p.failures != ()
        assert objs(p.revokes) == [obj(SCH, ANALYST, "SELECT")]

    def test_failures_deterministic(self):
        cur = converged_current()
        cur[("TABLE", TBL)] = cur[("TABLE", TBL)] + [g(SP_B, "MANAGE", "TABLE", TBL), g(SP_A, "MANAGE", "TABLE", TBL)]
        rev = {k: list(reversed(v)) for k, v in cur.items()}
        assert plan(want(ANALYST), cur).failures == plan(want(ANALYST), rev).failures


# ---------------------------------------------------------------- plan_grants: statements and quoting

class TestStatements:
    def test_revoke_uses_principal_as_printed(self):
        cur = converged_current()
        cur[("TABLE", TBL)] = cur[("TABLE", TBL)] + [g("GS_Other_Team", "SELECT", "TABLE", TBL)]
        (r,) = plan(want(ANALYST), cur).revokes
        assert _norm(r.statement) == f"REVOKE SELECT ON TABLE {quote_table(TBL)} FROM `GS_Other_Team`"
        assert r.object == obj(TBL, "gs_other_team", "SELECT")

    def test_principal_with_hyphen(self):
        p = plan(want("gs-analyst-uk"), {("TABLE", TBL): [], ("SCHEMA", SCH): [], ("CATALOG", CAT): []}, owners={})
        assert _norm(p.grants[-1].statement) == f"GRANT SELECT ON TABLE {quote_table(TBL)} TO `gs-analyst-uk`"

    def test_embedded_backtick_doubled(self):
        cur = converged_current()
        cur[("TABLE", TBL)] = cur[("TABLE", TBL)] + [g("gs`x", "SELECT", "TABLE", TBL)]
        (r,) = plan(want(ANALYST), cur).revokes
        assert _norm(r.statement).endswith("FROM `gs``x`")

    def test_object_key_with_backtick_quoted(self):
        odd = f"{CAT}.raw.cl`aims"
        cur = {("TABLE", odd): [g(SP_B, "SELECT", "TABLE", odd)]}
        (r,) = plan(set(), cur, owners={}).revokes
        assert quote_table(odd) in r.statement and "`cl``aims`" in r.statement

    def test_never_grants_or_revokes_wider_privileges(self):
        p = plan(set(want(ANALYST)) | want(SP_A), dev_current())
        for a in p.revokes + p.grants:
            s = a.statement.upper()
            assert "ALL PRIVILEGES" not in s and "MANAGE" not in s and "MODIFY" not in s
            assert s.startswith("GRANT ") or s.startswith("REVOKE ")


# ---------------------------------------------------------------- plan_grants: ordering and event ids

class TestOrderAndEvents:
    def everything(self) -> dict:
        cur = {
            ("TABLE", TBL): [g(SP_B, "SELECT", "TABLE", TBL)],
            ("TABLE", TBL2): [g(SP_A, "SELECT", "TABLE", TBL2)],
            ("SCHEMA", SCH): [g(SP_B, "SELECT", "SCHEMA", SCH), g(SP_B, "USE SCHEMA", "SCHEMA", SCH),
                              g(SP_A, "USE SCHEMA", "SCHEMA", SCH)],
            ("CATALOG", CAT): [g(SP_B, "SELECT", "CATALOG", CAT)],
        }
        return cur

    def test_revoke_order(self):
        p = plan(set(), self.everything(), owners={})
        assert objs(p.revokes) == [
            obj(TBL, SP_B, "SELECT"), obj(TBL2, SP_A, "SELECT"),  # TABLE SELECT, sorted by object
            obj(SCH, SP_B, "SELECT"),
            obj(SCH, SP_A, "USE SCHEMA"), obj(SCH, SP_B, "USE SCHEMA"),
            obj(CAT, SP_B, "SELECT"),
        ]

    def test_grant_order(self):
        empty = {("TABLE", TBL): [], ("TABLE", TBL2): [], ("SCHEMA", SCH): [], ("CATALOG", CAT): []}
        p = plan(want(SP_B) | want(SP_A, TBL2), empty, owners={})
        assert objs(p.grants) == [
            obj(CAT, SP_A, "USE CATALOG"), obj(CAT, SP_B, "USE CATALOG"),
            obj(SCH, SP_A, "USE SCHEMA"), obj(SCH, SP_B, "USE SCHEMA"),
            obj(TBL, SP_B, "SELECT"), obj(TBL2, SP_A, "SELECT"),
        ]

    def test_event_id_formula(self):
        p = plan(want(ANALYST), last_events={obj(TBL, ANALYST, "SELECT"): "ev-prev"})
        for a in p.revokes + p.grants:
            expected_last = "ev-prev" if a.object == obj(TBL, ANALYST, "SELECT") else None
            assert a.event_id == event_id_of(a.kind, a.object, expected_last)

    def test_event_ids_unique_and_deterministic(self):
        p1 = plan(want(SP_A), self.everything(), owners={})
        p2 = plan(want(SP_A), self.everything(), owners={})
        ids = [a.event_id for a in p1.revokes + p1.grants]
        assert ids == [a.event_id for a in p2.revokes + p2.grants]
        assert len(ids) == len(set(ids)) and all(isinstance(i, str) and i for i in ids)

    def test_grant_revoke_grant_distinct_ids(self):
        key = obj(TBL, ANALYST, "SELECT")
        last: dict[str, str] = {}
        # run 1: agreement live -> grant
        p1 = plan(want(ANALYST), {("TABLE", TBL): [], ("SCHEMA", SCH): [], ("CATALOG", CAT): []}, owners={},
                  last_events=dict(last))
        (g1,) = [a for a in p1.grants if a.object == key]
        last[key] = g1.event_id
        # run 2: agreement revoked -> revoke
        held = {("TABLE", TBL): [g(ANALYST, "SELECT", "TABLE", TBL)], ("SCHEMA", SCH): [], ("CATALOG", CAT): []}
        p2 = plan(set(), held, owners={}, last_events=dict(last))
        (r2,) = [a for a in p2.revokes if a.object == key]
        last[key] = r2.event_id
        # run 3: a new agreement -> grant again
        p3 = plan(want(ANALYST), {("TABLE", TBL): [], ("SCHEMA", SCH): [], ("CATALOG", CAT): []}, owners={},
                  last_events=dict(last))
        (g3,) = [a for a in p3.grants if a.object == key]
        assert len({g1.event_id, r2.event_id, g3.event_id}) == 3

    def test_crash_replay_same_ids(self):
        last = {obj(SCH, ANALYST, "SELECT"): "ev-1"}
        a = plan(want(ANALYST), last_events=dict(last))
        b = plan(want(ANALYST), last_events=dict(last))
        assert [x.event_id for x in a.revokes + a.grants] == [x.event_id for x in b.revokes + b.grants]

    def test_unrelated_last_events_change_nothing(self):
        assert plan(want(ANALYST)) == plan(want(ANALYST), last_events={"gate_sandbox.other|x|select": "ev-x"})


# ---------------------------------------------------------------- end to end (pure)

class TestEndToEnd:
    def test_live_agreement_to_plan(self):
        a = agr(consumer=ANALYST)
        live, refused = verify([a])
        wanted, skipped = wanted_grants(live, [appr()], catalog=CAT, managed_schemas=MANAGED)
        p = plan(wanted)
        assert refused == [] and skipped == []
        assert objs(p.grants) == [obj(TBL, ANALYST, "SELECT")]
        assert objs(p.revokes) == [obj(SCH, ANALYST, "SELECT")]

    def test_revoked_agreement_loses_access_next_run(self):
        a = agr(consumer=ANALYST)
        c = status_change(a, "revoked", reason="offboarded", triggered_by="owner.a@example.com", last_event_id=None)
        revoked = dataclasses.replace(a, status="revoked", version=2)
        live, refused = verify([revoked], newest_audit={a.agreement_id: c.after})
        wanted, _ = wanted_grants(live, [appr()], catalog=CAT, managed_schemas=MANAGED)
        p = plan(wanted, converged_current())
        assert refused == [] and wanted == set()
        assert obj(TBL, ANALYST, "SELECT") in objs(p.revokes)

    def test_expired_agreement_loses_access_after_sweep(self):
        a = agr(consumer=ANALYST, valid_to=NOW - timedelta(minutes=1))
        (c,) = expiry_sweep([a], NOW, last_events={})
        expired = dataclasses.replace(a, status="expired", version=2)
        live, _ = verify([expired], newest_audit={a.agreement_id: c.after})
        wanted, _ = wanted_grants(live, [appr()], catalog=CAT, managed_schemas=MANAGED)
        assert wanted == set()
        assert obj(TBL, ANALYST, "SELECT") in objs(plan(wanted, converged_current()).revokes)


# ---------------------------------------------------------------- review fixes (reviewer FIX FIRST, Slice B)

from gates.consumption.grants import (  # noqa: E402
    grantable,
    principal_names,
    resume_grant_rows,
    scope_record,
    should_record,
    task_actions,
    task_problems,
)


class TestForbiddenConsumers:
    @pytest.mark.parametrize("consumer", ["account users", "Account Users", "users", "`users`", "admins", "", "  "])
    def test_refused_never_granted(self, consumer):
        a = agr(consumer=consumer)
        live, refused = verify([a])
        assert live == [] and [aid for aid, _ in refused] == ["ag-0001"]
        wanted, _ = wanted_grants(live, [appr()], catalog=CAT, managed_schemas=MANAGED)
        assert wanted == set()


class TestResumeGrantRows:
    def row(self, active=True) -> GrantRow:
        return GrantRow(policy_id="gr_ag-1", securable=TBL, principal=SP_A, privilege="SELECT", active=active)

    def test_crash_before_first_merge_is_resumed_not_tampered(self):
        after = grant_row_dict(self.row())
        newest = {"gr_ag-1": ("ev-1", None, after)}
        (c,) = resume_grant_rows([], newest)
        assert (c.policy_id, c.before, c.after, c.event_id) == ("gr_ag-1", None, after, "ev-1")
        # once completed, the row is clean
        assert tampered_grants([self.row()], {"gr_ag-1": after}) == []

    def test_crash_before_retire_merge_is_resumed(self):
        before, after = grant_row_dict(self.row()), grant_row_dict(self.row(active=False))
        (c,) = resume_grant_rows([self.row()], {"gr_ag-1": ("ev-2", before, after)})
        assert c.after == after and c.event_id == "ev-2"

    def test_completed_write_not_resumed(self):
        after = grant_row_dict(self.row())
        assert resume_grant_rows([self.row()], {"gr_ag-1": ("ev-1", None, after)}) == []

    def test_hand_edit_is_not_resumed(self):
        # stored row matches neither before nor after: tampering, not an interrupted write
        before, after = grant_row_dict(self.row()), grant_row_dict(self.row(active=False))
        hand = GrantRow(policy_id="gr_ag-1", securable=TBL, principal=SP_B, privilege="SELECT", active=True)
        assert resume_grant_rows([hand], {"gr_ag-1": ("ev-2", before, after)}) == []
        assert tampered_grants([hand], {"gr_ag-1": after}) == ["gr_ag-1"]

    def test_non_grant_ids_ignored(self):
        assert resume_grant_rows([], {"mk_raw_pii": ("ev", None, {"policy_id": "mk_raw_pii"})}) == []


class TestGrantable:
    def test_only_agreements_that_produce_a_grant(self):
        ok, cur, unready = agr("ag-1"), agr("ag-2", product=CUR_TBL), agr("ag-3", product=TBL2)
        got, skipped = grantable([ok, cur, unready], [appr(), appr(TBL2, applied=None), appr(CUR_TBL)],
                                 catalog=CAT, managed_schemas=MANAGED)
        assert [a.agreement_id for a in got] == ["ag-1"] and len(skipped) == 2

    def test_unready_live_agreement_gets_no_active_row(self):
        a = agr(product=TBL2)
        got, _ = grantable([a], [appr(TBL2, applied=None)], catalog=CAT, managed_schemas=MANAGED)
        assert sync_grant_rows([a], {x.agreement_id for x in got}, {}, {}) == []


class TestPrincipalNames:
    def test_grant_uses_consumer_as_written(self):
        a = agr(consumer="GS_Analyst_UK")
        names = principal_names([a])
        empty = {("TABLE", TBL): [], ("SCHEMA", SCH): [], ("CATALOG", CAT): []}
        p = plan_grants(want("GS_Analyst_UK"), empty, {}, catalog=CAT, managed_schemas=MANAGED, exempt=EXEMPT,
                        last_events={}, names=names)
        assert all(a.statement.endswith("TO `GS_Analyst_UK`") for a in p.grants)
        assert {a.object for a in p.grants} == {obj(CAT, ANALYST, "USE CATALOG"), obj(SCH, ANALYST, "USE SCHEMA"),
                                                obj(TBL, ANALYST, "SELECT")}

    def test_backticks_stripped(self):
        assert principal_names([agr(consumer=" `gs_analyst_uk` ")]) == {ANALYST: "gs_analyst_uk"}


class TestTaskDecisions:
    def changes(self):
        on = RowChange("gr_a", None, {"active": True}, "e1")
        off = RowChange("gr_b", {"active": True}, {"active": False}, "e2")
        return on, off

    def test_revoke_task_revokes_and_retires_only(self):
        p = plan(want(ANALYST))
        on, off = self.changes()
        actions, rows = task_actions("revoke", p, [on, off], tampered=[])
        assert actions == p.revokes and rows == [off]

    def test_revoke_task_still_revokes_when_tampered_or_failing(self):
        cur = dev_current()
        cur[("TABLE", TBL)] = cur[("TABLE", TBL)] + [g(SP_B, "MANAGE", "TABLE", TBL)]
        p = plan(want(ANALYST), cur)
        actions, _ = task_actions("revoke", p, [], tampered=["gr_x"])
        assert p.failures and actions == p.revokes and actions

    def test_set_task_grants_and_activates(self):
        p = plan(want(ANALYST))
        on, off = self.changes()
        actions, rows = task_actions("set", p, [on, off], tampered=[])
        assert actions == p.grants and rows == [on, off]

    def test_set_task_blocked_by_tampered_rows(self):
        on, off = self.changes()
        assert task_actions("set", plan(want(ANALYST)), [on, off], tampered=["gr_x"]) == ([], [])

    def test_set_task_blocked_by_scope_failure(self):
        cur = dev_current()
        cur[("TABLE", TBL)] = cur[("TABLE", TBL)] + [g(SP_B, "MANAGE", "TABLE", TBL)]
        assert task_actions("set", plan(want(ANALYST), cur), list(self.changes()), tampered=[]) == ([], [])

    def test_unknown_mode(self):
        with pytest.raises(ValueError):
            task_actions("other", plan(), [], tampered=[])

    def test_revoke_task_fails_only_on_failed_statements(self):
        kw = dict(refused=[("ag-1", "x")], tampered=["gr_x"], failures=("f",))
        assert task_problems("revoke", failed=[], **kw) == []
        assert len(task_problems("revoke", failed=[("o", "grant_revoke", "E")], **kw)) == 1

    def test_set_task_fails_on_everything(self):
        assert len(task_problems("set", failed=[], refused=[("ag-1", "x")], tampered=["gr_x"],
                                 failures=("f",))) == 3
        assert task_problems("set", failed=[], refused=[], tampered=[], failures=()) == []


class TestRecordOnce:
    def test_should_record_only_on_change(self):
        assert should_record(None, {"a": 1}) is True
        assert should_record({"a": 1}, {"a": 1}) is False
        assert should_record({"a": 1}, {"a": 2}) is True

    def test_scope_record_clean_run_writes_nothing(self):
        assert scope_record((), None) is None
        assert scope_record((), {"failures": []}) is None

    def test_scope_record_failure_once(self):
        first = scope_record(("x",), None)
        assert first == {"failures": ["x"]}
        assert scope_record(("x",), first) is None

    def test_scope_record_clean_after_failure_once(self):
        assert scope_record((), {"failures": ["x"]}) == {"failures": []}
        assert scope_record((), {"failures": []}) is None


class TestDeadAgreementsNeverBlock:
    @pytest.mark.parametrize("status", ["revoked", "expired"])
    def test_audited_dead_agreement_with_no_request_is_not_refused(self, status):
        # a refused agreement is repaired by revoking it: once revoked (and audited) it must stop being refused,
        # even when its request row is missing (dev, Slice B step D)
        a = agr(status=status, version=2)
        assert verify([a], requests={}) == ([], [])

    def test_active_agreement_with_no_request_still_refused(self):
        assert refused_ids([agr()], requests={}) == {"ag-0001"}


# ---------------------------------------------------------------- second review round

from gates.consumption.grants import activations_after_apply, blocking_refusals, misfiled_grant_ids  # noqa: E402


class TestSecondReview:
    @pytest.mark.parametrize("consumer", ["account users", "users", "admins", ""])
    @pytest.mark.parametrize("status", ["revoked", "expired"])
    def test_dead_forbidden_consumer_not_refused(self, consumer, status):
        assert verify([agr(consumer=consumer, status=status, version=2)]) == ([], [])

    def test_duplicate_agreement_ids_refused_once(self):
        a, b = agr(), agr(consumer=SP_B)
        live, refused = verify([a, b], newest_audit=audited(a))
        assert live == [] and refused == [("ag-0001", "duplicate agreement_id rows")]

    def test_retired_row_keeps_what_was_granted(self):
        granted = GrantRow(policy_id="gr_ag-0001", securable=TBL, principal=SP_A, privilege="SELECT", active=True)
        hand_edited = agr(consumer=SP_B, product=TBL2)  # refused, so not live
        (c,) = sync_grant_rows([hand_edited], set(), {"gr_ag-0001": granted}, {})
        assert c.after == grant_row_dict(dataclasses.replace(granted, active=False))

    def test_revoke_task_resumes_retirements_only(self):
        row = GrantRow(policy_id="gr_ag-1", securable=TBL, principal=SP_A, privilege="SELECT", active=True)
        activation = {"gr_ag-1": ("ev-1", None, grant_row_dict(row))}
        assert resume_grant_rows([], activation, retire_only=True) == []
        assert len(resume_grant_rows([], activation)) == 1
        retire = {"gr_ag-1": ("ev-2", grant_row_dict(row), grant_row_dict(dataclasses.replace(row, active=False)))}
        assert len(resume_grant_rows([row], retire, retire_only=True)) == 1

    def test_failed_grant_does_not_activate_row(self):
        a = agr(consumer=ANALYST)
        on = RowChange(grant_policy_id(a.agreement_id), None, {"active": True}, "e1")
        assert activations_after_apply([on], [a], {obj(TBL, ANALYST, "SELECT")}, catalog=CAT) == []
        assert activations_after_apply([on], [a], {obj(CAT, ANALYST, "USE CATALOG")}, catalog=CAT) == []
        assert activations_after_apply([on], [a], set(), catalog=CAT) == [on]

    def test_other_agreements_failure_does_not_block_activation(self):
        a = agr(consumer=ANALYST)
        on = RowChange(grant_policy_id(a.agreement_id), None, {"active": True}, "e1")
        assert activations_after_apply([on], [a], {obj(TBL2, SP_B, "SELECT")}, catalog=CAT) == [on]

    def test_activation_needs_a_granting_agreement(self):
        on = RowChange("gr_unknown", None, {"active": True}, "e1")
        assert activations_after_apply([on], [], set(), catalog=CAT) == []

    def test_only_active_refusals_block(self):
        active, dead = agr("ag-1"), agr("ag-2", status="revoked", version=2)
        refused = [("ag-1", "x"), ("ag-2", "y")]
        assert blocking_refusals(refused, [active, dead]) == [("ag-1", "x")]

    def test_misfiled_gr_rows(self):
        assert misfiled_grant_ids([("gr_a", "grant"), ("gr_b", "mask"), ("mk_x", "mask")]) == ["gr_b"]


# ---------------------------------------------------------------- third review round

from gates.consumption.grants import pending_activations  # noqa: E402


class TestPendingActivation:
    def row(self, active=True) -> GrantRow:
        return GrantRow(policy_id="gr_ag-0001", securable=TBL, principal=SP_A, privilege="SELECT", active=active)

    def test_audited_unstored_activation_is_pending_not_tampered(self):
        newest = {"gr_ag-0001": ("ev-1", None, grant_row_dict(self.row()))}
        assert pending_activations([], newest) == {"gr_ag-0001"}
        assert resume_grant_rows([], newest, retire_only=True) == []  # never resumed

    def test_reactivation_pending(self):
        before, after = grant_row_dict(self.row(active=False)), grant_row_dict(self.row())
        newest = {"gr_ag-0001": ("ev-2", before, after)}
        assert pending_activations([self.row(active=False)], newest) == {"gr_ag-0001"}

    def test_retirement_and_completed_writes_not_pending(self):
        before, after = grant_row_dict(self.row()), grant_row_dict(self.row(active=False))
        assert pending_activations([self.row()], {"gr_ag-0001": ("ev", before, after)}) == set()
        assert pending_activations([self.row()], {"gr_ag-0001": ("ev", None, grant_row_dict(self.row()))}) == set()

    def test_hand_edit_not_pending(self):
        hand = dataclasses.replace(self.row(), principal=SP_B)
        newest = {"gr_ag-0001": ("ev", None, grant_row_dict(self.row()))}
        assert pending_activations([hand], newest) == set()

    def test_pending_activation_redone_only_while_agreement_grants(self):
        a = agr()
        # agreement dead: sync proposes nothing for a row that was never stored
        assert sync_grant_rows([dataclasses.replace(a, status="revoked", version=2)], set(), {}, {}) == []
        # agreement live but a scope failure: the set task writes no rows at all
        cur = dev_current()
        cur[("TABLE", TBL)] = cur[("TABLE", TBL)] + [g(SP_B, "MANAGE", "TABLE", TBL)]
        changes = sync_grant_rows([a], {a.agreement_id}, {}, {})
        assert task_actions("set", plan(want(SP_A), cur), changes, tampered=[]) == ([], [])
