"""Board lifecycle (§13, ADR-0003): create once under the lock, edit in place after.

Runs INSIDE the locked write transaction so the content is always post-write
consistent. The creation SEND is the one sanctioned inline API call (ADR-0003):
the sent message id must land on the ledger row in the same commit, or two
concurrent first writes would each see "no board yet" and create two boards.
The pin is NOT commit-critical, so it rides a post-commit outbound — a commit
failure then strands at worst an unpinned stray message, never a pinned one.
Every board failure is swallowed — a mutation must never roll back over its
board (§0.12).
"""

import logging
from typing import Any, Protocol

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from expensir.core.outbound import EditMessage, OutboundAction, PinChatMessage
from expensir.db.models import Group, Ledger
from expensir.domain.balances import net_positions
from expensir.domain.fx import ResolvedRate, api_legs, group_pins, rate_from_cache, today_utc
from expensir.domain.identity import display_names
from expensir.domain.simplify import simplify
from expensir.format.board import BoardLine, board_text
from expensir.format.keyboards import InlineKeyboard, board_keyboard

logger = logging.getLogger(__name__)

# conditional phrasing: the executor can't tell a rights refusal from a transient
# API failure, and this must not read as misinformation when the bot IS admin
CANT_PIN_WARNING = (
    "⚠️ I couldn't pin the board — it still works, just unpinned above. "
    "If I'm not a group admin, promoting me lets future boards pin."
)


class BoardMessenger(Protocol):
    """What board creation needs from the Telegram client (§13); tests fake it."""

    async def send_message(
        self, chat_id: int, text: str, reply_markup: dict[str, Any] | None = None
    ) -> dict[str, Any]: ...


async def sync_board(
    session: AsyncSession, group: Group, ledger_id: int, messenger: BoardMessenger | None
) -> list[OutboundAction]:
    """Bring one ledger's board in line with post-write balances (issue #9).

    Returns the best-effort edit for an existing board as data (§0.12); a missing
    board is created + pinned inline, right here under the per-group lock.
    """
    ledger = await session.get_one(Ledger, ledger_id)
    text, markup = await _board_view(session, group, ledger)
    if ledger.board_message_id is None or ledger.board_chat_id is None:
        if ledger.status != "open":
            # a board pins forever (§13 never-delete): never mint one for a ledger
            # this same mutation is retiring (archive, undo of /newledger)
            return []
        return await _create_board(session, group, ledger, text, markup, messenger)
    return [
        EditMessage(
            chat_id=ledger.board_chat_id,
            message_id=ledger.board_message_id,
            text=text,
            reply_markup=markup,
        )
    ]


async def suggested_transfers(
    session: AsyncSession, ledger_id: int, net: dict[int, dict[str, int]] | None = None
) -> list[BoardLine]:
    """Union over currencies of simplify's transfers, in stable order (§7.4) —
    the board shows them all, the settle sheet filters to one pair (ADR-0007).
    Pass `net` when the caller already replayed the ledger, so it isn't replayed twice."""
    if net is None:
        net = await net_positions(session, ledger_id)
    by_currency: dict[str, dict[int, int]] = {}
    for user_id, currencies in net.items():
        for currency, minor in currencies.items():
            by_currency.setdefault(currency, {})[user_id] = minor
    names = await display_names(session, list(net))
    return [
        BoardLine(
            from_id=debtor,
            to_id=creditor,
            from_name=names[debtor],
            to_name=names[creditor],
            amount_minor=minor,
            currency=currency,
        )
        for currency in sorted(by_currency)
        for debtor, creditor, minor in simplify(by_currency[currency])
    ]


async def _board_view(
    session: AsyncSession, group: Group, ledger: Ledger
) -> tuple[str, InlineKeyboard | None]:
    transfers = await suggested_transfers(session, ledger.id)
    home = group.home_currency
    rates = await board_rates(session, group, transfers)
    return (
        board_text(ledger_name=ledger.name, transfers=transfers, home=home, rates=rates),
        board_keyboard(transfers),
    )


async def board_rates(
    session: AsyncSession, group: Group, transfers: list[BoardLine]
) -> dict[str, ResolvedRate | None] | None:
    """The ≈ layer's rates for a board render, from pins + the CACHE only — this
    runs inside the locked write transaction, where FX transport is forbidden
    (§0.11): a stale cache renders dated; reads refresh it (§13). Two batched
    queries total, however many currencies are in play — the lock stays short."""
    home = group.home_currency
    if home is None:
        return None
    currencies = {t.currency for t in transfers if t.currency != home}
    if not currencies:
        return {}
    pins = await group_pins(session, group.id)
    legs = await api_legs(session, currencies | {home})
    today = today_utc()
    return {c: rate_from_cache(pins, legs, c, home, today=today) for c in currencies}


async def _create_board(
    session: AsyncSession,
    group: Group,
    ledger: Ledger,
    text: str,
    markup: InlineKeyboard | None,
    messenger: BoardMessenger | None,
) -> list[OutboundAction]:
    if messenger is None:
        return []  # nothing to send with; the next mutation retries creation
    try:
        sent = await messenger.send_message(
            chat_id=group.platform_chat_id, text=text, reply_markup=markup
        )
    except Exception:
        logger.warning("board create send failed; retrying on the next mutation", exc_info=True)
        return []
    try:
        # savepoint so the composite-unique backstop (§5) can only lose the board
        # ids, never the surrounding write
        async with session.begin_nested():
            ledger.board_chat_id = group.platform_chat_id
            ledger.board_message_id = sent["message_id"]
    except IntegrityError:
        logger.warning("board ids already claimed; leaving this board unrecorded", exc_info=True)
        return []
    return [
        PinChatMessage(
            chat_id=group.platform_chat_id,
            message_id=sent["message_id"],
            warn_text=CANT_PIN_WARNING,
        )
    ]
