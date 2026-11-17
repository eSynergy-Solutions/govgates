"""Databricks job: check the access model's groups against account membership (option B, 9 Nov).

Membership belongs to the identity provider: this job reads it and never changes it. It runs as the deploying person
(no run_as): the CI SP can't read account groups and there is no read-only group role. The result (pass/fail,
failures, warnings, the model file's sha256) is audited as `membership_check` (before = the previous result),
chained on the newest row: a same-day re-run with the same result writes nothing; any other result is a new newest
row. The access-model adapter refuses to write unless the newest result passed, is for the same model file and is
at most 26 hours old. Fails (non-zero) on a failed check: a declared group missing, or an isolated member (the
proxied persona) in any group.
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
from gates.consumption.access_model import (  # noqa: E402
    load_model,
    membership_report,
    membership_result,
    membership_write,
    model_sha,
)

ACTION = "membership_check"


def _args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    GateConfig.add_args(p)
    p.add_argument("--access-model-source", choices=["file", "api"], default="file")
    p.add_argument("--access-model-path", required=True)
    return p.parse_args()


def _isolated_groups(w, names: tuple[str, ...]) -> dict[str, set[str]]:
    """Every group each isolated principal is in (any group, not only the model's). Not found = left out (fails)."""
    out: dict[str, set[str]] = {}
    for name in names:
        found = w.api_client.do("GET", "/api/2.0/preview/scim/v2/ServicePrincipals",
                                query={"filter": f'displayName eq "{name}"'}).get("Resources") or []
        if len(found) != 1:
            continue
        sp = w.api_client.do("GET", f"/api/2.0/preview/scim/v2/ServicePrincipals/{found[0]['id']}")
        out[name] = {g.get("display") or g.get("value") for g in sp.get("groups") or []}
    return out


def _actual_membership(w, group_names: list[str]) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for name in group_names:
        found = w.api_client.do("GET", "/api/2.0/account/scim/v2/Groups",
                                query={"filter": f'displayName eq "{name}"'}).get("Resources") or []
        if len(found) != 1:
            continue
        g = w.api_client.do("GET", f"/api/2.0/account/scim/v2/Groups/{found[0]['id']}")
        out[name] = {m.get("display") for m in g.get("members") or [] if m.get("display")}
    return out


def main() -> None:
    a = _args()
    cfg = GateConfig.from_args(a)
    if a.access_model_source != "file":
        raise SystemExit("access_model_source = api is not built yet; use file")
    text = Path(a.access_model_path).read_text(encoding="utf-8")
    model = load_model(text)

    from databricks.sdk import WorkspaceClient

    w = WorkspaceClient()
    # a read error propagates before anything is written: no result recorded, so the adapter stays closed
    warnings, failures = membership_report(model, _actual_membership(w, [g.name for g in model.groups]),
                                           _isolated_groups(w, model.isolated_members))
    result = membership_result(warnings, failures, model_sha(text))

    from pyspark.sql import SparkSession

    spark = SparkSession.builder.getOrCreate()
    actor = spark.sql("SELECT session_user()").collect()[0][0]
    spark.conf.set("spark.sql.session.timeZone", "UTC")  # day boundaries are UTC, whatever the cluster's zone
    today = spark.sql("SELECT CAST(to_date(current_timestamp()) AS STRING) AS d").collect()[0].d
    rows = spark.sql(f"""
        SELECT event_id, after_json, CAST(to_date(event_at) AS STRING) AS d FROM {cfg.gov('audit_log')}
        WHERE gate = 'consumption' AND action = :a AND event_at <= current_timestamp()
        ORDER BY event_at DESC, event_id DESC LIMIT 1""", args={"a": ACTION}).collect()
    newest = (rows[0].event_id, json.loads(rows[0].after_json or "null"), rows[0].d) if rows else None
    event_id = membership_write(result, today, newest)
    if event_id is not None:
        before = json.dumps(newest[1], sort_keys=True) if newest else None
        _merge(spark, [(event_id, "consumption", ACTION, actor, a.access_model_path, before,
                        json.dumps(result, sort_keys=True))], AUDIT_SCHEMA, cfg.gov("audit_log"), AUDIT_MERGE)
    print("MEMBERSHIP_CHECK_RESULT " + json.dumps(result, sort_keys=True))
    if failures:
        raise SystemExit(f"membership check failed: {list(failures)}")


if __name__ == "__main__":
    main()
