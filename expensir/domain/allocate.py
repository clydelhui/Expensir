"""Largest-remainder allocation of whole minor units, rotating tie-break (§7.1, ADR-0008)."""

import hashlib
from fractions import Fraction
from math import floor


def stable_hash(seed: int, user_id: int) -> int:
    """Deterministic across processes and runs — never Python's builtin hash (§7.1)."""
    digest = hashlib.sha256(f"{seed}:{user_id}".encode()).digest()
    return int.from_bytes(digest[:8], "big")


def allocate(total_minor: int, weights: dict[int, int | float], seed: int) -> dict[int, int]:
    """Split total_minor into per-user whole minor-unit shares proportional to weights.

    The leftover units go to the largest fractional remainders; ties rotate
    deterministically per expense via the seed — never systematically the payer.
    """
    if total_minor <= 0:
        raise ValueError("total must be positive")
    if not weights or any(w <= 0 for w in weights.values()):
        raise ValueError("weights must be positive")

    exact = {u: Fraction(str(w)) for u, w in weights.items()}
    total_weight = sum(exact.values())
    raw = {u: total_minor * w / total_weight for u, w in exact.items()}
    base = {u: floor(raw[u]) for u in exact}
    short = total_minor - sum(base.values())

    order = sorted(exact, key=lambda u: (-(raw[u] - base[u]), stable_hash(seed, u)))
    for u in order[:short]:
        base[u] += 1

    assert sum(base.values()) == total_minor
    return base
