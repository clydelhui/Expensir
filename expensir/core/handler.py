"""Transport-agnostic entrypoint: dispatch(update_dict) -> list[OutboundAction] (§0.5)."""

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, cast

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from expensir.core.locking import per_group_lock
from expensir.core.outbound import (
    AnswerCallbackQuery,
    EditMessage,
    EditMessageReplyMarkup,
    OutboundAction,
    SendMessage,
)
from expensir.db.models import Group, Ledger, User
from expensir.domain.apply import (
    AppliedExpense,
    AppliedExpenseChange,
    AppliedLedgerOp,
    ApplyContext,
    apply_intent,
)
from expensir.domain.balances import net_positions
from expensir.domain.currency import resolve_currency
from expensir.domain.errors import Rejection
from expensir.domain.identity import (
    display_names,
    ensure_group,
    register_member,
    resolve_expense_id,
)
from expensir.domain.ledgers import ledgers_of
from expensir.domain.money import to_minor
from expensir.domain.undo import ToggleDirection, toggle
from expensir.format.keyboards import redo_keyboard, undo_keyboard
from expensir.format.render import (
    LedgerLine,
    balance_reply,
    delete_reply,
    edit_reply,
    expense_reply,
    ledgers_reply,
)
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
    parse_newledger,
    parse_percent,
    parse_shares,
    parse_switch,
    parse_unarchive,
)
from expensir.intents.schema import (
    AddExpense,
    ArchiveLedger,
    DeleteExpense,
    EditExpense,
    NewLedger,
    SetHomeCurrency,
    SetLoggingCurrency,
    ShowBalance,
    SplitMember,
    SwitchLedger,
    UnarchiveLedger,
)

_TOGGLE_DATA = re.compile(r"^v1:(undo|redo):(\d+)$")
_UNDO_WORDS = re.compile(r"\b(undo|undid|redo)\b", re.IGNORECASE)
_MENTION = re.compile(r"@\w+")
UNDONE_MARK = "\n\n↩️ Undone by "
UNDO_POINTER = (
    "I never undo from chat — tap the ↩️ Undo button on the message that recorded it instead."
)


@dataclass
class Reply:
    """A command's rendered result; undo_action_id marks it undoable (§9)."""

    text: str
    undo_action_id: int | None = None


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
    """Undo/redo button presses (§9). More callback namespaces arrive with later slices."""
    ack = AnswerCallbackQuery(callback_query_id=callback["id"])
    match = _TOGGLE_DATA.match(callback.get("data") or "")
    if match is None:
        return [ack]  # not a button of this slice; acknowledge so the spinner stops
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
        ]
    base = _strip_undone_mark(original_text)
    text = f"{base}{UNDONE_MARK}{outcome.undone_by_name or 'someone'}." if outcome.undone else base
    edit = EditMessage(
        chat_id=chat["id"], message_id=message["message_id"], text=text, reply_markup=markup
    )
    return [ack, edit]


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

        command = _command_of(message.get("text", ""), deps.bot_username)
        if command == "start":
            active = await session.get_one(Ledger, group.active_ledger_id)
            text = f"📒 {active.name} • Expensir is ready — try /equal, or /balance."
            return [SendMessage(chat_id=message["chat"]["id"], text=text)]
        runner = _RUNNERS.get(command or "")
        if runner is not None:
            reply = await _run_command(runner, message, session, group, actor)
            markup = None
            if reply.undo_action_id is not None:
                markup = undo_keyboard(reply.undo_action_id)
            return [
                SendMessage(
                    chat_id=message["chat"]["id"],
                    text=reply.text,
                    reply_markup=markup,
                    records_result_for_action_id=reply.undo_action_id,
                )
            ]

        text = message.get("text", "")
        if _mentions_bot(text, deps.bot_username) and _is_undo_request(text):
            # undo/redo is button-only, never honored from NL (§9); this guard
            # fronts the NL extractor, which arrives in a later slice (§12)
            return [SendMessage(chat_id=message["chat"]["id"], text=UNDO_POINTER)]

    return []


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
    return Reply(
        text=(
            f"🏠 Home currency set to {currency} — "
            f"other currencies will show a ≈ {currency} equivalent."
        ),
        undo_action_id=applied.action_id,
    )


async def _run_currency(
    message: dict[str, Any], session: AsyncSession, group: Group, actor: User | None
) -> Reply:
    currency = parse_currency(message["text"])
    ctx = ApplyContext(session=session, group=group, actor=actor, seed=message["message_id"])
    applied = await apply_intent(SetLoggingCurrency(currency=currency), ctx)
    assert isinstance(applied, AppliedLedgerOp)
    return Reply(
        text=(
            f"📒 {applied.ledger.name} now logs in {currency} — new expenses default to "
            f"{currency}; existing ones keep their currency."
        ),
        undo_action_id=applied.action_id,
    )


async def _run_newledger(
    message: dict[str, Any], session: AsyncSession, group: Group, actor: User | None
) -> Reply:
    name, currency = parse_newledger(message["text"])
    ctx = ApplyContext(session=session, group=group, actor=actor, seed=message["message_id"])
    applied = await apply_intent(NewLedger(name=name, logging_currency=currency), ctx)
    assert isinstance(applied, AppliedLedgerOp)
    logging = f" Logging currency: {currency}." if currency is not None else ""
    return Reply(
        text=f"📒 {name} is now the active ledger — new expenses land here.{logging}",
        undo_action_id=applied.action_id,
    )


async def _run_switch(
    message: dict[str, Any], session: AsyncSession, group: Group, actor: User | None
) -> Reply:
    name_or_id = parse_switch(message["text"])
    ctx = ApplyContext(session=session, group=group, actor=actor, seed=message["message_id"])
    applied = await apply_intent(SwitchLedger(name_or_id=name_or_id), ctx)
    assert isinstance(applied, AppliedLedgerOp)
    return Reply(
        text=f"📒 Switched to {applied.ledger.name} — new expenses land here.",
        undo_action_id=applied.action_id,
    )


async def _run_archive(
    message: dict[str, Any], session: AsyncSession, group: Group, actor: User | None
) -> Reply:
    name_or_id = parse_archive(message["text"])
    ctx = ApplyContext(session=session, group=group, actor=actor, seed=message["message_id"])
    applied = await apply_intent(ArchiveLedger(name_or_id=name_or_id), ctx)
    assert isinstance(applied, AppliedLedgerOp)
    text = f"📦 Archived 📒 {applied.ledger.name}."
    if applied.outstanding_balances:
        text += " ⚠️ It still has outstanding balances — they stay as they are."
    if applied.repointed_to is not None:
        text += f" 📒 {applied.repointed_to.name} is now the active ledger."
    return Reply(text=text, undo_action_id=applied.action_id)


async def _run_unarchive(
    message: dict[str, Any], session: AsyncSession, group: Group, actor: User | None
) -> Reply:
    name_or_id = parse_unarchive(message["text"])
    ctx = ApplyContext(session=session, group=group, actor=actor, seed=message["message_id"])
    applied = await apply_intent(UnarchiveLedger(name_or_id=name_or_id), ctx)
    assert isinstance(applied, AppliedLedgerOp)
    name = applied.ledger.name
    return Reply(
        text=f"📂 Reopened 📒 {name} — it's not active; /switch {name} to log expenses there.",
        undo_action_id=applied.action_id,
    )


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


_RUNNERS: dict[str, CommandRunner] = {
    "homecurrency": _run_homecurrency,
    "currency": _run_currency,
    "newledger": _run_newledger,
    "ledgers": _run_ledgers,
    "switch": _run_switch,
    "archive": _run_archive,
    "unarchive": _run_unarchive,
    "equal": _expense_runner(parse_equal),
    "exact": _expense_runner(parse_exact),
    "shares": _expense_runner(parse_shares),
    "percent": _expense_runner(parse_percent),
    "balance": _run_balance,
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


def _is_undo_request(text: str) -> bool:
    """An undo word leading the message (mentions aside) reads as a request;
    one buried mid-sentence is ordinary prose ("… to redo the paint job")."""
    words = _MENTION.sub(" ", text).split()
    return any(_UNDO_WORDS.fullmatch(word.strip(".,!?")) for word in words[:3])


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
