"""Row builders for gov.column_profiles and gov.term_proposals. Pure: Spark only consumes the tuples.

Explicit schemas so Delta appends never depend on type inference (tokens must stay INT, empty
pattern_hits must stay ARRAY<STRING>).
"""

from __future__ import annotations

import uuid

from gates.common.config import GateConfig
from gates.hydration.profile import ColumnProfile
from gates.hydration.propose import Proposal

PROFILE_SCHEMA = ("run_id string, table_name string, column_name string, data_type string, null_pct double, "
                  "distinct_pct double, pattern_hits array<string>, sample_hash string, schema_hash string, "
                  "any_pattern_hits array<string>, avg_length double")
PROPOSAL_SCHEMA = ("proposal_id string, run_id string, table_name string, column_name string, term_id string, "
                   "description string, sensitivity string, confidence double, method string, model string, "
                   "prompt_version string, tokens int, rationale string, status string")


def qualify_table(table: str, cfg: GateConfig) -> str:
    """A bare table name lives in the raw schema; a three-part name is used as given."""
    return table if table.count(".") == 2 else cfg.raw(table)


def profile_rows(run_id: str, table_name: str, schema_hash: str, profiles: list[ColumnProfile]) -> list[tuple]:
    return [(run_id, table_name, p.column_name, p.data_type, p.null_pct, p.distinct_pct, list(p.pattern_hits),
             p.sample_hash, schema_hash, list(p.any_pattern_hits), p.avg_length) for p in profiles]


def proposal_rows(run_id: str, table_name: str, proposals: list[Proposal], status: str = "proposed") -> list[tuple]:
    """status 'proposed' enters the review queue; 'eval' (force mode) never does: the guard and approve only
    read 'proposed' rows."""
    return [(str(uuid.uuid4()), run_id, table_name, p.column, p.term_id, p.description, p.sensitivity,
             float(p.confidence), p.method, p.model, p.prompt_version, int(p.tokens), p.rationale, status)
            for p in proposals]


def run_summary(run_id: str, table_name: str, schema_hash: str, proposals: list[Proposal], *,
                llm_calls: int, rejections: int, model: str | None, prompt_version: str | None = None) -> dict:
    return {
        "run_id": run_id,
        "table_name": table_name,
        "schema_hash": schema_hash,
        "columns_proposed": len(proposals),
        "fuzzy": sum(p.method == "fuzzy" for p in proposals),
        "llm": sum(p.method == "llm" for p in proposals),
        "rule": sum(p.method == "rule" for p in proposals),
        "no_match": sum(p.term_id is None and not (p.rationale or "").startswith("REJECTED") for p in proposals),
        "rejected": rejections,
        "llm_calls": llm_calls,
        "tokens": sum(p.tokens for p in proposals),
        "model": model,
        "prompt_version": prompt_version,
    }
