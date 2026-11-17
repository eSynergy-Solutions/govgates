import re

import pytest

from gates.hydration.patterns import FORCE_PII, PATTERNS, hits_from_rates, matches

CASES = {
    "email": (["a.b@example.com", "x_y+z@test.co.uk"], ["not an email", "a@b", "@example.com"]),
    "uk_ni_number": (["AB123456C", "ab 12 34 56 c", "JG103759A"], ["QQ12345", "123456789", "AB123456E", "DA123456A"]),
    "postcode": (["SW1A 1AA", "m1 1ae", "B338TH", "CR2 6XH", "DN55 1PT"], ["12345", "SW1A", "HELLO WORLD"]),
    "phone": (["+44 20 7946 0958", "020 7946 0958", "07700 900123", "+447700900123", "01632-960-001"],
              ["12", "2026-01-01", "not a phone", "1234.56"]),
    "sort_code": (["12-34-56", "00-00-01"], ["123456", "12-34-5", "1234-56"]),
    "iso_date": (["2026-11-01", "2026-11-01 10:15:00", "2026-11-01T10:15:00.123"],
                 ["01/11/2026", "2026-1-1", "20261101"]),
    "currency_amount": (["1234.56", "-12.5", "£1,234.56", "$99.99", "1,000,000.00"],
                        ["1234", "abc", "0.12345", "12-34-56"]),
    "iban": (["GB82WEST12345698765432", "GB82 WEST 1234 5698 7654 32", "DE89370400440532013000",
              "DE89 3704 0044 0532 0130 00", "FR1420041010050500013M02606", "NL91ABNA0417164300"],
             ["CL24000123456", "PO12345678901",
              "XX12ABCD12345678901234",  # XX is not an ISO country prefix
              "gb82west12345698765432",  # lowercase
              "GB82 WEST1234 5698 765432",  # irregular grouping
              "12-34-56", "AB123456C", "SW1A 1AA", "07700 900123", "user@example.com"]),
}


@pytest.mark.parametrize("name", list(CASES))
def test_pattern_accepts_and_rejects(name):
    good, bad = CASES[name]
    for v in good:
        assert matches(name, v), f"{name} should match {v!r}"
    for v in bad:
        assert not matches(name, v), f"{name} should not match {v!r}"


def test_every_contract_pattern_present():
    assert set(PATTERNS) == {"email", "uk_ni_number", "postcode", "phone", "sort_code", "iban", "iso_date",
                             "currency_amount"}


def test_patterns_are_java_regex_safe():
    # Spark rlike uses java.util.regex: no named groups, no Python-only syntax.
    for rx in PATTERNS.values():
        assert "(?P" not in rx
        re.compile(rx)


@pytest.mark.parametrize("name", sorted(PATTERNS))
def test_patterns_have_no_lookbehind_atomic_or_possessive(name):
    # Spark rlike (java.util.regex) compatibility, and Python `re` parity: keep to the common subset.
    rx = PATTERNS[name]
    for construct in ("(?<", "(?>", "*+", "++"):
        assert construct not in rx, f"{name} uses {construct!r}"


def test_iban_pattern_present():
    assert "iban" in PATTERNS


def test_hits_from_rates_uses_threshold_and_sorts():
    rates = {"postcode": 0.95, "email": 0.81, "phone": 0.2}
    assert hits_from_rates(rates, threshold=0.8) == ["email", "postcode"]
    assert hits_from_rates({}, threshold=0.8) == []


def test_force_pii_set_matches_spec():
    # specs/hydration.md step 6 / Sensitivity classes: IBAN joins the pii floor
    assert FORCE_PII == {"email", "uk_ni_number", "phone", "postcode", "sort_code", "iban"}
