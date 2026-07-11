"""The pinned board text (§13): a stateless projection of the simplified balances.

Each line is one suggested transfer with a WYSIWYG [Settle] button (ADR-0006),
its `≈ home` equivalent when the currency isn't the group's home, a total `≈`
line, and the rates footer naming the rate behind each pair in play.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from expensir.domain.convert import consolidate, equivalent_minor
from expensir.domain.fx import ResolvedRate, fmt_day, fmt_rate
from expensir.domain.money import fmt


@dataclass(frozen=True)
class BoardLine:
    """One suggested transfer, in simplify's stable order (§7.4)."""

    from_id: int
    to_id: int
    from_name: str
    to_name: str
    amount_minor: int
    currency: str


def equivalent_suffix(
    minor: int, currency: str, home: str | None, rates: Mapping[str, ResolvedRate | None] | None
) -> str:
    """' (≈ SGD 14.60)' for a non-home bucket amount; ' (≈ n/a)' when no rate (§7.6)."""
    if home is None or currency == home:
        return ""
    rate = (rates or {}).get(currency)
    if rate is None:
        return " (≈ n/a)"
    return f" (≈ {fmt(equivalent_minor(abs(minor), currency, rate.rate, home), home)})"


def rates_footer(home: str, rates: Mapping[str, ResolvedRate | None]) -> str | None:
    """The board/balance footer (§13): the rate behind each pair in play — pins
    marked, API rates dated only when stale (the §7.5 stale-surfacing)."""
    parts = []
    for currency in sorted(rates):
        rate = rates[currency]
        if rate is None:
            continue
        if rate.source == "manual":
            label = "pinned"
        elif rate.stale:
            label = f"ECB, {fmt_day(rate.fetched_at)}"
        else:
            label = "ECB"
        parts.append(f"1 {currency} = {fmt_rate(rate.rate)} {home} ({label})")
    return "≈ " + " · ".join(parts) if parts else None


def board_text(
    *,
    ledger_name: str,
    transfers: list[BoardLine],
    home: str | None = None,  # group home currency; None -> no ≈ layer (§7.6)
    rates: Mapping[str, ResolvedRate | None] | None = None,  # currency -> rate to home
) -> str:
    header = f"📒 {ledger_name} • Board"
    if not transfers:
        return f"{header}\nAll settled up."
    lines = [
        f"{t.from_name} → {t.to_name} {fmt(t.amount_minor, t.currency)}"
        + equivalent_suffix(t.amount_minor, t.currency, home, rates)
        for t in transfers
    ]
    if home is not None and any(t.currency != home for t in transfers):
        total = outstanding_line(
            ((t.currency, t.amount_minor) for t in transfers), home, rates or {}
        )
        if total is not None:
            lines.append(total)
        footer = rates_footer(home, rates or {})
        if footer is not None:
            lines.append(footer)
    return "\n".join([header, *lines])


def outstanding_line(
    buckets: Iterable[tuple[str, int]], home: str, rates: Mapping[str, ResolvedRate | None]
) -> str | None:
    """The total `≈ home` line the board AND balance share (§7.6): what's
    outstanding overall; buckets with no rate stay behind as explicit remainders."""
    total, remainders = consolidate(buckets, home, rates)
    if total == 0:
        return None  # nothing converted: a total would only restate the (≈ n/a)s
    remainder = "".join(f" + {r}" for r in remainders)
    return f"Total outstanding ≈ {fmt(total, home)}{remainder}"
