"""Databricks job: raise one consumption request (specs/consumption.md Flow 1), audited.

Writes gov.access_requests with status `received`. The request_id is derived from consumer, product, intended use
and the caller's request_ref, so the same submission is idempotent and a new ref can raise a request again after a
denial. The job runs as the CI service principal, so the person raising the request is an explicit, recorded
`requester` (the owner-decision step compares against it: nobody approves their own request). The requester is
caller-asserted: the platform does not record who triggered a run (consumption.md Flow 4, known limitation).

Reusing a request_ref with any changed field, the purpose note included, is refused (Decision 5): the owner approves
the note that is stored. The request_received audit row carries the note's hash, never its text.
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

from gates.common.config import GateConfig  # noqa: E402
from gates.common.spark_io import AUDIT_MERGE, AUDIT_SCHEMA, _merge  # noqa: E402
from gates.consumption.requests import received_state, request_id_for, submission_conflicts  # noqa: E402
from gates.hydration.apply import event_id_of  # noqa: E402

FIELDS = ("consumer", "product", "intended_use", "reason", "use_mode", "delivery", "access_mode", "requester")
REQ_SCHEMA = ("request_id string, requester string, securable string, reason string, status string, "
              "consumer string, intended_use string, use_mode string, delivery string, access_mode string")
REQ_MERGE = """
    MERGE INTO {t} t USING {s} s ON t.request_id = s.request_id
    WHEN NOT MATCHED THEN INSERT (request_id, requester, securable, reason, status, approver, requested_at,
      decided_at, consumer, intended_use, use_mode, delivery, access_mode)
      VALUES (s.request_id, s.requester, s.securable, s.reason, s.status, NULL, current_timestamp(), NULL,
      s.consumer, s.intended_use, s.use_mode, s.delivery, s.access_mode)"""


def _args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    GateConfig.add_args(p)
    for f in FIELDS:
        p.add_argument(f"--{f.replace('_', '-')}", default="")
    p.add_argument("--request-ref", required=True, help="the consumer's idempotency key for this request")
    return p.parse_args()


def main() -> None:
    a = _args()
    cfg = GateConfig.from_args(a)
    from pyspark.sql import SparkSession

    spark = SparkSession.builder.getOrCreate()
    actor = spark.sql("SELECT session_user()").collect()[0][0]
    v = {f: getattr(a, f).strip() for f in FIELDS}
    ref = a.request_ref.strip()
    if not ref:  # the job always passes the parameter, so argparse's required=True can't catch an empty one
        raise SystemExit("request_ref is required: it lets a request be raised again after a denial")
    rid = request_id_for(v["consumer"], v["product"], v["intended_use"], ref)
    found = spark.sql(f"""SELECT status, requester, use_mode, delivery, access_mode, reason
                          FROM {cfg.gov('access_requests')} WHERE request_id = :r""", args={"r": rid}).collect()
    if found:
        stored = found[0]
        differs = submission_conflicts(stored.asDict(), v)
        if differs:
            raise SystemExit(f"request_ref {ref!r} was already used for request {rid} with different {differs}")
        print("SUBMIT_RESULT " + json.dumps({"request_id": rid, "created": False, "status": stored.status}))
        return
    after = received_state(v, ref)
    _merge(spark, [(event_id_of(rid, "request_received"), "consumption", "request_received", actor, rid, None,
                    json.dumps(after, sort_keys=True))], AUDIT_SCHEMA, cfg.gov("audit_log"), AUDIT_MERGE)
    _merge(spark, [(rid, v["requester"], v["product"], v["reason"] or None, "received", v["consumer"],
                    v["intended_use"], v["use_mode"], v["delivery"], v["access_mode"])],
           REQ_SCHEMA, cfg.gov("access_requests"), REQ_MERGE)
    print("SUBMIT_RESULT " + json.dumps({"request_id": rid, "created": True, "status": "received"}))


if __name__ == "__main__":
    main()
