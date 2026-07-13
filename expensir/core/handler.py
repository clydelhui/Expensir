"""Transport-agnostic entrypoint: dispatch(update_dict) -> list[OutboundAction] (§0.5)."""

import contextlib
import dataclasses
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol, cast

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
from expensir.db.models import Action, Expense, FxRate, Group, Ledger, PendingIntent, User
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
    sealed_expense,
    split_shares,
)
from expensir.domain.balances import net_positions
from expensir.domain.currency import require_known_currency, resolve_currency
from expensir.domain.errors import AmbiguousExpense, AmbiguousRef, Rejection
from expensir.domain.fx import (
    FxProvider,
    api_legs,
    api_rate,
    api_rate_from_legs,
    equivalents_view,
    fmt_rate,
    refresh_api_rates,
    today_utc,
)
from expensir.domain.identity import (
    ambiguous_guidance,
    display_names,
    ensure_group,
    mark_left,
    register_member,
    registered_members_with_usernames,
    resolve_expense_id,
    resolve_expense_match,
    resolve_refs,
    usernames_of,
)
from expensir.domain.ledgers import find_ledger, ledgers_of
from expensir.domain.money import fmt, to_minor
from expensir.domain.settle import record_settlement, suggested_amount
from expensir.domain.transactions import (
    PAGE_SIZE,
    Direction,
    decode_cursor,
    list_transactions,
)
from expensir.domain.undo import ToggleDirection, toggle
from expensir.format.keyboards import (
    InlineKeyboard,
    confirm_keyboard,
    expense_pick_keyboard,
    pick_keyboard,
    redo_keyboard,
    transactions_pager_keyboard,
    undo_keyboard,
)
from expensir.format.render import (
    LedgerLine,
    MemberLine,
    PinView,
    action_proposal_reply,
    balance_reply,
    convert_reply,
    delete_reply,
    edit_reply,
    expense_pick_stage_reply,
    expense_reply,
    join_names,
    ledgers_reply,
    members_reply,
    pick_stage_reply,
    proposal_reply,
    rates_reply,
    settle_reply,
)
from expensir.format.transactions import transactions_fallback_reply, transactions_reply
from expensir.intents import nl
from expensir.intents.commands import (
    DELETE_USAGE,
    EDIT_USAGE,
    ParsedExpense,
    parse_archive,
    parse_autorate,
    parse_balance,
    parse_convert,
    parse_currency,
    parse_delete,
    parse_edit,
    parse_equal,
    parse_exact,
    parse_homecurrency,
    parse_members,
    parse_newledger,
    parse_percent,
    parse_rates,
    parse_setrate,
    parse_settle,
    parse_shares,
    parse_switch,
    parse_transactions,
    parse_unarchive,
)
from expensir.intents.schema import (
    AddExpense,
    ArchiveLedger,
    ClearFxRate,
    DeleteExpense,
    EditExpense,
    Intent,
    NewLedger,
    SetFxRate,
    SetHomeCurrency,
    SetLoggingCurrency,
    SettleUp,
    Setup,
    SetupTarget,
    ShowBalance,
    ShowTransactions,
    SplitMember,
    SwitchLedger,
    UnarchiveLedger,
    Unknown,
)
from expensir.llm.base import LLMClient, LLMUnavailable
from expensir.llm.wire import (
    WireAddExpense,
    WireDeleteExpense,
    WireEditExpense,
    WireSettleUp,
    WireSetup,
    WireUndoRedo,
    WireUnknown,
)

logger = logging.getLogger(__name__)

_TOGGLE_DATA = re.compile(r"^v1:(undo|redo):(\d+)$")
# Confirm/Cancel on a proposal (§10), keyed by the pending row's id
_CONFIRM_DATA = re.compile(r"^v1:(confirm|cancel):(\d+)$")
# a pick-list choice (§13): pending row + chosen member; the open slot is
# re-derived on tap, so the ref string never rides in the callback
_PICK_DATA = re.compile(r"^v1:pick:(\d+):(\d+)$")
# the expense flavour (§11 tertiary): pending row + chosen expense id
_PICKX_DATA = re.compile(r"^v1:pickx:(\d+):(\d+)$")
# the board [Settle] button (ADR-0006): tuple + shown amount as a staleness token
_SETTLE_DATA = re.compile(r"^v1:st:(\d+):(\d+):([A-Z]{3}):(\d+)$")
# a settle-sheet line (ADR-0007): same tuple + token, prefixed with the ledger —
# a sheet message is not the pinned board, so the tap can't resolve it from the chat
_SHEET_DATA = re.compile(r"^v1:sh:(\d+):(\d+):(\d+):([A-Z]{3}):(\d+)$")
# a /transactions pager tap (ADR-0012): pinned ledger, direction verb, keyset anchor
_TX_DATA = re.compile(r"^v1:tx:(\d+):([np]):(\d+):(expense|settlement):(\d+)$")
_INTENT: TypeAdapter[Intent] = TypeAdapter(Intent)
# an expense pick-list shows only this many, newest first (issue #14 grill):
# beyond it the message says what was dropped and points at #id
EXPENSE_PICK_CAP = 5
UNDONE_MARK = "\n\n↩️ Undone by "
UNDO_POINTER = (
    "I never undo from chat — tap the ↩️ Undo button on the message that recorded it instead."
)
SETUP_GUIDANCE = (
    "👥 To register someone who hasn't spoken yet, reply to one of their "
    "messages with /setup, or mention them by tapping their name so the "
    "mention links their account. A bare @username isn't enough."
)
PHOTO_FETCH_FAILURE = (
    "⚠️ I couldn't fetch that photo from Telegram — nothing was changed; " "try sending it again."
)


@dataclass
class Reply:
    """A command's rendered result; undo_action_id marks it undoable (§9).

    markup is for reads that carry their own buttons (the settle sheet,
    ADR-0007); an undoable result's Undo keyboard wins over it."""

    text: str
    undo_action_id: int | None = None
    markup: InlineKeyboard | None = None
    # a read that refreshed stale API rates re-renders the board (§13): the edit
    # rides along as data, rendered under the lock like every board edit
    board_sync: list[OutboundAction] = dataclasses.field(default_factory=list)


CommandRunner = Callable[
    [dict[str, Any], AsyncSession, Group, User | None, "Deps"], Awaitable[Reply]
]

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

HELP = (
    "📖 Expensir commands\n"
    "\n"
    "💸 Add an expense\n"
    "• Split equally: /equal 45 EUR dinner @alice @bob\n"
    "  (leave names off to split among everyone in the group)\n"
    "• Exact amounts: /exact 60 EUR taxi @alice=40 @bob=20\n"
    "• By shares: /shares 90 EUR villa @alice=2 @bob\n"
    "  (a bare name counts as 1 share)\n"
    "• By percent: /percent 100 EUR gift @alice=70 @bob=30\n"
    "The currency code is optional — without it I use the ledger's currency,\n"
    "or the group's home currency if the ledger has none set.\n"
    "\n"
    "⚖️ Balances & settling\n"
    "• /balance — who owes whom (/balance me for just yours)\n"
    "• /settle @alice 30 EUR — record that you paid Alice 30 EUR\n"
    "  (/settle @bob @alice 30 EUR if Bob paid her).\n"
    "  Bare /settle @alice shows what's still open between you two.\n"
    "\n"
    "📜 History & fixing mistakes\n"
    "• /transactions — the ledger's history, newest first\n"
    "• Reply to an expense with /delete to remove it, or /delete 12\n"
    "  (the #id on its line)\n"
    "• Reply to an expense with /edit 2026-07-01 team lunch —\n"
    "  a new date and/or description, all in one message\n"
    "  (or pick it by id: /edit 12 2026-07-01 team lunch).\n"
    "  Amounts and participants can't be edited; delete and re-add.\n"
    "\n"
    "👥 People\n"
    "• /members — everyone registered in this group\n"
    "• /setup — register someone who hasn't spoken yet: reply to one\n"
    "  of their messages with /setup (a bare @username isn't enough)\n"
    "\n"
    "📒 Ledgers & currency\n"
    "• /ledgers — list your ledgers\n"
    "• /newledger Tokyo JPY — create one (currency optional) and switch to it\n"
    "• /switch Tokyo — make another ledger active\n"
    "• /archive Tokyo and /unarchive Tokyo — close and reopen one\n"
    "• /homecurrency USD — the group's home currency; other currencies\n"
    "  also show a ≈ equivalent in it\n"
    "• /currency JPY — this ledger's own logging currency\n"
    "\n"
    "💱 Exchange rates (display only — never used in the math)\n"
    "• /setrate USD SGD 1.35 — pin the rate my ≈ figures use\n"
    "  (leave the number off to pin today's live rate)\n"
    "• /autorate USD SGD — unpin, back to the live daily rate\n"
    "• /rates — what's pinned, and today's live rates\n"
    "• /convert SGD — everyone's balance consolidated into one currency\n"
    "\n"
    "💬 You can also just @mention me or reply to my messages in plain\n"
    'language — "alice paid 20 for coffee" — receipt photos included.'
)


class FileSource(Protocol):
    """Fetches a Telegram file's bytes by file_id (getFile + download, §13).

    Returns None when the fetch fails, so the vision door replies transiently
    instead of crashing mid-update (issue #15 grill)."""

    async def download_file(self, file_id: str) -> bytes | None: ...


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
    # photo bytes for the vision door (issue #15); None leaves photos unanswered
    files: FileSource | None = None
    pending_ttl_minutes: int = 15  # proposal TTL (§10, §17)
    # live display rates (§7.5); None (unconfigured) degrades to pins + (≈ n/a)
    fx: FxProvider | None = None


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
    data = callback.get("data") or ""
    verb = data.split(":")[1] if data.startswith("v1:") and data.count(":") >= 2 else "unknown"
    logger.info("intent=cb:%s", verb)
    ack = AnswerCallbackQuery(callback_query_id=callback["id"])
    confirm = _CONFIRM_DATA.match(data)
    if confirm is not None:
        return await _handle_confirm_tap(callback, confirm, deps)
    pick = _PICK_DATA.match(data)
    if pick is not None:
        return await _handle_pick_tap(callback, pick, deps)
    pickx = _PICKX_DATA.match(data)
    if pickx is not None:
        return await _handle_pickx_tap(callback, pickx, deps)
    settle = _SETTLE_DATA.match(data)
    if settle is not None:
        return await _handle_settle_tap(callback, settle, deps)
    sheet = _SHEET_DATA.match(data)
    if sheet is not None:
        return await _handle_sheet_tap(callback, sheet, deps)
    tx = _TX_DATA.match(data)
    if tx is not None:
        return await _handle_tx_tap(callback, tx, deps)
    match = _TOGGLE_DATA.match(data)
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
    markup: InlineKeyboard | None

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
        except AmbiguousRef as ambiguous:
            # late ambiguity (issue #14 grill): a new member made a unique ref
            # ambiguous between propose and confirm. Commit nothing and put the
            # proposal (back) in the pick stage — the row survives, the clock
            # restarts, and Confirm returns once the slot is pinned (§13).
            choices = await _pick_labels(session, ambiguous.candidates)
            pending_store.refresh(pending, ttl_minutes=deps.pending_ttl_minutes)
            ack.text = "Someone new matches that name — tap who you meant."
            edit = EditMessage(
                chat_id=chat["id"],
                message_id=message["message_id"],
                text=pick_stage_reply(
                    ledger_name=ledger.name, gist=_proposal_gist(intent), ref=ambiguous.ref
                ),
                reply_markup=pick_keyboard(pending.id, choices),
            )
            return [ack, edit]
        except AmbiguousExpense as ambiguous:
            # same rule for a described expense that stopped being unique — or a
            # Confirm racing a still-open expense slot: nothing commits, the
            # proposal (re-)enters the expense pick stage (§13)
            pending_store.refresh(pending, ttl_minutes=deps.pending_ttl_minutes)
            ack.text = "More than one expense matches that — tap the one you meant."
            text, markup = _expense_pick_stage(intent, ambiguous, ledger, pending.id)
            edit = EditMessage(
                chat_id=chat["id"],
                message_id=message["message_id"],
                text=text,
                reply_markup=markup,
            )
            return [ack, edit]
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


async def _handle_pick_tap(
    callback: dict[str, Any], match: re.Match[str], deps: Deps
) -> list[OutboundAction]:
    """A pick-list choice (§13): pin the open slot to the tapped member and
    re-render the proposal — the next open slot, or Confirm once all pinned."""

    def pin(
        intent: Intent, slot: AmbiguousRef | AmbiguousExpense | None, chosen: int
    ) -> Intent | None:
        if not isinstance(slot, AmbiguousRef) or chosen not in {u.id for u in slot.candidates}:
            return None
        return nl.pin_ref(intent, slot.ref, chosen)

    return await _handle_pick_choice(callback, match, deps, pin)


async def _handle_pickx_tap(
    callback: dict[str, Any], match: re.Match[str], deps: Deps
) -> list[OutboundAction]:
    """The expense flavour of a pick (§11 tertiary, §13): pin the descriptive
    slot to the tapped expense and re-render — same loop, same Confirm gate."""

    def pin(
        intent: Intent, slot: AmbiguousRef | AmbiguousExpense | None, chosen: int
    ) -> Intent | None:
        if not isinstance(slot, AmbiguousExpense) or chosen not in {e.id for e in slot.candidates}:
            return None
        return nl.pin_expense(intent, chosen)

    return await _handle_pick_choice(callback, match, deps, pin)


async def _handle_pick_choice(
    callback: dict[str, Any],
    match: re.Match[str],
    deps: Deps,
    pin: Callable[[Intent, AmbiguousRef | AmbiguousExpense | None, int], Intent | None],
) -> list[OutboundAction]:
    """The shared pick-tap loop (§13): re-derive the open slot, let `pin`
    apply the choice (None = a stale keyboard), and re-render the proposal."""
    ack = AnswerCallbackQuery(callback_query_id=callback["id"])
    pending_id, chosen_id = int(match.group(1)), int(match.group(2))

    message = callback.get("message") or {}
    chat = message.get("chat")
    if chat is None or chat["type"] not in ("group", "supergroup"):
        return [ack]

    async with deps.session_factory() as session, session.begin():
        group = await ensure_group(session, chat["id"], chat.get("title"))
        presser = await _register_author(session, group.id, callback["from"])
        if presser is None:
            ack.text = "I can't tell who pressed that — anonymous admins can't pick."
            return [ack]
        pending = await session.get(PendingIntent, pending_id)
        if pending is None or pending.chat_id != chat["id"]:
            ack.text = "This proposal was already handled."
            return [ack]

        if pending_store.is_expired(pending):
            # expiry is computed on read (§10.4), on picks like on confirms
            await session.delete(pending)
            ack.text = "Nothing was recorded."
            edit = EditMessage(
                chat_id=chat["id"],
                message_id=message["message_id"],
                text="⌛ Expired — nothing was recorded. Send it again if it still applies.",
            )
            return [ack, edit]

        ledger = await session.get_one(Ledger, pending.ledger_id)
        intent = _INTENT.validate_python(pending.intent_json)
        converted = nl.ConvertedIntent(intent=intent, rounded_from=None)
        try:
            slot = await _first_ambiguity(
                intent, converted, session, group, presser, ledger, pending
            )
            pinned = pin(intent, slot, chosen_id)
            if pinned is None:
                # a stale keyboard: the slot was already pinned, or a refine changed
                # the intent under this button — re-render what is actually open
                ack.text = "That changed — the proposal is showing its current state."
            else:
                intent = pinned
                pending.intent_json = intent.model_dump(mode="json")
                pending_store.refresh(pending, ttl_minutes=deps.pending_ttl_minutes)
                converted = nl.ConvertedIntent(intent=intent, rounded_from=None)
                ack.text = "Got it."
            text, markup = await _proposal_stage(
                intent, converted, session, group, presser, ledger, pending
            )
        except (Rejection, ValueError) as exc:
            # the proposal can no longer render (a match with no candidates left,
            # an unknown ref surfacing after the ambiguous one): bury it like a
            # failed confirm rather than crash into a Telegram retry loop (§0.9)
            await session.delete(pending)  # consumed: a reply starts fresh (§10.4)
            ack.text = "Nothing was recorded."
            return [
                ack,
                EditMessage(
                    chat_id=chat["id"],
                    message_id=message["message_id"],
                    text=f"⚠️ That changed while you were deciding — nothing was recorded. {exc}",
                ),
            ]

    return [
        ack,
        EditMessage(
            chat_id=chat["id"],
            message_id=message["message_id"],
            text=text,
            reply_markup=markup,
        ),
    ]


async def _first_ambiguity(
    intent: Intent,
    converted: nl.ConvertedIntent,
    session: AsyncSession,
    group: Group,
    actor: User,
    ledger: Ledger,
    pending: PendingIntent,
) -> AmbiguousRef | AmbiguousExpense | None:
    """The proposal's first open pick slot (§13, one at a time) — a member ref
    or a descriptive expense — re-derived on read like expiry: pick-list state
    is never stored."""
    try:
        if isinstance(intent, AddExpense):
            await _expense_proposal_text(
                intent, converted, session, group, actor, ledger, pending.seed
            )
        else:
            await _proposal_summary(intent, session, group, actor, ledger)
    except (AmbiguousRef, AmbiguousExpense) as ambiguous:
        return ambiguous
    return None


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
    if isinstance(intent, SetFxRate):
        assert intent.rate is not None  # NL never proposes the fetch-and-pin form (§7.5)
        return _set_fx_rate_text(intent.base, intent.quote, intent.rate)
    if isinstance(intent, ClearFxRate):
        return _autorate_text(intent.base, intent.quote)
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


def _set_fx_rate_text(base: str, quote: str, rate: float) -> str:
    return (
        f"📌 Pinned 1 {base} = {fmt_rate(rate)} {quote} for ≈ figures. "
        f"Frozen until you /setrate again or /autorate {base} {quote}."
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


async def _handle_tx_tap(
    callback: dict[str, Any], match: re.Match[str], deps: Deps
) -> list[OutboundAction]:
    """A /transactions pager tap (ADR-0012): a plain read — no lock, no action
    row — that re-renders the tapped listing in place (snapshot semantics).
    The ledger is pinned in the callback, so the tap keeps paging the ledger
    the listing was rendered for even if the group /switch'ed meanwhile."""
    ack = AnswerCallbackQuery(callback_query_id=callback["id"])
    ledger_id = int(match.group(1))
    direction: Direction = "older" if match.group(2) == "n" else "newer"
    try:
        cursor = decode_cursor(":".join(match.group(3, 4, 5)))
    except ValueError:
        # a forged epoch_us beyond datetime's range: no such anchor, nothing to page
        return [ack]

    message = callback.get("message") or {}
    chat = message.get("chat")
    if chat is None or chat["type"] not in ("group", "supergroup"):
        return [ack]

    async with deps.session_factory() as session, session.begin():
        group = await ensure_group(session, chat["id"], chat.get("title"))
        ledger = await session.get(Ledger, ledger_id)
        if ledger is None or ledger.group_id != group.id:
            ack.text = "That button doesn't match anything I recorded."
            return [ack]
        page = await list_transactions(
            session, ledger.id, limit=PAGE_SIZE, cursor=cursor, direction=direction
        )
        if not page.rows and page.total and not (page.has_newer or page.has_older):
            # deletions left nothing strictly beyond the anchor on either side,
            # yet the ledger still stands (only the anchor row survives): the
            # past-the-end page would dead-end, so reset to /transactions' page 1
            page = await list_transactions(session, ledger.id, limit=PAGE_SIZE)
        if page.rows:
            text = transactions_reply(ledger_name=ledger.name, page=page)
        else:
            # past the end: the rows behind the tapped button were deleted
            # meanwhile; the way back anchors on the tapped cursor itself
            text = transactions_fallback_reply(
                ledger_name=ledger.name, page=page, direction=direction
            )
        markup = transactions_pager_keyboard(ledger.id, page, cursor)
    edit = EditMessage(
        chat_id=chat["id"], message_id=message["message_id"], text=text, reply_markup=markup
    )
    return [ack, edit]


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
    message = _claim_caption_command(message, deps)
    image: bytes | None = None
    if _vision_invocation(message, deps):
        # photo bytes are fetched BEFORE the transaction below (issue #15
        # review): a slow download must not hold a pooled DB connection or
        # delay the webhook's 200 any longer than the model call already does
        image = await _download_photo(message, deps)
        if image is None:
            return [SendMessage(chat_id=message["chat"]["id"], text=PHOTO_FETCH_FAILURE)]
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
        if command is not None and (command in _INLINE_COMMANDS or command in _RUNNERS):
            logger.info("intent=cmd:%s", command)
        if command == "start":
            active = await session.get_one(Ledger, group.active_ledger_id)
            text = f"📒 {active.name} • Expensir is ready — try /equal, or /balance."
            return [SendMessage(chat_id=message["chat"]["id"], text=text)]
        if command == "help":
            return [SendMessage(chat_id=message["chat"]["id"], text=HELP)]
        runner = _RUNNERS.get(command or "")
        if runner is not None:
            try:
                reply = await _run_command(runner, message, session, group, actor, deps)
            except AmbiguousRef as ambiguous:
                # ambiguous reference resolution makes ANY intent fuzzy (§0.7):
                # the slash door parks a pick-stage proposal too (§13)
                return await _park_ambiguous_slash(ambiguous, message, session, group, actor, deps)
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
                *reply.board_sync,
            ]

        reply_to = message.get("reply_to_message")
        if reply_to is not None and deps.llm is not None:
            pending = await pending_store.by_message(
                session, message["chat"]["id"], reply_to["message_id"]
            )
            if pending is not None:
                if "photo" in message and image is None:
                    # the vision door is closed (or this is an album stray):
                    # the photo goes unanswered, but expiry burial is state
                    # hygiene, not an answer — it still happens (issue #15 review)
                    if pending_store.is_expired(pending):
                        return await _bury_expired(pending, session)
                    return []
                return await _handle_reply_to_pending(
                    pending, message, session, group, actor, deps, image
                )
            if _from_this_bot(reply_to, deps.bot_username):
                # a reply to any NON-pending bot message — an old result, the
                # board, a dead proposal — is a fresh NL intent, exactly as if
                # @mentioned: the mention tax lifts after the first interaction (§10.5)
                if "photo" in message:
                    if image is None:
                        return []  # door closed or album stray: invisible (issue #15)
                    return await _handle_vision(message, session, group, actor, deps, image)
                return await _handle_nl_text(message, session, group, actor, deps)

        text = message.get("text", "")
        if _mentions_bot(text, deps.bot_username) and deps.llm is not None:
            return await _handle_nl_text(message, session, group, actor, deps)

        # a receipt photo needs the same invocation as text (§13 privacy): a
        # mention in its caption (the reply door is handled above)
        if (
            "photo" in message
            and image is not None
            and _mentions_bot(message.get("caption", ""), deps.bot_username)
        ):
            return await _handle_vision(message, session, group, actor, deps, image)

    return []


async def _park_ambiguous_slash(
    ambiguous: AmbiguousRef,
    message: dict[str, Any],
    session: AsyncSession,
    group: Group,
    actor: User | None,
    deps: Deps,
) -> list[OutboundAction]:
    """A slash mutation that hit an ambiguous reference parks as a proposal in
    the pick stage (§0.7, §13) — same loop, same buttons, same commit."""
    if ambiguous.intent is None or actor is None:
        # a read (the settle sheet) can't host a pick-list, and an anonymous
        # admin can't propose: guidance instead
        return [SendMessage(chat_id=message["chat"]["id"], text=ambiguous_guidance(ambiguous.ref))]
    ledger = await session.get_one(Ledger, group.active_ledger_id)
    converted = nl.ConvertedIntent(intent=ambiguous.intent, rounded_from=None)
    return await _propose(converted, message, session, group, actor, deps, ledger)


def _from_this_bot(reply_to: dict[str, Any], bot_username: str | None) -> bool:
    """Whether a reply target is one of OUR messages — another bot's isn't ours,
    and with no known username we claim nothing rather than guess."""
    sender = reply_to.get("from") or {}
    if not sender.get("is_bot") or bot_username is None:
        return False
    return str(sender.get("username", "")).lower() == bot_username.lower()


async def _handle_reply_to_pending(
    pending: PendingIntent,
    message: dict[str, Any],
    session: AsyncSession,
    group: Group,
    actor: User | None,
    deps: Deps,
    image: bytes | None = None,
) -> list[OutboundAction]:
    """A reply to a live proposal is a correction (§10.2): refine the parked
    intent and edit the proposal in place — the reply's author is the actor.
    image is the reply's photo, already fetched (issue #15): the vision refine
    MERGES it into the parked intent; the caption is the correction text."""
    assert deps.llm is not None and pending.message_id is not None
    if actor is None:
        return []  # anonymous admins can't correct, like they can't propose
    if pending_store.is_expired(pending):
        # expiry is computed on read (§10.4); no resend dead-end: the reply that
        # discovers it buries the proposal AND is processed as a fresh intent
        buried = await _bury_expired(pending, session)
        if image is not None:
            return [*buried, *await _handle_vision(message, session, group, actor, deps, image)]
        return [*buried, *await _handle_nl_text(message, session, group, actor, deps)]
    correction = _strip_bot_mention(
        message.get("text", "") or message.get("caption", ""), deps.bot_username
    )
    ledger = await session.get_one(Ledger, pending.ledger_id)  # the PINNED ledger (§10)
    # when the proposal is waiting on a pick, the reply may be the answer (§13):
    # hand the model the open slot's candidates as ready-to-echo pinned refs
    prior = _INTENT.validate_python(pending.intent_json)
    try:
        slot = await _first_ambiguity(
            prior,
            nl.ConvertedIntent(intent=prior, rounded_from=None),
            session,
            group,
            actor,
            ledger,
            pending,
        )
    except (Rejection, ValueError):
        # the parked intent no longer renders at all (its matches vanished):
        # no choices to offer, but the correction itself may repair it
        slot = None
    candidates = None
    if isinstance(slot, AmbiguousRef):
        labels = await _pick_labels(session, slot.candidates)
        candidates = [f"id:{user_id} = {label}" for user_id, label in labels]
    elif isinstance(slot, AmbiguousExpense):
        # capped like the buttons: the reply picks among what the message shows
        candidates = [
            f"expense_id:{e.id} = {e.description} — {fmt(e.amount_minor, e.currency)}"
            for e in slot.candidates[:EXPENSE_PICK_CAP]
        ]
    try:
        wire = await deps.llm.refine(pending.intent_json, correction, candidates, image=image)
    except LLMUnavailable:
        # a transport failure is NOT the user's sentence (issue #13 grill), and
        # it is no reason to touch the proposal either
        return [
            SendMessage(
                chat_id=pending.chat_id,
                text=(
                    "⚠️ I couldn't reach my language model just now — the proposal is "
                    "unchanged; try that correction again in a moment."
                ),
            )
        ]
    if image is not None and not isinstance(wire, WireAddExpense | WireSettleUp | WireUnknown):
        # a photo only ever means money moving (§12): the same coercion as the
        # fresh photo door — the model is never trusted to widen its own door
        # (§0), however the conflicting refine/vision addenda resolve
        wire = WireUnknown(reason=f"vision refine produced a non-photo kind: {wire.kind}")
    logger.info("intent=refine:%s", wire.kind)
    if isinstance(wire, WireUndoRedo):
        # detected, never honored (§9) — and not a correction: proposal untouched
        return [SendMessage(chat_id=pending.chat_id, text=UNDO_POINTER)]
    if isinstance(wire, WireSetup):
        # targets come from the MESSAGE (§11), and this reply targets the BOT's
        # proposal — without a text_mention there is nobody to register
        targets, _, _ = _setup_entries(message)
        if not targets:
            return [SendMessage(chat_id=pending.chat_id, text=SETUP_GUIDANCE)]
        converted = nl.ConvertedIntent(intent=Setup(targets=targets), rounded_from=None)
    else:
        converted = nl.to_intent(
            wire, logging_currency=ledger.logging_currency, home_currency=group.home_currency
        )
    intent = converted.intent
    if isinstance(intent, Unknown):
        # not readable as a correction: the proposal stands exactly as it was
        return [
            SendMessage(
                chat_id=pending.chat_id,
                text=(
                    "🤷 I couldn't read that as a correction — the proposal is unchanged. "
                    "Reply again with the fix, or tap ✖ Cancel."
                ),
            )
        ]
    try:
        reply = await _nl_read_reply(intent, session, group, actor, deps)
    except AmbiguousRef as ambiguous:
        # a read can't host a pick-list stage: guidance, proposal untouched
        return [SendMessage(chat_id=pending.chat_id, text=ambiguous_guidance(ambiguous.ref))]
    except (Rejection, ValueError) as exc:
        return [SendMessage(chat_id=pending.chat_id, text=str(exc))]
    if reply is not None:
        # a read mid-decision runs inline; it is not a correction, so the
        # proposal (and its TTL) stays exactly as it was (issue #14 grill)
        return [
            SendMessage(chat_id=pending.chat_id, text=reply.text, reply_markup=reply.markup),
            *reply.board_sync,
        ]
    # a "me" the correction introduces is the REPLIER's (issue #14 grill); the
    # original proposer's refs were pinned at park time and echo back as id:<n>
    intent = nl.pin_me(intent, actor.id)
    try:
        # a descriptive expense reference pins NOW when unique, like the fresh
        # door (§10.3 WYSIWYG): the previewed #id is the one that commits
        intent = await _pin_unique_match(intent, session, ledger)
    except (Rejection, ValueError) as exc:
        return [SendMessage(chat_id=pending.chat_id, text=str(exc))]
    try:
        # savepoint: a rejected correction (unknown member, bad currency) must
        # not kill a good proposal — everything rolls back and it stands.
        # The seed stays the ORIGINAL proposal's: WYSIWYG shares survive
        # refines (§7.1); an ambiguous new ref renders as the pick stage (§13).
        async with session.begin_nested():
            text, markup = await _proposal_stage(
                intent, converted, session, group, actor, ledger, pending
            )
            pending.intent_json = intent.model_dump(mode="json")
            pending_store.refresh(pending, ttl_minutes=deps.pending_ttl_minutes)
    except (Rejection, ValueError) as exc:
        return [SendMessage(chat_id=pending.chat_id, text=str(exc))]
    return [
        EditMessage(
            chat_id=pending.chat_id,
            message_id=pending.message_id,
            text=text,
            reply_markup=markup,
        )
    ]


async def _handle_nl_text(
    message: dict[str, Any], session: AsyncSession, group: Group, actor: User | None, deps: Deps
) -> list[OutboundAction]:
    """The NL text path (§12): extract -> propose; nothing commits until Confirm (§0.7)."""
    assert deps.llm is not None
    chat_id = message["chat"]["id"]
    stripped = _strip_bot_mention(message.get("text", ""), deps.bot_username)
    logger.debug("nl text: %r", stripped)
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
    logger.info("intent=nl:%s", wire.kind)
    return await _run_fresh_wire(wire, message, session, group, actor, deps)


async def _run_fresh_wire(
    wire: Any,
    message: dict[str, Any],
    session: AsyncSession,
    group: Group,
    actor: User | None,
    deps: Deps,
) -> list[OutboundAction]:
    """A fresh extraction's shared tail (text and vision doors alike): the
    savepoint scopes a rejection's rollback to the NL handling itself, like
    _run_command — author registration earlier in the transaction still commits."""
    chat_id = message["chat"]["id"]
    try:
        async with session.begin_nested():
            return await _run_nl_intent(wire, message, session, group, actor, deps)
    except AmbiguousRef as ambiguous:
        # only reads reach here — mutations render ambiguity as a pick-list
        # stage (§13); a read can't host one, so guidance instead
        return [SendMessage(chat_id=chat_id, text=ambiguous_guidance(ambiguous.ref))]
    except (Rejection, ValueError) as exc:
        return [SendMessage(chat_id=chat_id, text=str(exc))]


def _vision_door_open(deps: Deps) -> bool:
    """Photos are invisible unless BOTH a vision-capable LLM and a file source
    are wired (issue #15 grill) — exactly like text mentions with no LLM."""
    return deps.llm is not None and deps.llm.supports_vision and deps.files is not None


def _claim_caption_command(message: dict[str, Any], deps: Deps) -> dict[str, Any]:
    """A slash command typed as a photo caption is deterministic input: surface
    it as the message's text so the command path claims it, never the fuzzy
    vision door (issue #15 review). A caption that is no known command of ours
    stays a caption."""
    caption = message.get("caption", "")
    if "text" in message or not caption.startswith("/"):
        return message
    command = _command_of(caption, deps.bot_username)
    if command not in _INLINE_COMMANDS and command not in _RUNNERS:
        return message
    return {**message, "text": caption, "entities": message.get("caption_entities", [])}


def _album_stray(message: dict[str, Any]) -> bool:
    """A media group (album) arrives as one update per photo, so reading every
    item would run vision N times for one user action (issue #15 review). Only
    the captioned item speaks for an album; caption-less items are invisible."""
    return "media_group_id" in message and "caption" not in message


def _vision_invocation(message: dict[str, Any], deps: Deps) -> bool:
    """True when this photo will enter a vision door, so its bytes are needed
    (§13 invocation rules: a caption mention, or a reply to this bot)."""
    if "photo" not in message or "text" in message or not _vision_door_open(deps):
        return False
    if _album_stray(message):
        return False
    reply_to = message.get("reply_to_message")
    if reply_to is not None and _from_this_bot(reply_to, deps.bot_username):
        return True
    return _mentions_bot(message.get("caption", ""), deps.bot_username)


async def _download_photo(message: dict[str, Any], deps: Deps) -> bytes | None:
    assert deps.files is not None  # _vision_invocation checked the door
    largest = _largest_photo(message)
    if largest is None:
        return None  # malformed photo array: nothing usable to fetch
    return await deps.files.download_file(largest["file_id"])


def _largest_photo(message: dict[str, Any]) -> dict[str, Any] | None:
    """Telegram sends several compressed sizes of one photo; the largest reads
    best (issue #15). Sizes usually arrive smallest-first, but pick by area.
    None when the array is empty or no size carries a file_id (forged/malformed
    update — the webhook validates only the secret header, issue #15 review)."""
    sizes = [
        size for size in cast(list[dict[str, Any]], message.get("photo") or []) if "file_id" in size
    ]
    if not sizes:
        return None
    return max(sizes, key=lambda size: size.get("width", 0) * size.get("height", 0))


async def _bury_expired(pending: PendingIntent, session: AsyncSession) -> list[OutboundAction]:
    """Expiry discovered by a reply (§10.4): mark the proposal dead, drop the row."""
    actions: list[OutboundAction] = []
    if pending.message_id is not None:
        actions.append(
            EditMessage(
                chat_id=pending.chat_id,
                message_id=pending.message_id,
                text="⌛ Expired — nothing was recorded.",
            )
        )
    await session.delete(pending)
    return actions


async def _handle_vision(
    message: dict[str, Any],
    session: AsyncSession,
    group: Group,
    actor: User | None,
    deps: Deps,
    image: bytes,
) -> list[OutboundAction]:
    """The receipt-photo door (issue #15): extract_vision -> the identical
    propose loop as text (§0.7); a photo never commits directly. The image
    bytes were fetched before the transaction opened."""
    assert deps.llm is not None
    chat_id = message["chat"]["id"]
    caption = _strip_bot_mention(message.get("caption", ""), deps.bot_username)
    try:
        wire = await deps.llm.extract_vision(image, caption)
    except LLMUnavailable:
        return [
            SendMessage(
                chat_id=chat_id,
                text=(
                    "⚠️ I couldn't reach my vision model just now — try again in "
                    'a moment, or log it as text, e.g. "I paid 40 for dinner".'
                ),
            )
        ]
    if not isinstance(wire, WireAddExpense | WireSettleUp | WireUnknown):
        # a photo only ever means money moving (§12): the restriction lives in
        # the prompt, but the model is never trusted to widen its own door (§0)
        wire = WireUnknown(reason=f"vision produced a non-photo kind: {wire.kind}")
    logger.info("intent=vision:%s", wire.kind)
    if isinstance(wire, WireUnknown):
        # "try rephrasing" makes no sense for a photo (issue #15 grill)
        return [
            SendMessage(
                chat_id=chat_id,
                text=(
                    "🤷 I couldn't read that receipt — tell me the amount and what "
                    'it was for instead, e.g. "I paid 40 for dinner".'
                ),
            )
        ]
    return await _run_fresh_wire(wire, message, session, group, actor, deps)


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
            intent=await _resolve_expense_wire(wire, message, session, ledger), rounded_from=None
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
    reply = await _nl_read_reply(intent, session, group, actor, deps)
    if reply is not None:
        return [
            SendMessage(chat_id=message["chat"]["id"], text=reply.text, reply_markup=reply.markup),
            *reply.board_sync,
        ]
    return await _propose(converted, message, session, group, actor, deps, ledger)


async def _pin_unique_match(intent: Intent, session: AsyncSession, ledger: Ledger) -> Intent:
    """Pin a refined delete/edit's descriptive match when it is unique (§10.3);
    ambiguity stays unpinned for the pick stage, no match raises the guidance."""
    if (
        not isinstance(intent, DeleteExpense | EditExpense)
        or intent.expense_id is not None
        or intent.match is None
    ):
        return intent
    try:
        expense = await resolve_expense_match(session, ledger.id, intent.match)
    except AmbiguousExpense:
        return intent  # the proposal renders the expense pick stage (§13)
    return intent.model_copy(update={"expense_id": expense.id})


async def _resolve_expense_wire(
    wire: Any, message: dict[str, Any], session: AsyncSession, ledger: Ledger
) -> DeleteExpense | EditExpense:
    """Pin the expense a NL delete/edit means (§11): reply-to-target primary,
    a bare #id fallback, descriptive match tertiary. A unique match pins NOW —
    the proposal names one concrete expense, so a deletion in the meantime
    fails the confirm instead of silently retargeting (§10.3 WYSIWYG)."""
    reply_to = message.get("reply_to_message")
    expense_id = await resolve_expense_id(
        session,
        platform_chat_id=message["chat"]["id"],
        reply_message_id=reply_to["message_id"] if reply_to is not None else None,
        explicit_id=wire.expense_id,
    )
    if expense_id is None and wire.match is not None:
        # ambiguity parks unpinned: the proposal renders the expense pick stage (§13)
        with contextlib.suppress(AmbiguousExpense):
            expense_id = (await resolve_expense_match(session, ledger.id, wire.match)).id
    if expense_id is None and wire.match is None:
        raise Rejection(
            '🤷 Which expense? Reply to its result message or give its #id — e.g. "delete #42".'
        )
    if isinstance(wire, WireDeleteExpense):
        return DeleteExpense(expense_id=expense_id, match=wire.match)
    return EditExpense(
        expense_id=expense_id,
        match=wire.match,
        description=wire.description,
        occurred_on=wire.occurred_on,
    )


async def _nl_read_reply(
    intent: Intent, session: AsyncSession, group: Group, actor: User | None, deps: Deps
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
        return await _balance_reply(intent, session, group, actor, deps)
    if isinstance(intent, ShowTransactions):
        return await _transactions_reply(session, group)
    if isinstance(intent, SettleUp) and intent.amount_minor is None:
        return await _sheet_reply(intent, session, group, actor, deps)
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
    # first-person refs anchor to their introducer (issue #14 grill): pin the
    # proposer's "me" before the intent is rendered or parked
    intent = nl.pin_me(converted.intent, actor.id)
    parked = await pending_store.park(
        session,
        chat_id=message["chat"]["id"],
        ledger_id=ledger.id,
        proposer=actor,
        seed=message["message_id"],
        intent=intent,
        ttl_minutes=deps.pending_ttl_minutes,
    )
    # rendered AFTER parking: an ambiguous ref renders as the pick-list stage,
    # whose buttons carry the pending row's id (§13); a rejection rolls the
    # park back with everything else
    text, markup = await _proposal_stage(intent, converted, session, group, actor, ledger, parked)
    return [
        SendMessage(
            chat_id=message["chat"]["id"],
            text=text,
            reply_markup=markup,
            records_message_for_pending_id=parked.id,
        )
    ]


async def _proposal_stage(
    intent: Intent,
    converted: nl.ConvertedIntent,
    session: AsyncSession,
    group: Group,
    actor: User,
    ledger: Ledger,
    pending: PendingIntent,
) -> tuple[str, InlineKeyboard]:
    """What the proposal message shows right now (§10): the full WYSIWYG preview
    with Confirm/Cancel — or, while a reference is ambiguous, the pick-list
    stage for the FIRST open slot (one slot at a time, §13)."""
    try:
        if isinstance(intent, AddExpense):
            text = await _expense_proposal_text(
                intent, converted, session, group, actor, ledger, pending.seed
            )
        else:
            summary = await _proposal_summary(intent, session, group, actor, ledger)
            text = action_proposal_reply(ledger_name=ledger.name, summary=summary)
    except AmbiguousRef as ambiguous:
        choices = await _pick_labels(session, ambiguous.candidates)
        text = pick_stage_reply(
            ledger_name=ledger.name, gist=_proposal_gist(intent), ref=ambiguous.ref
        )
        return text, pick_keyboard(pending.id, choices)
    except AmbiguousExpense as ambiguous:
        return _expense_pick_stage(intent, ambiguous, ledger, pending.id)
    return text, confirm_keyboard(pending.id)


def _expense_pick_stage(
    intent: Intent, ambiguous: AmbiguousExpense, ledger: Ledger, pending_id: int
) -> tuple[str, InlineKeyboard]:
    """The expense pick stage's message + buttons (§11 tertiary, §13): capped
    at the newest few, saying what was dropped."""
    shown = ambiguous.candidates[:EXPENSE_PICK_CAP]  # newest first
    text = expense_pick_stage_reply(
        ledger_name=ledger.name,
        gist=_proposal_gist(intent),
        query=ambiguous.query,
        shown=len(shown),
        total=len(ambiguous.candidates),
    )
    return text, expense_pick_keyboard(pending_id, _expense_pick_labels(shown))


def _proposal_gist(intent: Intent) -> str:
    """The unresolved one-liner a pick stage shows — no refs, no shares: those
    can't render until the slot is pinned."""
    if isinstance(intent, AddExpense):
        assert intent.currency is not None  # concrete after nl.to_intent (§3)
        return f"{intent.description} — {fmt(intent.amount_minor, intent.currency)}"
    if isinstance(intent, SettleUp) and intent.amount_minor is not None:
        assert intent.currency is not None
        return f"a payment of {fmt(intent.amount_minor, intent.currency)}"
    return intent.kind.replace("_", " ")


def _expense_pick_labels(candidates: list[Expense]) -> list[tuple[int, str]]:
    """Expense pick buttons (§13): the #id plus enough of the row to choose by."""
    return [
        (e.id, f"#{e.id} {e.description} — {fmt(e.amount_minor, e.currency)}") for e in candidates
    ]


async def _pick_labels(session: AsyncSession, candidates: list[User]) -> list[tuple[int, str]]:
    """Pick buttons labelled apart (§13): two Sams differ by @handle; two
    stale-colliding @handles differ by display name."""
    usernames = await usernames_of(session, [u.id for u in candidates])
    return [
        (u.id, f"{u.display_name} (@{usernames[u.id]})" if usernames.get(u.id) else u.display_name)
        for u in candidates
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
    intent: Intent, session: AsyncSession, group: Group, actor: User, ledger: Ledger
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
        expense = await _previewed_expense(intent, session, group, ledger)
        return f"Delete expense #{expense.id} ({expense.description})."
    if isinstance(intent, EditExpense):
        expense = await _previewed_expense(intent, session, group, ledger)
        changes = [
            f'description → "{intent.description}"' if intent.description is not None else "",
            f"date → {intent.occurred_on}" if intent.occurred_on is not None else "",
        ]
        return f"Edit #{expense.id}: {', '.join(c for c in changes if c)}."
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
    if isinstance(intent, SetFxRate):
        assert intent.rate is not None  # NL never proposes the fetch-and-pin form (§7.5)
        return (
            f"📌 Pin 1 {intent.base} = {fmt_rate(intent.rate)} {intent.quote} for ≈ figures "
            f"(display only, frozen until re-pinned or /autorate)."
        )
    if isinstance(intent, ClearFxRate):
        return (
            f"💱 Unpin {intent.base}→{intent.quote} — ≈ figures follow the live daily "
            f"rate again."
        )
    raise AssertionError(f"unproposable intent kind: {intent.kind}")


async def _previewed_expense(
    intent: DeleteExpense | EditExpense, session: AsyncSession, group: Group, ledger: Ledger
) -> Expense:
    """The expense a delete/edit preview names (§11): pinned id when there is
    one — behind the same ledger seal the commit enforces (§0.10), so the
    preview never names (or leaks) what Confirm would refuse — else the
    descriptive match: unique, ambiguous (pick stage), or nothing."""
    if intent.expense_id is not None:
        return await sealed_expense(session, group.id, ledger.id, intent.expense_id)
    if intent.match is None:
        raise Rejection(
            '🤷 Which expense? Reply to its result message or give its #id — e.g. "delete #42".'
        )
    return await resolve_expense_match(session, ledger.id, intent.match)


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
    deps: Deps,
) -> Reply:
    """Run a mutating command; on rejection every write it made rolls back (§0.9).

    The savepoint scopes the rollback to the command itself — the author
    registration earlier in this transaction still commits.
    """
    try:
        async with session.begin_nested():
            return await runner(message, session, group, actor, deps)
    except (Rejection, ValueError) as exc:
        return Reply(text=str(exc))


async def _run_homecurrency(
    message: dict[str, Any], session: AsyncSession, group: Group, actor: User | None, deps: Deps
) -> Reply:
    currency = parse_homecurrency(message["text"])
    ctx = ApplyContext(session=session, group=group, actor=actor, seed=message["message_id"])
    applied = await apply_intent(SetHomeCurrency(currency=currency), ctx)
    assert applied is not None
    return Reply(text=_home_currency_text(currency), undo_action_id=applied.action_id)


async def _run_setrate(
    message: dict[str, Any], session: AsyncSession, group: Group, actor: User | None, deps: Deps
) -> Reply:
    parsed = parse_setrate(message["text"])
    if parsed.rate is None:
        # fetch-and-pin (§7.5): resolved HERE, before apply_intent takes the lock,
        # so the domain layer never touches FX transport. Validate the codes first —
        # a typo'd code corrects loudly (ADR-0009) instead of reading as "FX down"
        require_known_currency(parsed.base)
        require_known_currency(parsed.quote)
        # the fetch rides the shared TTL cache (one triangulation, one formula),
        # warming it for the next read; a stale leftover never pins silently —
        # "pin today's rate" means today's
        today = today_utc()
        await refresh_api_rates(session, deps.fx, {parsed.base, parsed.quote}, today=today)
        resolved = await api_rate(session, parsed.base, parsed.quote, today=today)
        if resolved is None or resolved.stale:
            raise Rejection(
                f"🤷 I couldn't fetch {parsed.base}→{parsed.quote} right now — "
                f"give me a number instead: /setrate {parsed.base} {parsed.quote} 1.35"
            )
        rate = resolved.rate
    else:
        rate = float(parsed.rate)
    ctx = ApplyContext(session=session, group=group, actor=actor, seed=message["message_id"])
    applied = await apply_intent(SetFxRate(base=parsed.base, quote=parsed.quote, rate=rate), ctx)
    assert applied is not None
    return Reply(
        text=_set_fx_rate_text(parsed.base, parsed.quote, rate),
        undo_action_id=applied.action_id,
    )


async def _run_convert(
    message: dict[str, Any], session: AsyncSession, group: Group, actor: User | None, deps: Deps
) -> Reply:
    target = parse_convert(message["text"])
    intent = ShowBalance(convert_to=target)
    return await _balance_reply(intent, session, group, actor, deps)


async def _run_rates(
    message: dict[str, Any], session: AsyncSession, group: Group, actor: User | None, deps: Deps
) -> Reply:
    """/rates — a read (§0.7): the group's pins with live references, then API
    rates for the pairs in play on the active ledger. Never other groups' concerns."""
    parse_rates(message["text"])
    today = today_utc()
    home = group.home_currency
    pins = (
        (
            await session.execute(
                select(FxRate)
                .where(FxRate.group_id == group.id, FxRate.source == "manual")
                .order_by(FxRate.base_currency, FxRate.quote_currency)
            )
        )
        .scalars()
        .all()
    )
    ledger = await session.get_one(Ledger, group.active_ledger_id)
    net = await net_positions(session, ledger.id)
    pinned_pairs = {frozenset((p.base_currency, p.quote_currency)) for p in pins}
    in_play_currencies = {
        currency
        for by_currency in net.values()
        for currency, minor in by_currency.items()
        # a fully-settled bucket is not in play: balance/board hide it, so do we
        if minor
        and home is not None
        and currency != home
        and frozenset((currency, home)) not in pinned_pairs
    }
    symbols = {s for p in pins for s in (p.base_currency, p.quote_currency)} | in_play_currencies
    if in_play_currencies and home is not None:
        symbols.add(home)
    await refresh_api_rates(session, deps.fx, symbols, today=today)
    legs = await api_legs(session, symbols)  # ONE batched read serves every line below
    names = await display_names(session, [p.set_by for p in pins if p.set_by is not None])
    pin_views = [
        PinView(
            base=p.base_currency,
            quote=p.quote_currency,
            rate=p.rate,
            by_name=names.get(p.set_by, "someone") if p.set_by is not None else "someone",
            on=p.fetched_at,
            reference=api_rate_from_legs(legs, p.base_currency, p.quote_currency, today=today),
        )
        for p in pins
    ]
    in_play = {
        currency: api_rate_from_legs(legs, currency, home, today=today)
        for currency in in_play_currencies
        if home is not None
    }
    return Reply(text=rates_reply(pins=pin_views, in_play=in_play, home=home))


async def _run_autorate(
    message: dict[str, Any], session: AsyncSession, group: Group, actor: User | None, deps: Deps
) -> Reply:
    base, quote = parse_autorate(message["text"])
    ctx = ApplyContext(session=session, group=group, actor=actor, seed=message["message_id"])
    applied = await apply_intent(ClearFxRate(base=base, quote=quote), ctx)
    assert applied is not None
    return Reply(text=_autorate_text(base, quote), undo_action_id=applied.action_id)


def _autorate_text(base: str, quote: str) -> str:
    return (
        f"💱 {base}→{quote} unpinned — ≈ figures follow the live daily rate again. "
        f"Pin one anytime: /setrate {base} {quote} 1.35"
    )


async def _run_currency(
    message: dict[str, Any], session: AsyncSession, group: Group, actor: User | None, deps: Deps
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
    message: dict[str, Any], session: AsyncSession, group: Group, actor: User | None, deps: Deps
) -> Reply:
    name, currency = parse_newledger(message["text"])
    ctx = ApplyContext(session=session, group=group, actor=actor, seed=message["message_id"])
    applied = await apply_intent(NewLedger(name=name, logging_currency=currency), ctx)
    assert isinstance(applied, AppliedLedgerOp)
    return Reply(text=_new_ledger_text(name, currency), undo_action_id=applied.action_id)


async def _run_switch(
    message: dict[str, Any], session: AsyncSession, group: Group, actor: User | None, deps: Deps
) -> Reply:
    name_or_id = parse_switch(message["text"])
    ctx = ApplyContext(session=session, group=group, actor=actor, seed=message["message_id"])
    applied = await apply_intent(SwitchLedger(name_or_id=name_or_id), ctx)
    assert isinstance(applied, AppliedLedgerOp)
    return Reply(text=_switch_text(applied.ledger.name), undo_action_id=applied.action_id)


async def _run_archive(
    message: dict[str, Any], session: AsyncSession, group: Group, actor: User | None, deps: Deps
) -> Reply:
    name_or_id = parse_archive(message["text"])
    ctx = ApplyContext(session=session, group=group, actor=actor, seed=message["message_id"])
    applied = await apply_intent(ArchiveLedger(name_or_id=name_or_id), ctx)
    assert isinstance(applied, AppliedLedgerOp)
    return Reply(text=_archive_text(applied), undo_action_id=applied.action_id)


async def _run_unarchive(
    message: dict[str, Any], session: AsyncSession, group: Group, actor: User | None, deps: Deps
) -> Reply:
    name_or_id = parse_unarchive(message["text"])
    ctx = ApplyContext(session=session, group=group, actor=actor, seed=message["message_id"])
    applied = await apply_intent(UnarchiveLedger(name_or_id=name_or_id), ctx)
    assert isinstance(applied, AppliedLedgerOp)
    return Reply(text=_unarchive_text(applied.ledger.name), undo_action_id=applied.action_id)


async def _run_ledgers(
    message: dict[str, Any], session: AsyncSession, group: Group, actor: User | None, deps: Deps
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
    message: dict[str, Any], session: AsyncSession, group: Group, actor: User | None, deps: Deps
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


async def _run_transactions(
    message: dict[str, Any], session: AsyncSession, group: Group, actor: User | None, deps: Deps
) -> Reply:
    parse_transactions(message["text"])  # no-arg guard; a trailing token is a usage error
    return await _transactions_reply(session, group)


async def _transactions_reply(session: AsyncSession, group: Group) -> Reply:
    """The show_transactions read, shared by /transactions and NL (§4: one Intent contract)."""
    # a content read (ADR-0012): no lock, no action row, active ledger only (§0.7, §0.10)
    ledger = await session.get_one(Ledger, group.active_ledger_id)
    page = await list_transactions(session, ledger.id, limit=PAGE_SIZE)
    return Reply(
        text=transactions_reply(ledger_name=ledger.name, page=page),
        markup=transactions_pager_keyboard(ledger.id, page),
    )


def _expense_runner(parse: Callable[[str], ParsedExpense]) -> CommandRunner:
    async def run(
        message: dict[str, Any],
        session: AsyncSession,
        group: Group,
        actor: User | None,
        deps: Deps,
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
    message: dict[str, Any], session: AsyncSession, group: Group, actor: User | None, deps: Deps
) -> Reply:
    parsed = parse_settle(message["text"])
    if parsed.amount is None:
        sheet_intent = SettleUp(from_ref=parsed.from_ref, to_ref=parsed.to_ref)
        return await _sheet_reply(sheet_intent, session, group, actor, deps)
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
    intent: SettleUp, session: AsyncSession, group: Group, actor: User | None, deps: Deps
) -> Reply:
    """The settle sheet (ADR-0007), shared by /settle and NL: a read — no lock,
    no action row (§0.7); the buttons' WYSIWYG amount tokens guard staleness."""
    pair = await resolve_refs(session, group.id, [intent.from_ref, intent.to_ref], actor)
    ledger = await session.get_one(Ledger, group.active_ledger_id)
    net = await net_positions(session, ledger.id)  # replayed ONCE: sheet + freshness share it
    text, markup = await sheet_view(
        session, ledger, pair[intent.from_ref], pair[intent.to_ref], net=net
    )
    # a ledger read joins the §13 board-freshness pact: refresh the ledger's
    # in-play pairs per the TTL, and re-render the board if anything landed
    currencies = {c for by_currency in net.values() for c, minor in by_currency.items() if minor}
    _, fetched = await _rates_for_currencies(session, group, currencies, deps)
    return Reply(
        text=text,
        markup=markup,
        board_sync=await _read_triggered_board_refresh(session, group, deps, fetched),
    )


async def _run_delete(
    message: dict[str, Any], session: AsyncSession, group: Group, actor: User | None, deps: Deps
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
    message: dict[str, Any], session: AsyncSession, group: Group, actor: User | None, deps: Deps
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
    message: dict[str, Any], session: AsyncSession, group: Group, actor: User | None, deps: Deps
) -> Reply:
    intent = ShowBalance(scope=parse_balance(message["text"]))
    return await _balance_reply(intent, session, group, actor, deps)


async def _balance_reply(
    intent: ShowBalance, session: AsyncSession, group: Group, actor: User | None, deps: Deps
) -> Reply:
    """The show_balance read, shared by /balance and NL (§4: one Intent contract)."""
    # a read: no lock, no action row, sealed to the active ledger (§0.7, §0.10, §8)
    ledger = await session.get_one(Ledger, group.active_ledger_id)
    net = await net_positions(session, ledger.id)
    if intent.scope == "me":
        if actor is None:
            raise Rejection("I can't tell who you are — anonymous admins have no balance.")
        entries = [(actor.display_name, net.get(actor.id, {}))]
        as_me = True
    else:
        names = await display_names(session, list(net))
        entries = [(names[user_id], by_currency) for user_id, by_currency in net.items()]
        as_me = False
    currencies = {c for _, by_currency in entries for c in by_currency}
    if intent.convert_to is not None:
        # /convert (§7.6): consolidate per member into the TARGET, not home
        target = require_known_currency(intent.convert_to)  # ADR-0009: an input edge
        target_rates, fetched = await equivalents_view(
            session, group.id, deps.fx, currencies, target, today=today_utc()
        )
        return Reply(
            text=convert_reply(
                ledger_name=ledger.name,
                target=target,
                entries=entries,
                rates=target_rates,
                as_me=as_me,
            ),
            board_sync=await _read_triggered_board_refresh(session, group, deps, fetched),
        )
    rates, fetched = await _rates_for_currencies(session, group, currencies, deps)
    return Reply(
        text=balance_reply(
            ledger_name=ledger.name,
            entries=entries,
            as_me=as_me,
            home=group.home_currency,
            rates=rates,
        ),
        board_sync=await _read_triggered_board_refresh(session, group, deps, fetched),
    )


async def _rates_for_currencies(
    session: AsyncSession,
    group: Group,
    currencies: set[str],
    deps: Deps,
) -> tuple[dict[str, Any] | None, bool]:
    """The ≈ layer's rates for the buckets about to render (§7.6): refreshes the
    API cache per the same-day TTL first — a display read, never ledger math."""
    if group.home_currency is None:
        return None, False
    return await equivalents_view(
        session, group.id, deps.fx, currencies, group.home_currency, today=today_utc()
    )


async def _read_triggered_board_refresh(
    session: AsyncSession, group: Group, deps: Deps, fetched: bool
) -> list[OutboundAction]:
    """§13: a ledger read whose TTL refresh actually landed fresh rates re-renders
    the board — the fetch already happened OUTSIDE the lock; only the render +
    edit run under it, so it can't race a concurrent write's board edit."""
    if not fetched:
        return []
    await per_group_lock(session, group.id)
    await session.refresh(group)  # post-lock re-read (ADR-0003)
    assert group.active_ledger_id is not None  # ensure_group invariant (ADR-0004)
    return await sync_board(session, group, group.active_ledger_id, deps.client)


async def _run_setup(
    message: dict[str, Any], session: AsyncSession, group: Group, actor: User | None, deps: Deps
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


# commands answered inline in _handle_group_message, outside _RUNNERS — they
# need no session/group/actor plumbing. Anything that enumerates every command
# we claim (the caption door, a future setMyCommands menu) must include these.
_INLINE_COMMANDS = frozenset({"start", "help"})

_RUNNERS: dict[str, CommandRunner] = {
    "setup": _run_setup,
    "homecurrency": _run_homecurrency,
    "currency": _run_currency,
    "setrate": _run_setrate,
    "autorate": _run_autorate,
    "rates": _run_rates,
    "convert": _run_convert,
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
    "transactions": _run_transactions,
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
