"""Databricks job: score one hydration eval run against gov.golden_set (specs/eval.md).

Reads only the proposals written under --run-id (the eval hydrates every golden-set table in force mode under one
shared run_id), computes the spec metrics plus the extra_* metrics, logs one MLflow run to the experiment named by
config, and upserts one summary row into gov.eval_runs. Re-running for the same run_id replaces the eval_runs row
and logs into the same MLflow run.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import re
import sys
from pathlib import Path

try:
    _SRC = Path(__file__).resolve().parents[1]
except NameError:
    _SRC = Path(os.getcwd()).resolve().parent
sys.path.insert(0, str(_SRC))

from gates.common.config import GateConfig  # noqa: E402
from gates.eval.metrics import GoldenRow, ProposalRow, evaluate, per_column  # noqa: E402
from gates.hydration.approve import AUTO_ACCEPT_CONFIDENCE  # noqa: E402

RUN_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
SPEC_METRICS = ("term_precision", "coverage", "no_match_accuracy", "pii_recall", "fuzzy_share", "tokens_per_dataset")


def _args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    GateConfig.add_args(p)
    p.add_argument("--run-id", required=True, help="the shared run_id the eval hydration wrote under")
    p.add_argument("--fuzzy-accept", type=float, required=True)
    p.add_argument("--threshold", type=float, default=AUTO_ACCEPT_CONFIDENCE, help="approve auto-rule confidence")
    p.add_argument("--mlflow-experiment", required=True, help="workspace path of the MLflow experiment")
    return p.parse_args()


def _experiment_path(path: str) -> str:
    """MLflow wants /Users/... not /Workspace/Users/...; make sure the parent folder exists.

    The bundle normally owns the experiment (resources/eval_experiment.yml); then nothing is created, because the
    job's principal may only read the folder it sits in.
    """
    path = path.removeprefix("/Workspace")
    import mlflow

    if mlflow.get_experiment_by_name(path) is not None:
        return path
    from databricks.sdk import WorkspaceClient

    WorkspaceClient().workspace.mkdirs(path.rsplit("/", 1)[0])
    return path


def _one(values: set, name: str) -> str | None:
    values = {v for v in values if v}
    if len(values) > 1:
        raise SystemExit(f"run mixes {name}s {sorted(values)}; one eval run is one (prompt_version, model)")
    return next(iter(values), None)


def _csv(rows: list[dict]) -> str:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=list(rows[0]))
    w.writeheader()
    w.writerows(rows)
    return buf.getvalue()


ENV_PACKAGES = ("mlflow", "mlflow-skinny", "databricks-sdk", "numpy", "pandas", "pyarrow", "pydantic", "pyyaml",
                "rapidfuzz")


def _env_versions() -> dict[str, str | None]:
    """Installed versions in this job's environment (Q8 evidence: the lock pins direct dependencies only)."""
    from importlib.metadata import PackageNotFoundError, version

    out: dict[str, str | None] = {}
    for name in ENV_PACKAGES:
        try:
            out[name] = version(name)
        except PackageNotFoundError:
            out[name] = None
    return out


def main() -> None:
    print("ENV_VERSIONS " + json.dumps(_env_versions(), sort_keys=True))
    a = _args()
    cfg = GateConfig.from_args(a)
    if not RUN_ID.fullmatch(a.run_id):
        raise SystemExit(f"--run-id must match {RUN_ID.pattern}")
    if a.fuzzy_accept != int(a.fuzzy_accept):
        raise SystemExit("--fuzzy-accept must be a whole number (gov.eval_runs.fuzzy_accept is INT)")
    from pyspark.sql import SparkSession

    spark = SparkSession.builder.getOrCreate()
    golden = [GoldenRow(r.table_name, r.column_name, r.expected_term_id, r.expected_sensitivity, r.difficulty)
              for r in spark.table(cfg.gov("golden_set")).collect()]
    rows = spark.sql(f"""
        -- table_name stays fully qualified, as in gov.golden_set. A rejected placeholder is the only row with
        -- no sensitivity and zero confidence (the floor always yields a class for a valid answer).
        SELECT table_name, column_name, term_id, sensitivity, confidence, method, tokens,
               (sensitivity IS NULL AND coalesce(confidence, 0) = 0) AS rejected, model, prompt_version
        FROM {cfg.gov('term_proposals')} WHERE run_id = :r
    """, args={"r": a.run_id}).collect()
    if not rows:
        raise SystemExit(f"no proposals under run_id {a.run_id}; run the eval hydration first")
    proposals = [ProposalRow(r.table_name, r.column_name, r.term_id, r.sensitivity, float(r.confidence or 0),
                             r.method, int(r.tokens or 0), bool(r.rejected)) for r in rows]
    prompt_version = _one({r.prompt_version for r in rows}, "prompt_version")
    model = _one({r.model for r in rows}, "model")

    metrics = evaluate(golden, proposals, threshold=a.threshold)
    if metrics["extra_missing_count"] == len(golden):
        raise SystemExit(f"no proposal under run_id {a.run_id} joined the golden set; table names disagree?")
    detail = per_column(golden, proposals)

    import mlflow

    mlflow.set_experiment(_experiment_path(a.mlflow_experiment))
    existing = mlflow.search_runs(filter_string=f"tags.eval_run_id = '{a.run_id}'", output_format="list")
    with mlflow.start_run(run_id=existing[0].info.run_id if existing else None, run_name=a.run_id):
        mlflow.set_tag("eval_run_id", a.run_id)
        mlflow.log_params({"prompt_version": prompt_version, "model": model, "fuzzy_accept": a.fuzzy_accept,
                           "threshold": a.threshold})
        mlflow.log_metrics({k: float(v) for k, v in metrics.items() if v is not None})
        mlflow.log_text(_csv(detail), "per_column.csv")

    from pyspark.sql import functions as F

    eval_t = cfg.gov("eval_runs")
    record = {"run_id": a.run_id, "prompt_version": prompt_version, "model": model,
              "fuzzy_accept": int(a.fuzzy_accept),
              **{k: metrics.get(k) for k in SPEC_METRICS},
              **{k: float(v) if v is not None else None for k, v in metrics.items() if k.startswith("extra_")}}
    schema = ", ".join(["run_id string", "prompt_version string", "model string", "fuzzy_accept int"]
                       + [f"{k} double" for k in list(record)[4:]])
    (spark.createDataFrame([tuple(record.values())], schema).withColumn("run_at", F.current_timestamp())
        .createOrReplaceTempView("eval_run_row"))
    # one row per run_id; a re-score replaces it atomically (columns come from sql/ddl, never evolved at runtime)
    spark.sql(f"""
        MERGE INTO {eval_t} t USING eval_run_row s ON t.run_id = s.run_id
        WHEN MATCHED THEN UPDATE SET * WHEN NOT MATCHED THEN INSERT *
    """)
    print("EVAL_RESULT " + json.dumps({"run_id": a.run_id, "prompt_version": prompt_version, "model": model,
                                       **metrics}, sort_keys=True, default=str))


if __name__ == "__main__":
    main()
