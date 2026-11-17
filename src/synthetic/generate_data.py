"""Seeded synthetic data generator.

Pure Python (no Spark) so it runs and tests anywhere. Loaders turn the output into Delta tables.

    python -m synthetic.generate_data --out build/synthetic --seed 42 --scale 1
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import random
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from importlib import resources
from pathlib import Path
from typing import Any

import yaml
from faker import Faker

from synthetic.fields import TABLES, Col, Table

REGIONS = (("UK", 0.7), ("US", 0.2), ("AU", 0.1))
REGION_CCY = {"UK": "GBP", "US": "USD", "AU": "AUD"}
FX_TO_GBP = {"GBP": 1.0, "USD": 0.79, "EUR": 0.85, "AUD": 0.52}
LOBS = ("Property", "Marine", "Aviation", "Cyber", "Casualty", "Financial Lines", "Energy")
ID_TERMS = {
    "client_identifier", "policy_reference", "claim_identifier", "broker_code",
    "underwriter_identifier", "payment_identifier", "quote_reference",
}
ID_WIDTH = {"client_identifier": 6, "policy_reference": 7, "claim_identifier": 7, "broker_code": 4,
            "underwriter_identifier": 4, "payment_identifier": 8, "quote_reference": 7}

Columns = dict[str, list[Any]]

# All dates are relative to this fixed day so the same seed gives the same data on any day, anywhere.
ANCHOR = date(2026, 11, 1)

# Terms other generators read from the same row, generated first (lower = earlier).
PRIORITY = {"client_type": 0, "region_code": 0, "currency_code": 1, "policy_currency": 1}


@dataclass
class Ctx:
    rng: random.Random
    fake: Faker
    table: Table
    n: int
    pools: dict[str, list[str]]
    row: dict[str, list[Any]] = field(default_factory=dict)  # term -> values already generated

    def choice(self, seq, weights=None, k=None):
        return self.rng.choices(seq, weights=weights, k=k or self.n)

    def has(self, term: str) -> bool:
        return term in self.row


def load_glossary() -> dict:
    text = resources.files("synthetic").joinpath("glossary.yaml").read_text()
    return yaml.safe_load(text)


# ---------------------------------------------------------------- value generators

def _offset_days(spec: str) -> int:
    """'-2y' -> -730, '+60d' -> 60, 'today' -> 0. Relative to ANCHOR, never to the real clock."""
    if spec == "today":
        return 0
    n, unit = int(spec[:-1]), spec[-1]
    return n * 365 if unit == "y" else n


def _days(ctx: Ctx, start: str, end: str) -> list[date]:
    lo, hi = _offset_days(start), _offset_days(end)
    return [ANCHOR + timedelta(days=ctx.rng.randint(lo, hi)) for _ in range(ctx.n)]


def _ts(ctx: Ctx) -> list[datetime]:
    base = datetime.combine(ANCHOR, datetime.min.time())
    return [base - timedelta(seconds=ctx.rng.randint(0, 3 * 365 * 86400)) for _ in range(ctx.n)]


def _dob(ctx: Ctx) -> list[date]:
    return [ANCHOR - timedelta(days=ctx.rng.randint(18 * 365, 85 * 365)) for _ in range(ctx.n)]


def _money(ctx: Ctx, mu: float, sigma: float) -> list[float]:
    return [round(ctx.rng.lognormvariate(mu, sigma), 2) for _ in range(ctx.n)]


def _flag(ctx: Ctx, p: float, yn: bool = False) -> list:
    vals = [ctx.rng.random() < p for _ in range(ctx.n)]
    return ["Y" if v else "N" for v in vals] if yn else vals


def _regions(ctx: Ctx) -> list[str]:
    if ctx.table.region:
        return [ctx.table.region] * ctx.n
    names, weights = zip(*REGIONS)
    return ctx.choice(names, weights)


def _ids(ctx: Ctx, term: str) -> list[str]:
    if ctx.table.pk == term:
        width = ID_WIDTH[term]
        start = 10 ** (width - 1)
        prefix = ctx.table.id_prefix or ""
        ids = [f"{prefix}{start + i}" for i in range(ctx.n)]
        ctx.pools.setdefault(term, ids)  # first table to mint a pool owns it
        return ids
    pool = ctx.pools.get(term)
    if not pool:
        raise ValueError(f"{ctx.table.name}: no pool for {term}; generate its owning table first")
    return ctx.choice(pool)


def _ni(ctx: Ctx) -> list[str]:
    letters = "ABCEGHJKLMNPRSTWXYZ"
    return [
        "".join(ctx.rng.choices(letters, k=2)) + f"{ctx.rng.randint(0, 999999):06d}" + ctx.rng.choice("ABCD")
        for _ in range(ctx.n)
    ]


def _names(ctx: Ctx) -> list[str]:
    types = ctx.row.get("client_type")
    out = []
    for i in range(ctx.n):
        corporate = types is not None and types[i] == "Corporate"
        out.append(ctx.fake.company() if corporate else ctx.fake.name())
    return out


def _expiry(ctx: Ctx) -> list[date]:
    starts = ctx.row.get("policy_inception_date")
    if not starts:
        return _days(ctx, "-2y", "+1y")
    return [s + timedelta(days=365) for s in starts]


def _nwp(ctx: Ctx) -> list[float]:
    gwp = ctx.row.get("gross_written_premium") or _money(ctx, 8.5, 1.0)
    return [round(g * ctx.rng.uniform(0.72, 0.9), 2) for g in gwp]


def _sum_insured(ctx: Ctx) -> list[float]:
    gwp = ctx.row.get("gross_written_premium") or _money(ctx, 8.5, 1.0)
    return [round(g * ctx.rng.uniform(50, 400), -2) for g in gwp]


def _ccy(ctx: Ctx) -> list[str]:
    regions = ctx.row.get("region_code") or _regions(ctx)
    return [REGION_CCY[r] if ctx.rng.random() > 0.1 else "EUR" for r in regions]


def _notified(ctx: Ctx) -> list[date]:
    loss = ctx.row.get("date_of_loss") or _days(ctx, "-2y", "today")
    return [d + timedelta(days=ctx.rng.randint(0, 60)) for d in loss]


def _reserve(ctx: Ctx) -> list[float]:
    amt = ctx.row.get("claim_amount") or _money(ctx, 9.0, 1.3)
    return [round(a * ctx.rng.uniform(0.0, 0.8), 2) for a in amt]


def _paid(ctx: Ctx) -> list[float]:
    amt = ctx.row.get("claim_amount") or _money(ctx, 9.0, 1.3)
    rsv = ctx.row.get("claim_reserve_amount") or [0.0] * ctx.n
    return [round(max(a - r, 0.0), 2) for a, r in zip(amt, rsv)]


def _claim_notes(ctx: Ctx) -> list[str]:
    perils = ("water damage to stock", "fire in the warehouse", "storm damage to roof", "theft of equipment",
              "third-party injury on site", "ransomware incident", "vehicle impact to frontage")
    out = []
    for _ in range(ctx.n):
        peril = ctx.rng.choice(perils)
        if ctx.rng.random() < 0.3:  # planted PII: the reason this column is sensitive
            out.append(f"Insured {ctx.fake.name()} called on {ctx.fake.phone_number()} to report {peril}.")
        else:
            out.append(f"Loss adjuster appointed for {peril}. Awaiting documents.")
    return out


def _settled(ctx: Ctx) -> list[date | None]:
    loss = ctx.row.get("date_of_loss") or _days(ctx, "-2y", "today")
    return [d + timedelta(days=ctx.rng.randint(30, 400)) if ctx.rng.random() < 0.6 else None for d in loss]


def _converted(ctx: Ctx) -> list[str]:
    status = ctx.row.get("quote_status")
    if status:
        return ["Y" if s == "Accepted" else "N" for s in status]
    return _flag(ctx, 0.3, yn=True)


def _fx(ctx: Ctx) -> list[float]:
    ccys = ctx.row.get("currency_code") or ctx.row.get("policy_currency") or ["GBP"] * ctx.n
    return [round(FX_TO_GBP[c] * ctx.rng.uniform(0.98, 1.02), 4) for c in ccys]


def _cat(ctx: Ctx) -> list[str | None]:
    events = ("CAT-STORM-2025-01", "CAT-FLOOD-2024-07", "CAT-WILDFIRE-2025-03", "CAT-HAIL-2024-05")
    return [ctx.rng.choice(events) if ctx.rng.random() < 0.1 else None for _ in range(ctx.n)]


GEN: dict[str, Callable[[Ctx], list]] = {
    "client_full_name": _names,
    "date_of_birth": _dob,
    "national_insurance_number": _ni,
    "social_security_number": lambda c: [c.fake.ssn() for _ in range(c.n)],
    "tax_file_number": lambda c: [f"{c.rng.randint(100, 999)} {c.rng.randint(100, 999)} {c.rng.randint(100, 999)}"
                                  for _ in range(c.n)],
    "email_address": lambda c: [c.fake.email() for _ in range(c.n)],
    "phone_number": lambda c: [c.fake.phone_number() for _ in range(c.n)],
    "postal_address": lambda c: [c.fake.street_address().replace("\n", ", ") for _ in range(c.n)],
    "postcode": lambda c: [c.fake.postcode() for _ in range(c.n)],
    "region_code": _regions,
    "client_type": lambda c: c.choice(("Individual", "Corporate"), (0.75, 0.25)),
    "client_since_date": lambda c: _days(c, "-25y", "today"),
    "marketing_consent_flag": lambda c: _flag(c, 0.55),
    "vulnerable_customer_flag": lambda c: _flag(c, 0.05, yn=True),
    "occupation": lambda c: [c.fake.job() for _ in range(c.n)],
    "annual_revenue": lambda c: _money(c, 15.0, 1.5),
    "employee_count": lambda c: [int(c.rng.lognormvariate(3.5, 1.4)) + 1 for _ in range(c.n)],
    "industry_sector_code": lambda c: [f"{c.rng.randint(10000, 99999)}" for _ in range(c.n)],
    "broker_name": lambda c: [f"{c.fake.last_name()} {c.rng.choice(('Insurance Brokers', 'Risk Partners', 'Broking'))}"
                              for _ in range(c.n)],
    "fca_firm_reference": lambda c: [f"{c.rng.randint(100000, 999999)}" for _ in range(c.n)],
    "broker_contact_email": lambda c: [c.fake.email() for _ in range(c.n)],
    "broker_tier": lambda c: c.choice(("Gold", "Silver", "Bronze"), (0.2, 0.4, 0.4)),
    "broker_onboarding_date": lambda c: _days(c, "-15y", "today"),
    "underwriter_name": lambda c: [c.fake.name() for _ in range(c.n)],
    "underwriting_authority_limit": lambda c: c.choice((250_000, 500_000, 1_000_000, 5_000_000, 10_000_000)),
    "underwriting_team": lambda c: c.choice(("Property", "Marine & Energy", "Cyber", "Casualty",
                                             "Financial Lines", "Specialty")),
    "policy_line_of_business": lambda c: c.choice(LOBS),
    "policy_inception_date": lambda c: _days(c, "-3y", "+60d"),
    "policy_expiry_date": _expiry,
    "gross_written_premium": lambda c: _money(c, 8.5, 1.0),
    "net_written_premium": _nwp,
    "policy_status": lambda c: c.choice(("Live", "Expired", "Cancelled", "Lapsed"), (0.6, 0.3, 0.05, 0.05)),
    "policy_currency": _ccy,
    "currency_code": _ccy,
    "sum_insured": _sum_insured,
    "deductible_amount": lambda c: c.choice((250, 500, 1000, 2500, 5000, 10000, 25000)),
    "commission_rate": lambda c: [round(c.rng.uniform(7.5, 25.0), 1) for _ in range(c.n)],
    "coverage_territory": lambda c: c.choice(("UK only", "Europe", "Worldwide excl USA/Canada", "Worldwide",
                                              "USA/Canada", "Australasia")),
    "record_created_timestamp": _ts,
    "record_updated_timestamp": _ts,
    "date_of_loss": lambda c: _days(c, "-2y", "today"),
    "claim_notification_date": _notified,
    "claim_amount": lambda c: _money(c, 9.0, 1.3),
    "claim_reserve_amount": _reserve,
    "claim_paid_amount": _paid,
    "claim_status": lambda c: c.choice(("Open", "Closed", "Reopened", "Declined"), (0.45, 0.45, 0.05, 0.05)),
    "cause_of_loss": lambda c: c.choice(("FLD", "FIRE", "THFT", "STRM", "LIAB", "CYBR", "IMPCT", "EOW")),
    "claim_handler_identifier": lambda c: [f"H{c.rng.randint(100, 999)}" for _ in range(c.n)],
    "claim_description": _claim_notes,
    "loss_location": lambda c: [f"{c.fake.street_address().replace(chr(10), ', ')}, {c.fake.city()}"
                                for _ in range(c.n)],
    "litigation_flag": lambda c: _flag(c, 0.08),
    "catastrophe_code": _cat,
    "fraud_indicator": lambda c: _flag(c, 0.03, yn=True),
    "subrogation_amount": lambda c: [round(c.rng.uniform(500, 50000), 2) if c.rng.random() < 0.15 else 0.0
                                     for _ in range(c.n)],
    "claim_settlement_date": _settled,
    "sort_code": lambda c: [f"{c.rng.randint(10, 99)}-{c.rng.randint(10, 99)}-{c.rng.randint(10, 99)}"
                            for _ in range(c.n)],
    "bank_account_last_four": lambda c: [f"{c.rng.randint(0, 9999):04d}" for _ in range(c.n)],
    "payment_amount": lambda c: _money(c, 7.0, 1.1),
    "payment_date": lambda c: _days(c, "-2y", "today"),
    "payment_method": lambda c: c.choice(("BACS", "Card", "Direct Debit", "CHAPS")),
    "payment_status": lambda c: c.choice(("Settled", "Pending", "Failed", "Reversed"), (0.85, 0.08, 0.04, 0.03)),
    "invoice_number": lambda c: [f"INV{c.rng.randint(1_000_000, 9_999_999)}" for _ in range(c.n)],
    "exchange_rate": _fx,
    "quote_date": lambda c: _days(c, "-2y", "today"),
    "quoted_premium": lambda c: _money(c, 8.3, 1.0),
    "quote_status": lambda c: c.choice(("Issued", "Accepted", "Declined", "Expired", "NTU"),
                                       (0.25, 0.3, 0.15, 0.2, 0.1)),
    "conversion_flag": _converted,
    "risk_score": lambda c: [c.rng.randint(1, 100) for _ in range(c.n)],
    "referral_flag": lambda c: _flag(c, 0.12),
    "source_system_code": lambda c: c.choice(("BRKSYS", "QUOTEX", "LEGACY_UW", "PORTAL")),
}

NO_MATCH: dict[str, Callable[[Ctx], list]] = {
    "batch_id": lambda c: [20260000 + i // 500 for i in range(c.n)],
    "blank": lambda c: [None] * c.n,
    "yn_random": lambda c: c.choice(("Y", "N")),
    "hex8": lambda c: [f"{c.rng.getrandbits(32):08x}" for _ in range(c.n)],
    "int_noise": lambda c: [c.rng.randint(0, 9999) for _ in range(c.n)],
    "sha": lambda c: [hashlib.sha256(str(c.rng.random()).encode()).hexdigest() for _ in range(c.n)],
    "words": lambda c: [" ".join(c.fake.words(3)) for _ in range(c.n)],
    "mostly_null": lambda c: [c.fake.word() if c.rng.random() < 0.05 else None for _ in range(c.n)],
    "state": lambda c: [c.fake.state_abbr() for _ in range(c.n)],
    "alnum": lambda c: [c.fake.bothify("??-#####").upper() for _ in range(c.n)],
    "float_noise": lambda c: [round(c.rng.uniform(-1, 1), 6) for _ in range(c.n)],
}


# ---------------------------------------------------------------- table assembly

def _column(ctx: Ctx, col: Col) -> list:
    if col.term is None:
        return NO_MATCH[col.kind](ctx)
    if col.term in ID_TERMS:
        return _ids(ctx, col.term)
    if col.term not in GEN:
        raise KeyError(f"No generator for term {col.term}")
    return GEN[col.term](ctx)


def generate(seed: int = 42, scale: float = 1.0) -> dict[str, Columns]:
    """Return {table_name: {column_name: values}} for every table in TABLES."""
    rng = random.Random(seed)
    pools: dict[str, list[str]] = {}
    out: dict[str, Columns] = {}
    for i, table in enumerate(TABLES):
        fake = Faker(table.locale)
        fake.seed_instance(seed + i)
        n = max(1, int(table.rows * scale))
        ctx = Ctx(rng=random.Random(rng.random()), fake=fake, table=table, n=n, pools=pools)
        # Generate dependency-bearing terms first so others can read them (e.g. expiry from inception).
        order = sorted(table.cols, key=lambda c: PRIORITY.get(c.term, 9))
        cols: Columns = {}
        for col in order:
            values = _column(ctx, col)
            cols[col.name] = values
            if col.term:
                ctx.row.setdefault(col.term, values)
        out[table.name] = {c.name: cols[c.name] for c in table.cols}
    return out


def golden_set(glossary: dict | None = None) -> list[dict]:
    glossary = glossary or load_glossary()
    sens = {t["term_id"]: t["sensitivity"] for t in glossary["terms"]}
    rows = []
    for table in TABLES:
        for col in table.cols:
            rows.append({
                "table_name": table.name,
                "column_name": col.name,
                "expected_term_id": col.term,
                "expected_sensitivity": sens[col.term] if col.term else (col.sens or "internal"),
                "difficulty": col.difficulty,
            })
    return rows


def user_attributes() -> list[dict]:
    """Persona attributes for row filters. Principals are persona group names until service principals exist."""
    return [
        {"principal": "gs_analyst_uk", "region": "UK", "line_of_business": None},
        {"principal": "gs_analyst_global", "region": "ALL", "line_of_business": None},
        {"principal": "gs_claims_handler", "region": "UK", "line_of_business": None},
        {"principal": "gs_data_owner", "region": "ALL", "line_of_business": None},
        {"principal": "gs_no_access", "region": "NONE", "line_of_business": None},
    ]


def write_csv(data: dict[str, Columns], out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    for name, cols in data.items():
        names = list(cols)
        with open(out_dir / f"{name}.csv", "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(names)
            w.writerows(zip(*cols.values()))
    gs = golden_set()
    with open(out_dir / "golden_set.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(gs[0]))
        w.writeheader()
        w.writerows(gs)


def main() -> None:
    p = argparse.ArgumentParser(description="Generate synthetic governance-gate data as CSV")
    p.add_argument("--out", default="build/synthetic")
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--scale", type=float, default=1.0)
    a = p.parse_args()
    data = generate(a.seed, a.scale)
    write_csv(data, Path(a.out))
    total = sum(len(next(iter(c.values()))) for c in data.values())
    print(f"Wrote {len(data)} tables, {total} rows, golden set {len(golden_set())} columns to {a.out}")


if __name__ == "__main__":
    main()
