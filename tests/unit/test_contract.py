"""One list of sensitivity classes (specs/contracts.md Rules; specs/hydration.md Sensitivity classes).

The governed tag `${tag_prefix}_sensitivity` rejects any value outside its allowed list (case-sensitive), so the
code constant, the contract doc and the tag's VALUES must agree exactly, in order.
"""

from __future__ import annotations

import re
from pathlib import Path

import gates.eval.metrics as metrics
import gates.hydration.propose as propose
from gates.common.contract import SENSITIVITIES

ROOT = Path(__file__).resolve().parents[2]
CONTRACTS = ROOT / "specs" / "contracts.md"
GOVERNED_TAGS = ROOT / "sql" / "setup" / "governed_tags.sql"


def _contract_values() -> tuple[str, ...]:
    text = CONTRACTS.read_text(encoding="utf-8")
    m = re.search(r"^- Sensitivity values:(.*)$", text, re.MULTILINE)
    assert m, "Sensitivity values line not found in specs/contracts.md"
    return tuple(re.findall(r"`([^`]+)`", m.group(1)))


def _governed_tag_values() -> tuple[str, ...]:
    text = GOVERNED_TAGS.read_text(encoding="utf-8")
    m = re.search(r"CREATE\s+GOVERNED\s+TAG[^;]*?\$\{tag_prefix\}_sensitivity[^;]*?VALUES\s*\(([^)]*)\)",
                  text, re.IGNORECASE | re.DOTALL)
    assert m, "CREATE GOVERNED TAG ${tag_prefix}_sensitivity ... VALUES (...) not found"
    return tuple(a or b for a, b in re.findall(r"'([^']*)'|\"([^\"]*)\"", m.group(1)))


def test_sensitivities_are_the_four_classes_in_order():
    assert SENSITIVITIES == ("public", "internal", "confidential", "pii")
    assert isinstance(SENSITIVITIES, tuple)


def test_matches_contracts_md_exactly():
    assert _contract_values() == SENSITIVITIES


def test_matches_governed_tag_values_exactly():
    assert _governed_tag_values() == SENSITIVITIES


def test_propose_and_metrics_use_the_contract_constant():
    assert propose.SENSITIVITIES is SENSITIVITIES
    assert metrics.SENSITIVITIES is SENSITIVITIES


# ---------------------------------------------------------------- row-filter tag key (specs/consumption.md)

def test_rls_tag_key():
    from gates.common.contract import rls_tag_key
    assert rls_tag_key("gg") == "gg_rls_attribute"
    assert rls_tag_key("xx") == "xx_rls_attribute"


def test_rls_tag_key_validates_prefix_like_tag_keys():
    import pytest

    from gates.common.contract import rls_tag_key, tag_keys
    for bad in ("", "g-g", "g g", "g.g", "g'g"):
        with pytest.raises(ValueError):
            tag_keys(bad)
        with pytest.raises(ValueError):
            rls_tag_key(bad)


def test_tag_keys_still_exactly_two_and_exclude_rls():
    from gates.common.contract import tag_keys
    keys = tag_keys("gg")
    assert keys == ("gg_sensitivity", "gg_business_term")
    assert "gg_rls_attribute" not in keys
