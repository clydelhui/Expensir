"""Same-currency settlement recording (§7.3, ADR-0002, ADR-0006, ADR-0007).

Every settlement — board tap or custom /settle — lands here: exactly one
currency, one direction, one settlements row, one actions row, individually
undoable. Callers validate and pick the ledger; this module only writes.
"""

from collections import defaultdict
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from expensir.db.models import Action, Settlement, User
from expensir.domain.balances import net_positions
from expensir.domain.simplify import simplify
from expensir.intents.schema import SettleUp


@dataclass
class RecordedSettlement:
    action_id: int
    settlement: Settlement


async def record_settlement(
    session: AsyncSession,
    *,
    ledger_id: int,
    actor: User,
    payer: User,
    receiver: User,
    intent: SettleUp,
) -> RecordedSettlement:
    """Append the one actions row + the one settlements row (ADR-0007).

    The actor is the recorder, not necessarily a party — any member may record
    any settlement; the action row audits who (ADR-0002).
    """
    assert intent.amount_minor is not None and intent.currency is not None
    action = Action(
        ledger_id=ledger_id,
        actor_user_id=actor.id,
        kind=intent.kind,
        intent_json=intent.model_dump(mode="json"),
        before_image=None,
    )
    session.add(action)
    await session.flush()
    settlement = Settlement(
        ledger_id=ledger_id,
        from_user=payer.id,
        to_user=receiver.id,
        amount_minor=intent.amount_minor,
        currency=intent.currency,
        created_by_action_id=action.id,
    )
    session.add(settlement)
    await session.flush()
    return RecordedSettlement(action_id=action.id, settlement=settlement)


async def overpayment_credits(
    session: AsyncSession, ledger_id: int, currency: str
) -> dict[int, int]:
    """user_id -> the part of a member's credit their settlement payments explain (§9).

    A member is "owed" when their net is negative; only the portion covered by
    what they net-paid in standing settlements counts as an overpayment credit —
    being owed for expenses one fronted is not a credit.
    """
    net = await net_positions(session, ledger_id)
    paid_net: dict[int, int] = defaultdict(int)
    settlements = (
        (
            await session.execute(
                select(Settlement).where(
                    Settlement.ledger_id == ledger_id,
                    Settlement.currency == currency,
                    Settlement.deleted_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    for settlement in settlements:
        paid_net[settlement.from_user] += settlement.amount_minor
        paid_net[settlement.to_user] -= settlement.amount_minor
    credits: dict[int, int] = {}
    for user_id, paid in paid_net.items():
        owed = -net.get(user_id, {}).get(currency, 0)
        credit = min(owed, paid)
        if credit > 0:
            credits[user_id] = credit
    return credits


def suggested_amount(net_ccy: dict[int, int], from_user: int, to_user: int) -> int | None:
    """The current solver-suggested from→to transfer for one currency (ADR-0006).

    None means the line is gone — simplify no longer proposes that pair/direction.
    """
    for debtor, creditor, minor in simplify(net_ccy):
        if debtor == from_user and creditor == to_user:
            return minor
    return None
