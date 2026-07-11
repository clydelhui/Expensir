"""Convert & equivalents (§7.6) — pure reads, DISPLAY ONLY. Nothing stored,
nothing here ever feeds apply_intent or settlement math (ADR-0001)."""

from collections.abc import Iterable, Mapping
from decimal import Decimal

from expensir.domain.fx import ResolvedRate
from expensir.domain.money import fmt, minor_digits, round_half_up


def equivalent_minor(minor: int, currency: str, rate: float, home: str) -> int:
    """`minor` of `currency` at a display rate, in home-currency minor units.

    Rounds half-up at the home currency's minor unit — at display, never stored
    (§7.6), so per-bucket lines and the total they sum to always agree."""
    major = Decimal(minor) / (10 ** minor_digits(currency))
    return round_half_up(major * Decimal(str(rate)) * (10 ** minor_digits(home)))


def consolidate(
    buckets: Iterable[tuple[str, int]],
    target: str,
    rates: Mapping[str, ResolvedRate | None],
) -> tuple[int, list[str]]:
    """Collapse (currency, minor) buckets into target minor units at the display
    rates. Buckets with no rate never vanish — they come back as rendered
    remainders like 'JPY 100 (≈ n/a)' for the caller to append (§7.6)."""
    total = 0
    remainders: list[str] = []
    for currency, minor in buckets:
        if not minor:
            continue
        rate = rates.get(currency)
        if currency == target:
            total += minor
        elif rate is not None:
            total += equivalent_minor(minor, currency, rate.rate, target)
        else:
            remainders.append(f"{fmt(abs(minor), currency)} (≈ n/a)")
    return total, remainders
