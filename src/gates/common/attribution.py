"""Who committed an owner-input row: change data feed (_commit_version) joined to DESCRIBE HISTORY (userName,
timestamp). Shared by both gates' owner inputs (review_decisions, request_decisions, permitted_use_inputs), so the
identity rules are the same everywhere. Spark edge only; the rules are pure functions in gates.common.cdf.

Result per key: committed_by (None = unknown, refused by the planners), committed_at (commit timestamp, the only
trusted time), insert_version, attribution_error (row changed after its insert, or stale). If the feed cannot be read
every row is held: it stays pending and is not used up.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from gates.common.cdf import (
    cdf_start_version,
    commit_times,
    committers,
    insert_versions,
    modified_after_insert,
)

# Change files are vacuumed after delta.deletedFileRetentionDuration (7 days by default): read inside that.
CDF_WINDOW = timedelta(days=6)


@dataclass(frozen=True)
class Attribution:
    committed_by: str | None = None
    committed_at: datetime | None = None
    insert_version: int | None = None
    attribution_error: str | None = None
    hold: bool = False


def attribute(spark, table: str, key_col: str, keys: list[str]) -> dict[str, Attribution]:
    """key -> Attribution for every key asked for (a key with no insert in the window gets committed_by None)."""
    if not keys:
        return {}
    try:
        hist = spark.sql(f"DESCRIBE HISTORY {table}").collect()
        now = spark.sql("SELECT current_timestamp()").collect()[0][0]  # same clock and zone as the history
        props = {r.key: r.value for r in spark.sql(f"SHOW TBLPROPERTIES {table}").collect()}
        enabled_now = str(props.get("delta.enableChangeDataFeed", "")).strip().lower() == "true"
        start = cdf_start_version([(h.version, h.timestamp, h.operation, dict(h.operationParameters or {}))
                                   for h in hist], now, CDF_WINDOW, enabled_now=enabled_now)
        if start is None:
            if enabled_now and not any(h.timestamp >= now - CDF_WINDOW for h in hist):
                stale = (f"stale: no change data in the last {CDF_WINDOW.days} days; insert the row again")
                return {k: Attribution(attribution_error=stale) for k in keys}
            print(f"WARN change data feed is off or its start is unknown on {table}: rows held")
            return {k: Attribution(hold=True) for k in keys}
        changes = [(r[0], r[1], r[2]) for r in spark.sql(
            f"SELECT {key_col}, _commit_version, _change_type FROM table_changes('{table}', {int(start)})").collect()]
    except Exception as e:  # noqa: BLE001 - attribution failure holds the rows, never decides them
        print(f"WARN change data feed unreadable for {table} ({type(e).__name__}): rows held")
        return {k: Attribution(hold=True) for k in keys}
    who = committers(changes, {h.version: h.userName for h in hist})
    when = commit_times(changes, {h.version: h.timestamp for h in hist})
    tampered = modified_after_insert(changes)
    versions = insert_versions(changes)
    reused = "modified after insert, or its id was already used: insert a new row with a new id"
    return {k: Attribution(committed_by=who.get(k), committed_at=when.get(k), insert_version=versions.get(k),
                           attribution_error=reused if k in tampered else None) for k in keys}
