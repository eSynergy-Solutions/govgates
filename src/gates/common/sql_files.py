"""Render DDL files with ${placeholders} into individual statements. Pure Python, unit-tested."""

from __future__ import annotations

import re
from pathlib import Path

_PLACEHOLDER = re.compile(r"\$\{(\w+)\}")
_STATEMENT_END = re.compile(r";[ \t]*$", re.MULTILINE)  # only a semicolon that ends a line ends a statement


def render(sql: str, subs: dict[str, str]) -> str:
    def repl(m: re.Match) -> str:
        key = m.group(1)
        if key not in subs:
            raise KeyError(f"No value for placeholder ${{{key}}}")
        return subs[key]

    return _PLACEHOLDER.sub(repl, sql)


def split_statements(sql: str) -> list[str]:
    """Split on semicolons at line ends; drop comment-only lines. Semicolons inside a line are kept."""
    lines = [ln for ln in sql.splitlines() if not ln.strip().startswith("--")]
    parts = _STATEMENT_END.split("\n".join(lines))
    return [p.strip() for p in parts if p.strip()]


def statements_from_dir(ddl_dir: str | Path, subs: dict[str, str]) -> list[tuple[str, str]]:
    out = []
    for path in sorted(Path(ddl_dir).glob("*.sql")):
        for stmt in split_statements(render(path.read_text(), subs)):
            out.append((path.name, stmt))
    return out


_ADD_COLUMNS = re.compile(r"^\s*ALTER\s+TABLE\s+(\S+)\s+ADD\s+COLUMNS\s*\((.*)\)\s*$", re.IGNORECASE | re.DOTALL)


def ddl_then_views(ddl_dir: str | Path, views_dir: str | Path | None,
                   subs: dict[str, str]) -> list[tuple[str, str]]:
    """Every DDL statement, then every view (CREATE OR REPLACE, stateless), each in file-name order."""
    out = statements_from_dir(ddl_dir, subs)
    if views_dir is not None:
        out += statements_from_dir(views_dir, subs)
    return out


def _split_top(s: str) -> list[str]:
    """Split a column list on commas outside quotes and brackets."""
    out, buf, depth, quote = [], [], 0, None
    for ch in s:
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in "'\"":
            quote = ch
        elif ch in "(<":
            depth += 1
        elif ch in ")>":
            depth -= 1
        elif ch == "," and depth == 0:
            out.append("".join(buf).strip())
            buf = []
            continue
        buf.append(ch)
    if "".join(buf).strip():
        out.append("".join(buf).strip())
    return out


def add_columns(stmt: str) -> tuple[str, list[tuple[str, str]]] | None:
    """(table, [(column, definition)]) for `ALTER TABLE t ADD COLUMNS (...)`, else None."""
    m = _ADD_COLUMNS.match(stmt)
    if not m:
        return None
    cols = [(c.split()[0].strip("`"), c) for c in _split_top(m.group(2))]
    return m.group(1), cols


def missing_columns_statement(stmt: str, existing: set[str]) -> str | None:
    """The ADD COLUMNS statement reduced to columns not in `existing` (case-insensitive); None if nothing is left.
    SQL has no ADD COLUMN IF NOT EXISTS, so this keeps setup idempotent without catching errors."""
    parsed = add_columns(stmt)
    if parsed is None:
        return stmt
    table, cols = parsed
    have = {c.lower() for c in existing}
    keep = [d for name, d in cols if name.lower() not in have]
    return f"ALTER TABLE {table} ADD COLUMNS ({', '.join(keep)})" if keep else None
