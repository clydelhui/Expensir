"""Performs the OutboundActions the core returns as data (§0.12)."""

import logging
import time
from collections import Counter

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from expensir.core.outbound import OutboundAction
from expensir.db.models import Action, PendingIntent
from expensir.telegram.client import TelegramClient

logger = logging.getLogger(__name__)


async def execute(
    actions: list[OutboundAction],
    client: TelegramClient,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> None:
    started = time.monotonic()
    counts: Counter[str] = Counter()
    for action in actions:
        counts[action.kind] += 1
        logger.debug("effect %s: %r", action.kind, action)
        if action.kind == "send_message":
            sent = await client.send_message(
                chat_id=action.chat_id, text=action.text, reply_markup=action.reply_markup
            )
            if action.records_result_for_action_id is not None and session_factory is not None:
                await _record_result(
                    session_factory,
                    action.records_result_for_action_id,
                    chat_id=action.chat_id,
                    message_id=sent["message_id"],
                )
            if action.records_message_for_pending_id is not None and session_factory is not None:
                await _record_pending_message(
                    session_factory,
                    action.records_message_for_pending_id,
                    message_id=sent["message_id"],
                )
        elif action.kind == "edit_message":
            # cosmetic and best-effort (§9): the DB transaction already committed,
            # so a failed edit must lose only the message sync, never the undo
            try:
                await client.edit_message_text(
                    chat_id=action.chat_id,
                    message_id=action.message_id,
                    text=action.text,
                    reply_markup=action.reply_markup,
                )
            except Exception:
                logger.warning("editMessageText failed; leaving the message stale", exc_info=True)
        elif action.kind == "edit_message_reply_markup":
            # best-effort for the same reason as edit_message
            try:
                await client.edit_message_reply_markup(
                    chat_id=action.chat_id,
                    message_id=action.message_id,
                    reply_markup=action.reply_markup,
                )
            except Exception:
                logger.warning(
                    "editMessageReplyMarkup failed; leaving the button stale", exc_info=True
                )
        elif action.kind == "pin_chat_message":
            # best-effort (§13): pinning needs admin rights; on refusal the board
            # stays unpinned and the warning (creation-only, so once) is sent
            try:
                await client.pin_chat_message(chat_id=action.chat_id, message_id=action.message_id)
            except Exception:
                logger.warning("pinChatMessage failed; board stays unpinned", exc_info=True)
                if action.warn_text is not None:
                    try:
                        await client.send_message(chat_id=action.chat_id, text=action.warn_text)
                    except Exception:
                        logger.warning("pin warning send failed", exc_info=True)
        elif action.kind == "answer_callback_query":
            try:
                await client.answer_callback_query(
                    callback_query_id=action.callback_query_id, text=action.text
                )
            except Exception:
                logger.warning("answerCallbackQuery failed", exc_info=True)
    if actions:
        summary = " ".join(f"{kind}={n}" for kind, n in counts.items())
        logger.info("effects sent %s %dms", summary, (time.monotonic() - started) * 1000)


async def _record_result(
    session_factory: async_sessionmaker[AsyncSession],
    action_id: int,
    chat_id: int,
    message_id: int,
) -> None:
    async with session_factory() as session, session.begin():
        action = await session.get_one(Action, action_id)
        action.result_chat_id = chat_id
        action.result_message_id = message_id


async def _record_pending_message(
    session_factory: async_sessionmaker[AsyncSession],
    pending_id: int,
    message_id: int,
) -> None:
    """Key the pending row by its just-sent proposal message (§10). The row may
    already be gone — a fast Cancel/Confirm wins harmlessly."""
    async with session_factory() as session, session.begin():
        pending = await session.get(PendingIntent, pending_id)
        if pending is not None:
            pending.message_id = message_id
