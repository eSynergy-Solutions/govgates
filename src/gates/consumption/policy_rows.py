"""Writer side of gov.access_policies (specs/consumption.md: the audited policy-row writer). Pure: no Spark.

A mask / row_filter row is only ever written through this path, by the access-model adapter (task
access_model_sync of job consumption_apply): MERGE plus a `policy_row_set` audit row with before/after. Rows are
never deleted; `active = false` retires one. The stored set is validated as a whole, so a write that would leave
the generator unable to plan (e.g. a second active row on one schema) is refused. A row's securable never changes:
moving a filter means a new policy_id and retiring the old one.
"""

from __future__ import annotations

from dataclasses import asdict

from gates.consumption.row_filter import PolicyRow, validate_rows
from gates.hydration.apply import event_id_of


def row_dict(r: PolicyRow) -> dict:
    d = asdict(r)
    d["except_principals"] = sorted(set(r.except_principals))
    return d


RESERVED_ADAPTER = "am_"
# Spark-side shapes for gov.access_policies writes (strings only; the jobs run them)
ROW_SCHEMA = ("policy_id string, policy_type string, securable string, principal string, privilege string, "
              "match_tag_key string, match_tag_value string, function_name string, except_principals array<string>, "
              "active boolean")
ROW_MERGE = """
    MERGE INTO {t} t USING {s} s ON t.policy_id = s.policy_id
    WHEN MATCHED THEN UPDATE SET policy_type = s.policy_type, securable = s.securable, principal = s.principal,
      privilege = s.privilege, match_tag_key = s.match_tag_key, match_tag_value = s.match_tag_value,
      function_name = s.function_name, except_principals = s.except_principals, active = s.active
    WHEN NOT MATCHED THEN INSERT *"""


def _normalise(stored: dict) -> dict:
    d = dict(stored)
    d["except_principals"] = sorted(set(d.get("except_principals") or ()))
    d["active"] = bool(d.get("active"))
    return d


def _as_row(d: dict) -> PolicyRow:
    return PolicyRow(d["policy_id"], d["policy_type"], d.get("securable") or "", d.get("match_tag_key") or "",
                     d.get("match_tag_value") or "", d.get("function_name") or "",
                     tuple(d.get("except_principals") or ()), bool(d.get("active")))


def rows_change(stored: list[dict], new: list[PolicyRow], *, last_event_ids: dict[str, str], catalog: str,
                rls_tag: str, sensitivity_tag: str, hydration_principal: str, gov_schema: str,
                allow_reserved: bool = False, reassert: frozenset[str] = frozenset()
                ) -> list[tuple[dict | None, dict, str]]:
    """[(before, after, event_id)] for the rows in `new` that change the stored set; [] when nothing changes.

    `stored` is every stored row_filter and mask row. The end state (stored overlaid with `new` by policy_id) is
    validated once as a whole, so a batch that swaps rows in one write is judged on where it ends, not on an
    intermediate state. `last_event_ids` holds the newest policy_row_set event per policy_id, so returning to an
    earlier state is still a new, audited event. Rows in `reassert` are audited even when unchanged (their stored
    row differs from their newest audit). Raises ValueError on any invalid row or end state.
    """
    from gates.consumption.masks import function_clashes, validate_mask_rows  # masks imports row_dict from here

    errors: list[str] = []
    by_id = {str(d["policy_id"]): _normalise(d) for d in stored}
    ids = [r.policy_id for r in new]
    if len(set(ids)) != len(ids):
        errors.append(f"a policy_id appears more than once in one write: {sorted(ids)}")
    for r in new:
        if r.policy_id.startswith("gr_"):
            errors.append(f"policy_id {r.policy_id!r}: the gr_ prefix is reserved for the grants generator")
        if r.policy_id.startswith(RESERVED_ADAPTER) and not allow_reserved:
            errors.append(f"policy_id {r.policy_id!r}: the {RESERVED_ADAPTER} prefix is reserved for the "
                          "access-model adapter")
        if r.policy_type not in ("row_filter", "mask"):
            errors.append(f"policy_type {r.policy_type!r} is not written by this writer")
        before = by_id.get(r.policy_id)
        if before is not None and str(before.get("securable") or "").lower() != r.securable.lower():
            errors.append(f"policy_id {r.policy_id!r} is on {before.get('securable')!r}; a row never moves "
                          "schema: retire it and write a new policy_id")
    end = {pid: _as_row(d) for pid, d in by_id.items()}
    end.update({r.policy_id: r for r in new})
    rows = list(end.values())
    errors += validate_rows(rows, catalog, rls_tag, hydration_principal, gov_schema)
    errors += validate_mask_rows(rows, catalog=catalog, sensitivity_tag=sensitivity_tag,
                                 hydration_principal=hydration_principal, gov_schema=gov_schema)
    errors += function_clashes(rows)
    if errors:
        raise ValueError("; ".join(errors))
    changes = []
    for r in new:
        after = row_dict(r)
        before = by_id.get(r.policy_id)
        if before == after and r.policy_id not in reassert:
            continue
        changes.append((before, after, event_id_of("policy_row_set", r.policy_id, before, after,
                                                   last_event_ids.get(r.policy_id))))
    return changes


def row_change(existing: dict | None, new: PolicyRow, *, others: list[PolicyRow], last_event_id: str | None,
               catalog: str, rls_tag: str, sensitivity_tag: str, hydration_principal: str,
               gov_schema: str) -> tuple[dict | None, dict, str] | None:
    """One-row form of rows_change: (before, after, event_id) when the stored row must change; None when equal.

    `others` are all other stored row_filter and mask rows. The `am_` prefix is refused here: adapter rows are
    written only by the adapter, as a batch.
    """
    stored = [row_dict(o) for o in others if o.policy_id != new.policy_id]
    if existing is not None:
        stored.append(dict(existing, policy_id=new.policy_id, policy_type=existing.get("policy_type", new.policy_type)))
    out = rows_change(stored, [new], last_event_ids={new.policy_id: last_event_id} if last_event_id else {},
                      catalog=catalog, rls_tag=rls_tag, sensitivity_tag=sensitivity_tag,
                      hydration_principal=hydration_principal, gov_schema=gov_schema)
    return out[0] if out else None
