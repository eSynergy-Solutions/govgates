"""Databricks job: create the catalog (if allowed), run every file in sql/ddl, then sql/views. Idempotent."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

try:
    _SRC = Path(__file__).resolve().parents[1]
except NameError:  # some notebook-style runners don't set __file__
    _SRC = Path(os.getcwd()).resolve().parent
sys.path.insert(0, str(_SRC))

from gates.common.config import GateConfig  # noqa: E402
from gates.common.sql_files import add_columns, ddl_then_views, missing_columns_statement  # noqa: E402


def main() -> None:
    p = argparse.ArgumentParser()
    GateConfig.add_args(p)
    p.add_argument("--ddl-dir", required=True)
    p.add_argument("--views-dir", default=None, help="run sql/views/*.sql after the DDL (CREATE OR REPLACE VIEW)")
    a = p.parse_args()
    cfg = GateConfig.from_args(a)

    from pyspark.sql import SparkSession

    spark = SparkSession.builder.getOrCreate()

    try:
        spark.sql(f"CREATE CATALOG IF NOT EXISTS {cfg.catalog}")
    except Exception as e:  # e.g. no CREATE CATALOG privilege; fine if it already exists
        print(f"CREATE CATALOG skipped: {e}")
    spark.sql(f"DESCRIBE CATALOG {cfg.catalog}").collect()  # fails loudly if the catalog really is missing

    stmts = ddl_then_views(a.ddl_dir, a.views_dir, cfg.substitutions())
    for fname, stmt in stmts:
        parsed = add_columns(stmt)
        if parsed is not None:  # no ADD COLUMN IF NOT EXISTS in SQL: add only the columns that are missing
            cat, sch, tbl = (p.strip("`").lower() for p in parsed[0].split("."))
            existing = {r[0] for r in spark.sql(
                f"SELECT column_name FROM `{cat}`.information_schema.columns "
                "WHERE table_schema = :s AND table_name = :t", args={"s": sch, "t": tbl}).collect()}
            stmt = missing_columns_statement(stmt, existing)
            if stmt is None:
                print(f"[{fname}] ADD COLUMNS on {parsed[0]}: all present, skipped")
                continue
        first = stmt.splitlines()[0][:100]
        print(f"[{fname}] {first}")
        spark.sql(stmt)
    print(f"Ran {len(stmts)} DDL + view statements against catalog {cfg.catalog}")


if __name__ == "__main__":
    main()
