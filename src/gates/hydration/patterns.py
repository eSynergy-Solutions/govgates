"""Value-pattern detectors used by the profiler. Regexes must work in both Python `re` and Java (Spark rlike).

Only the *rate* of matches leaves Spark; values never do.
"""

from __future__ import annotations

import re

PATTERNS: dict[str, str] = {
    "email": r"^[A-Za-z0-9._%+'-]+@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}$",
    # HMRC prefix rules: first letter not D,F,I,Q,U,V; second not D,F,I,O,Q,U,V; suffix A-D.
    "uk_ni_number": r"(?i)^[A-CEGHJ-PR-TW-Z][A-CEGHJ-NPR-TW-Z]\s?\d{2}\s?\d{2}\s?\d{2}\s?[A-D]$",
    "postcode": r"(?i)^[A-Z]{1,2}\d[A-Z\d]?\s?\d[A-Z]{2}$",
    "phone": r"^(\+44\s?\(?0?\)?\s?|0)\d{2,4}[\s-]?\d{3,4}[\s-]?\d{3,4}$",
    "sort_code": r"^\d{2}-\d{2}-\d{2}$",
    # ISO 13616: a country in the IBAN registry, 2 check digits, 11-30 alphanumerics, either unspaced or in
    # groups of four with a shorter last group. Uppercase only; no mod-97 check (rlike can't do it).
    "iban": r"^(AD|AE|AL|AT|AZ|BA|BE|BG|BH|BI|BR|BY|CH|CR|CY|CZ|DE|DJ|DK|DO|EE|EG|ES|FI|FK|FO|FR|GB|GE|GI|GL|GR"
            r"|GT|HR|HU|IE|IL|IQ|IS|IT|JO|KW|KZ|LB|LC|LI|LT|LU|LV|LY|MC|MD|ME|MK|MN|MR|MT|MU|NI|NL|NO|OM|PK|PL"
            r"|PS|PT|QA|RO|RS|RU|SA|SC|SD|SE|SI|SK|SM|SO|ST|SV|TL|TN|TR|UA|VA|VG|XK|YE)\d{2}"
            r"([A-Z0-9]{11,30}|( [A-Z0-9]{4}){2,7}( [A-Z0-9]{1,4})?)$",
    "iso_date": r"^\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?)?$",
    "currency_amount": r"^[£$€]?-?\d{1,3}(,\d{3})+\.\d{1,2}$|^[£$€]?-?\d+\.\d{1,2}$",
}

# Hits on these force sensitivity to at least `pii` (specs/hydration.md).
FORCE_PII: set[str] = {"email", "uk_ni_number", "phone", "postcode", "sort_code", "iban"}

DEFAULT_HIT_THRESHOLD = 0.8  # share of non-null values that must match


def matches(name: str, value: str) -> bool:
    return re.search(PATTERNS[name], value) is not None


def hits_from_rates(rates: dict[str, float], threshold: float = DEFAULT_HIT_THRESHOLD) -> list[str]:
    return sorted(name for name, rate in rates.items() if rate is not None and rate >= threshold)
