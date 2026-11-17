"""Databricks job: a person revokes one consumption agreement, audited (specs/consumption.md Decisions (8 Nov) 3).

Only `active -> revoked`; expiry is the grants generator's sweep, and status never returns to `active`. A reason and
the person triggering the change are required: the job runs as the CI service principal, so `actor` alone would not
say who asked. Audit first (`agreement_status_set`, before/after = the full agreement state, plus reason and
triggered_by), then the UPDATE, only while the stored row is still `before`. A re-run after a crash between the two
reuses the audit row and completes the UPDATE; a re-run after success is refused (already revoked). The next
`consumption_apply` run retires the grant row and revokes the access.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

try:
    _SRC = Path(__file__).resolve().parents[1]
except NameError:
    _SRC = Path(os.getcwd()).resolve().parent
sys.path.insert(0, str(_SRC))

from dataclasses import replace  # noqa: E402

from gates.common.config import GateConfig  # noqa: E402
from gates.consumption.grants import _canon, agreement_from, agreement_state, status_change  # noqa: E402
from jobs.consumption_grants import (  # noqa: E402
    _newest_agreement_audit,
    resume_status_writes,
    write_status_change,
)


def _args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    GateConfig.add_args(p)
    p.add_argument("--agreement-id", required=True)
    p.add_argument("--status", default="revoked")
    p.add_argument("--reason", default="")
    p.add_argument("--triggered-by", default="", help="the person asking for the change (not the run principal)")
    return p.parse_args()


def main() -> None:
    a = _args()
    cfg = GateConfig.from_args(a)
    from pyspark.sql import SparkSession

    spark = SparkSession.builder.getOrCreate()
    actor = spark.sql("SELECT session_user()").collect()[0][0]
    aid = a.agreement_id.strip()

    def load():
        rows = spark.sql(f"SELECT * FROM {cfg.gov('consumption_agreements')} WHERE agreement_id = :id",
                         args={"id": aid}).collect()
        if len(rows) > 1:
            raise SystemExit(f"{len(rows)} rows for agreement {aid!r}; fix by hand before writing")
        return agreement_from(rows[0].asDict()) if rows else None

    agreement = load()
    newest = _newest_agreement_audit(spark, cfg)
    if agreement is not None and resume_status_writes(spark, cfg, [agreement], newest):
        done = load()
        print("AGREEMENT_STATUS_RESULT " + json.dumps({"agreement_id": aid, "resumed": True,
                                                       "state": agreement_state(done)}, sort_keys=True))
        return
    nw = newest.get(aid)
    state = agreement_state(agreement) if agreement is not None else None
    if (state is not None and nw and nw["action"] == "agreement_status_set"
            and _canon(nw["after"]) != state and nw["before"] != state):
        raise SystemExit(f"refused: agreement {aid!r} has an audited status change that is not stored and cannot be "
                         "resumed (a concurrent change won); repair by hand")
    try:
        change = status_change(agreement, a.status.strip(), reason=a.reason, triggered_by=a.triggered_by,
                               last_event_id=(newest.get(aid) or {}).get("event_id"))
    except ValueError as e:
        raise SystemExit(f"refused: {e}") from e
    # a legacy or hand-edited row can still be revoked (the safe direction); the audit says it wasn't verified
    verified = bool(nw) and _canon(nw["after"]) == agreement_state(agreement)
    change = replace(change, meta={**change.meta, "before_verified": verified})
    write_status_change(spark, cfg, change, actor, newest.get(aid))
    print("AGREEMENT_STATUS_RESULT " + json.dumps({"agreement_id": aid, "changed": True, "before": change.before,
                                                   "after": change.after, "meta": change.meta}, sort_keys=True))


if __name__ == "__main__":
    main()
