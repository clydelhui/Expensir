"""Undo/redo (§9): persistent buttons, idempotent toggle, operator lock — slice 4 (#5)."""

from datetime import timedelta

from sqlalchemy import select

from expensir.core.handler import dispatch
from expensir.db.models import Action, Expense, Group, utcnow
from expensir.transports.executor import execute
from tests.factories import bot_added_update, callback_update, message_update, user

ALICE = user(1001, "Alice", "alice")
BOB = user(1002, "Bob", "bob")


async def setup_group(deps, chat_id: int = -42, home: str | None = "EUR") -> None:
    await dispatch(bot_added_update(chat_id=chat_id, by=ALICE), deps)
    if home is not None:
        await dispatch(
            message_update(update_id=90, chat_id=chat_id, text=f"/homecurrency {home}"), deps
        )
    await dispatch(message_update(update_id=91, chat_id=chat_id, text="hello", from_user=BOB), deps)


async def read_action(deps, kind: str) -> Action:
    async with deps.session_factory() as session:
        return (await session.execute(select(Action).where(Action.kind == kind))).scalar_one()


class RecordingClient:
    """Fake Telegram client: remembers every call, mints message ids."""

    def __init__(self):
        self.sent: list[dict] = []
        self.next_message_id = 555

    async def send_message(self, chat_id: int, text: str, reply_markup: dict | None = None) -> dict:
        self.sent.append({"chat_id": chat_id, "text": text, "reply_markup": reply_markup})
        return {"message_id": self.next_message_id, "chat": {"id": chat_id}}


async def test_an_expense_result_carries_an_undo_button_holding_the_action_id(deps):
    await setup_group(deps)

    [reply] = await dispatch(
        message_update(update_id=5, chat_id=-42, text="/equal 60 dinner @alice @bob"), deps
    )

    action = await read_action(deps, "add_expense")
    [[button]] = reply.reply_markup["inline_keyboard"]
    assert button["text"] == "↩️ Undo"
    assert button["callback_data"] == f"v1:undo:{action.id}"


async def test_sending_an_undoable_reply_stores_the_result_ids_on_the_action(deps):
    await setup_group(deps)
    outbound = await dispatch(
        message_update(update_id=5, chat_id=-42, text="/equal 60 dinner @alice @bob"), deps
    )

    client = RecordingClient()
    await execute(outbound, client, session_factory=deps.session_factory)

    # the sent message's ids land back on the action so undo can edit it and
    # reply-to-target can resolve it (§8, §9, §11)
    action = await read_action(deps, "add_expense")
    assert action.result_chat_id == -42
    assert action.result_message_id == 555
    [sent] = client.sent
    assert sent["reply_markup"] == reply_markup_of(f"v1:undo:{action.id}")


def reply_markup_of(callback_data: str, text: str = "↩️ Undo") -> dict:
    return {"inline_keyboard": [[{"text": text, "callback_data": callback_data}]]}


async def add_dinner(deps, chat_id: int = -42) -> tuple[Action, str]:
    """Record /equal 60 dinner @alice @bob and send its reply; returns (action, reply text)."""
    outbound = await dispatch(
        message_update(update_id=5, chat_id=chat_id, text="/equal 60 dinner @alice @bob"), deps
    )
    await execute(outbound, RecordingClient(), session_factory=deps.session_factory)
    return await read_action(deps, "add_expense"), outbound[0].text


async def balance_text(deps, chat_id: int = -42, update_id: int = 200) -> str:
    [reply] = await dispatch(
        message_update(update_id=update_id, chat_id=chat_id, text="/balance"), deps
    )
    return reply.text


async def test_pressing_undo_reverses_the_expense_and_restores_prior_balances(deps):
    await setup_group(deps)
    action, text = await add_dinner(deps)
    assert "Bob owes" in await balance_text(deps, update_id=200)

    await dispatch(
        callback_update(
            update_id=6,
            chat_id=-42,
            data=f"v1:undo:{action.id}",
            from_user=BOB,
            message_text=text,
        ),
        deps,
    )

    assert "settled up" in await balance_text(deps, update_id=201)
    async with deps.session_factory() as session:
        expense = (await session.execute(select(Expense))).scalar_one()
        assert expense.deleted_at is not None  # soft-deleted, never erased (§0.4)
        refreshed = await session.get_one(Action, action.id)
        assert refreshed.undone_at is not None
    assert refreshed.undone_by is not None


async def test_undo_edits_the_same_message_and_flips_the_button_to_redo(deps):
    await setup_group(deps)
    action, text = await add_dinner(deps)

    outbound = await dispatch(
        callback_update(
            update_id=6,
            chat_id=-42,
            data=f"v1:undo:{action.id}",
            from_user=BOB,
            message_id=555,
            message_text=text,
        ),
        deps,
    )

    [answer, edit] = outbound
    assert answer.kind == "answer_callback_query"
    assert answer.callback_query_id == "cbq-1"
    assert "undone" in answer.text.lower()
    assert edit.kind == "edit_message"  # edited in place, never deleted (§9)
    assert (edit.chat_id, edit.message_id) == (-42, 555)
    assert edit.text.startswith(text)
    assert "Undone by Bob" in edit.text
    assert edit.reply_markup == reply_markup_of(f"v1:redo:{action.id}", text="↪️ Redo")


async def test_redo_reapplies_the_expense_and_restores_the_original_message(deps):
    await setup_group(deps)
    action, text = await add_dinner(deps)
    [_, undo_edit] = await dispatch(
        callback_update(
            update_id=6,
            chat_id=-42,
            data=f"v1:undo:{action.id}",
            from_user=BOB,
            message_text=text,
        ),
        deps,
    )

    [answer, edit] = await dispatch(
        callback_update(
            update_id=7,
            chat_id=-42,
            data=f"v1:redo:{action.id}",
            from_user=ALICE,
            message_text=undo_edit.text,  # the message as undo left it
        ),
        deps,
    )

    assert "redone" in answer.text.lower()
    assert edit.text == text  # the "Undone by" note is gone
    assert edit.reply_markup == reply_markup_of(f"v1:undo:{action.id}")
    assert "Bob owes" in await balance_text(deps, update_id=201)
    async with deps.session_factory() as session:
        expense = (await session.execute(select(Expense))).scalar_one()
        assert expense.deleted_at is None
        refreshed = await session.get_one(Action, action.id)
        assert refreshed.undone_at is None
        assert refreshed.undone_by is None


async def latest_action(deps, kind: str) -> Action:
    async with deps.session_factory() as session:
        return (
            await session.execute(
                select(Action).where(Action.kind == kind).order_by(Action.id.desc()).limit(1)
            )
        ).scalar_one()


async def home_currency(deps, chat_id: int = -42) -> str | None:
    async with deps.session_factory() as session:
        group = (
            await session.execute(select(Group).where(Group.platform_chat_id == chat_id))
        ).scalar_one()
        return group.home_currency


async def test_homecurrency_is_undoable_and_undo_restores_the_previous_value(deps):
    await setup_group(deps, home="EUR")

    [reply] = await dispatch(
        message_update(update_id=5, chat_id=-42, text="/homecurrency USD"), deps
    )

    action = await latest_action(deps, "set_home_currency")
    [[button]] = reply.reply_markup["inline_keyboard"]
    assert button["callback_data"] == f"v1:undo:{action.id}"
    assert await home_currency(deps) == "USD"

    await dispatch(
        callback_update(
            update_id=6, chat_id=-42, data=f"v1:undo:{action.id}", message_text=reply.text
        ),
        deps,
    )
    assert await home_currency(deps) == "EUR"

    await dispatch(
        callback_update(
            update_id=7, chat_id=-42, data=f"v1:redo:{action.id}", message_text=reply.text
        ),
        deps,
    )
    assert await home_currency(deps) == "USD"


async def test_undoing_the_first_homecurrency_restores_unset(deps):
    await setup_group(deps, home=None)
    [reply] = await dispatch(
        message_update(update_id=5, chat_id=-42, text="/homecurrency EUR"), deps
    )
    action = await latest_action(deps, "set_home_currency")

    await dispatch(
        callback_update(
            update_id=6, chat_id=-42, data=f"v1:undo:{action.id}", message_text=reply.text
        ),
        deps,
    )

    assert await home_currency(deps) is None


async def test_undo_on_an_inaccessible_old_message_still_flips_the_keyboard(deps):
    await setup_group(deps)
    action, _ = await add_dinner(deps)

    # Telegram sends InaccessibleMessage (chat + message_id, no text) for
    # callbacks on very old messages; the button must still flip or redo
    # becomes unreachable — the direction lives only in the callback data (§9)
    [answer, edit] = await dispatch(
        callback_update(
            update_id=6, chat_id=-42, data=f"v1:undo:{action.id}", from_user=BOB, message_text=None
        ),
        deps,
    )

    assert "undone" in answer.text.lower()
    assert edit.kind == "edit_message_reply_markup"
    assert (edit.chat_id, edit.message_id) == (-42, 555)
    assert edit.reply_markup == reply_markup_of(f"v1:redo:{action.id}", text="↪️ Redo")
    async with deps.session_factory() as session:
        expense = (await session.execute(select(Expense))).scalar_one()
        assert expense.deleted_at is not None

    [answer2, edit2] = await dispatch(
        callback_update(
            update_id=7,
            chat_id=-42,
            data=f"v1:redo:{action.id}",
            from_user=ALICE,
            message_text=None,
        ),
        deps,
    )

    assert "redone" in answer2.text.lower()
    assert edit2.reply_markup == reply_markup_of(f"v1:undo:{action.id}")
    async with deps.session_factory() as session:
        expense = (await session.execute(select(Expense))).scalar_one()
        assert expense.deleted_at is None


async def test_a_stale_redo_tap_on_an_active_action_noops(deps):
    await setup_group(deps)
    action, text = await add_dinner(deps)

    # a leftover ↪️ Redo button pressed while the action is NOT undone
    outbound = await dispatch(
        callback_update(
            update_id=6,
            chat_id=-42,
            data=f"v1:redo:{action.id}",
            from_user=BOB,
            message_text=text,
        ),
        deps,
    )

    assert "already redone" in outbound[0].text.lower()
    async with deps.session_factory() as session:
        expense = (await session.execute(select(Expense))).scalar_one()
        assert expense.deleted_at is None  # state unchanged


class BrokenEditClient(RecordingClient):
    async def edit_message_text(
        self, chat_id: int, message_id: int, text: str, reply_markup: dict | None = None
    ) -> dict:
        raise RuntimeError("telegram is down")

    async def answer_callback_query(self, callback_query_id: str, text: str | None = None) -> dict:
        return {}


async def test_a_failed_telegram_edit_does_not_roll_back_the_undo(deps):
    await setup_group(deps)
    action, text = await add_dinner(deps)

    outbound = await dispatch(
        callback_update(
            update_id=6,
            chat_id=-42,
            data=f"v1:undo:{action.id}",
            from_user=BOB,
            message_text=text,
        ),
        deps,
    )
    await execute(outbound, BrokenEditClient(), session_factory=deps.session_factory)

    async with deps.session_factory() as session:  # the DB transaction IS the undo (§9)
        expense = (await session.execute(select(Expense))).scalar_one()
        assert expense.deleted_at is not None


async def test_nl_undo_is_never_honored_and_points_to_the_button(deps):
    await setup_group(deps)
    action, _ = await add_dinner(deps)

    [reply] = await dispatch(
        message_update(update_id=6, chat_id=-42, text="@expensir_bot undo that", from_user=BOB),
        deps,
    )

    assert "↩️" in reply.text  # the templated pointer to the button (§9, §12)
    async with deps.session_factory() as session:
        expense = (await session.execute(select(Expense))).scalar_one()
        assert expense.deleted_at is None  # nothing was performed
        refreshed = await session.get_one(Action, action.id)
        assert refreshed.undone_at is None


async def test_a_mention_merely_containing_an_undo_word_is_not_treated_as_an_undo_request(deps):
    await setup_group(deps)
    await add_dinner(deps)

    # "redo" here is part of the expense description, not an undo/redo request —
    # the pointer must not steal messages meant for the NL extractor (§12)
    outbound = await dispatch(
        message_update(
            update_id=6,
            chat_id=-42,
            text="@expensir_bot I paid 30 to redo the paint job",
            from_user=BOB,
        ),
        deps,
    )

    assert outbound == []


async def test_a_polite_nl_undo_request_still_gets_the_pointer(deps):
    await setup_group(deps)
    await add_dinner(deps)

    [reply] = await dispatch(
        message_update(
            update_id=6, chat_id=-42, text="@expensir_bot please undo that", from_user=BOB
        ),
        deps,
    )

    assert "↩️" in reply.text


async def backdate_action(deps, action_id: int, hours: int) -> None:
    async with deps.session_factory() as session, session.begin():
        action = await session.get_one(Action, action_id)
        action.created_at = utcnow() - timedelta(hours=hours)


async def test_after_the_window_only_the_operator_may_undo_and_the_refusal_names_them(deps):
    deps.operator_user_id = 1001  # Alice is the operator
    await setup_group(deps)
    action, text = await add_dinner(deps)
    await backdate_action(deps, action.id, hours=25)  # simulate an old action; no scheduler (§9)

    refused = await dispatch(
        callback_update(
            update_id=6,
            chat_id=-42,
            data=f"v1:undo:{action.id}",
            from_user=BOB,
            message_text=text,
        ),
        deps,
    )

    [answer] = refused  # no edit: the button stays (§9)
    assert "🔒" in answer.text
    assert "@alice" in answer.text
    async with deps.session_factory() as session:
        expense = (await session.execute(select(Expense))).scalar_one()
        assert expense.deleted_at is None  # nothing changed

    accepted = await dispatch(
        callback_update(
            update_id=7,
            chat_id=-42,
            data=f"v1:undo:{action.id}",
            from_user=ALICE,
            message_text=text,
        ),
        deps,
    )

    assert "undone" in accepted[0].text.lower()
    assert "settled up" in await balance_text(deps, update_id=201)


async def test_a_forged_callback_cannot_toggle_another_groups_action(deps):
    await setup_group(deps, chat_id=-42)
    await setup_group(deps, chat_id=-43)
    action, _ = await add_dinner(deps, chat_id=-43)

    # a forged press in group -42 carrying group -43's action id (§9: never
    # toggle across groups)
    outbound = await dispatch(
        callback_update(update_id=6, chat_id=-42, data=f"v1:undo:{action.id}", from_user=BOB),
        deps,
    )

    [answer] = outbound  # no edit: the foreign message is left alone
    assert "doesn't match" in answer.text
    async with deps.session_factory() as session:
        expense = (await session.execute(select(Expense))).scalar_one()
        assert expense.deleted_at is None  # the other group's expense is untouched
        refreshed = await session.get_one(Action, action.id)
        assert refreshed.undone_at is None


async def test_an_anonymous_admin_press_is_refused_without_toggling(deps):
    await setup_group(deps)
    action, text = await add_dinner(deps)
    anonymous_admin = {"id": 1087968824, "is_bot": True, "first_name": "Group"}

    outbound = await dispatch(
        callback_update(
            update_id=6,
            chat_id=-42,
            data=f"v1:undo:{action.id}",
            from_user=anonymous_admin,
            message_text=text,
        ),
        deps,
    )

    [answer] = outbound
    assert "anonymous" in answer.text.lower()
    async with deps.session_factory() as session:
        expense = (await session.execute(select(Expense))).scalar_one()
        assert expense.deleted_at is None  # nothing toggled


async def test_the_lock_refusal_falls_back_to_the_operators_display_name(deps):
    CHARLIE = user(1003, "Charlie", username=None)
    deps.operator_user_id = 1003
    await setup_group(deps)
    await dispatch(  # Charlie speaks so he is registered — but has no @username
        message_update(update_id=92, chat_id=-42, text="hello", from_user=CHARLIE), deps
    )
    action, text = await add_dinner(deps)
    await backdate_action(deps, action.id, hours=25)

    [answer] = await dispatch(
        callback_update(
            update_id=6, chat_id=-42, data=f"v1:undo:{action.id}", from_user=BOB, message_text=text
        ),
        deps,
    )

    assert "🔒" in answer.text
    assert "Charlie" in answer.text


async def test_the_lock_refusal_names_a_generic_operator_when_none_is_known(deps):
    deps.operator_user_id = 424242  # configured but never seen in any group
    await setup_group(deps)
    action, text = await add_dinner(deps)
    await backdate_action(deps, action.id, hours=25)

    [answer] = await dispatch(
        callback_update(
            update_id=6, chat_id=-42, data=f"v1:undo:{action.id}", from_user=BOB, message_text=text
        ),
        deps,
    )

    assert "🔒" in answer.text
    assert "the operator" in answer.text


async def test_a_double_tap_of_undo_noops_with_already_undone(deps):
    await setup_group(deps)
    action, text = await add_dinner(deps)
    press = lambda uid: dispatch(  # noqa: E731
        callback_update(
            update_id=uid,
            chat_id=-42,
            data=f"v1:undo:{action.id}",
            from_user=BOB,
            message_text=text,
        ),
        deps,
    )
    await press(6)

    outbound = await press(7)  # stale second tap: the message still shows ↩️ Undo

    answer = outbound[0]
    assert "already undone" in answer.text.lower()
    async with deps.session_factory() as session:
        expense = (await session.execute(select(Expense))).scalar_one()
        assert expense.deleted_at is not None  # state unchanged
    assert "settled up" in await balance_text(deps, update_id=201)
