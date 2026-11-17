"""Proposals: fuzzy (no LLM) and LLM (unresolved columns batched, at most 40 per call by default; Q4 wide tables).

The model only ever sees column names, expanded names, profile stats, pattern hits and candidate terms.
Its output is validated strictly: a term_id that is not one of that column's candidates (or NO_MATCH),
a bad sensitivity, an out-of-range confidence, a missing or duplicate column -> rejected and logged.
Rejected columns still get a proposal row (term_id null, confidence 0) so the owner sees the gap.

Sensitivity is floored in code: max(pattern floor, chosen term's sensitivity, proposed sensitivity),
ordered public < internal < confidential < pii. The model may raise a class, never lower it.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from gates.common.contract import SENSITIVITIES
from gates.common.llm_client import LLMClient
from gates.hydration.match import Candidate, MatchResult, noise_tokens_of
from gates.hydration.patterns import FORCE_PII
from gates.hydration.profile import ColumnProfile

PROMPT_VERSION = "propose_v3"  # keep in step with the bundle variable prompt_version
PROMPTS_DIR = Path(__file__).with_name("prompts")
NO_MATCH = "NO_MATCH"

_FENCE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL)


@dataclass(frozen=True)
class ColumnInput:
    column: str
    expanded: str
    profile: ColumnProfile
    candidates: list[Candidate]


@dataclass(frozen=True)
class Proposal:
    column: str
    term_id: str | None
    description: str | None
    sensitivity: str | None
    confidence: float
    rationale: str | None
    method: str  # fuzzy | llm | rule
    model: str | None = None
    prompt_version: str | None = None
    tokens: int = 0


@dataclass(frozen=True)
class Rejection:
    column: str
    reason: str
    output: str | None = None


@dataclass
class LLMResult:
    proposals: list[Proposal] = field(default_factory=list)
    rejections: list[Rejection] = field(default_factory=list)
    calls: int = 0
    tokens: int = 0
    model: str | None = None


def enforce_floor(sensitivity: str | None, pattern_hits: list[str],
                  term_sensitivity: str | None = None) -> str | None:
    levels = [s for s in (sensitivity, term_sensitivity) if s in SENSITIVITIES]
    if FORCE_PII.intersection(pattern_hits):
        levels.append("pii")
    if not levels:
        return None
    top = max(levels, key=SENSITIVITIES.index)
    # public only if the chosen term is public; otherwise internal is the floor (a NO_MATCH is never public)
    return "internal" if top == "public" and term_sensitivity != "public" else top


def split_tokens(total: int, n: int) -> list[int]:
    """Spread one call's tokens over its n proposals so the column sums to the real total."""
    if n <= 0:
        return []
    base, rem = divmod(total, n)
    return [base + (1 if i < rem else 0) for i in range(n)]


def fuzzy_proposal(match: MatchResult, profile: ColumnProfile) -> Proposal:
    c = match.accepted
    assert c is not None
    return Proposal(
        column=match.column, term_id=c.term_id, description=c.definition,
        sensitivity=enforce_floor(c.sensitivity, profile.pattern_hits, c.sensitivity),
        confidence=round(c.score / 100.0, 4),
        rationale=f"fuzzy match on '{match.expanded}' (token_set_ratio {c.score:g})", method="fuzzy",
    )


_VERSION = re.compile(r"propose_v\d+")
_PLACEHOLDER = re.compile(r"\{\{([^{}]*)\}\}")
PLACEHOLDERS = frozenset({"table", "columns_json", "table_columns"})


def noise_proposal(column: str, profile: ColumnProfile, tokens: list[str]) -> Proposal:
    """Deterministic NO_MATCH for a name made only of noise tokens (specs/hydration.md, noise names first)."""
    return Proposal(
        column=column, term_id=None, description="Column name carries no business meaning.",
        sensitivity=enforce_floor("internal", profile.pattern_hits, None), confidence=1.0,
        rationale=f"name made only of uninformative tokens: {', '.join(tokens)}", method="rule",
    )


def _free_text(profile: ColumnProfile) -> bool:
    return profile.avg_length is not None and profile.avg_length > 30 and profile.distinct_pct > 50


def rule_proposal(column: str, profile: ColumnProfile, noise: frozenset[str],
                  weak: frozenset[str] = frozenset()) -> Proposal | None:
    """Noise-name rule with its exceptions (specs/hydration.md). None -> the column goes on to fuzzy/LLM: a value
    pattern on >= 80% of values, any PII-pattern hit, or a free-text profile, so the LLM's sensitivity rules apply."""
    tokens = noise_tokens_of(column, noise, weak)
    if (tokens is None or profile.pattern_hits or FORCE_PII.intersection(profile.any_pattern_hits)
            or _free_text(profile)):
        return None
    return noise_proposal(column, profile, tokens)


def load_prompt(version: str = PROMPT_VERSION) -> tuple[str, str]:
    if not _VERSION.fullmatch(version or ""):
        raise ValueError(f"prompt version must look like propose_v<N>, got {version!r}")
    text = (PROMPTS_DIR / f"{version}.md").read_text(encoding="utf-8")
    _, rest = text.split("## SYSTEM", 1)
    system, user = rest.split("## USER", 1)
    return system.strip(), user.strip()


def check_prompt(version: str = PROMPT_VERSION) -> set[str]:
    """The template's placeholder names; ValueError on any outside PLACEHOLDERS. Checks the template, not content."""
    system, user = load_prompt(version)
    names = set(_PLACEHOLDER.findall(system + user))
    unknown = names - PLACEHOLDERS
    if unknown:
        raise ValueError(f"prompt {version} has unknown placeholders {sorted(unknown)}")
    return names


def _column_payload(ci: ColumnInput) -> dict:
    p = ci.profile
    return {
        "column": ci.column,
        "expanded_name": ci.expanded,
        "data_type": p.data_type,
        "null_pct": p.null_pct,
        "distinct_pct": p.distinct_pct,
        "pattern_hits": p.pattern_hits,
        "any_pattern_hits": p.any_pattern_hits,
        "avg_length": p.avg_length,
        "candidates": [{"term_id": c.term_id, "name": c.name, "definition": c.definition,
                        "sensitivity": c.sensitivity, "fuzzy_score": c.score} for c in ci.candidates],
    }


def build_messages(table_name: str, inputs: list[ColumnInput], version: str = PROMPT_VERSION,
                   table_columns: list[str] | None = None) -> list[dict]:
    """{{table_columns}} (every column name in the table, names only) is filled only when the prompt asks for it,
    so a prompt without it gets exactly the input it was released with. A placeholder left over is an error."""
    names = check_prompt(version)
    if "table_columns" in names and table_columns is None:
        raise ValueError(f"prompt {version} needs table_columns")
    system, user = load_prompt(version)
    values = {
        "table": table_name.split(".")[-1],  # entity context only; catalog/schema names add nothing
        "columns_json": json.dumps([_column_payload(ci) for ci in inputs], indent=1),
        "table_columns": json.dumps(list(table_columns or [])),
    }
    # one pass over the template: inserted content (definitions, names) is never re-scanned or substituted
    system, user = (_PLACEHOLDER.sub(lambda m: values[m.group(1)], t) for t in (system, user))
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _validate(entry: dict, ci: ColumnInput) -> str | None:
    allowed = {c.term_id for c in ci.candidates} | {NO_MATCH}
    if entry.get("term_id") not in allowed:
        return f"term_id {entry.get('term_id')!r} is not a candidate for this column"
    if entry.get("sensitivity") not in SENSITIVITIES:
        return f"sensitivity {entry.get('sensitivity')!r} not in {SENSITIVITIES}"
    conf = entry.get("confidence")
    if isinstance(conf, bool) or not isinstance(conf, int | float) or not 0.0 <= conf <= 1.0:
        return f"confidence {conf!r} is not a number in [0, 1]"
    return None


def parse_response(text: str, inputs: list[ColumnInput]) -> tuple[list[Proposal], list[Rejection]]:
    by_col = {ci.column: ci for ci in inputs}
    m = _FENCE.match(text or "")
    try:
        data = json.loads(m.group(1) if m else text)
        entries = data["columns"]
        assert isinstance(entries, list)
    except Exception as e:  # noqa: BLE001 - any parse failure rejects the whole batch
        return [], [Rejection(c, f"unparseable response: {type(e).__name__}") for c in by_col]

    proposals: dict[str, Proposal] = {}
    rejections: list[Rejection] = []
    for entry in entries:
        col = entry.get("column") if isinstance(entry, dict) else None
        raw = json.dumps(entry)[:2000]
        if col not in by_col:
            rejections.append(Rejection(str(col), "unknown column in response", raw))
            continue
        if col in proposals or any(r.column == col for r in rejections):
            rejections.append(Rejection(col, "duplicate column in response", raw))
            continue
        reason = _validate(entry, by_col[col])
        if reason:
            rejections.append(Rejection(col, reason, raw))
            continue
        term = entry["term_id"]
        proposals[col] = Proposal(
            column=col, term_id=None if term == NO_MATCH else term,
            description=(entry.get("description") or None), sensitivity=entry["sensitivity"],
            confidence=float(entry["confidence"]), rationale=(entry.get("rationale") or None), method="llm",
        )
    answered = set(proposals) | {r.column for r in rejections}
    rejections += [Rejection(c, "missing from response") for c in by_col if c not in answered]
    return [proposals[c] for c in by_col if c in proposals], rejections


def _term_sensitivity(ci: ColumnInput, term_id: str | None) -> str | None:
    return next((c.sensitivity for c in ci.candidates if c.term_id == term_id), None) if term_id else None


def max_tokens_for(n_columns: int) -> int:
    return min(16000, 1500 + 300 * n_columns)


MAX_COLUMNS_PER_CALL = 40
# Above this a chunk's output budget hits max_tokens_for's 16000 cap and long answers truncate (the Q4 failure).
MAX_COLUMNS_CEILING = (16000 - 1500) // 300


def check_columns_per_call(n: int) -> int:
    """The job-level setting: a positive whole number no larger than MAX_COLUMNS_CEILING."""
    chunk_columns([], n)
    if n > MAX_COLUMNS_CEILING:
        raise ValueError(f"max columns per call {n} is above {MAX_COLUMNS_CEILING}: answers would be truncated")
    return n


def chunk_columns(items: list, max_per_call: int) -> list[list]:
    """ceil(n / max) chunks of near-equal size (the first n % k one larger), input order kept (Q4: wide tables)."""
    if isinstance(max_per_call, bool) or not isinstance(max_per_call, int) or max_per_call < 1:
        raise ValueError(f"max columns per call must be a positive whole number, got {max_per_call!r}")
    n = len(items)
    if n == 0:
        return []
    k = -(-n // max_per_call)
    size, extra = divmod(n, k)
    out, start = [], 0
    for i in range(k):
        end = start + size + (1 if i < extra else 0)
        out.append(list(items[start:end]))
        start = end
    return out


def _propose_chunk(table_name: str, inputs: list[ColumnInput], client: LLMClient, version: str,
                   table_columns: list[str] | None) -> tuple[list[Proposal], list[Rejection], int, str | None]:
    resp = client.chat(build_messages(table_name, inputs, version, table_columns), temperature=0.0,
                       max_tokens=max_tokens_for(len(inputs)))
    parsed, rejections = parse_response(resp.text, inputs)
    by_col = {p.column: p for p in parsed}
    rejected_reason = {r.column: r.reason for r in rejections if r.column not in by_col}
    rows = [by_col.get(ci.column) or Proposal(
        column=ci.column, term_id=None, description=None, sensitivity=None, confidence=0.0,
        rationale=f"REJECTED: {rejected_reason.get(ci.column, 'no valid output')}", method="llm",
    ) for ci in inputs]
    model = resp.model or client.model
    tokens = split_tokens(resp.total_tokens, len(rows))
    final = [
        Proposal(p.column, p.term_id, p.description,
                 enforce_floor(p.sensitivity, ci.profile.pattern_hits, _term_sensitivity(ci, p.term_id)),
                 p.confidence, p.rationale, "llm", model, version, t)
        for p, ci, t in zip(rows, inputs, tokens)
    ]
    return final, rejections, resp.total_tokens, model


def propose_llm(table_name: str, inputs: list[ColumnInput], client: LLMClient,
                version: str = PROMPT_VERSION, table_columns: list[str] | None = None,
                max_columns_per_call: int = MAX_COLUMNS_PER_CALL) -> LLMResult:
    """One call per chunk of at most max_columns_per_call columns. Every chunk sees the whole table's column names
    (table_columns); only the proposed columns are chunked. A bad response rejects its own chunk's columns only."""
    chunks = chunk_columns(inputs, max_columns_per_call)  # validates before any call
    if not chunks:
        return LLMResult(model=client.model)
    proposals: list[Proposal] = []
    rejections: list[Rejection] = []
    tokens, model = 0, None
    chunk_of = {ci.column: i for i, chunk in enumerate(chunks) for ci in chunk}
    for i, chunk in enumerate(chunks):
        p, r, t, m = _propose_chunk(table_name, chunk, client, version, table_columns)
        proposals += p
        # a stray answer for a column of another chunk is ignored; that column keeps its own chunk's answer
        rejections += [Rejection(x.column, "unknown column for this chunk (belongs to another chunk; ignored)",
                                 x.output) if x.reason == "unknown column in response" and chunk_of.get(x.column,
                                                                                                    i) != i else x
                       for x in r]
        tokens += t
        model = model or m
    return LLMResult(proposals, rejections, calls=len(chunks), tokens=tokens, model=model or client.model)
