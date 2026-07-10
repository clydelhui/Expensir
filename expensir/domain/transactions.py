"""The merged transaction stream (ADR-0012): expenses + settlements in one
total order — `created_at DESC`, tiebroken `(kind, id)` — shared by the
listing and the feed. `occurred_on` is display-only (§7.2), never ordering."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import ClassVar, Literal, cast

from sqlalchemy import ColumnElement, and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from expensir.db.models import Expense, ExpenseSplit, Settlement
from expensir.domain.identity import display_names

TransactionKind = Literal["expense", "settlement"]
Direction = Literal["older", "newer"]

PAGE_SIZE = 10

# the two row types behind the umbrella, in tiebreak order: 'expense' < 'settlement'
_TABLES: tuple[tuple[type[Expense] | type[Settlement], TransactionKind], ...] = (
    (Expense, "expense"),
    (Settlement, "settlement"),
)


@dataclass(frozen=True)
class ExpenseRow:
    """An expense as both surfaces render it — already carrying names and
    counts so the format layer never goes back to the database."""

    kind: ClassVar[TransactionKind] = "expense"
    id: int
    amount_minor: int
    currency: str
    created_at: datetime
    created_by_action_id: int  # the feed's per-row Undo button (ADR-0013)
    description: str
    payer_name: str
    participant_count: int
    occurred_on: str | None  # display only (§7.2)
    edited: bool


@dataclass(frozen=True)
class SettlementRow:
    """A settlement as both surfaces render it."""

    kind: ClassVar[TransactionKind] = "settlement"
    id: int
    amount_minor: int
    currency: str
    created_at: datetime
    created_by_action_id: int  # the feed's per-row Undo button (ADR-0013)
    from_name: str
    to_name: str


# a tagged union, like Intent (intents/schema.py): each kind carries only its
# own fields, so the formatter dispatches on type instead of trusting optionals
TransactionRow = ExpenseRow | SettlementRow


@dataclass(frozen=True)
class TransactionCursor:
    """A keyset anchor: the edge row's position in the total order (ADR-0012).
    Anchors survive deletion of the anchor row — the predicates never need it."""

    created_at: datetime
    kind: TransactionKind
    id: int


_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def encode_cursor(anchor: TransactionCursor) -> str:
    """The cursor's wire fragment, `<epoch_us>:<kind>:<row_id>` (ADR-0012).

    Integer arithmetic against the UTC epoch — never .timestamp(), which would
    read the naive datetimes SQLite hands back as *local* time and bake a
    tz-shifted anchor into the callback_data."""
    created_at = anchor.created_at
    if created_at.tzinfo is None:  # SQLite returns DateTime(timezone=True) naive, in UTC
        created_at = created_at.replace(tzinfo=UTC)
    epoch_us = (created_at - _EPOCH) // timedelta(microseconds=1)
    return f"{epoch_us}:{anchor.kind}:{anchor.id}"


def decode_cursor(fragment: str) -> TransactionCursor:
    """Inverse of encode_cursor, back to an aware-UTC anchor; ValueError on a
    malformed fragment."""
    epoch_us, kind, row_id = fragment.split(":")
    if kind not in ("expense", "settlement"):
        raise ValueError(f"unknown transaction kind: {kind!r}")
    try:
        created_at = _EPOCH + timedelta(microseconds=int(epoch_us))
    except OverflowError as error:
        # a forged epoch beyond datetime's range is just another malformed
        # fragment: keep the documented contract for every caller
        raise ValueError(f"epoch_us out of range: {epoch_us}") from error
    return TransactionCursor(
        created_at=created_at,
        kind=cast("TransactionKind", kind),
        id=int(row_id),
    )


@dataclass(frozen=True)
class TransactionPage:
    rows: list[TransactionRow]  # in stream order: newest first
    has_newer: bool
    has_older: bool
    total: int  # standing transactions in the whole ledger, not this page


async def list_transactions(
    session: AsyncSession,
    ledger_id: int,
    *,
    limit: int,
    cursor: TransactionCursor | None = None,
    direction: Direction = "older",
) -> TransactionPage:
    """One page of a ledger's standing transactions, merged and totally
    ordered; sealed — other ledgers never leak in (§0.10).

    No cursor: the newest `limit`. direction="older": the `limit` rows strictly
    older than the anchor. direction="newer": the `limit` rows strictly newer,
    nearest the anchor (ascending, reversed back into stream order)."""
    reverse_stream = cursor is not None and direction == "newer"
    merged: list[tuple[TransactionKind, Expense | Settlement]] = []
    for model, kind in _TABLES:
        stmt = select(model).where(standing(model, ledger_id))
        if cursor is not None:
            stmt = stmt.where(_beyond(model, kind, cursor, direction))
        if reverse_stream:
            # nearest-the-anchor first, so the limit trims the far end
            stmt = stmt.order_by(model.created_at.asc(), model.id.desc())
        else:
            stmt = stmt.order_by(model.created_at.desc(), model.id)
        rows = (await session.execute(stmt.limit(limit + 1))).scalars().all()
        # select() over the union loses the concrete row type; each batch is its table's
        merged.extend((kind, cast("Expense | Settlement", row)) for row in rows)

    # stream order: created_at DESC, then (kind, id) ASC — two stable sorts
    merged.sort(key=lambda pair: (pair[0], pair[1].id))
    merged.sort(key=lambda pair: pair[1].created_at, reverse=True)
    # the limit rows nearest the anchor: the top going older, the bottom going newer
    page = merged[-limit:] if reverse_stream else merged[:limit]

    # the limit+1 over-fetch already proves has-more on the fetch side; only the
    # anchor side needs a probe — and page 1 has no anchor side at all
    more_fetched = len(merged) > limit
    if cursor is None:
        has_newer, has_older = False, more_fetched
    elif reverse_stream:
        has_newer = more_fetched
        has_older = await _exists_beyond(
            session, ledger_id, _edge_cursor(page, -1, cursor), "older"
        )
    else:
        has_newer = await _exists_beyond(session, ledger_id, _edge_cursor(page, 0, cursor), "newer")
        has_older = more_fetched
    return TransactionPage(
        rows=await _enrich(session, page),
        has_newer=has_newer,
        has_older=has_older,
        total=await _standing_count(session, ledger_id),
    )


def standing(model: type[Expense] | type[Settlement], ledger_id: int) -> ColumnElement[bool]:
    """A standing transaction (CONTEXT.md): in this ledger, not soft-deleted."""
    return and_(model.ledger_id == ledger_id, model.deleted_at.is_(None))


def _edge_cursor(
    page: list[tuple[TransactionKind, Expense | Settlement]],
    index: int,
    fallback: TransactionCursor | None,
) -> TransactionCursor | None:
    """The position the has_newer/has_older probes measure from: the page's edge
    row, or — when the page came back empty — the anchor itself."""
    if not page:
        return fallback
    kind, row = page[index]
    return TransactionCursor(created_at=row.created_at, kind=kind, id=row.id)


def _beyond(
    model: type[Expense] | type[Settlement],
    kind: TransactionKind,
    anchor: TransactionCursor,
    side: Direction,
) -> ColumnElement[bool]:
    """Rows strictly past `anchor` on one side of the total order — created_at
    DESC then (kind, id) ASC, so 'expense' < 'settlement' lexically is exactly
    the tiebreak's kind order."""
    if side == "older":
        if kind == anchor.kind:
            return or_(
                model.created_at < anchor.created_at,
                and_(model.created_at == anchor.created_at, model.id > anchor.id),
            )
        if kind > anchor.kind:  # this table sorts after the anchor's kind on ties
            return model.created_at <= anchor.created_at
        return model.created_at < anchor.created_at
    if kind == anchor.kind:
        return or_(
            model.created_at > anchor.created_at,
            and_(model.created_at == anchor.created_at, model.id < anchor.id),
        )
    if kind < anchor.kind:  # this table sorts before the anchor's kind on ties
        return model.created_at >= anchor.created_at
    return model.created_at > anchor.created_at


async def _exists_beyond(
    session: AsyncSession, ledger_id: int, ref: TransactionCursor | None, side: Direction
) -> bool:
    if ref is None:
        return False
    for model, kind in _TABLES:
        found = await session.scalar(
            select(model.id)
            .where(standing(model, ledger_id), _beyond(model, kind, ref, side))
            .limit(1)
        )
        if found is not None:
            return True
    return False


async def _standing_count(session: AsyncSession, ledger_id: int) -> int:
    total = 0
    for model, _ in _TABLES:
        count = await session.scalar(
            select(func.count()).select_from(model).where(standing(model, ledger_id))
        )
        total += count or 0
    return total


async def _enrich(
    session: AsyncSession, page: list[tuple[TransactionKind, Expense | Settlement]]
) -> list[TransactionRow]:
    """Attach the display names and participant counts the two-line render needs."""
    if not page:
        return []  # nothing to name: skip the queries entirely
    user_ids: set[int] = set()
    expense_ids: list[int] = []
    for kind, row in page:
        if kind == "expense":
            assert isinstance(row, Expense)
            user_ids.add(row.payer_id)
            expense_ids.append(row.id)
        else:
            assert isinstance(row, Settlement)
            user_ids.update((row.from_user, row.to_user))
    names = await display_names(session, list(user_ids))
    counts: dict[int, int] = {}
    if expense_ids:
        counted = await session.execute(
            select(ExpenseSplit.expense_id, func.count())
            .where(ExpenseSplit.expense_id.in_(expense_ids))
            .group_by(ExpenseSplit.expense_id)
        )
        counts = {expense_id: count for expense_id, count in counted.tuples()}

    rows: list[TransactionRow] = []
    for kind, row in page:
        if kind == "expense":
            assert isinstance(row, Expense)
            rows.append(
                ExpenseRow(
                    id=row.id,
                    amount_minor=row.amount_minor,
                    currency=row.currency,
                    created_at=row.created_at,
                    created_by_action_id=row.created_by_action_id,
                    description=row.description,
                    payer_name=names[row.payer_id],
                    participant_count=counts.get(row.id, 0),
                    occurred_on=row.occurred_on,
                    edited=row.edited_at is not None,
                )
            )
        else:
            assert isinstance(row, Settlement)
            rows.append(
                SettlementRow(
                    id=row.id,
                    amount_minor=row.amount_minor,
                    currency=row.currency,
                    created_at=row.created_at,
                    created_by_action_id=row.created_by_action_id,
                    from_name=names[row.from_user],
                    to_name=names[row.to_user],
                )
            )
    return rows
