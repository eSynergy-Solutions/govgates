"""Synthetic consumption stubs (specs/consumption.md Config seams): an architecture-pattern list and permitted uses
for one product, until the client supplies its pattern list and hydration approval captures permitted uses
(Decision 4). Loaded insert-only: an existing row is never updated, and a product that already has permitted-use
rows gets no seed rows, so the seed never overwrites what an owner set.
"""

from __future__ import annotations

PATTERNS = [
    # pattern_id, use_mode, delivery, access_mode, allowed, reason
    ("pat_analytical_batch_system", "analytical", "batch", "system", True, "Batch analytics by a system principal"),
    ("pat_analytical_batch_proxy", "analytical", "batch", "proxy", True, "Batch analytics on behalf of a user"),
    ("pat_operational_stream_system", "operational", "stream", "system", False,
     "Streaming raw records into operational systems is not an allowed pattern"),
]

# use_code, approval, conditions, max_term_days
CLAIMS_USES = [
    ("reporting", "standing", None, 365),
    ("ml_training", "approval_required", "Model cards must list this product as a source", 180),
    ("external_share", "prohibited", None, None),
]


def pattern_rows() -> list[dict]:
    return [{"pattern_id": pid, "use_mode": um, "delivery": d, "access_mode": am, "allowed": ok, "reason": why,
             "version": 1, "active": True} for pid, um, d, am, ok, why in PATTERNS]


def permitted_use_rows(product: str, set_by: str, set_at) -> list[dict]:
    return [{"product": product, "use_code": code, "approval": approval, "conditions": cond,
             "max_term_days": term, "set_by": set_by, "set_at": set_at, "version": 1, "active": True}
            for code, approval, cond, term in CLAIMS_USES]
