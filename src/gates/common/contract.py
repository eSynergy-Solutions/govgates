"""Contract values shared by both gates (specs/contracts.md Rules)."""

from __future__ import annotations

# Ordered low to high. Must equal the allowed values of the governed tag <tag_prefix>_sensitivity exactly
# (case-sensitive); sql/setup/governed_tags.sql records that tag and tests/unit/test_contract.py pins all three.
SENSITIVITIES: tuple[str, ...] = ("public", "internal", "confidential", "pii")


def _check_prefix(prefix: str) -> None:
    if not prefix or not all(ch.isalnum() or ch == "_" for ch in prefix):
        raise ValueError(f"tag prefix {prefix!r} must be letters, digits or _")


def tag_keys(prefix: str) -> tuple[str, str]:
    """(<prefix>_sensitivity, <prefix>_business_term): the two UC tag keys hydration apply owns."""
    _check_prefix(prefix)
    return f"{prefix}_sensitivity", f"{prefix}_business_term"


def rls_tag_key(prefix: str) -> str:
    """<prefix>_rls_attribute: the UC tag key the consumption generator owns (specs/consumption.md)."""
    _check_prefix(prefix)
    return f"{prefix}_rls_attribute"
