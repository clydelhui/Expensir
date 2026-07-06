"""Ledger lookups shared by the lifecycle ops (§8, ADR-0004). Pure queries, no writes."""

import re

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from expensir.db.models import Expense, Ledger
from expensir.domain.errors import Rejection

_ID_REF = re.compile(r"^#?(\d+)$")


async def ledgers_of(session: AsyncSession, group_id: int) -> list[Ledger]:
    """Every ledger of the group, in creation order."""
    return list(
        (
            await session.execute(
                select(Ledger).where(Ledger.group_id == group_id).order_by(Ledger.id)
            )
        ).scalars()
    )


async def has_transactions(session: AsyncSession, ledger_id: int) -> bool:
    """Any non-deleted expense? Gates undo of new_ledger (ADR-0004).
    Settlements join this check in their slice."""
    expense = (
        await session.execute(
            select(Expense.id)
            .where(Expense.ledger_id == ledger_id, Expense.deleted_at.is_(None))
            .limit(1)
        )
    ).scalar_one_or_none()
    return expense is not None


async def most_recent_open(
    session: AsyncSession, group_id: int, exclude_id: int | None = None
) -> Ledger | None:
    """The most-recently-created open ledger — the deterministic repoint target (ADR-0004)."""
    query = (
        select(Ledger)
        .where(Ledger.group_id == group_id, Ledger.status == "open")
        .order_by(Ledger.created_at.desc(), Ledger.id.desc())
    )
    if exclude_id is not None:
        query = query.where(Ledger.id != exclude_id)
    return (await session.execute(query.limit(1))).scalar_one_or_none()


async def find_ledger(session: AsyncSession, group_id: int, name_or_id: str) -> Ledger:
    """Resolve a /switch-style reference: '#3'/'3' by id, else case-insensitive name.

    Unknown or ambiguous references reject the whole intent (§0.9's spirit): never guess.
    """
    ledgers = await ledgers_of(session, group_id)
    id_match = _ID_REF.match(name_or_id.strip())
    if id_match is not None:
        ledger_id = int(id_match.group(1))
        for ledger in ledgers:
            if ledger.id == ledger_id:
                return ledger
    wanted = name_or_id.strip().lower()
    matches = [ledger for ledger in ledgers if ledger.name.lower() == wanted]
    if len(matches) > 1:
        raise Rejection(
            f"🤔 More than one ledger here is called {name_or_id.strip()} — "
            "use its id from /ledgers instead."
        )
    if not matches:
        raise Rejection(f"🚫 No ledger called {name_or_id.strip()} — /ledgers to see them.")
    return matches[0]
