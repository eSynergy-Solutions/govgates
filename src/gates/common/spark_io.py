"""Spark helpers kept thin: turn column dicts into DataFrames with an explicit schema."""

from __future__ import annotations

import json
import uuid
from datetime import date, datetime
from typing import Any


def _spark_type(values: list[Any]):
    from pyspark.sql import types as T

    sample = next((v for v in values if v is not None), None)
    if isinstance(sample, bool):
        return T.BooleanType()
    if isinstance(sample, int):
        return T.LongType()
    if isinstance(sample, float):
        return T.DoubleType()
    if isinstance(sample, datetime):
        return T.TimestampType()
    if isinstance(sample, date):
        return T.DateType()
    if isinstance(sample, list):
        return T.ArrayType(T.StringType())
    return T.StringType()


def columns_to_df(spark, cols: dict[str, list[Any]]):
    from pyspark.sql import types as T

    schema = T.StructType([T.StructField(name, _spark_type(vals), True) for name, vals in cols.items()])
    rows = list(zip(*cols.values()))
    return spark.createDataFrame(rows, schema=schema)


def records_to_df(spark, records: list[dict]):
    keys = list(records[0])
    return columns_to_df(spark, {k: [r[k] for r in records] for k in keys})


def write_audit(spark, audit_table: str, gate: str, action: str, obj: str,
                before: dict | None = None, after: dict | None = None) -> None:
    from pyspark.sql import functions as F

    df = spark.createDataFrame(
        [(str(uuid.uuid4()), gate, action, obj,
          json.dumps(before, default=str) if before else None,
          json.dumps(after, default=str) if after else None)],
        "event_id string, gate string, action string, object string, before_json string, after_json string",
    )
    df = df.withColumn("actor", F.expr("current_user()")).withColumn("event_at", F.current_timestamp())
    df.select("event_id", "gate", "action", "actor", "object", "before_json", "after_json", "event_at") \
      .write.mode("append").saveAsTable(audit_table)


def _iso(ts) -> str | None:
    return ts.isoformat() if ts is not None else None


def _merge(spark, rows: list[tuple], schema: str, target: str, sql: str) -> None:
    if not rows:
        return
    view = f"src_{uuid.uuid4().hex}"
    spark.createDataFrame(rows, schema).createOrReplaceTempView(view)
    spark.sql(sql.format(t=target, s=view))


AUDIT_SCHEMA = ("event_id string, gate string, action string, actor string, object string, before_json string, "
                "after_json string")
AUDIT_MERGE = """
    MERGE INTO {t} t USING {s} s ON t.event_id = s.event_id
    WHEN NOT MATCHED THEN INSERT (event_id, gate, action, actor, object, before_json, after_json, event_at)
      VALUES (s.event_id, s.gate, s.action, s.actor, s.object, s.before_json, s.after_json, current_timestamp())"""
