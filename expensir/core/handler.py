"""Transport-agnostic entrypoint: dispatch(update_dict) -> list[OutboundAction] (§0.5)."""

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, cast

from pydantic import TypeAdapter
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from expensir.core import pending as pending_store
from expensir.core.board import BoardMessenger, sync_board
from expensir.core.locking import per_group_lock
from expensir.core.outbound import (
    AnswerCallbackQuery,
    EditMessage,
    EditMessageReplyMarkup,
    OutboundAction,
    SendMessage,
)
from expensir.core.sheet import sheet_view
from expensir.db.models import Action, Group, Ledger, PendingIntent, User
from expensir.domain.apply import (
    Applied,
    AppliedExpense,
    AppliedExpenseChange,
    AppliedLedgerOp,
    AppliedSettlement,
    AppliedSetup,
    ApplyContext,
    apply_intent,
    expense_participants,
    split_shares,
)
from expensir.domain.balances import net_positions
from expensir.domain.currency import resolve_currency
from expensir.domain.errors import Rejection
from expensir.domain.identity import (
    display_names,
    ensure_group,
    mark_left,
    register_member,
    registered_members_with_usernames,
    resolve_expense_id,
    resolve_refs,
)
from expensir.domain.ledgers import find_ledger, ledgers_of
from expensir.domain.money import fmt, to_minor
from expensir.domain.settle import record_settlement, suggested_amount
from expensir.domain.undo import ToggleDirection, toggle
from expensir.format.keyboards import (
    InlineKeyboard,
    confirm_keyboard,
    redo_keyboard,
    undo_keyboard,
)
from expensir.format.render import (
    LedgerLine,
    MemberLine,
    action_proposal_reply,
    balance_reply,
    delete_reply,
    edit_reply,
    expense_reply,
    join_names,
    ledgers_reply,
    members_reply,
    proposal_reply,
    settle_reply,
)
from expensir.intents import nl
from expensir.intents.commands import (
    DELETE_USAGE,
    EDIT_USAGE,
    ParsedExpense,
    parse_archive,
    parse_balance,
    parse_currency,
    parse_delete,
    parse_edit,
    parse_equal,
    parse_exact,
    parse_homecurrency,
    parse_members,
    parse_newledger,
    parse_percent,
    parse_settle,
    parse_shares,
    parse_switch,
    parse_unarchive,
)
from expensir.intents.schema import (
    AddExpense,
    ArchiveLedger,
    DeleteExpense,
    EditExpense,
    Intent,
    NewLedger,
    SetHomeCurrency,
    SetLoggingCurrency,
    SettleUp,
    Setup,
    SetupTarget,
    ShowBalance,
    SplitMember,
    SwitchLedger,
    UnarchiveLedger,
    Unknown,
)
from expensir.llm.base import LLMClient, LLMUnavailable
from expensir.llm.wire import WireDeleteExpense, WireEditExpense, WireSetup, WireUndoRedo

_TOGGLE_DATA = re.compile(r"^v1:(undo|redo):(\d+)$")
# Confirm/Cancel on a proposal (§10), keyed by the pending row's id
_CONFIRM_DATA = re.compile(r"^v1:(confirm|cancel):(\d+)$")
# the board [Settle] button (ADR-0006): tuple + shown amount as a staleness token
_SETTLE_DATA = re.compile(r"^v1:st:(\d+):(\d+):([A-Z]{3}):(\d+)$")
# a settle-sheet line (ADR-0007): same tuple + token, prefixed with the ledger —
# a sheet message is not the pinned board, so the tap can't resolve it from the chat
_SHEET_DATA = re.compile(r"^v1:sh:(\d+):(\d+):(\d+):([A-Z]{3}):(\d+)$")
_INTENT: TypeAdapter[Intent] = TypeAdapter(Intent)
UNDONE_MARK = "\n\n↩️ Undone by "
UNDO_POINTER = (
    "I never undo from chat — tap the ↩️ Undo button on the message that recorded it instead."
)
SETUP_GUIDANCE = (
    "👥 To register someone who hasn't spoken yet, reply to one of their "
    "messages with /setup, or mention them by tapping their name so the "
    "mention links their account. A bare @username isn't enough."
)


@dataclass
class Reply:
    """A command's rendered result; undo_action_id marks it undoable (§9).

    markup is for reads that carry their own buttons (the settle sheet,
    ADR-0007); an undoable result's Undo keyboard wins over it."""

    text: str
    undo_action_id: int | None = None
    markup: InlineKeyboard | None = None


CommandRunner = Callable[[dict[str, Any], AsyncSession, Group, User | None], Awaitable[Reply]]

WELCOME = (
    "👋 Expensir is on the case!\n"
    "\n"
    "To get set up:\n"
    "• Set the group's home currency with /homecurrency <ISO> — balances in other "
    "currencies will also show a ≈ equivalent in it.\n"
    "• Optionally give a ledger its own logging currency with /currency <ISO>.\n"
    "• Register people who haven't spoken yet by replying to one of their messages with "
    "/setup. A bare @username can't be added — Telegram only shows me accounts that "
    "interact or are tagged directly.\n"
    "• I only act when you @mention me, reply to my messages, or use a command — "
    "receipt photos and natural language included."
)


@dataclass
class Deps:
    session_factory: async_sessionmaker[AsyncSession]
    # from getMe at startup; when unknown, @bot-suffixed commands are not claimed
    bot_username: str | None = None
    operator_user_id: int | None = None  # telegram user id; may undo locked actions (§9)
    undo_window_hours: int = 24
    # board creation sends inside the locked transaction (ADR-0003); None (tests
    # without one) skips creation, and the next mutation with a client retries
    client: BoardMessenger | None = None
    # the NL extractor (§12, ADR-0010); None (unconfigured) leaves mentions unanswered
    llm: LLMClient | None = None
    pending_ttl_minutes: int = 15  # proposal TTL (§10, §17)


async def dispatch(update: dict[str, Any], deps: Deps) -> list[OutboundAction]:
    my_chat_member = update.get("my_chat_member")
    if my_chat_member is not None and _is_bot_added_to_group(my_chat_member):
        return await _handle_bot_added(my_chat_member, deps)

    callback_query = update.get("callback_query")
    if callback_query is not None:
        return await _handle_callback_query(callback_query, deps)

    message = update.get("message")
    if message is not None and message["chat"]["type"] in ("group", "supergroup"):
        return await _handle_group_message(message, deps)

    return []


async def _handle_callback_query(callback: dict[str, Any], deps: Deps) -> list[OutboundAction]:
    """Undo/redo (§9) and board [Settle] (ADR-0006) button presses."""
    ack = AnswerCallbackQuery(callback_query_id=callback["id"])
    confirm = _CONFIRM_DATA.match(callback.get("data") or "")
    if confirm is not None:
        return await _handle_confirm_tap(callback, confirm, deps)
    settle = _SETTLE_DATA.match(callback.get("data") or "")
    if settle is not None:
        return await _handle_settle_tap(callback, settle, deps)
    sheet = _SHEET_DATA.match(callback.get("data") or "")
    if sheet is not None:
        return await _handle_sheet_tap(callback, sheet, deps)
    match = _TOGGLE_DATA.match(callback.get("data") or "")
    if match is None:
        return [ack]  # not a button of any slice so far; acknowledge so the spinner stops
    direction = cast(ToggleDirection, match.group(1))
    action_id = int(match.group(2))

    message = callback.get("message") or {}
    chat = message.get("chat")
    if chat is None or chat["type"] not in ("group", "supergroup"):
        return [ack]

    async with deps.session_factory() as session, session.begin():
        group = await ensure_group(session, chat["id"], chat.get("title"))
        presser = await _register_author(session, group.id, callback["from"])
        if presser is None:
            ack.text = "I can't tell who pressed that — anonymous admins can't undo."
            return [ack]
        outcome = await toggle(
            session,
            group,
            action_id,
            direction,
            presser,
            presser_platform_id=callback["from"]["id"],
            operator_platform_id=deps.operator_user_id,
            window_hours=deps.undo_window_hours,
        )
        board: list[OutboundAction] = []
        if outcome.toggled_ledger_id is not None:
            # an undo/redo is a mutation like any other: its ledger's board
            # re-renders post-toggle, inside the locked transaction (§13)
            board = await sync_board(session, group, outcome.toggled_ledger_id, deps.client)

    ack.text = outcome.answer
    if outcome.undone is None:
        # nothing to sync (unknown action / locked): button stays
        return [ack]
    markup = redo_keyboard(action_id) if outcome.undone else undo_keyboard(action_id)
    original_text = message.get("text")
    if not original_text:
        # InaccessibleMessage (callback on a very old message): no text to
        # re-render, but the button must still flip or redo becomes unreachable
        return [
            ack,
            EditMessageReplyMarkup(
                chat_id=chat["id"], message_id=message["message_id"], reply_markup=markup
            ),
            *board,
        ]
    base = _strip_undone_mark(original_text)
    text = f"{base}{UNDONE_MARK}{outcome.undone_by_name or 'someone'}." if outcome.undone else base
    edit = EditMessage(
        chat_id=chat["id"], message_id=message["message_id"], text=text, reply_markup=markup
    )
    return [ack, edit, *board]


async def _handle_confirm_tap(
    callback: dict[str, Any], match: re.Match[str], deps: Deps
) -> list[OutboundAction]:
    """Confirm/Cancel on a proposal (§10): confirm re-resolves + re-validates the
    UNRESOLVED intent against the PINNED ledger under the lock, applies, and
    consumes the row — a double-tap finds nothing."""
    ack = AnswerCallbackQuery(callback_query_id=callback["id"])
    verb, pending_id = match.group(1), int(match.group(2))

    message = callback.get("message") or {}
    chat = message.get("chat")
    if chat is None or chat["type"] not in ("group", "supergroup"):
        return [ack]

    async with deps.session_factory() as session, session.begin():
        group = await ensure_group(session, chat["id"], chat.get("title"))
        presser = await _register_author(session, group.id, callback["from"])
        if presser is None:
            ack.text = "I can't tell who pressed that — anonymous admins can't confirm."
            return [ack]
        pending = await session.get(PendingIntent, pending_id)
        if pending is None or pending.chat_id != chat["id"]:
            # consumed (double-tap) or a forged id: either way, nothing to do
            ack.text = "This proposal was already handled."
            return [ack]

        if pending_store.is_expired(pending):
            # expiry is computed on read (§10): consume and mark; a reply to the
            # dead proposal starts a fresh NL intent (slice 13)
            await session.delete(pending)
            ack.text = "Nothing was recorded."
            edit = EditMessage(
                chat_id=chat["id"],
                message_id=message["message_id"],
                text="⌛ Expired — nothing was recorded. Send it again if it still applies.",
            )
            return [ack, edit]

        if verb == "cancel":
            # drop the row and mark the message; nothing was ever created (§10)
            await session.delete(pending)
            ack.text = "Cancelled."
            edit = EditMessage(
                chat_id=chat["id"],
                message_id=message["message_id"],
                text="✖ Cancelled — nothing was recorded.",
            )
            return [ack, edit]

        # the intent commits to the ledger PINNED at propose time (§10): take the
        # lock, then re-resolve + re-validate against it — a concurrent /switch
        # never redirects a pending proposal
        await per_group_lock(session, group.id)
        await session.refresh(group)
        ledger = await session.get_one(Ledger, pending.ledger_id)
        assert ledger.group_id == group.id  # pinned at propose time, same group
        if ledger.status != "open":
            # re-validation failure (§10): the pinned ledger closed meanwhile.
            # Consume the row — a reply to the dead proposal starts fresh.
            await session.delete(pending)
            ack.text = "Nothing was recorded."
            edit = EditMessage(
                chat_id=chat["id"],
                message_id=message["message_id"],
                text=(
                    f"⚠️ That changed while you were deciding — 📒 {ledger.name} was "
                    "archived, so nothing was recorded. Unarchive it and resend if "
                    "it still applies."
                ),
            )
            return [ack, edit]
        intent = _INTENT.validate_python(pending.intent_json)
        proposer = await session.get_one(User, pending.proposer_user_id)
        ctx = ApplyContext(
            session=session,
            group=group,
            actor=presser,
            seed=pending.seed,
            source="nl",
            ledger_id=pending.ledger_id,
            me=proposer,
        )
        try:
            # savepoint: a failed re-validation rolls back apply's partial writes
            # while the consume + registration above still commit (§0.9)
            async with session.begin_nested():
                applied = await apply_intent(intent, ctx)
        except Rejection as exc:
            await session.delete(pending)  # consumed: a reply starts fresh (§10.4)
            ack.text = "Nothing was recorded."
            edit = EditMessage(
                chat_id=chat["id"],
                message_id=message["message_id"],
                text=f"⚠️ That changed while you were deciding — nothing was recorded. {exc}",
            )
            return [ack, edit]
        assert applied is not None
        await session.delete(pending)  # consume: a second tap finds nothing (§10)
        if isinstance(applied, AppliedSetup):
            # registration is permanent (§8): no Undo affordance on the result
            text = "\n".join(_setup_member_lines(applied))
            markup = None
            action_id = applied.action_id  # None when nothing actually registered
        else:
            text = _committed_text(intent, applied, pinned_ledger_name=ledger.name)
            markup = undo_keyboard(applied.action_id)
            action_id = applied.action_id
        board: list[OutboundAction] = []
        if action_id is not None:
            # the proposal message IS the result message: record it in-transaction
            # so undo can edit it and reply-to-target can resolve it (§8)
            action = await session.get_one(Action, action_id)
            action.result_chat_id = chat["id"]
            action.result_message_id = message.get("message_id")
            if action.ledger_id is not None:
                # every mutation re-renders its ledger's board, like the slash path (§13)
                board = await sync_board(session, group, action.ledger_id, deps.client)

    ack.text = "✅ Recorded."
    edit = EditMessage(
        chat_id=chat["id"],
        message_id=message["message_id"],
        text=text,
        reply_markup=markup,
    )
    return [ack, edit, *board]


def _committed_text(intent: Intent, applied: Applied, *, pinned_ledger_name: str) -> str:
    """The committed result a confirmed proposal's message edits into (§10) —
    the same renders the slash paths use (§4: one contract, one look)."""
    if isinstance(intent, AddExpense):
        assert isinstance(applied, AppliedExpense) and intent.currency is not None
        return expense_reply(
            ledger_name=pinned_ledger_name,
            expense_id=applied.expense_id,
            amount_minor=intent.amount_minor,
            currency=intent.currency,
            description=intent.description,
            payer_name=applied.payer.display_name,
            participant_names=[u.display_name for u in applied.participants],
            split_type=intent.split_type,
            shares=[(u.display_name, applied.shares[u.id]) for u in applied.participants],
        )
    if isinstance(intent, SettleUp):
        assert isinstance(applied, AppliedSettlement)
        return settle_reply(
            ledger_name=pinned_ledger_name,
            from_name=applied.from_user.display_name,
            to_name=applied.to_user.display_name,
            amount_minor=applied.amount_minor,
            currency=applied.currency,
        )
    if isinstance(intent, DeleteExpense):
        assert isinstance(applied, AppliedExpenseChange)
        return delete_reply(
            ledger_name=pinned_ledger_name,
            expense_id=applied.expense.id,
            amount_minor=applied.expense.amount_minor,
            currency=applied.expense.currency,
            description=applied.expense.description,
        )
    if isinstance(intent, EditExpense):
        assert isinstance(applied, AppliedExpenseChange)
        return edit_reply(
            ledger_name=pinned_ledger_name,
            expense_id=applied.expense.id,
            amount_minor=applied.expense.amount_minor,
            currency=applied.expense.currency,
            description=applied.expense.description,
            occurred_on=applied.expense.occurred_on,
        )
    if isinstance(intent, SwitchLedger):
        assert isinstance(applied, AppliedLedgerOp)
        return _switch_text(applied.ledger.name)
    if isinstance(intent, NewLedger):
        assert isinstance(applied, AppliedLedgerOp)
        return _new_ledger_text(applied.ledger.name, intent.logging_currency)
    if isinstance(intent, ArchiveLedger):
        assert isinstance(applied, AppliedLedgerOp)
        return _archive_text(applied)
    if isinstance(intent, UnarchiveLedger):
        assert isinstance(applied, AppliedLedgerOp)
        return _unarchive_text(applied.ledger.name)
    if isinstance(intent, SetHomeCurrency):
        return _home_currency_text(intent.currency)
    if isinstance(intent, SetLoggingCurrency):
        assert isinstance(applied, AppliedLedgerOp)
        return _logging_currency_text(applied.ledger.name, intent.currency)
    raise AssertionError(f"unrendered confirmed kind: {intent.kind}")


def _switch_text(name: str) -> str:
    return f"📒 Switched to {name} — new expenses land here."


def _new_ledger_text(name: str, logging_currency: str | None) -> str:
    logging = f" Logging currency: {logging_currency}." if logging_currency is not None else ""
    return f"📒 {name} is now the active ledger — new expenses land here.{logging}"


def _archive_text(applied: AppliedLedgerOp) -> str:
    text = f"📦 Archived 📒 {applied.ledger.name}."
    if applied.outstanding_balances:
        text += " ⚠️ It still has outstanding balances — they stay as they are."
    if applied.repointed_to is not None:
        text += f" 📒 {applied.repointed_to.name} is now the active ledger."
    return text


def _unarchive_text(name: str) -> str:
    return f"📂 Reopened 📒 {name} — it's not active; /switch {name} to log expenses there."


def _home_currency_text(currency: str) -> str:
    return (
        f"🏠 Home currency set to {currency} — "
        f"other currencies will show a ≈ {currency} equivalent."
    )


def _logging_currency_text(ledger_name: str, currency: str) -> str:
    return (
        f"📒 {ledger_name} now logs in {currency} — new expenses default to "
        f"{currency}; existing ones keep their currency."
    )


def _setup_member_lines(applied: AppliedSetup) -> list[str]:
    """The per-person outcome lines /setup and a confirmed NL setup share (§11)."""
    lines = [
        f"✅ Registered {member.display_name} — they can now appear in expenses and settlements."
        for member in applied.registered
    ]
    lines += [f"👥 {member.display_name} is already a member." for member in applied.already]
    lines += [
        f"🚪 {member.display_name} left the group — their balances are kept, and they'll "
        "be back in once they re-join or send a message here themselves."
        for member in applied.departed
    ]
    return lines


async def _handle_settle_tap(
    callback: dict[str, Any], match: re.Match[str], deps: Deps
) -> list[OutboundAction]:
    """A board [Settle] tap (ADR-0006): WYSIWYG under the lock. Fresh records the
    shown amount; stale warns + refreshes; a gone line answers 'Already settled.'"""
    ack = AnswerCallbackQuery(callback_query_id=callback["id"])
    from_id, to_id = int(match.group(1)), int(match.group(2))
    currency, shown_minor = match.group(3), int(match.group(4))

    message = callback.get("message") or {}
    chat = message.get("chat")
    if chat is None or chat["type"] not in ("group", "supergroup"):
        return [ack]

    async with deps.session_factory() as session, session.begin():
        group = await ensure_group(session, chat["id"], chat.get("title"))
        presser = await _register_author(session, group.id, callback["from"])
        if presser is None:
            ack.text = "I can't tell who pressed that — anonymous admins can't record settlements."
            return [ack]
        # the tapped board names the ledger: taps on an old ledger's board settle
        # THAT ledger, mirroring how every mutation syncs its own board (§13)
        ledger = (
            await session.execute(
                select(Ledger).where(
                    Ledger.board_chat_id == chat["id"],
                    Ledger.board_message_id == message.get("message_id"),
                )
            )
        ).scalar_one_or_none()
        if ledger is None or ledger.group_id != group.id:
            ack.text = "That button doesn't match anything I recorded."
            return [ack]

        # recompute under the lock (ADR-0006): the shown amount is only a token
        await per_group_lock(session, group.id)
        await session.refresh(group)
        current = await _current_suggested(session, ledger.id, from_id, to_id, currency)
        if current != shown_minor:
            ack.text = (
                "Already settled."
                if current is None
                else "⚠️ The board was out of date — nothing recorded. Tap again."
            )
            board = await sync_board(session, group, ledger.id, deps.client)
            return [ack, *board]

        # both ids sit in a transfer simplify just proposed, so the rows exist
        payer = await session.get_one(User, from_id)
        receiver = await session.get_one(User, to_id)
        result = await _record_settle_tap(
            session, ledger, presser, payer, receiver, currency, shown_minor, chat["id"]
        )
        board = await sync_board(session, group, ledger.id, deps.client)
    ack.text = "🤝 Recorded."
    return [ack, result, *board]


async def _handle_sheet_tap(
    callback: dict[str, Any], match: re.Match[str], deps: Deps
) -> list[OutboundAction]:
    """A settle-sheet [Settle] tap (ADR-0007): the board tap's WYSIWYG guard,
    plus the sheet itself refreshes in place on every outcome — it is the
    surface the presser is looking at."""
    ack = AnswerCallbackQuery(callback_query_id=callback["id"])
    ledger_id = int(match.group(1))
    from_id, to_id = int(match.group(2)), int(match.group(3))
    currency, shown_minor = match.group(4), int(match.group(5))

    message = callback.get("message") or {}
    chat = message.get("chat")
    if chat is None or chat["type"] not in ("group", "supergroup"):
        return [ack]

    async with deps.session_factory() as session, session.begin():
        group = await ensure_group(session, chat["id"], chat.get("title"))
        presser = await _register_author(session, group.id, callback["from"])
        if presser is None:
            ack.text = "I can't tell who pressed that — anonymous admins can't record settlements."
            return [ack]
        ledger = await session.get(Ledger, ledger_id)
        payer = await session.get(User, from_id)
        receiver = await session.get(User, to_id)
        if ledger is None or ledger.group_id != group.id or payer is None or receiver is None:
            ack.text = "That button doesn't match anything I recorded."
            return [ack]

        # recompute under the lock (ADR-0006): the shown amount is only a token
        await per_group_lock(session, group.id)
        await session.refresh(group)
        current = await _current_suggested(session, ledger.id, from_id, to_id, currency)
        sheet_message_id = message.get("message_id")
        if current != shown_minor:
            ack.text = (
                "Already settled."
                if current is None
                else "⚠️ The sheet was out of date — nothing recorded. Tap again."
            )
            sheet = await _refresh_sheet(session, ledger, payer, receiver, chat, sheet_message_id)
            return [ack, *sheet]

        result = await _record_settle_tap(
            session, ledger, presser, payer, receiver, currency, shown_minor, chat["id"]
        )
        board = await sync_board(session, group, ledger.id, deps.client)
        sheet = await _refresh_sheet(session, ledger, payer, receiver, chat, sheet_message_id)
    ack.text = "🤝 Recorded."
    return [ack, result, *sheet, *board]


async def _current_suggested(
    session: AsyncSession, ledger_id: int, from_id: int, to_id: int, currency: str
) -> int | None:
    """The transfer a tap's amount token must match, recomputed under the lock
    (ADR-0006); None means the line is gone."""
    net = await net_positions(session, ledger_id)
    net_ccy = {user_id: by_ccy.get(currency, 0) for user_id, by_ccy in net.items()}
    return suggested_amount(net_ccy, from_id, to_id)


async def _record_settle_tap(
    session: AsyncSession,
    ledger: Ledger,
    presser: User,
    payer: User,
    receiver: User,
    currency: str,
    amount_minor: int,
    chat_id: int,
) -> SendMessage:
    """Record one fresh [Settle] tap — board or sheet line, the same thing:
    one settlement, one action, its own Undo (ADR-0006, ADR-0007)."""
    recorded = await record_settlement(
        session,
        ledger_id=ledger.id,
        actor=presser,
        payer=payer,
        receiver=receiver,
        intent=SettleUp(
            from_ref=payer.display_name,
            to_ref=receiver.display_name,
            amount_minor=amount_minor,
            currency=currency,
        ),
    )
    text = settle_reply(
        ledger_name=ledger.name,
        from_name=payer.display_name,
        to_name=receiver.display_name,
        amount_minor=amount_minor,
        currency=currency,
    )
    return SendMessage(
        chat_id=chat_id,
        text=text,
        reply_markup=undo_keyboard(recorded.action_id),
        records_result_for_action_id=recorded.action_id,
    )


async def _refresh_sheet(
    session: AsyncSession,
    ledger: Ledger,
    a: User,
    b: User,
    chat: dict[str, Any],
    message_id: int | None,
) -> list[OutboundAction]:
    """Re-render the tapped sheet against the truth (edit in place, best-effort)."""
    if message_id is None:
        return []
    text, markup = await sheet_view(session, ledger, a, b)
    return [EditMessage(chat_id=chat["id"], message_id=message_id, text=text, reply_markup=markup)]


def _strip_undone_mark(text: str) -> str:
    index = text.rfind(UNDONE_MARK)
    return text[:index] if index != -1 else text


async def _handle_bot_added(my_chat_member: dict[str, Any], deps: Deps) -> list[OutboundAction]:
    chat = my_chat_member["chat"]
    async with deps.session_factory() as session, session.begin():
        group = await ensure_group(session, chat["id"], chat.get("title"))
        await _register_author(session, group.id, my_chat_member["from"])
    return [SendMessage(chat_id=chat["id"], text=WELCOME)]


async def _handle_group_message(message: dict[str, Any], deps: Deps) -> list[OutboundAction]:
    async with deps.session_factory() as session, session.begin():
        group = await ensure_group(session, message["chat"]["id"], message["chat"].get("title"))
        actor = None
        if "from" in message:
            actor = await _register_author(session, group.id, message["from"])

        joined = message.get("new_chat_members")
        if joined is not None:
            # join carries full live User objects (§11): auto-register each; a
            # departed member re-joining reactivates. Lifecycle event — no reply.
            for tg_user in joined:
                await _register_author(session, group.id, tg_user)  # skips bots
            return []

        left = message.get("left_chat_member")
        if left is not None:
            # processed AFTER author registration: a voluntary leaver's own `from`
            # must not outlive their leave (§11). Lifecycle event — no reply.
            if not left.get("is_bot"):
                await mark_left(session, group.id, left["id"])
            return []

        command = _command_of(message.get("text", ""), deps.bot_username)
        if command == "start":
            active = await session.get_one(Ledger, group.active_ledger_id)
            text = f"📒 {active.name} • Expensir is ready — try /equal, or /balance."
            return [SendMessage(chat_id=message["chat"]["id"], text=text)]
        runner = _RUNNERS.get(command or "")
        if runner is not None:
            reply = await _run_command(runner, message, session, group, actor)
            markup = reply.markup
            board: list[OutboundAction] = []
            if reply.undo_action_id is not None:
                markup = undo_keyboard(reply.undo_action_id)
                # every mutation re-renders its ledger's board, post-write, still
                # inside the locked transaction (§13, issue #9)
                action = await session.get_one(Action, reply.undo_action_id)
                assert action.ledger_id is not None  # every undoable kind is ledger activity
                board = await sync_board(session, group, action.ledger_id, deps.client)
            return [
                SendMessage(
                    chat_id=message["chat"]["id"],
                    text=reply.text,
                    reply_markup=markup,
                    records_result_for_action_id=reply.undo_action_id,
                ),
                *board,
            ]

        text = message.get("text", "")
        if _mentions_bot(text, deps.bot_username) and deps.llm is not None:
            return await _handle_nl_text(message, session, group, actor, deps)

    return []


async def _handle_nl_text(
    message: dict[str, Any], session: AsyncSession, group: Group, actor: User | None, deps: Deps
) -> list[OutboundAction]:
    """The NL text path (§12): extract -> propose; nothing commits until Confirm (§0.7).

    The savepoint scopes a rejection's rollback to the NL handling itself, like
    _run_command: author registration earlier in the transaction still commits."""
    assert deps.llm is not None
    chat_id = message["chat"]["id"]
    stripped = _strip_bot_mention(message.get("text", ""), deps.bot_username)
    try:
        wire = await deps.llm.extract_text(stripped)
    except LLMUnavailable:
        # a transport failure is NOT the user's sentence (issue #13 grill): say
        # so plainly rather than asking them to rephrase
        return [
            SendMessage(
                chat_id=chat_id,
                text=(
                    "⚠️ I couldn't reach my language model just now — try again in "
                    "a moment, or use a slash command like /equal or /balance."
                ),
            )
        ]
    try:
        async with session.begin_nested():
            return await _run_nl_intent(wire, message, session, group, actor, deps)
    except (Rejection, ValueError) as exc:
        return [SendMessage(chat_id=chat_id, text=str(exc))]


async def _run_nl_intent(
    wire: Any,
    message: dict[str, Any],
    session: AsyncSession,
    group: Group,
    actor: User | None,
    deps: Deps,
) -> list[OutboundAction]:
    """Route one extracted intent: reads run immediately, mutations propose (§0.7)."""
    if isinstance(wire, WireUndoRedo):
        # detected, never honored (§9): the reply stays templated app-side
        return [SendMessage(chat_id=message["chat"]["id"], text=UNDO_POINTER)]
    ledger = await session.get_one(Ledger, group.active_ledger_id)
    if isinstance(wire, WireDeleteExpense | WireEditExpense):
        converted = nl.ConvertedIntent(
            intent=await _resolve_expense_wire(wire, message, session), rounded_from=None
        )
    elif isinstance(wire, WireSetup):
        # targets come from the MESSAGE, not the model (§11): only the reply
        # target and text_mentions carry account ids
        targets, _, _ = _setup_entries(message)
        if not targets:
            return [SendMessage(chat_id=message["chat"]["id"], text=SETUP_GUIDANCE)]
        converted = nl.ConvertedIntent(intent=Setup(targets=targets), rounded_from=None)
    else:
        converted = nl.to_intent(
            wire, logging_currency=ledger.logging_currency, home_currency=group.home_currency
        )
    intent = converted.intent
    reply = await _nl_read_reply(intent, session, group, actor)
    if reply is not None:
        return [
            SendMessage(chat_id=message["chat"]["id"], text=reply.text, reply_markup=reply.markup)
        ]
    return await _propose(converted, message, session, group, actor, deps, ledger)


async def _resolve_expense_wire(
    wire: Any, message: dict[str, Any], session: AsyncSession
) -> DeleteExpense | EditExpense:
    """Pin the expense a NL delete/edit means (§11): reply-to-target primary,
    a bare #id in the text fallback. Descriptive matching arrives in slice 13."""
    reply_to = message.get("reply_to_message")
    expense_id = await resolve_expense_id(
        session,
        platform_chat_id=message["chat"]["id"],
        reply_message_id=reply_to["message_id"] if reply_to is not None else None,
        explicit_id=wire.expense_id,
    )
    if expense_id is None:
        raise Rejection(
            "🤷 Which expense? Reply to its result message or give its #id — " 'e.g. "delete #42".'
        )
    if isinstance(wire, WireDeleteExpense):
        return DeleteExpense(expense_id=expense_id)
    return EditExpense(
        expense_id=expense_id, description=wire.description, occurred_on=wire.occurred_on
    )


async def _nl_read_reply(
    intent: Intent, session: AsyncSession, group: Group, actor: User | None
) -> Reply | None:
    """Run an NL read/no-op immediately (§0.7); None means the intent is a mutation."""
    if isinstance(intent, Unknown):
        return Reply(
            text=(
                "🤷 I couldn't make sense of that — try rephrasing, e.g. "
                '"I paid 40 for dinner, split with Sam".'
            )
        )
    if isinstance(intent, ShowBalance):
        return await _balance_reply(intent, session, group, actor)
    if isinstance(intent, SettleUp) and intent.amount_minor is None:
        return await _sheet_reply(intent, session, group, actor)
    return None


async def _propose(
    converted: nl.ConvertedIntent,
    message: dict[str, Any],
    session: AsyncSession,
    group: Group,
    actor: User | None,
    deps: Deps,
    ledger: Ledger,
) -> list[OutboundAction]:
    """Render a proposal + park the UNRESOLVED intent, pinned to the active ledger (§10).

    Resolution here is a read-only preview: unknown/ambiguous refs reject the
    whole intent now, but nothing is created until Confirm."""
    if actor is None:
        raise Rejection(
            "I can't tell who sent that — anonymous admins can't record changes. "
            "Turn off 'Remain anonymous' and try again."
        )
    intent = converted.intent
    seed = message["message_id"]
    if isinstance(intent, AddExpense):
        text = await _expense_proposal_text(intent, converted, session, group, actor, ledger, seed)
    else:
        summary = await _proposal_summary(intent, session, group, actor)
        text = action_proposal_reply(ledger_name=ledger.name, summary=summary)

    parked = await pending_store.park(
        session,
        chat_id=message["chat"]["id"],
        ledger_id=ledger.id,
        proposer=actor,
        seed=seed,
        intent=intent,
        ttl_minutes=deps.pending_ttl_minutes,
    )
    return [
        SendMessage(
            chat_id=message["chat"]["id"],
            text=text,
            reply_markup=confirm_keyboard(parked.id),
            records_message_for_pending_id=parked.id,
        )
    ]


async def _expense_proposal_text(
    intent: AddExpense,
    converted: nl.ConvertedIntent,
    session: AsyncSession,
    group: Group,
    actor: User,
    ledger: Ledger,
    seed: int,
) -> str:
    """The WYSIWYG expense preview (§7.1, §10): shares exactly as they'd commit."""
    assert intent.currency is not None  # concrete after nl.to_intent (§3)
    refs = [intent.payer_ref] + [p.user_ref for p in intent.participants]
    resolved = await resolve_refs(session, group.id, refs, actor)
    participants = await expense_participants(intent, resolved, session, group.id)
    shares = split_shares(intent, resolved, participants, seed)
    return proposal_reply(
        ledger_name=ledger.name,
        amount_minor=intent.amount_minor,
        currency=intent.currency,
        description=intent.description,
        payer_name=resolved[intent.payer_ref].display_name,
        shares=[(u.display_name, shares[u.id]) for u in participants],
        rounded_from=converted.rounded_from,
    )


async def _proposal_summary(
    intent: Intent, session: AsyncSession, group: Group, actor: User
) -> str:
    """One line saying what Confirm will do — refs previewed read-only (§6)."""
    if isinstance(intent, SettleUp):
        assert intent.amount_minor is not None and intent.currency is not None  # reads never park
        pair = await resolve_refs(session, group.id, [intent.from_ref, intent.to_ref], actor)
        payer, receiver = pair[intent.from_ref], pair[intent.to_ref]
        return (
            f"🤝 {payer.display_name} paid {receiver.display_name} "
            f"{fmt(intent.amount_minor, intent.currency)}."
        )
    if isinstance(intent, DeleteExpense):
        return f"Delete expense #{intent.expense_id}."
    if isinstance(intent, EditExpense):
        changes = [
            f'description → "{intent.description}"' if intent.description is not None else "",
            f"date → {intent.occurred_on}" if intent.occurred_on is not None else "",
        ]
        return f"Edit #{intent.expense_id}: {', '.join(c for c in changes if c)}."
    if isinstance(intent, SwitchLedger):
        target = await find_ledger(session, group.id, intent.name_or_id)
        return f"Switch to 📒 {target.name} — new expenses land there."
    if isinstance(intent, NewLedger):
        logging = f" logging in {intent.logging_currency}" if intent.logging_currency else ""
        return f"Create 📒 {intent.name}{logging} and make it the active ledger."
    if isinstance(intent, ArchiveLedger):
        if intent.name_or_id is None:
            active = await session.get_one(Ledger, group.active_ledger_id)
            return f"Archive 📒 {active.name} (the active ledger)."
        target = await find_ledger(session, group.id, intent.name_or_id)
        return f"Archive 📒 {target.name}."
    if isinstance(intent, UnarchiveLedger):
        target = await find_ledger(session, group.id, intent.name_or_id)
        return f"Reopen 📒 {target.name} (without switching to it)."
    if isinstance(intent, SetHomeCurrency):
        return f"Set the group's home currency to {intent.currency}."
    if isinstance(intent, SetLoggingCurrency):
        active = await session.get_one(Ledger, group.active_ledger_id)
        return f"Log 📒 {active.name} in {intent.currency} from now on."
    if isinstance(intent, Setup):
        names = join_names([t.display_name for t in intent.targets])
        return f"Register {names} as members — this is permanent (no Undo)."
    raise AssertionError(f"unproposable intent kind: {intent.kind}")


def _strip_bot_mention(text: str, bot_username: str | None) -> str:
    """The extractor sees the message minus the addressing '@bot' itself."""
    if bot_username is None:
        return text.strip()
    return re.sub(f"@{re.escape(bot_username)}", "", text, flags=re.IGNORECASE).strip()


async def _run_command(
    runner: CommandRunner,
    message: dict[str, Any],
    session: AsyncSession,
    group: Group,
    actor: User | None,
) -> Reply:
    """Run a mutating command; on rejection every write it made rolls back (§0.9).

    The savepoint scopes the rollback to the command itself — the author
    registration earlier in this transaction still commits.
    """
    try:
        async with session.begin_nested():
            return await runner(message, session, group, actor)
    except (Rejection, ValueError) as exc:
        return Reply(text=str(exc))


async def _run_homecurrency(
    message: dict[str, Any], session: AsyncSession, group: Group, actor: User | None
) -> Reply:
    currency = parse_homecurrency(message["text"])
    ctx = ApplyContext(session=session, group=group, actor=actor, seed=message["message_id"])
    applied = await apply_intent(SetHomeCurrency(currency=currency), ctx)
    assert applied is not None
    return Reply(text=_home_currency_text(currency), undo_action_id=applied.action_id)


async def _run_currency(
    message: dict[str, Any], session: AsyncSession, group: Group, actor: User | None
) -> Reply:
    currency = parse_currency(message["text"])
    ctx = ApplyContext(session=session, group=group, actor=actor, seed=message["message_id"])
    applied = await apply_intent(SetLoggingCurrency(currency=currency), ctx)
    assert isinstance(applied, AppliedLedgerOp)
    return Reply(
        text=_logging_currency_text(applied.ledger.name, currency),
        undo_action_id=applied.action_id,
    )


async def _run_newledger(
    message: dict[str, Any], session: AsyncSession, group: Group, actor: User | None
) -> Reply:
    name, currency = parse_newledger(message["text"])
    ctx = ApplyContext(session=session, group=group, actor=actor, seed=message["message_id"])
    applied = await apply_intent(NewLedger(name=name, logging_currency=currency), ctx)
    assert isinstance(applied, AppliedLedgerOp)
    return Reply(text=_new_ledger_text(name, currency), undo_action_id=applied.action_id)


async def _run_switch(
    message: dict[str, Any], session: AsyncSession, group: Group, actor: User | None
) -> Reply:
    name_or_id = parse_switch(message["text"])
    ctx = ApplyContext(session=session, group=group, actor=actor, seed=message["message_id"])
    applied = await apply_intent(SwitchLedger(name_or_id=name_or_id), ctx)
    assert isinstance(applied, AppliedLedgerOp)
    return Reply(text=_switch_text(applied.ledger.name), undo_action_id=applied.action_id)


async def _run_archive(
    message: dict[str, Any], session: AsyncSession, group: Group, actor: User | None
) -> Reply:
    name_or_id = parse_archive(message["text"])
    ctx = ApplyContext(session=session, group=group, actor=actor, seed=message["message_id"])
    applied = await apply_intent(ArchiveLedger(name_or_id=name_or_id), ctx)
    assert isinstance(applied, AppliedLedgerOp)
    return Reply(text=_archive_text(applied), undo_action_id=applied.action_id)


async def _run_unarchive(
    message: dict[str, Any], session: AsyncSession, group: Group, actor: User | None
) -> Reply:
    name_or_id = parse_unarchive(message["text"])
    ctx = ApplyContext(session=session, group=group, actor=actor, seed=message["message_id"])
    applied = await apply_intent(UnarchiveLedger(name_or_id=name_or_id), ctx)
    assert isinstance(applied, AppliedLedgerOp)
    return Reply(text=_unarchive_text(applied.ledger.name), undo_action_id=applied.action_id)


async def _run_ledgers(
    message: dict[str, Any], session: AsyncSession, group: Group, actor: User | None
) -> Reply:
    # a read: no lock, no action row (§0.7); the one place other ledgers are visible
    lines = [
        LedgerLine(
            ledger_id=ledger.id,
            name=ledger.name,
            is_active=ledger.id == group.active_ledger_id,
            is_archived=ledger.status == "archived",
            logging_currency=ledger.logging_currency,
        )
        for ledger in await ledgers_of(session, group.id)
    ]
    return Reply(text=ledgers_reply(lines))


async def _run_members(
    message: dict[str, Any], session: AsyncSession, group: Group, actor: User | None
) -> Reply:
    # a container-inspection read (ADR-0011): slash-only, no lock, no action row (§0.7)
    parse_members(message["text"])  # no-arg guard; a trailing token is a usage error
    lines = [
        MemberLine(
            display_name=member.display_name,
            username=username,
            is_you=actor is not None and member.id == actor.id,
        )
        for member, username in await registered_members_with_usernames(session, group.id)
    ]
    return Reply(text=members_reply(lines))


def _expense_runner(parse: Callable[[str], ParsedExpense]) -> CommandRunner:
    async def run(
        message: dict[str, Any], session: AsyncSession, group: Group, actor: User | None
    ) -> Reply:
        return await _run_expense(message, session, group, actor, parse(message["text"]))

    return run


def _split_members(parsed: ParsedExpense, currency: str) -> list[SplitMember]:
    if parsed.split_type == "exact":
        # per-person amounts are money: minor-unit conversion needs the resolved currency
        members = []
        for ref, value in zip(parsed.participant_refs, parsed.participant_values, strict=True):
            minor, was_rounded = to_minor(value, currency)
            if was_rounded:
                # exact parts are the user's stated amounts: rounding one would either
                # mis-report the sum check or silently commit altered numbers (§3)
                raise ValueError(
                    f"{value} doesn't land on the smallest {currency} unit — "
                    f"exact amounts can't be finer than that."
                )
            members.append(SplitMember(user_ref=ref, exact_minor=minor))
        return members
    if parsed.split_type == "shares":
        return [
            SplitMember(user_ref=ref, weight=float(value))
            for ref, value in zip(parsed.participant_refs, parsed.participant_values, strict=True)
        ]
    if parsed.split_type == "percent":
        return [
            SplitMember(user_ref=ref, percent=float(value))
            for ref, value in zip(parsed.participant_refs, parsed.participant_values, strict=True)
        ]
    return [SplitMember(user_ref=ref) for ref in parsed.participant_refs]


async def _run_expense(
    message: dict[str, Any],
    session: AsyncSession,
    group: Group,
    actor: User | None,
    parsed: ParsedExpense,
) -> Reply:
    # lock before resolving the currency: it depends on the active ledger, which a
    # concurrent /switch could repoint between our read and the write (ADR-0003)
    await per_group_lock(session, group.id)
    await session.refresh(group)
    ledger = await session.get_one(Ledger, group.active_ledger_id)
    currency = resolve_currency(parsed.currency, ledger.logging_currency, group.home_currency)
    # to_minor runs before apply's recognized-currency check (ADR-0009), so an
    # amount-shaped error can mention an unrecognized code — accepted; don't "fix"
    # it by validating here, or the NL path stops inheriting the check
    amount_minor, was_rounded = to_minor(parsed.amount, currency)

    intent = AddExpense(
        payer_ref="me",
        amount_minor=amount_minor,
        currency=currency,
        description=parsed.description,
        split_type=parsed.split_type,
        participants=_split_members(parsed, currency),
    )
    ctx = ApplyContext(session=session, group=group, actor=actor, seed=message["message_id"])
    applied = await apply_intent(intent, ctx)
    assert isinstance(applied, AppliedExpense)
    text = expense_reply(
        ledger_name=ledger.name,
        expense_id=applied.expense_id,
        amount_minor=amount_minor,
        currency=currency,
        description=parsed.description,
        payer_name=applied.payer.display_name,
        participant_names=[u.display_name for u in applied.participants],
        rounded_from=parsed.amount if was_rounded else None,
        split_type=parsed.split_type,
        shares=[(u.display_name, applied.shares[u.id]) for u in applied.participants],
    )
    return Reply(text=text, undo_action_id=applied.action_id)


async def _run_settle(
    message: dict[str, Any], session: AsyncSession, group: Group, actor: User | None
) -> Reply:
    parsed = parse_settle(message["text"])
    if parsed.amount is None:
        sheet_intent = SettleUp(from_ref=parsed.from_ref, to_ref=parsed.to_ref)
        return await _sheet_reply(sheet_intent, session, group, actor)
    assert parsed.currency is not None
    # the currency is explicit on /settle (§4): no active-ledger resolution, so
    # minor-unit conversion can happen before the lock. It also runs before apply's
    # recognized-currency check (ADR-0009) — same accepted ordering as _run_expense
    amount_minor, was_rounded = to_minor(parsed.amount, parsed.currency)
    intent = SettleUp(
        from_ref=parsed.from_ref,
        to_ref=parsed.to_ref,
        amount_minor=amount_minor,
        currency=parsed.currency,
    )
    ctx = ApplyContext(session=session, group=group, actor=actor, seed=message["message_id"])
    applied = await apply_intent(intent, ctx)
    assert isinstance(applied, AppliedSettlement)
    ledger = await session.get_one(Ledger, group.active_ledger_id)
    text = settle_reply(
        ledger_name=ledger.name,
        from_name=applied.from_user.display_name,
        to_name=applied.to_user.display_name,
        amount_minor=applied.amount_minor,
        currency=applied.currency,
        rounded_from=parsed.amount if was_rounded else None,
    )
    return Reply(text=text, undo_action_id=applied.action_id)


async def _sheet_reply(
    intent: SettleUp, session: AsyncSession, group: Group, actor: User | None
) -> Reply:
    """The settle sheet (ADR-0007), shared by /settle and NL: a read — no lock,
    no action row (§0.7); the buttons' WYSIWYG amount tokens guard staleness."""
    pair = await resolve_refs(session, group.id, [intent.from_ref, intent.to_ref], actor)
    ledger = await session.get_one(Ledger, group.active_ledger_id)
    text, markup = await sheet_view(session, ledger, pair[intent.from_ref], pair[intent.to_ref])
    return Reply(text=text, markup=markup)


async def _run_delete(
    message: dict[str, Any], session: AsyncSession, group: Group, actor: User | None
) -> Reply:
    explicit_id = parse_delete(message["text"])
    expense_id = await _referenced_expense_id(message, session, group, explicit_id, DELETE_USAGE)

    ctx = ApplyContext(session=session, group=group, actor=actor, seed=message["message_id"])
    applied = await apply_intent(DeleteExpense(expense_id=expense_id), ctx)
    assert isinstance(applied, AppliedExpenseChange)
    ledger = await session.get_one(Ledger, group.active_ledger_id)
    text = delete_reply(
        ledger_name=ledger.name,
        expense_id=applied.expense.id,
        amount_minor=applied.expense.amount_minor,
        currency=applied.expense.currency,
        description=applied.expense.description,
    )
    return Reply(text=text, undo_action_id=applied.action_id)


async def _run_edit(
    message: dict[str, Any], session: AsyncSession, group: Group, actor: User | None
) -> Reply:
    parsed = parse_edit(message["text"])
    expense_id = await _referenced_expense_id(
        message, session, group, parsed.expense_id, EDIT_USAGE
    )

    intent = EditExpense(
        expense_id=expense_id, description=parsed.description, occurred_on=parsed.occurred_on
    )
    ctx = ApplyContext(session=session, group=group, actor=actor, seed=message["message_id"])
    applied = await apply_intent(intent, ctx)
    assert isinstance(applied, AppliedExpenseChange)
    ledger = await session.get_one(Ledger, group.active_ledger_id)
    text = edit_reply(
        ledger_name=ledger.name,
        expense_id=applied.expense.id,
        amount_minor=applied.expense.amount_minor,
        currency=applied.expense.currency,
        description=applied.expense.description,
        occurred_on=applied.expense.occurred_on,
    )
    return Reply(text=text, undo_action_id=applied.action_id)


async def _referenced_expense_id(
    message: dict[str, Any],
    session: AsyncSession,
    group: Group,
    explicit_id: int | None,
    usage: str,
) -> int:
    """Resolve the expense a delete/edit refers to (§11): reply primary, #id fallback."""
    # lock before resolving the reference: resolution + seal check + write must
    # see one consistent active ledger (ADR-0003)
    await per_group_lock(session, group.id)
    await session.refresh(group)
    reply_to = message.get("reply_to_message")
    expense_id = await resolve_expense_id(
        session,
        platform_chat_id=message["chat"]["id"],
        reply_message_id=reply_to["message_id"] if reply_to is not None else None,
        explicit_id=explicit_id,
    )
    if expense_id is None:
        raise ValueError(usage)
    return expense_id


async def _run_balance(
    message: dict[str, Any], session: AsyncSession, group: Group, actor: User | None
) -> Reply:
    intent = ShowBalance(scope=parse_balance(message["text"]))
    return await _balance_reply(intent, session, group, actor)


async def _balance_reply(
    intent: ShowBalance, session: AsyncSession, group: Group, actor: User | None
) -> Reply:
    """The show_balance read, shared by /balance and NL (§4: one Intent contract)."""
    # a read: no lock, no action row, sealed to the active ledger (§0.7, §0.10, §8)
    ledger = await session.get_one(Ledger, group.active_ledger_id)
    net = await net_positions(session, ledger.id)
    if intent.scope == "me":
        if actor is None:
            raise Rejection("I can't tell who you are — anonymous admins have no balance.")
        entries = [(actor.display_name, net.get(actor.id, {}))]
        return Reply(text=balance_reply(ledger_name=ledger.name, entries=entries, as_me=True))
    names = await display_names(session, list(net))
    entries = [(names[user_id], by_currency) for user_id, by_currency in net.items()]
    return Reply(text=balance_reply(ledger_name=ledger.name, entries=entries))


async def _run_setup(
    message: dict[str, Any], session: AsyncSession, group: Group, actor: User | None
) -> Reply:
    """/setup registers pre-existing members (§11): reply target and text_mentions.

    Permanent — the Reply never carries an undo_action_id, so no Undo button."""
    targets, bare_usernames, bot_names = _setup_entries(message)
    if not targets and not bare_usernames and not bot_names:
        return Reply(text=SETUP_GUIDANCE)

    lines: list[str] = []
    if targets:
        ctx = ApplyContext(session=session, group=group, actor=actor, seed=message["message_id"])
        applied = await apply_intent(Setup(targets=targets), ctx)
        assert isinstance(applied, AppliedSetup)
        lines = _setup_member_lines(applied)
    lines += [
        f"🚫 {username} can't be added from a bare @username — Telegram doesn't let me "
        "look accounts up by name. They can send a message here once, or someone can "
        "reply to one of their messages with /setup."
        for username in bare_usernames
    ]
    lines += [f"🚫 {name} is a bot — bots can't be members." for name in bot_names]
    return Reply(text="\n".join(lines))


def _setup_entries(message: dict[str, Any]) -> tuple[list[SetupTarget], list[str], list[str]]:
    """Everyone a /setup names (§11): resolvable targets (the reply target and
    text_mentions, which embed the user id), bare @usernames (unresolvable by the
    Bot API), and bots (never members)."""
    targets: list[SetupTarget] = []
    bare_usernames: list[str] = []
    bot_names: list[str] = []

    def collect(tg_user: dict[str, Any]) -> None:
        if tg_user.get("is_bot"):
            bot_names.append(_display_name(tg_user))  # no ghosts, no bot members (§11)
        else:
            targets.append(_setup_target(tg_user))

    reply_to = message.get("reply_to_message")
    if reply_to is not None and "from" in reply_to:
        collect(reply_to["from"])
    text = message.get("text", "")
    for entity in message.get("entities", []):
        if entity["type"] == "text_mention":
            collect(entity["user"])
        elif entity["type"] == "mention":
            bare_usernames.append(text[entity["offset"] : entity["offset"] + entity["length"]])
    return targets, bare_usernames, bot_names


def _setup_target(tg_user: dict[str, Any]) -> SetupTarget:
    return SetupTarget(
        platform_user_id=tg_user["id"],
        display_name=_display_name(tg_user),
        username=tg_user.get("username"),
    )


_RUNNERS: dict[str, CommandRunner] = {
    "setup": _run_setup,
    "homecurrency": _run_homecurrency,
    "currency": _run_currency,
    "newledger": _run_newledger,
    "ledgers": _run_ledgers,
    "members": _run_members,
    "switch": _run_switch,
    "archive": _run_archive,
    "unarchive": _run_unarchive,
    "equal": _expense_runner(parse_equal),
    "exact": _expense_runner(parse_exact),
    "shares": _expense_runner(parse_shares),
    "percent": _expense_runner(parse_percent),
    "balance": _run_balance,
    "settle": _run_settle,
    "delete": _run_delete,
    "edit": _run_edit,
}


async def _register_author(
    session: AsyncSession, group_id: int, tg_user: dict[str, Any]
) -> User | None:
    if tg_user.get("is_bot"):
        # GroupAnonymousBot (anonymous admins), Telegram service accounts: no ghosts (§11)
        return None
    return await register_member(
        session,
        group_id,
        platform_user_id=tg_user["id"],
        display_name=_display_name(tg_user),
        username=tg_user.get("username"),
    )


def _display_name(tg_user: dict[str, Any]) -> str:
    parts = [tg_user.get("first_name"), tg_user.get("last_name")]
    return " ".join(p for p in parts if p)


def _is_bot_added_to_group(my_chat_member: dict[str, Any]) -> bool:
    return (
        my_chat_member["chat"]["type"] in ("group", "supergroup")
        and not _is_member(my_chat_member["old_chat_member"])
        and _is_member(my_chat_member["new_chat_member"])
    )


def _is_member(chat_member: dict[str, Any]) -> bool:
    # Bot API: 'restricted' counts as in-the-group iff is_member is true
    status = chat_member["status"]
    if status == "restricted":
        return bool(chat_member.get("is_member"))
    return status in ("member", "administrator", "creator")


def _mentions_bot(text: str, bot_username: str | None) -> bool:
    return bot_username is not None and f"@{bot_username.lower()}" in text.lower()


def _command_of(text: str, bot_username: str | None) -> str | None:
    """'/start@expensir_bot arg' -> 'start'; None for non-commands and other bots' commands."""
    if not text.startswith("/"):
        return None
    command, _, addressee = text.split()[0][1:].partition("@")
    if addressee and (bot_username is None or addressee.lower() != bot_username.lower()):
        return None
    return command.lower() or None
