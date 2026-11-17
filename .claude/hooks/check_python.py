"""Quality hooks.

  check_python.py lint   PostToolUse on Edit/Write: ruff on the edited .py file; problems go back to Claude.
  check_python.py tests  Stop: if any .py file is changed and uncommitted, run the unit tests; a failure
                         sends Claude back to fix it (once per stop, never in a loop).
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

from _common import project_dir, read_event


def _ruff() -> str | None:
    """ruff on PATH, else the one installed next to the interpreter running this hook (the project venv)."""
    return shutil.which("ruff") or shutil.which("ruff", path=str(Path(sys.executable).parent))


def lint(event: dict) -> None:
    path = (event.get("tool_input") or {}).get("file_path") or ""
    ruff = _ruff()
    if not path.endswith(".py") or not ruff:
        sys.exit(0)
    res = subprocess.run([ruff, "check", path], cwd=project_dir(event), capture_output=True, text=True)
    if res.returncode != 0:
        print(f"ruff found problems in {path}:\n{res.stdout[-3000:]}", file=sys.stderr)
        sys.exit(2)
    sys.exit(0)


def tests(event: dict) -> None:
    if event.get("stop_hook_active"):
        sys.exit(0)
    root = project_dir(event)
    status = subprocess.run(["git", "status", "--porcelain"], cwd=root, capture_output=True, text=True).stdout
    if not any(line.rstrip().endswith(".py") for line in status.splitlines()):
        sys.exit(0)
    res = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/unit", "-q", "-x", "--no-header"],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=240,
    )
    if res.returncode not in (0, 5):  # 5 = no tests collected
        print("Unit tests fail with uncommitted changes. Fix before finishing:\n" + res.stdout[-3000:], file=sys.stderr)
        sys.exit(2)
    sys.exit(0)


if __name__ == "__main__":
    try:
        {"lint": lint, "tests": tests}[sys.argv[1]](read_event())
    except SystemExit:
        raise
    except Exception as exc:  # fail open on hook bugs
        print(f"check_python hook error (ignored): {exc}", file=sys.stderr)
        sys.exit(0)
