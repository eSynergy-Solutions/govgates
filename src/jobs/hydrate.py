"""Databricks job: hydration gate for one table (specs/hydration.md steps 1-5).

guard -> profile -> expand + fuzzy match -> LLM calls for the rest (near-equal chunks of at most
llm_max_columns_per_call columns, default 40; Q4) -> gov.term_proposals + audit.
Re-running on an unchanged, fully covered table exits at the guard: zero LLM calls, nothing written.

Eval/force mode (--force --run-id <id>): skip the guard and propose every column of every table under one shared
run_id, so the eval (src/jobs/run_eval.py) measures a prompt or model change with real LLM calls.
--table takes a bare name, a comma-separated list, or @golden_set (every table in gov.golden_set).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import uuid
from pathlib import Path

try:
    _SRC = Path(__file__).resolve().parents[1]
except NameError:
    _SRC = Path(os.getcwd()).resolve().parent
sys.path.insert(0, str(_SRC))

from gates.common.config import GateConfig  # noqa: E402
from gates.common.llm_client import make_client  # noqa: E402
from gates.common.spark_io import write_audit  # noqa: E402
from gates.hydration.guard import decide, schema_hash  # noqa: E402
from gates.hydration.match import (  # noqa: E402,E501
    Term,
    load_abbreviations,
    load_ignore,
    load_noise,
    load_weak_noise,
    match_column,
)
from gates.hydration.profile import profile_dataframe  # noqa: E402
from gates.hydration.propose import (  # noqa: E402
    MAX_COLUMNS_PER_CALL,
    PROMPT_VERSION,
    ColumnInput,
    check_columns_per_call,
    check_prompt,
    fuzzy_proposal,
    propose_llm,
    rule_proposal,
)
from gates.hydration.records import (  # noqa: E402
    PROFILE_SCHEMA,
    PROPOSAL_SCHEMA,
    profile_rows,
    proposal_rows,
    qualify_table,
    run_summary,
)


def _args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    GateConfig.add_args(p)
    p.add_argument("--table", required=True,
                   help="bare name (raw schema), catalog.schema.table, comma-separated list, or @golden_set")
    p.add_argument("--force", action="store_true", help="eval mode: bypass the guard, propose every column")
    p.add_argument("--run-id", default="", help="run_id to write under (shared across tables in an eval run)")
    p.add_argument("--fuzzy-accept", type=float, default=90)
    p.add_argument("--prompt-version", default=PROMPT_VERSION, help="prompts/<version>.md, e.g. propose_v2")
    p.add_argument("--llm-max-columns-per-call", type=int, default=MAX_COLUMNS_PER_CALL,
                   help="unresolved columns per LLM call (Q4: wide tables are split into near-equal chunks)")
    p.add_argument("--llm-provider", default="ai_query", choices=["ai_query", "openai"])
    p.add_argument("--llm-endpoint", default="", help="serving endpoint name (ai_query)")
    p.add_argument("--llm-base-url", default="", help="OpenAI-compatible base URL (openai)")
    p.add_argument("--llm-model", default="", help="model name (openai)")
    p.add_argument("--llm-secret-scope", default="", help="secret scope holding the API key (openai)")
    p.add_argument("--llm-secret-key", default="", help="secret key name (openai)")
    return p.parse_args()


def _set(spark, sql: str, **args) -> set[str]:
    return {r[0] for r in spark.sql(sql, args=args).collect()}


def _load_terms(spark, cfg: GateConfig) -> list[Term]:
    rows = spark.sql(f"""
        SELECT term_id, name, definition, synonyms, sensitivity FROM {cfg.gov('business_terms')}
        QUALIFY row_number() OVER (PARTITION BY term_id ORDER BY version DESC NULLS LAST, loaded_at DESC) = 1
    """).collect()
    return [Term(r.term_id, r.name, r.definition or "", list(r.synonyms or []), r.sensitivity) for r in rows]


def _client(a: argparse.Namespace):
    if a.llm_provider == "openai":
        from databricks.sdk import WorkspaceClient

        key = WorkspaceClient().dbutils.secrets.get(a.llm_secret_scope, a.llm_secret_key)
        return make_client("openai", base_url=a.llm_base_url, api_key=key, model=a.llm_model)
    return make_client("ai_query", endpoint=a.llm_endpoint)


def _emit(result: dict) -> None:
    print("HYDRATE_RESULT " + json.dumps(result, sort_keys=True))


RUN_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")


def _tables(spark, cfg: GateConfig, spec: str) -> list[str]:
    if spec.strip() == "@golden_set":
        return sorted(_set(spark, f"SELECT DISTINCT table_name FROM {cfg.gov('golden_set')}"))
    return [t.strip() for t in spec.split(",") if t.strip()]


def main() -> None:
    a = _args()
    cfg = GateConfig.from_args(a)
    from pyspark.sql import SparkSession

    check_prompt(a.prompt_version)  # fail fast on a bad version or template, before any write
    check_columns_per_call(a.llm_max_columns_per_call)  # fail fast on a bad chunk size, before any write
    spark = SparkSession.builder.getOrCreate()
    a.run_id = a.run_id.strip()
    if a.force != bool(a.run_id):
        raise SystemExit("--force and --run-id go together: an eval run is scoped by one shared run_id")
    if a.run_id and not RUN_ID.fullmatch(a.run_id):
        raise SystemExit(f"--run-id must match {RUN_ID.pattern}")
    for t in _tables(spark, cfg, a.table):
        hydrate_table(spark, cfg, a, qualify_table(t, cfg))


def hydrate_table(spark, cfg: GateConfig, a: argparse.Namespace, table: str) -> None:
    from pyspark.sql import functions as F

    df = spark.table(table)
    columns = [(f.name, f.dataType.simpleString()) for f in df.schema.fields]
    h = schema_hash(columns)

    # 1. Guard
    profiles_t, proposals_t, approved_t = cfg.gov("column_profiles"), cfg.gov("term_proposals"), \
        cfg.gov("approved_metadata")
    seen = _set(spark, f"SELECT DISTINCT schema_hash FROM {profiles_t} WHERE table_name = :t", t=table)
    approved = _set(spark, f"SELECT column_name FROM {approved_t} WHERE table_name = :t", t=table)
    pending = _set(spark, f"""
        SELECT p.column_name FROM {proposals_t} p
        WHERE p.table_name = :t AND p.status = 'proposed'
          AND p.run_id IN (SELECT run_id FROM {profiles_t} WHERE table_name = :t AND schema_hash = :h)
    """, t=table, h=h)
    decision = decide(h, seen, [c for c, _ in columns], approved, pending, force=a.force)
    if decision.skip:
        _emit({"table_name": table, "schema_hash": h, "skipped": True, "reason": decision.reason, "llm_calls": 0})
        return

    run_id = a.run_id or str(uuid.uuid4())
    if a.force:  # idempotent per (run_id, table): a repaired eval run replaces, never duplicates
        for t in (profiles_t, proposals_t):
            spark.sql(f"DELETE FROM {t} WHERE run_id = :r AND table_name = :t", args={"r": run_id, "t": table})
    print(f"run {run_id}: {decision.reason}; proposing {len(decision.to_propose)} of {len(columns)} column(s)")

    # 2. Profile (every column: the profile row records the schema_hash this run saw)
    profiles = profile_dataframe(df)
    # mergeSchema: tables created before any_pattern_hits/avg_length existed gain them on first append.
    (spark.createDataFrame(profile_rows(run_id, table, h, profiles), PROFILE_SCHEMA)
        .withColumn("profiled_at", F.current_timestamp())
        .write.mode("append").option("mergeSchema", "true").saveAsTable(profiles_t))
    by_col = {p.column_name: p for p in profiles}

    # 3. Expand + fuzzy match
    terms, abbreviations, ignore = _load_terms(spark, cfg), load_abbreviations(), load_ignore()
    noise, weak = load_noise(), load_weak_noise()
    fuzzy, unresolved = [], []
    for col in decision.to_propose:
        # 3a. noise names: deterministic NO_MATCH (exceptions in rule_proposal send the column on)
        rule = rule_proposal(col, by_col[col], noise, weak)
        if rule:
            fuzzy.append(rule)
            continue
        m = match_column(col, terms, abbreviations, fuzzy_accept=a.fuzzy_accept, table=table,
                         ignore=ignore)
        if m.accepted:
            fuzzy.append(fuzzy_proposal(m, by_col[col]))
        else:
            unresolved.append(ColumnInput(col, m.expanded, by_col[col], m.candidates))

    # 4. LLM calls for everything fuzzy could not settle, at most llm_max_columns_per_call columns each
    llm = propose_llm(table, unresolved, _client(a), version=a.prompt_version,
                      table_columns=[c for c, _ in columns],
                      max_columns_per_call=a.llm_max_columns_per_call) if unresolved else None
    proposals = fuzzy + (llm.proposals if llm else [])

    # 5. Proposals + audit
    if proposals:
        rows = proposal_rows(run_id, table, proposals, status="eval" if a.force else "proposed")
        spark.createDataFrame(rows, PROPOSAL_SCHEMA) \
            .withColumn("created_at", F.current_timestamp()).write.mode("append").saveAsTable(proposals_t)
    summary = run_summary(run_id, table, h, proposals, llm_calls=llm.calls if llm else 0,
                          rejections=len(llm.rejections) if llm else 0, model=llm.model if llm else None,
                          prompt_version=a.prompt_version)
    audit_t = cfg.gov("audit_log")
    write_audit(spark, audit_t, "hydration", "proposals_written", table, after={**summary, "forced": bool(a.force)})
    if llm and llm.rejections:
        write_audit(spark, audit_t, "hydration", "llm_output_rejected", table,
                    after={"run_id": run_id, "rejections": [r.__dict__ for r in llm.rejections]})
    _emit({**summary, "skipped": False, "forced": bool(a.force)})


if __name__ == "__main__":
    main()
