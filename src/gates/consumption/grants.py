"""Grants generator planner (specs/consumption.md: Policy model `grant` rows; Decisions (8 Nov) item 3). Pure.

Agreements say whether. An agreement is trusted only when its stored row equals its newest audited state
(`agreement_registered` / `agreement_status_set`) and its request names the same consumer and product: a row
written by hand is refused, so it never produces a grant. A live agreement produces one audited `grant` row in
gov.access_policies and USE CATALOG / USE SCHEMA / SELECT on the agreed product, nothing wider.

Drift: in the managed scope (the catalog, the `grant_schemas` schemas and their tables), a direct SELECT or USE
SCHEMA no live agreement backs is revoked. USE CATALOG is only ever added. ALL PRIVILEGES, MANAGE or ownership in
scope by anyone but the exempt principals (CI SP, owner_exception) is a failure, never a revoke. Revokes are
ordered before grants and planned even when there are failures, so a crash or a failure never widens access.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields, replace
from datetime import UTC, datetime

from gates.consumption.row_filter import Action
from gates.hydration.apply import Approved, _needs_stamp, event_id_of, quote_ident, quote_table

GRANT_PREFIX = "gr_"
SWEEP = {"reason": "valid_to passed", "triggered_by": "sweep"}
_TIMES = ("valid_from", "valid_to")
_WIDE = ("ALL PRIVILEGES", "MANAGE")
_REVOKE_ORDER = (("TABLE", "SELECT"), ("SCHEMA", "SELECT"), ("SCHEMA", "USE SCHEMA"), ("CATALOG", "SELECT"))
_GRANT_ORDER = (("CATALOG", "USE CATALOG"), ("SCHEMA", "USE SCHEMA"), ("TABLE", "SELECT"))
_ON = {"CATALOG": "CATALOG", "SCHEMA": "SCHEMA", "TABLE": "TABLE"}
_FORBIDDEN_CONSUMERS = {"", "account users", "users", "admins"}  # everyone, or a workspace-local group

Want = tuple[str, str, str, str]  # (object_type, object_key lower, principal normalised, privilege)


@dataclass(frozen=True)
class Agreement:
    agreement_id: str
    request_id: str
    consumer: str
    product: str
    use_code: str
    access_mode: str
    status: str
    valid_from: datetime
    valid_to: datetime
    version: int


@dataclass(frozen=True)
class StatusChange:
    agreement_id: str
    before: dict
    after: dict
    event_id: str
    meta: dict


@dataclass(frozen=True)
class GrantRow:
    policy_id: str
    securable: str
    principal: str
    privilege: str
    active: bool


@dataclass(frozen=True)
class RowChange:
    policy_id: str
    before: dict | None
    after: dict
    event_id: str


@dataclass(frozen=True)
class Grant:
    principal: str
    action_type: str
    object_type: str
    object_key: str


@dataclass(frozen=True)
class GrantPlan:
    revokes: list[Action]
    grants: list[Action]
    failures: tuple[str, ...]


# ---------------------------------------------------------------- agreements

def _utc(v) -> datetime:
    if isinstance(v, str):
        v = datetime.fromisoformat(v.replace("Z", "+00:00"))
    return v.replace(tzinfo=UTC) if v.tzinfo is None else v.astimezone(UTC)


def agreement_from(row: dict) -> Agreement:
    return Agreement(**{f.name: row[f.name] for f in fields(Agreement)})


def agreement_state(a: Agreement) -> dict:
    d = asdict(a)
    for k in _TIMES:
        d[k] = _utc(d[k]).isoformat()
    d["version"] = int(d["version"])
    return d


def _canon(audited: dict) -> dict | None:
    try:
        return agreement_state(agreement_from(audited))
    except (KeyError, TypeError, ValueError):
        return None


def _refusal(a: Agreement, audited: dict | None, request: dict | None) -> str | None:
    if not audited:
        return "no audited state for this agreement"
    if _canon(audited) != agreement_state(a):
        return "stored agreement differs from its newest audited state"
    if a.status != "active":  # matches its audit and can never grant: dead, not refused (revoking repairs a bad one)
        return None
    if normalise_principal(a.consumer) in _FORBIDDEN_CONSUMERS:
        return f"consumer {a.consumer!r} may never be granted (everyone, empty or a workspace group)"
    if request is None:
        return f"request {a.request_id!r} not found"
    if (request.get("consumer") or "") != a.consumer:
        return "request consumer differs from the agreement"
    if (request.get("securable") or "").lower() != a.product.lower():
        return "request product differs from the agreement"
    return None


def verify_agreements(agreements: list[Agreement], *, newest_audit: dict[str, dict | None],
                      evidence_steps: dict[str, set[str]], requests: dict[str, dict],
                      now: datetime) -> tuple[list[Agreement], list[tuple[str, str]]]:
    now = _utc(now)
    live: list[Agreement] = []
    refused: list[tuple[str, str]] = []
    counts: dict[str, int] = {}
    for a in agreements:
        counts[a.agreement_id] = counts.get(a.agreement_id, 0) + 1
    for a in sorted(agreements, key=lambda x: x.agreement_id):
        if counts[a.agreement_id] > 1:
            if (a.agreement_id, "duplicate agreement_id rows") not in refused:
                refused.append((a.agreement_id, "duplicate agreement_id rows"))
            continue
        why = _refusal(a, newest_audit.get(a.agreement_id), requests.get(a.request_id))
        if why:
            refused.append((a.agreement_id, why))
        elif (a.status == "active" and _utc(a.valid_to) > now and a.access_mode == "system"
              and "agreement" in evidence_steps.get(a.request_id, set())):
            live.append(a)
    return live, refused


def _change(a: Agreement, status: str, meta: dict, last_event_id: str | None) -> StatusChange:
    before = agreement_state(a)
    after = agreement_state(replace(a, status=status, version=int(a.version) + 1))
    eid = event_id_of("agreement_status_set", a.agreement_id, before, after, meta, last_event_id)
    return StatusChange(a.agreement_id, before, after, eid, dict(meta))


def expiry_sweep(agreements: list[Agreement], now: datetime, *, last_events: dict[str, str]) -> list[StatusChange]:
    """Only pass verified agreements: sweeping a hand-written row would launder it into an audited state."""
    now = _utc(now)
    return [_change(a, "expired", SWEEP, last_events.get(a.agreement_id))
            for a in sorted(agreements, key=lambda x: x.agreement_id)
            if a.status == "active" and _utc(a.valid_to) <= now]


def status_change(a: Agreement | None, new_status: str, *, reason: str, triggered_by: str,
                  last_event_id: str | None) -> StatusChange:
    if a is None:
        raise ValueError("unknown agreement_id")
    if new_status != "revoked":
        raise ValueError(f"a person may only set 'revoked' (got {new_status!r}); expiry is the generator's sweep")
    if a.status != "active":
        raise ValueError(f"agreement {a.agreement_id!r} is {a.status!r}; status never returns or changes again")
    if not (reason or "").strip():
        raise ValueError("a reason is required")
    if not (triggered_by or "").strip():
        raise ValueError("the person triggering the change is required")
    return _change(a, "revoked", {"reason": reason.strip(), "triggered_by": triggered_by.strip()}, last_event_id)


# ---------------------------------------------------------------- grant rows

def grant_policy_id(agreement_id: str) -> str:
    return GRANT_PREFIX + agreement_id


def grant_row_dict(r: GrantRow) -> dict:
    return asdict(r)


def sync_grant_rows(agreements: list[Agreement], live_ids: set[str], existing: dict[str, GrantRow],
                    last_events: dict[str, str]) -> list[RowChange]:
    """One row per agreement that is live or already has a row. Rows are never deleted: dead -> active False,
    including a row whose agreement is gone."""
    wanted: dict[str, GrantRow] = {}
    for a in agreements:
        pid = grant_policy_id(a.agreement_id)
        if a.agreement_id in live_ids:
            wanted[pid] = GrantRow(pid, a.product, a.consumer, "SELECT", True)
        elif pid in existing:  # retire what was granted, never values from a possibly hand-edited agreement
            wanted[pid] = replace(existing[pid], active=False)
    for pid, row in existing.items():
        wanted.setdefault(pid, replace(row, active=False))
    out = []
    for pid in sorted(wanted):
        before = grant_row_dict(existing[pid]) if pid in existing else None
        after = grant_row_dict(wanted[pid])
        if before != after:
            out.append(RowChange(pid, before, after,
                                 event_id_of("policy_row_set", pid, before, after, last_events.get(pid))))
    return out


def resume_grant_rows(rows: list[GrantRow], newest: dict[str, tuple[str, dict | None, dict | None]], *,
                      retire_only: bool = False) -> list[RowChange]:
    """Grant-row writes audited but not stored (a crash between the audit and the MERGE): the stored row (or its
    absence) still equals the newest audit's `before`. Complete them with the audited `after`; never a new audit."""
    stored = {r.policy_id: grant_row_dict(r) for r in rows}
    out = []
    for pid in sorted(newest):
        eid, before, after = newest[pid]
        if not pid.startswith(GRANT_PREFIX) or not after or (retire_only and after.get("active")):
            continue
        have = stored.get(pid)
        if have != after and have == before:
            out.append(RowChange(pid, before, after, eid))
    return out


def pending_activations(rows: list[GrantRow], newest: dict[str, tuple[str, dict | None, dict | None]]) -> set[str]:
    """Activations audited but not stored (a crash in the set task's row write): not tampering. They are never
    resumed; the set task's normal sync redoes them only while the agreement still grants, reusing the audit row.
    Known gap: a hand-deleted row right after its first activation looks the same as this crash and is re-created,
    not logged as policy_refused (access is unaffected: grants come from live agreements)."""
    stored = {r.policy_id: grant_row_dict(r) for r in rows}
    return {pid for pid, (_, before, after) in newest.items()
            if pid.startswith(GRANT_PREFIX) and after and after.get("active")
            and stored.get(pid) != after and stored.get(pid) == before}


def tampered_grants(rows: list[GrantRow], newest_after: dict[str, dict | None]) -> list[str]:
    """Grant rows that differ from their newest audited write, have none, or are gone though audited."""
    bad, present = set(), set()
    for r in rows:
        present.add(r.policy_id)
        after = newest_after.get(r.policy_id)
        if not after or {k: after.get(k) for k in grant_row_dict(r)} != grant_row_dict(r):
            bad.add(r.policy_id)
    bad |= {pid for pid, after in newest_after.items()
            if after and pid.startswith(GRANT_PREFIX) and pid not in present}
    return sorted(bad)


# ---------------------------------------------------------------- wanted grants

def normalise_principal(p: str) -> str:
    s = (p or "").strip()
    if len(s) >= 2 and s[0] == "`" and s[-1] == "`":
        s = s[1:-1]
    return s.strip().lower()


def product_ready(product: str, approved: list[Approved]) -> bool:
    mine = [r for r in approved if r.table_name.lower() == product.lower()]
    return bool(mine) and not any(_needs_stamp(r) for r in mine)


def _parts(key: str) -> list[str]:
    return key.lower().split(".")


def _in_scope(otype: str, key: str, catalog: str, managed: set[str]) -> bool:
    p = _parts(key)
    if p[0] != catalog.lower():
        return False
    if otype == "CATALOG":
        return len(p) == 1
    if otype == "SCHEMA":
        return len(p) == 2 and p[1] in managed
    return otype == "TABLE" and len(p) == 3 and p[1] in managed


def grantable(live: list[Agreement], approved: list[Approved], *, catalog: str,
              managed_schemas: list[str]) -> tuple[list[Agreement], list[str]]:
    """Live agreements that actually produce a grant: a table in a managed schema, fully applied to UC."""
    managed = {s.lower() for s in managed_schemas}
    ok: list[Agreement] = []
    skipped: list[str] = []
    for a in sorted(live, key=lambda x: x.agreement_id):
        product = a.product.strip().lower()
        if not _in_scope("TABLE", product, catalog, managed):
            skipped.append(f"agreement {a.agreement_id}: {a.product} is not a table in a managed schema")
        elif not product_ready(product, approved):
            skipped.append(f"agreement {a.agreement_id}: {a.product} has unapplied or no approved metadata")
        else:
            ok.append(a)
    return ok, skipped


def principal_names(agreements: list[Agreement]) -> dict[str, str]:
    """normalised principal -> the consumer as written, so a GRANT names the principal exactly as agreed."""
    return {normalise_principal(a.consumer): a.consumer.strip().strip("`")
            for a in sorted(agreements, key=lambda x: x.agreement_id)}


def wanted_grants(live: list[Agreement], approved: list[Approved], *, catalog: str,
                  managed_schemas: list[str]) -> tuple[set[Want], list[str]]:
    ok, skipped = grantable(live, approved, catalog=catalog, managed_schemas=managed_schemas)
    wanted: set[Want] = set()
    for a in ok:
        product = a.product.strip().lower()
        p = normalise_principal(a.consumer)
        wanted |= {("TABLE", product, p, "SELECT"), ("SCHEMA", product.rsplit(".", 1)[0], p, "USE SCHEMA"),
                   ("CATALOG", catalog.lower(), p, "USE CATALOG")}
    return wanted, skipped


# ---------------------------------------------------------------- plan

def _otype(t: str) -> str:
    t = (t or "").strip().upper()
    return "SCHEMA" if t == "DATABASE" else t


def _quote_on(otype: str, key: str) -> str:
    return quote_ident(key) if otype == "CATALOG" else quote_table(key)


def _obj(key: str, principal: str, privilege: str) -> str:
    return f"{key}|{principal}|{privilege}".lower()


def plan_grants(wanted: set[Want], current: dict[tuple[str, str], list[Grant]], owners: dict[tuple[str, str], str],
                *, catalog: str, managed_schemas: list[str], exempt: list[str],
                last_events: dict[str, str], names: dict[str, str] | None = None) -> GrantPlan:
    names = names or {}
    managed = {s.lower() for s in managed_schemas}
    free = {normalise_principal(e) for e in exempt if (e or "").strip()}
    held: set[Want] = set()
    revokes: dict[tuple[int, str], Action] = {}
    failures: set[str] = set()

    for (qtype, qkey), rows in current.items():
        qtype = _otype(qtype)
        if not _in_scope(qtype, qkey, catalog, managed):
            continue
        for r in rows:
            if _otype(r.object_type) != qtype or r.object_key.lower() != qkey.lower():
                continue  # inherited: seen as direct at its own level
            p, priv = normalise_principal(r.principal), r.action_type.strip().upper()
            held.add((qtype, qkey.lower(), p, priv))
            if p in free:
                continue
            if priv in _WIDE:
                failures.add(f"{r.principal} holds {priv} on {qtype} {r.object_key}: reads around the agreements")
            elif (qtype, priv) in _REVOKE_ORDER and (qtype, qkey.lower(), p, priv) not in wanted:
                o = _obj(r.object_key, p, priv)
                revokes[(_REVOKE_ORDER.index((qtype, priv)), o)] = Action(
                    "grant_revoke", o,
                    f"REVOKE {priv} ON {_ON[qtype]} {_quote_on(qtype, r.object_key)} FROM {quote_ident(r.principal)}",
                    event_id_of("grant_revoke", o, last_events.get(o)),
                    {"object_type": qtype, "object_key": r.object_key, "principal": r.principal, "privilege": priv},
                    None)

    for (otype, key), owner in owners.items():
        otype = _otype(otype)
        if owner and _in_scope(otype, key, catalog, managed) and normalise_principal(owner) not in free:
            failures.add(f"{owner} owns {otype} {key}: only the CI SP or owner_exception may own managed objects")

    grants: dict[tuple[int, str], Action] = {}
    for w in wanted:
        if w in held:
            continue
        otype, key, p, priv = w
        o = _obj(key, p, priv)
        grants[(_GRANT_ORDER.index((otype, priv)), o)] = Action(
            "grant_set", o, f"GRANT {priv} ON {_ON[otype]} {_quote_on(otype, key)} TO {quote_ident(names.get(p, p))}",
            event_id_of("grant_set", o, last_events.get(o)), None,
            {"object_type": otype, "object_key": key, "principal": p, "privilege": priv})

    return GrantPlan([revokes[k] for k in sorted(revokes)], [grants[k] for k in sorted(grants)],
                     tuple(sorted(failures)))


# ---------------------------------------------------------------- job decisions (pure; the job only does I/O)

def task_actions(mode: str, plan: GrantPlan, row_changes: list[RowChange], *,
                 tampered: list[str]) -> tuple[list[Action], list[RowChange]]:
    """revoke: every planned revoke, row retirements only. set: grants and row activations, nothing at all while a
    grant row is tampered or the scope check fails (a grant row is never active without its grant)."""
    if mode == "revoke":
        return list(plan.revokes), [c for c in row_changes if not c.after["active"]]
    if mode != "set":
        raise ValueError(f"mode {mode!r}")
    if tampered or plan.failures:
        return [], []
    return list(plan.grants), list(row_changes)


def task_problems(mode: str, *, failed: list, refused: list, tampered: list[str],
                  failures: tuple[str, ...]) -> list[str]:
    """Why the task exits non-zero. The revoke task fails only on failed statements, so a refused agreement or a
    scope failure never stops other agreements' grants; the set task fails on all of them (audited either way)."""
    out = [f"{len(failed)} grant action(s) failed: {failed}"] if failed else []
    if mode == "revoke":
        return out
    if refused:
        out.append(f"agreements refused (not as audited): {[x for x, _ in refused]}")
    if tampered:
        out.append(f"grant rows refused (not as last audited): {tampered}")
    if failures:
        out.append(f"scope check failed (grants skipped): {list(failures)}")
    return out


def should_record(last_after: dict | None, after: dict) -> bool:
    """A refusal / scope-check audit row is written only when its content changes (never one row per run)."""
    return last_after != after


def scope_record(failures: tuple[str, ...], last_after: dict | None) -> dict | None:
    """The grant_scope_check row to write: failures when they change, and one clean row after a failing one."""
    after = {"failures": list(failures)}
    if not failures and not (last_after or {}).get("failures"):
        return None
    return after if should_record(last_after, after) else None


def blocking_refusals(refused: list[tuple[str, str]], agreements: list[Agreement]) -> list[tuple[str, str]]:
    """Refusals that fail the set task: only rows stored as active could ever grant. A refused non-active row is
    audited but never blocks other agreements."""
    active = {a.agreement_id for a in agreements if a.status == "active"}
    return [r for r in refused if r[0] in active]


def misfiled_grant_ids(rows: list[tuple[str, str]]) -> list[str]:
    """(policy_id, policy_type) rows: a gr_ id stored as anything but a grant row is tampering."""
    return sorted({pid for pid, t in rows if pid.startswith(GRANT_PREFIX) and t != "grant"})


def activations_after_apply(row_changes: list[RowChange], granting: list[Agreement], failed_objects: set[str], *,
                            catalog: str) -> list[RowChange]:
    """Set task, after the GRANTs ran: a row is activated only when none of its agreement's grants failed (held or
    granted now). A crash before this write is completed by the next run's sync."""
    by_pid = {grant_policy_id(a.agreement_id): a for a in granting}
    failed = {o.lower() for o in failed_objects}
    out = []
    for c in row_changes:
        if not c.after["active"]:
            out.append(c)
            continue
        a = by_pid.get(c.policy_id)
        if a is None:
            continue
        product, p = a.product.strip().lower(), normalise_principal(a.consumer)
        mine = {_obj(product, p, "SELECT"), _obj(product.rsplit(".", 1)[0], p, "USE SCHEMA"),
                _obj(catalog, p, "USE CATALOG")}
        if not mine & failed:
            out.append(c)
    return out
