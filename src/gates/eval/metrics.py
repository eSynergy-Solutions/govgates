"""Hydration eval metrics (specs/eval.md). Pure Python, no Spark.

The caller passes the golden set and the proposals of ONE eval run (scoped by run_id). Join is exact on
(table_name, column_name); proposals outside the golden set are ignored; a rejected proposal counts as missing.
Ratios with a zero denominator are None. extra_* keys are extras, not spec metrics.
"""

from __future__ import annotations

from dataclasses import dataclass

from gates.common.contract import SENSITIVITIES


@dataclass(frozen=True)
class GoldenRow:
    table_name: str
    column_name: str
    expected_term_id: str | None
    expected_sensitivity: str
    difficulty: str


@dataclass(frozen=True)
class ProposalRow:
    table_name: str
    column_name: str
    term_id: str | None
    sensitivity: str | None
    confidence: float
    method: str
    tokens: int
    rejected: bool = False


def _ratio(num: int, den: int) -> float | None:
    return num / den if den else None


def _join(golden: list[GoldenRow], proposals: list[ProposalRow]) -> dict[tuple[str, str], ProposalRow]:
    keys = {(g.table_name, g.column_name) for g in golden}
    joined: dict[tuple[str, str], ProposalRow] = {}
    for p in proposals:
        k = (p.table_name, p.column_name)
        if k not in keys:
            continue
        if k in joined:
            raise ValueError(f"duplicate proposal for {k[0]}.{k[1]} in one eval run")
        joined[k] = p
    return joined


def _live(p: ProposalRow | None) -> ProposalRow | None:
    return None if p is None or p.rejected else p


def evaluate(golden: list[GoldenRow], proposals: list[ProposalRow],
             threshold: float = 0.8) -> dict[str, float | int | None]:
    joined = _join(golden, proposals)
    pairs = [(g, _live(joined.get((g.table_name, g.column_name)))) for g in golden]
    live = [(g, p) for g, p in pairs if p is not None]
    with_term = [(g, p) for g, p in live if p.term_id is not None]
    true_term = [(g, p) for g, p in pairs if g.expected_term_id is not None]
    no_match = [(g, p) for g, p in pairs if g.expected_term_id is None]
    pii = [(g, p) for g, p in pairs if g.expected_sensitivity == "pii"]
    rank = SENSITIVITIES.index

    m: dict[str, float | int | None] = {
        "term_precision": _ratio(sum(p.term_id == g.expected_term_id for g, p in with_term), len(with_term)),
        "coverage": _ratio(sum(p is not None and p.term_id is not None and p.confidence >= threshold
                               for _, p in true_term), len(true_term)),  # term proposals only, never NO_MATCH
        "no_match_accuracy": _ratio(sum(p is not None and p.term_id is None for _, p in no_match), len(no_match)),
        "pii_recall": _ratio(sum(p is not None and p.sensitivity == "pii" for _, p in pii), len(pii)),
        "fuzzy_share": _ratio(sum(p.method == "fuzzy" for p in joined.values()), len(joined)),
        "tokens_per_dataset": _ratio(sum(p.tokens for p in joined.values()),
                                     len({p.table_name for p in joined.values()})),
    }
    for d in sorted({g.difficulty for g in golden}):
        sub = [(g, p) for g, p in with_term if g.difficulty == d]
        m[f"term_precision_{d}"] = _ratio(sum(p.term_id == g.expected_term_id for g, p in sub), len(sub))
    m["extra_recall_at_threshold"] = _ratio(
        sum(p is not None and p.term_id == g.expected_term_id and p.confidence >= threshold for g, p in true_term),
        len(true_term))
    m["extra_sensitivity_accuracy"] = _ratio(
        sum(p is not None and p.sensitivity == g.expected_sensitivity for g, p in pairs), len(pairs))
    m["extra_under_classified_count"] = sum(
        p.sensitivity in SENSITIVITIES and rank(p.sensitivity) < rank(g.expected_sensitivity) for g, p in live)
    m["extra_missing_count"] = len(pairs) - len(live)
    m["extra_rule_share"] = _ratio(sum(p.method == "rule" for p in joined.values()), len(joined))
    return m


def per_column(golden: list[GoldenRow], proposals: list[ProposalRow]) -> list[dict]:
    joined = _join(golden, proposals)
    rows = []
    for g in golden:
        p = _live(joined.get((g.table_name, g.column_name)))
        rows.append({
            "table_name": g.table_name, "column_name": g.column_name, "difficulty": g.difficulty,
            "expected_term_id": g.expected_term_id, "proposed_term_id": p.term_id if p else None,
            "term_correct": p is not None and p.term_id == g.expected_term_id,
            "expected_sensitivity": g.expected_sensitivity, "proposed_sensitivity": p.sensitivity if p else None,
            "method": p.method if p else None, "confidence": p.confidence if p else None, "missing": p is None,
        })
    return rows
