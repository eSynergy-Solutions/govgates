"""Shared helpers for the Claude Code guard hooks.

Hooks read one JSON object on stdin and answer with JSON on stdout (PreToolUse) or an exit code.
They fail open on their own bugs: a broken hook must not brick the session. The permission rules in
settings.json are the second layer.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

# Files the agent must not change without the lead saying yes. They are the prompts and dictionary, or the
# answers the build is graded against.
PROTECTED = (
    "tests/access/matrix.yaml",
    "src/gates/hydration/prompts/",
    "src/gates/hydration/abbreviations.yaml",
    "src/synthetic/fields.py",  # golden set is derived from it
    "src/synthetic/glossary.yaml",  # term_ids referenced by the golden set
    "eval/golden_candidates.csv",
    "config/access_model.example.yaml",  # stands in for the authorisation service: decides who sees PII
    ".claude/settings.json",
    ".claude/hooks/",
)


# What a shell command must mention to count as touching a protected file. Shorter than PROTECTED so a
# relative path from a subfolder (`cp prompts/propose_v2.md ...`) still matches. Over-asking is fine.
SHELL_MARKERS = (
    "matrix.yaml",
    "prompts/",
    "propose_v",
    "abbreviations.yaml",
    "fields.py",
    "glossary.yaml",
    "golden_candidates.csv",
    "access_model.example",
    "settings.json",
    ".claude/hooks",
    "hooks/_common",
    "guard_bash",
    "guard_edit",
    "check_python",
)


def project_dir(event: dict) -> Path:
    root = os.environ.get("CLAUDE_PROJECT_DIR") or event.get("cwd") or os.getcwd()
    return Path(root).resolve()


def read_event() -> dict:
    try:
        return json.load(sys.stdin)
    except Exception:
        return {}


def rel_path(path: str, root: Path) -> str:
    """Repo-relative, forward-slash, lower-case path (Windows-safe). Empty string if outside the repo."""
    try:
        p = Path(path)
        if not p.is_absolute():
            p = root / p
        rel = os.path.relpath(p.resolve(), root)
    except Exception:
        return ""
    rel = rel.replace("\\", "/")
    return "" if rel.startswith("..") else rel.lower()


def is_protected(rel: str) -> bool:
    return any(rel == p or (p.endswith("/") and rel.startswith(p)) for p in PROTECTED)


def decide(decision: str, reason: str) -> None:
    """PreToolUse answer: 'allow', 'deny' or 'ask'."""
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": decision,
                    "permissionDecisionReason": reason,
                }
            }
        )
    )
    sys.exit(0)


def denylist(root: Path) -> list[str]:
    """Names from .client-denylist, tolerant of UTF-16, BOM and CRLF (same rules as the shell script)."""
    f = root / ".client-denylist"
    if not f.exists():
        return []
    raw = f.read_bytes()
    text = raw.decode("utf-16") if b"\x00" in raw else raw.decode("utf-8-sig", errors="ignore")
    return [n.strip() for n in text.splitlines() if n.strip()]


def mentions_client(text: str, names: list[str]) -> bool:
    # Letters/digits only as boundaries, so snake_case and kebab-case uses (name_prod, name-dev) are caught too.
    return any(re.search(rf"(?<![A-Za-z0-9]){re.escape(n)}(?![A-Za-z0-9])", text, re.IGNORECASE) for n in names)


def tracked_files_with_client(root: Path, names: list[str]) -> list[str]:
    """Tracked files mentioning a denylisted name. Never returns the name itself."""
    out = subprocess.run(["git", "ls-files", "-z"], cwd=root, capture_output=True, check=True).stdout
    hits = []
    for rel in filter(None, out.decode().split("\0")):
        try:
            text = (root / rel).read_text(encoding="utf-8")
        except Exception:
            continue  # binary or unreadable
        if mentions_client(text, names):
            hits.append(rel)
    return hits
