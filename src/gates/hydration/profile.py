"""Column profiler. One Spark aggregation per table; only counts and hashes come back to the driver.

No raw values are stored or sent anywhere: the driver sees totals, non-null counts, approximate distinct
counts, per-pattern match counts, average value length and an order-independent value fingerprint per
column. pattern_hits = patterns matching >= threshold of non-null values; any_pattern_hits = patterns
matching at least one value (catches PII buried in free text). The scan covers the whole table.
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field

from gates.hydration.patterns import DEFAULT_HIT_THRESHOLD, PATTERNS, hits_from_rates

_FP_MOD = 1_000_000_007  # keeps the fingerprint sum far from long overflow (ANSI mode would fail)


@dataclass(frozen=True)
class ColumnProfile:
    column_name: str
    data_type: str
    null_pct: float
    distinct_pct: float
    pattern_hits: list[str]
    sample_hash: str
    any_pattern_hits: list[str] = field(default_factory=list)
    avg_length: float | None = None

    def as_record(self) -> dict:
        return asdict(self)


def profile_from_stats(column: str, data_type: str, total: int, non_null: int, distinct: int,
                       pattern_counts: dict[str, int], fingerprint: int | None, avg_length: float | None = None,
                       threshold: float = DEFAULT_HIT_THRESHOLD) -> ColumnProfile:
    null_pct = round(100.0 * (total - non_null) / total, 2) if total else 0.0
    distinct_pct = round(min(100.0, 100.0 * distinct / non_null), 2) if non_null else 0.0
    rates = {k: (v or 0) / non_null for k, v in pattern_counts.items()} if non_null else {}
    digest = hashlib.sha256(f"{column.lower()}|{data_type}|{non_null}|{fingerprint}".encode()).hexdigest()
    any_hits = sorted(k for k, v in pattern_counts.items() if v)
    avg_len = round(float(avg_length), 2) if avg_length is not None and non_null else None
    return ColumnProfile(column, data_type, null_pct, distinct_pct, hits_from_rates(rates, threshold), digest,
                         any_hits, avg_len)


def profile_dataframe(df, threshold: float = DEFAULT_HIT_THRESHOLD) -> list[ColumnProfile]:
    """Spark edge: one aggregate over the whole table."""
    from pyspark.sql import functions as F

    aggs = [F.count(F.lit(1)).alias("__total")]
    for i, f in enumerate(df.schema.fields):
        c = F.col(f"`{f.name}`")
        s = c.cast("string")
        aggs += [
            F.count(c).alias(f"nn_{i}"),
            F.approx_count_distinct(c, rsd=0.01).alias(f"d_{i}"),
            F.sum(F.pmod(F.xxhash64(s), F.lit(_FP_MOD))).alias(f"fp_{i}"),
            F.avg(F.length(s)).alias(f"len_{i}"),
        ]
        aggs += [F.sum(F.when(s.rlike(rx), 1).otherwise(0)).alias(f"p_{i}_{name}") for name, rx in PATTERNS.items()]
    row = df.agg(*aggs).collect()[0].asDict()

    total = row["__total"]
    return [
        profile_from_stats(
            column=f.name, data_type=f.dataType.simpleString(), total=total,
            non_null=row[f"nn_{i}"], distinct=row[f"d_{i}"],
            pattern_counts={name: row[f"p_{i}_{name}"] for name in PATTERNS},
            fingerprint=row[f"fp_{i}"], avg_length=row[f"len_{i}"], threshold=threshold,
        )
        for i, f in enumerate(df.schema.fields)
    ]
