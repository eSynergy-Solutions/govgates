"""Hydration guard: decide whether a table needs proposals at all. Pure functions, no Spark.

A column is *covered* when it has approved metadata, or a pending (status `proposed`) row from a run
with the same schema_hash. Re-running on an unchanged, fully covered table therefore makes zero LLM
calls and writes nothing.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass, field


def schema_hash(columns: Iterable[tuple[str, str]]) -> str:
    """sha256 of sorted (column_name, data_type) pairs. Names and types are lower-cased first."""
    pairs = sorted((name.lower(), dtype.lower()) for name, dtype in columns)
    return hashlib.sha256("\n".join(f"{n}\t{t}" for n, t in pairs).encode()).hexdigest()


@dataclass(frozen=True)
class GuardDecision:
    skip: bool
    reason: str
    to_propose: list[str] = field(default_factory=list)


def decide(current_hash: str, seen_hashes: set[str], columns: list[str],
           approved: set[str], pending_same_hash: set[str], force: bool = False) -> GuardDecision:
    if force:  # eval mode: bypass the guard, propose every column (specs/hydration.md step 1)
        return GuardDecision(False, "forced (eval mode): proposing every column", list(columns))
    covered = {c.lower() for c in approved | pending_same_hash}
    uncovered = [c for c in columns if c.lower() not in covered]
    if current_hash in seen_hashes and not uncovered:
        return GuardDecision(True, "schema unchanged and every column covered")
    if current_hash not in seen_hashes:
        reason = "new table or schema change"
    else:
        reason = f"{len(uncovered)} column(s) without approved or pending metadata"
    return GuardDecision(False, reason, uncovered)
