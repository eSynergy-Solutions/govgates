"""Databricks job: load synthetic raw tables, the glossary, the golden set and persona attributes.

Raw tables, golden set and user attributes are overwritten (they are generated). The glossary is
MERGEd into gov.business_terms so terms added by hand are never lost.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime
from pathlib import Path

try:
    _SRC = Path(__file__).resolve().parents[1]
except NameError:
    _SRC = Path(os.getcwd()).resolve().parent
sys.path.insert(0, str(_SRC))

from gates.common.config import GateConfig  # noqa: E402
from gates.common.spark_io import columns_to_df, records_to_df, write_audit  # noqa: E402
from synthetic.generate_data import generate, golden_set, load_glossary  # noqa: E402


def main() -> None:
    p = argparse.ArgumentParser()
    GateConfig.add_args(p)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--scale", type=float, default=1.0)
    a = p.parse_args()
    cfg = GateConfig.from_args(a)

    from pyspark.sql import SparkSession

    spark = SparkSession.builder.getOrCreate()

    # 1. Raw tables
    data = generate(a.seed, a.scale)
    for name, cols in data.items():
        target = cfg.raw(name)
        columns_to_df(spark, cols).write.mode("overwrite").option("overwriteSchema", "true").saveAsTable(target)
        spark.sql(f"COMMENT ON TABLE {target} IS 'Synthetic data (seed {a.seed}). Not real people or policies.'")
        print(f"Loaded {target}: {len(next(iter(cols.values())))} rows")

    # 2. Glossary -> MERGE into business_terms
    glossary = load_glossary()
    now = datetime.now().replace(microsecond=0)
    terms = [{
        "term_id": t["term_id"], "name": t["name"], "definition": t["definition"],
        "synonyms": list(t.get("synonyms", [])), "sensitivity": t["sensitivity"], "domain": t["domain"],
        "version": int(glossary.get("version", 1)), "loaded_at": now,
    } for t in glossary["terms"]]
    records_to_df(spark, terms).createOrReplaceTempView("_glossary_src")
    spark.sql(f"""
        MERGE INTO {cfg.gov('business_terms')} t
        USING (SELECT term_id, name, definition, synonyms, sensitivity, domain,
                      CAST(version AS INT) AS version, loaded_at FROM _glossary_src) s
        ON t.term_id = s.term_id
        WHEN MATCHED THEN UPDATE SET *
        WHEN NOT MATCHED THEN INSERT *
    """)
    print(f"Merged {len(terms)} glossary terms into {cfg.gov('business_terms')}")

    # 3. Golden set (fully qualified table names)
    gs = [{**r, "table_name": cfg.raw(r["table_name"])} for r in golden_set(glossary)]
    records_to_df(spark, gs).write.mode("overwrite").saveAsTable(cfg.gov("golden_set"))
    print(f"Wrote {len(gs)} golden-set rows to {cfg.gov('golden_set')}")

    # 4. ref.user_attributes is not loaded here: the access-model adapter (access_model_sync) is its only writer.

    # 5. Consumption stubs, insert-only (never update; no seed for a product that already has permitted uses)
    _seed_consumption(spark, cfg, now)

    write_audit(spark, cfg.gov("audit_log"), gate="setup", action="load_synthetic", obj=cfg.catalog,
                after={"seed": a.seed, "scale": a.scale, "tables": len(data), "terms": len(terms)})
    print("Done.")


def _seed_consumption(spark, cfg: GateConfig, now) -> None:
    import json

    from gates.common.spark_io import AUDIT_MERGE, AUDIT_SCHEMA, _merge
    from gates.hydration.apply import event_id_of
    from synthetic.consumption_seed import pattern_rows, permitted_use_rows

    actor = spark.sql("SELECT session_user()").collect()[0][0]
    patterns_t, uses_t = cfg.gov("architecture_patterns"), cfg.gov("permitted_uses")
    have = {r[0] for r in spark.sql(f"SELECT pattern_id FROM {patterns_t}").collect()}
    new_patterns = [r for r in pattern_rows() if r["pattern_id"] not in have]
    product = cfg.raw("claims")
    has_uses = spark.sql(f"SELECT count(*) FROM {uses_t} WHERE product = :p", args={"p": product}).collect()[0][0]
    new_uses = [] if has_uses else permitted_use_rows(product, actor, now)
    audit = [(event_id_of("architecture_pattern_set", r["pattern_id"], r["version"]), "consumption",
              "architecture_pattern_set", actor, r["pattern_id"], None, json.dumps(r, sort_keys=True, default=str))
             for r in new_patterns]
    audit += [(event_id_of("permitted_use_set", r["product"], r["use_code"], r["version"]), "consumption",
               "permitted_use_set", actor, f"{r['product']}:{r['use_code']}", None,
               json.dumps(r, sort_keys=True, default=str)) for r in new_uses]
    _merge(spark, audit, AUDIT_SCHEMA, cfg.gov("audit_log"), AUDIT_MERGE)
    if new_patterns:
        records_to_df(spark, new_patterns).createOrReplaceTempView("_patterns_src")
        spark.sql(f"""MERGE INTO {patterns_t} t USING (SELECT pattern_id, use_mode, delivery, access_mode, allowed,
                      reason, CAST(version AS INT) AS version, active FROM _patterns_src) s
                      ON t.pattern_id = s.pattern_id WHEN NOT MATCHED THEN INSERT *""")
    if new_uses:
        records_to_df(spark, new_uses).createOrReplaceTempView("_uses_src")
        spark.sql(f"""MERGE INTO {uses_t} t USING (SELECT product, use_code, approval, conditions,
                      CAST(max_term_days AS INT) AS max_term_days, set_by, set_at, CAST(version AS INT) AS version,
                      active FROM _uses_src) s
                      ON t.product = s.product AND t.use_code = s.use_code AND t.version = s.version
                      WHEN NOT MATCHED THEN INSERT *""")
    print(f"Consumption stubs: {len(new_patterns)} pattern(s), {len(new_uses)} permitted use(s) inserted")


if __name__ == "__main__":
    main()
