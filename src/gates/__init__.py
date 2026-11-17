"""Governance gates: hydration and consumption."""

from __future__ import annotations

from pathlib import Path


def _version() -> str:
    """The release version from the repo's VERSION file (the one place it lives; pyproject reads it too). Read from
    the file next to the source, so it also works in a job that runs the bundle's files without installing them."""
    candidate = Path(__file__).resolve().parents[2] / "VERSION"  # <repo>/src/gates/__init__.py -> <repo>/VERSION
    if candidate.is_file():
        return candidate.read_text(encoding="utf-8").strip()
    from importlib.metadata import PackageNotFoundError, version  # installed without the repo: package metadata

    try:
        return version("govgates-accelerator")
    except PackageNotFoundError:  # never let `import gates` fail (every job imports it)
        return "0+unknown"


__version__ = _version()
