"""FX rate resolution (§7.5) — DISPLAY ONLY, never in ledger math (ADR-0001).

Pure reads and row bookkeeping over fx_rates. The network transport (Frankfurter)
lives behind the FxProvider protocol at the handler layer (§0.8); nothing here
performs I/O beyond the session it is handed.

Render paths resolve many pairs at once, so the lookups are BATCHED: one query
for the group's pins (group_pins), one for the EUR legs (api_legs), then pure
in-Python resolution per pair (rate_from_cache) — never a query per pair.
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from expensir.db.models import FxRate, utcnow

Pair = frozenset[str]  # a pin covers the UNORDERED pair (§7.5)


class FxProvider(Protocol):
    """Fetches today's EUR-based rates for the given symbols (§7.5) — Frankfurter
    behind a seam (§0.8). DISPLAY ONLY: nothing on a write path may call this.

    Returns the rates it knows (unsupported symbols simply absent), or None when
    the API is unreachable — callers fall back to the cache and surface its date."""

    async def eur_rates(self, symbols: set[str]) -> dict[str, float] | None: ...


def today_utc() -> date:
    """The calendar day the same-day TTL keys on (§7.5): UTC, always."""
    return utcnow().date()


def fmt_rate(rate: float) -> str:
    """A display rate: up to 6 decimals, trailing zeros trimmed ('1.35', '0.007421')."""
    return f"{rate:.6f}".rstrip("0").rstrip(".") or "0"


def fmt_day(moment: datetime) -> str:
    """'9 Jul' — the stale-date rendering shared by the footers and /rates (§13)."""
    return f"{moment.day} {moment:%b}"


async def find_pin(session: AsyncSession, group_id: int, base: str, quote: str) -> FxRate | None:
    """The group's manual pin on the UNORDERED pair (§7.5), whichever direction
    it was stated in — one row per pair, so at most one can exist."""
    return (await group_pins(session, group_id)).get(frozenset((base, quote)))


async def group_pins(session: AsyncSession, group_id: int) -> dict[Pair, FxRate]:
    """ALL of the group's pins in one query, keyed by unordered pair."""
    rows = (
        (
            await session.execute(
                select(FxRate).where(FxRate.group_id == group_id, FxRate.source == "manual")
            )
        )
        .scalars()
        .all()
    )
    return {frozenset((row.base_currency, row.quote_currency)): row for row in rows}


@dataclass
class ResolvedRate:
    """One pair's current display rate with its provenance, for the ≈ layer (§7.6)
    and the rates footer (§13)."""

    rate: float
    source: str  # 'manual' | 'api'
    fetched_at: datetime  # manual: the pin moment; api: the older leg's fetch
    stale: bool  # api only: not fetched today (UTC) — dated in the footer (§13)


def is_stale(fetched_at: datetime, today: date) -> bool:
    """Same-calendar-day TTL (§7.5): keyed on when WE fetched, in UTC."""
    if fetched_at.tzinfo is None:  # SQLite returns naive datetimes; storage is UTC (§16)
        fetched_at = fetched_at.replace(tzinfo=UTC)
    return fetched_at.astimezone(UTC).date() != today


def rate_from_cache(
    pins: dict[Pair, FxRate], legs: dict[str, FxRate], base: str, quote: str, *, today: date
) -> ResolvedRate | None:
    """FROM→TO from already-fetched rows (§7.5): the pin on the unordered pair
    wins (reciprocal for the reverse direction); else EUR-based API legs,
    triangulated via EUR — never a pin as a leg. None = no rate; callers render
    `(≈ n/a)`, never block."""
    pin = pins.get(frozenset((base, quote)))
    if pin is not None:
        rate = pin.rate if pin.base_currency == base else 1 / pin.rate
        return ResolvedRate(rate=rate, source="manual", fetched_at=pin.fetched_at, stale=False)
    return api_rate_from_legs(legs, base, quote, today=today)


def api_rate_from_legs(
    legs: dict[str, FxRate], base: str, quote: str, *, today: date
) -> ResolvedRate | None:
    """FROM→TO from EUR legs alone — resolution's fallback tier, and /rates'
    live reference beside a pin (§7.6)."""
    if base != "EUR" and base not in legs:
        return None
    if quote != "EUR" and quote not in legs:
        return None
    # EUR→X legs: FROM→TO = (EUR→TO) / (EUR→FROM); an EUR endpoint is the 1.0 leg
    to_leg, from_leg = legs.get(quote), legs.get(base)
    rate = (to_leg.rate if to_leg else 1.0) / (from_leg.rate if from_leg else 1.0)
    fetched = [leg.fetched_at for leg in (to_leg, from_leg) if leg is not None]
    return ResolvedRate(
        rate=rate,
        source="api",
        fetched_at=min(fetched),
        stale=any(is_stale(moment, today) for moment in fetched),
    )


async def resolve_rate(
    session: AsyncSession, group_id: int, base: str, quote: str, *, today: date
) -> ResolvedRate | None:
    """Single-pair convenience over rate_from_cache; render paths batch instead."""
    pins = await group_pins(session, group_id)
    legs = await api_legs(session, {base, quote})
    return rate_from_cache(pins, legs, base, quote, today=today)


async def api_rate(
    session: AsyncSession, base: str, quote: str, *, today: date
) -> ResolvedRate | None:
    """Single-pair convenience over api_rate_from_legs."""
    return api_rate_from_legs(await api_legs(session, {base, quote}), base, quote, today=today)


async def refresh_api_rates(
    session: AsyncSession, provider: FxProvider | None, symbols: set[str], *, today: date
) -> bool:
    """Same-day TTL (§7.5): when any needed EUR leg is missing or stale, refetch
    them all in one call and upsert the deployment-global cache. A failed fetch
    leaves the cache standing — its date surfaces in display.

    Returns whether anything landed: True means displays rendered from the old
    cache (the board) are now outdated — the §13 read-triggered refresh signal."""
    needed = {s for s in symbols if s != "EUR"}
    if not needed:
        return False
    legs = await api_legs(session, needed)
    if all(s in legs and not is_stale(legs[s].fetched_at, today) for s in needed):
        return False
    if provider is None:
        return False
    fetched = await provider.eur_rates(needed)
    if fetched is None:
        return False
    now = utcnow()
    landed = False
    for symbol in needed:
        rate = fetched.get(symbol)
        if rate is None:
            continue  # unsupported: stays missing; callers render (≈ n/a)
        leg = legs.get(symbol)
        if leg is not None:
            leg.rate = rate
            leg.fetched_at = now
            landed = True
            continue
        row = FxRate(
            group_id=None,
            base_currency="EUR",
            quote_currency=symbol,
            rate=rate,
            source="api",
            fetched_at=now,
        )
        try:
            # reads refresh concurrently across groups/processes with no shared
            # lock: the partial unique index (§5) is the backstop, and losing
            # the race is fine — the winner's row is just as fresh
            async with session.begin_nested():
                session.add(row)
                await session.flush()
        except IntegrityError:
            continue
        landed = True
    return landed


async def equivalents_view(
    session: AsyncSession,
    group_id: int,
    provider: FxProvider | None,
    currencies: set[str],
    home: str,
    *,
    today: date,
) -> tuple[dict[str, ResolvedRate | None], bool]:
    """Each non-home currency's rate to home (§7.6), refreshing the API cache
    first per the TTL. Pinned pairs never trigger a fetch (§13). The second
    element is refresh_api_rates' fetched-something signal."""
    pairs = {c for c in currencies if c != home}
    if not pairs:
        return {}, False
    pins = await group_pins(session, group_id)
    unpinned = {c for c in pairs if frozenset((c, home)) not in pins}
    fetched = False
    legs: dict[str, FxRate] = {}
    if unpinned:
        fetched = await refresh_api_rates(session, provider, unpinned | {home}, today=today)
        legs = await api_legs(session, unpinned | {home})
    return {c: rate_from_cache(pins, legs, c, home, today=today) for c in pairs}, fetched


async def api_legs(session: AsyncSession, symbols: set[str]) -> dict[str, FxRate]:
    """The deployment-global EUR-based cache rows for these symbols (§7.5)."""
    rows = (
        (
            await session.execute(
                select(FxRate).where(
                    FxRate.group_id.is_(None),
                    FxRate.source == "api",
                    FxRate.base_currency == "EUR",
                    FxRate.quote_currency.in_(symbols - {"EUR"}),
                )
            )
        )
        .scalars()
        .all()
    )
    return {row.quote_currency: row for row in rows}


async def restore_pin(
    session: AsyncSession, group_id: int, base: str, quote: str, image: dict[str, Any] | None
) -> None:
    """Put the pair's pin back to a recorded state (§8, §9): the before_image on
    undo, the action's own statement on redo. None = the pair unpinned."""
    pin = await find_pin(session, group_id, base, quote)
    if image is None:
        if pin is not None:
            await session.delete(pin)
        return
    if pin is None:
        pin = FxRate(group_id=group_id, source="manual")
        session.add(pin)
    pin.base_currency = image["base"]
    pin.quote_currency = image["quote"]
    pin.rate = image["rate"]
    pin.fetched_at = datetime.fromisoformat(image["fetched_at"])
    pin.set_by = image["set_by"]


def pin_image(pin: FxRate | None) -> dict[str, Any] | None:
    """The minimal before_image of a pin (§8): enough to restore it on undo,
    None when the pair wasn't pinned."""
    if pin is None:
        return None
    return {
        "base": pin.base_currency,
        "quote": pin.quote_currency,
        "rate": pin.rate,
        "fetched_at": pin.fetched_at.isoformat(),
        "set_by": pin.set_by,
    }
