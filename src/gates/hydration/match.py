"""Abbreviation expansion and fuzzy matching of column names against the glossary. Pure Python.

Scoring is rapidfuzz token_set_ratio (specs/hydration.md step 3), taken as the max over every reading of
the column name (raw, and each combination of expansions) x every reading of the term name and synonyms.
Both sides go through the same expansion, so "cust_id" meets the synonym "customer id" as
"customer identifier" on each side.

A fuzzy proposal is made only when:
- one term is strictly on top at or above fuzzy_accept (token_set_ratio scores 100 whenever one side's
  tokens are a subset of the other's, so a bare "status" ties every "<x> status" term), and
- the column name has no ambiguous token (one with more than one expansion). Those columns go to the
  LLM with every reading shown in expanded_name.
- some top-scoring (column reading, target reading) pair has full column-token coverage: every column
  token appears in that target. token_set_ratio scores tmp_col 100 against the synonym "col"; the
  unexplained "tmp" blocks the accept. Extra target tokens are fine.
"""

from __future__ import annotations

import itertools
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from rapidfuzz import fuzz, utils

ABBREVIATIONS_PATH = Path(__file__).with_name("abbreviations.yaml")
DEFAULT_FUZZY_ACCEPT = 90
TOP_K = 5
MAX_VARIANTS = 64  # guard against a name made of many ambiguous tokens

_SPLIT = re.compile(r"[^A-Za-z0-9]+")
_CAMEL = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+")

Abbreviations = dict[str, list[str]]


@dataclass(frozen=True)
class Term:
    term_id: str
    name: str
    definition: str
    synonyms: list[str]
    sensitivity: str | None = None


@dataclass(frozen=True)
class Candidate:
    term_id: str
    name: str
    definition: str
    sensitivity: str | None
    score: float


@dataclass(frozen=True)
class MatchResult:
    column: str
    expanded: str
    accepted: Candidate | None
    candidates: list[Candidate]
    ambiguous_tokens: list[str] = field(default_factory=list)


def _as_list(v) -> list[str]:
    return [str(x).lower() for x in v] if isinstance(v, list) else [str(v).lower()]


@dataclass(frozen=True)
class IgnoreRules:
    """Column-name tokens that carry no meaning for matching (abbreviations.yaml `ignore_tokens`).

    currencies are dropped (gwp_gbp -> gwp). With strip_trailing_digits, a digit run glued to a *known* word in the
    same name part is dropped (addr_line1 -> addr line, addr2 -> addr); known = an abbreviation key or the whole
    one-word expansion of one. Unknown words keep their digits (acct_last4, Col17), and a digit that is its own part
    (LEGACY_REF_2) is kept, so noise names stay noise.
    """
    currencies: frozenset[str] = frozenset()
    strip_trailing_digits: bool = False


def load_ignore(path: str | Path = ABBREVIATIONS_PATH) -> IgnoreRules:
    block = yaml.safe_load(Path(path).read_text(encoding="utf-8")).get("ignore_tokens") or {}
    return IgnoreRules(frozenset(str(c).lower() for c in block.get("currencies") or []),
                       bool(block.get("strip_trailing_digits", False)))


def load_noise(path: str | Path = ABBREVIATIONS_PATH) -> frozenset[str]:
    """`noise_tokens` from abbreviations.yaml: words that carry no business meaning in a column name."""
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    return frozenset(str(t).lower() for t in data.get("noise_tokens") or [])


def load_weak_noise(path: str | Path = ABBREVIATIONS_PATH) -> frozenset[str]:
    """`weak_noise_tokens`: noise only next to another noise word (ref, user)."""
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    return frozenset(str(t).lower() for t in data.get("weak_noise_tokens") or [])


def noise_tokens_of(column: str, noise: frozenset[str], weak: frozenset[str] = frozenset()) -> list[str] | None:
    """The column's tokens when every one is a noise word or a bare number and at least one is a noise word that is
    not weak (Col17, user_def_3, LEGACY_REF_2); None otherwise (col_cd: 'cd' means code; '17', 'user_ref' alone)."""
    tokens = tokenize(column)
    strong = noise - weak
    if tokens and all(t in noise or t.isdigit() for t in tokens) and any(t in strong for t in tokens):
        return tokens
    return None


def _known_words(abbreviations: Abbreviations) -> set[str]:
    return set(abbreviations) | {e for v in abbreviations.values() for e in _as_list(v) if " " not in e}


def load_abbreviations(path: str | Path = ABBREVIATIONS_PATH) -> Abbreviations:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    return {str(k).lower(): _as_list(v) for k, v in (data.get("abbreviations") or {}).items()}


def tokenize(name: str, abbreviations: Abbreviations | None = None, ignore: IgnoreRules | None = None) -> list[str]:
    out: list[str] = []
    kept: list[str] = []
    known = _known_words(abbreviations or {}) if ignore and ignore.strip_trailing_digits else set()
    for part in _SPLIT.split(name.strip()):
        toks = [t.lower() for t in _CAMEL.findall(part)]
        out += toks
        if ignore:
            if ignore.strip_trailing_digits and len(toks) >= 2 and toks[-1].isdigit() and toks[-2] in known:
                toks = toks[:-1]
            toks = [t for t in toks if t not in ignore.currencies]
        kept += toks
    return kept if ignore and kept else out


def _readings(token: str, abbreviations: Abbreviations) -> list[str]:
    return _as_list(abbreviations.get(token, [token]))


def expand(tokens: list[str], abbreviations: Abbreviations) -> str:
    """Display form: ambiguous tokens show every reading, e.g. 'policy (expiry/exposure) date'."""
    parts = []
    for t in tokens:
        r = _readings(t, abbreviations)
        parts.append(r[0] if len(r) == 1 else "(" + "/".join(r) + ")")
    return " ".join(parts)


def expand_variants(tokens: list[str], abbreviations: Abbreviations) -> set[str]:
    combos = itertools.islice(itertools.product(*(_readings(t, abbreviations) for t in tokens)), MAX_VARIANTS)
    return {" ".join(c) for c in combos}


def ambiguous_tokens(tokens: list[str], abbreviations: Abbreviations) -> list[str]:
    return [t for t in dict.fromkeys(tokens) if len(_readings(t, abbreviations)) > 1]


def _variants(text: str, abbreviations: Abbreviations) -> set[str]:
    return _token_variants(tokenize(text), abbreviations)


def _token_variants(tokens: list[str], abbreviations: Abbreviations) -> set[str]:
    return {" ".join(tokens)} | expand_variants(tokens, abbreviations)


def _targets(term: Term, abbreviations: Abbreviations) -> set[str]:
    return set().union(*(_variants(t, abbreviations) for t in [term.name, *term.synonyms]))


def _ratio(q: str, t: str) -> float:
    return fuzz.token_set_ratio(q, t, processor=utils.default_process)


def _covered(q: str, t: str) -> bool:
    return set(utils.default_process(q).split()) <= set(utils.default_process(t).split())


def _score(queries: set[str], term: Term, abbreviations: Abbreviations) -> float:
    return max(_ratio(q, t) for q in queries for t in _targets(term, abbreviations))


def has_coverage(queries: set[str], term: Term, score: float, abbreviations: Abbreviations) -> bool:
    """True when some pair scoring `score` explains every column token (specs/hydration.md step 3)."""
    return any(_covered(q, t) for q in queries for t in _targets(term, abbreviations)
               if round(_ratio(q, t), 2) == score)


def _singular(token: str) -> str:
    return token[:-1] if len(token) > 3 and token.endswith("s") else token


def table_tokens(table: str | None) -> set[str]:
    """Singularised tokens of the bare table name: 'cat.raw.payments' -> {'payment'}."""
    return {_singular(t) for t in tokenize(table.split(".")[-1])} if table else set()


def _table_overlap(term: Term, tt: set[str]) -> int:
    words = {_singular(w) for text in [term.name, *term.synonyms] for w in tokenize(text)}
    return len(tt & words)


def match_column(column: str, terms: list[Term], abbreviations: Abbreviations,
                 fuzzy_accept: float = DEFAULT_FUZZY_ACCEPT, top_k: int = TOP_K,
                 table: str | None = None, ignore: IgnoreRules | None = None) -> MatchResult:
    """Equal scores are ordered by overlap with the table name, then term_id (specs/hydration.md step 3).
    The tie-break only orders candidates: accepting still needs a strictly higher top score."""
    tokens = tokenize(column, abbreviations, ignore)
    # Guard: if ignore rules stripped something and one token is left (Value17 -> value, gwp_gbp -> gwp), the
    # remaining word is too generic to trust alone; never fuzzy-accept, let the LLM see the original name.
    stripped_to_one = len(tokens) == 1 and tokens != tokenize(column)
    queries = _token_variants(tokens, abbreviations)
    tt = table_tokens(table)
    by_id = {t.term_id: t for t in terms}
    scored = sorted(
        (Candidate(t.term_id, t.name, t.definition, t.sensitivity, round(_score(queries, t, abbreviations), 2))
         for t in terms),
        key=lambda c: (-c.score, -_table_overlap(by_id[c.term_id], tt), c.term_id),
    )
    top = scored[:top_k]
    ambiguous = ambiguous_tokens(tokens, abbreviations)
    accepted = None
    if (top and not ambiguous and not stripped_to_one and top[0].score >= fuzzy_accept
            and (len(scored) == 1 or scored[1].score < top[0].score)
            and has_coverage(queries, _term_of(terms, top[0].term_id), top[0].score, abbreviations)):
        accepted = top[0]
    return MatchResult(column, expand(tokens, abbreviations), accepted, top, ambiguous)


def _term_of(terms: list[Term], term_id: str) -> Term:
    return next(t for t in terms if t.term_id == term_id)
