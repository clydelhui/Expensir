"""Balance replay (§7.2): derived, never stored. The net is a sum of deltas,
so it is order-independent — occurred_on and back-dating never change it (§0.4).

Settlements join the replay in their slice as another event type.
"""

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from expensir.db.models import Expense, ExpenseSplit


@dataclass(frozen=True)
class ExpenseEvent:
    payer_id: int
    currency: str
    amount_minor: int
    shares: tuple[tuple[int, int], ...]  # (user_id, owed_minor)


def replay(events: Iterable[ExpenseEvent]) -> dict[int, dict[str, int]]:
    """net[user][currency] in minor units; positive = owes the pool (§7.2)."""
    net: dict[int, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for event in events:
        net[event.payer_id][event.currency] -= event.amount_minor
        for user_id, owed_minor in event.shares:
            net[user_id][event.currency] += owed_minor
    return {user: dict(by_currency) for user, by_currency in net.items()}


async def net_positions(session: AsyncSession, ledger_id: int) -> dict[int, dict[str, int]]:
    """Replay one ledger's non-deleted expenses; sealed — other ledgers never leak in."""
    expenses = (
        (
            await session.execute(
                select(Expense).where(Expense.ledger_id == ledger_id, Expense.deleted_at.is_(None))
            )
        )
        .scalars()
        .all()
    )
    if not expenses:
        return {}
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
    shares_by_expense: dict[int, list[tuple[int, int]]] = defaultdict(list)
    for split in splits:
        shares_by_expense[split.expense_id].append((split.user_id, split.owed_minor))
    return replay(
        ExpenseEvent(e.payer_id, e.currency, e.amount_minor, tuple(shares_by_expense[e.id]))
        for e in expenses
    )
