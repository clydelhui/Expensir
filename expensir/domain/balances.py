"""Balance replay (§7.2): derived, never stored. The net is a sum of deltas,
so it is order-independent — occurred_on and back-dating never change it (§0.4).
"""

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from expensir.db.models import Expense, ExpenseSplit, Settlement


@dataclass(frozen=True)
class ExpenseEvent:
    payer_id: int
    currency: str
    amount_minor: int
    shares: tuple[tuple[int, int], ...]  # (user_id, owed_minor)


@dataclass(frozen=True)
class SettlementEvent:
    """A recorded stated payment (ADR-0002): absorbed as-is, never policed."""

    from_user: int
    to_user: int
    currency: str
    amount_minor: int


def replay(events: Iterable[ExpenseEvent | SettlementEvent]) -> dict[int, dict[str, int]]:
    """net[user][currency] in minor units; positive = owes the pool (§7.2)."""
    net: dict[int, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for event in events:
        if isinstance(event, ExpenseEvent):
            net[event.payer_id][event.currency] -= event.amount_minor
            for user_id, owed_minor in event.shares:
                net[user_id][event.currency] += owed_minor
        else:
            net[event.from_user][event.currency] -= event.amount_minor
            net[event.to_user][event.currency] += event.amount_minor
    return {user: dict(by_currency) for user, by_currency in net.items()}


async def net_positions(session: AsyncSession, ledger_id: int) -> dict[int, dict[str, int]]:
    """Replay one ledger's non-deleted expenses + settlements; sealed — other
    ledgers never leak in."""
    expenses = (
        (
            await session.execute(
                select(Expense).where(Expense.ledger_id == ledger_id, Expense.deleted_at.is_(None))
            )
        )
        .scalars()
        .all()
    )
    settlements = (
        (
            await session.execute(
                select(Settlement).where(
                    Settlement.ledger_id == ledger_id, Settlement.deleted_at.is_(None)
                )
            )
        )
        .scalars()
        .all()
    )
    if not expenses and not settlements:
        return {}
    shares_by_expense: dict[int, list[tuple[int, int]]] = defaultdict(list)
    if expenses:
        # join re-applies the seal + deleted_at predicates rather than an IN over ids:
        # an id list past ~32k would exceed asyncpg's bind-parameter cap
        splits = (
            (
                await session.execute(
                    select(ExpenseSplit)
                    .join(Expense, ExpenseSplit.expense_id == Expense.id)
                    .where(Expense.ledger_id == ledger_id, Expense.deleted_at.is_(None))
                )
            )
            .scalars()
            .all()
        )
        for split in splits:
            shares_by_expense[split.expense_id].append((split.user_id, split.owed_minor))
    events: list[ExpenseEvent | SettlementEvent] = [
        ExpenseEvent(e.payer_id, e.currency, e.amount_minor, tuple(shares_by_expense[e.id]))
        for e in expenses
    ]
    events.extend(
        SettlementEvent(s.from_user, s.to_user, s.currency, s.amount_minor) for s in settlements
    )
    return replay(events)
