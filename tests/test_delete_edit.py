"""Delete & edit expense (§4, §8, §11): reply-to-target and #id resolution — slice 6 (#7)."""

from sqlalchemy import select

from expensir.core.handler import dispatch
from expensir.db.models import Action, Expense, Group, Ledger
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


class RecordingClient:
    """Fake Telegram client: remembers every call, mints message ids."""

    def __init__(self, first_message_id: int = 555):
        self.sent: list[dict] = []
        self.next_message_id = first_message_id

    async def send_message(self, chat_id: int, text: str, reply_markup: dict | None = None) -> dict:
        self.sent.append({"chat_id": chat_id, "text": text, "reply_markup": reply_markup})
        minted = self.next_message_id
        self.next_message_id += 1
        return {"message_id": minted, "chat": {"id": chat_id}}


async def add_dinner(deps, chat_id: int = -42, update_id: int = 5) -> tuple[int, int]:
    """Record /equal 60 dinner @alice @bob and send its reply.

    Returns (expense_id, result_message_id) — the ids delete/edit resolve by.
    """
    outbound = await dispatch(
        message_update(update_id=update_id, chat_id=chat_id, text="/equal 60 dinner @alice @bob"),
        deps,
    )
    client = RecordingClient(first_message_id=500 + update_id)
    await execute(outbound, client, session_factory=deps.session_factory)
    async with deps.session_factory() as session:
        expense = (
            await session.execute(select(Expense).order_by(Expense.id.desc()).limit(1))
        ).scalar_one()
        action = await session.get_one(Action, expense.created_by_action_id)
        assert action.result_message_id is not None
        return expense.id, action.result_message_id


async def balance_text(deps, chat_id: int = -42, update_id: int = 200) -> str:
    [reply] = await dispatch(
        message_update(update_id=update_id, chat_id=chat_id, text="/balance"), deps
    )
    return reply.text


async def read_expense(deps, expense_id: int) -> Expense:
    async with deps.session_factory() as session:
        return await session.get_one(Expense, expense_id)


async def test_delete_by_id_soft_deletes_and_balances_recompute(deps):
    await setup_group(deps)
    expense_id, _ = await add_dinner(deps)
    assert "Bob owes" in await balance_text(deps, update_id=200)

    [reply] = await dispatch(
        message_update(update_id=6, chat_id=-42, text=f"/delete {expense_id}", from_user=BOB), deps
    )

    assert f"#{expense_id}" in reply.text
    assert "dinner" in reply.text
    # deleted expenses disappear from balance replay but remain in the DB (§0.4)
    assert "settled up" in await balance_text(deps, update_id=201)
    expense = await read_expense(deps, expense_id)
    assert expense.deleted_at is not None


async def delete_expense(deps, expense_id: int, update_id: int = 6) -> tuple[Action, str]:
    """Run /delete <id> and send its reply; returns (delete action, reply text)."""
    outbound = await dispatch(
        message_update(
            update_id=update_id, chat_id=-42, text=f"/delete {expense_id}", from_user=BOB
        ),
        deps,
    )
    await execute(outbound, RecordingClient(first_message_id=700 + update_id), deps.session_factory)
    async with deps.session_factory() as session:
        action = (
            await session.execute(
                select(Action)
                .where(Action.kind == "delete_expense")
                .order_by(Action.id.desc())
                .limit(1)
            )
        ).scalar_one()
        return action, outbound[0].text


async def test_undo_of_a_delete_restores_the_expense_and_redo_deletes_it_again(deps):
    await setup_group(deps)
    expense_id, _ = await add_dinner(deps)
    action, text = await delete_expense(deps, expense_id)
    assert "settled up" in await balance_text(deps, update_id=200)

    [answer, _] = await dispatch(
        callback_update(
            update_id=7,
            chat_id=-42,
            data=f"v1:undo:{action.id}",
            from_user=ALICE,
            message_text=text,
        ),
        deps,
    )

    assert "undone" in answer.text.lower()
    expense = await read_expense(deps, expense_id)
    assert expense.deleted_at is None  # the delete's undo RESTORES the expense (§8)
    assert "Bob owes" in await balance_text(deps, update_id=201)

    [answer2, _] = await dispatch(
        callback_update(
            update_id=8,
            chat_id=-42,
            data=f"v1:redo:{action.id}",
            from_user=ALICE,
            message_text=text,
        ),
        deps,
    )

    assert "redone" in answer2.text.lower()
    expense = await read_expense(deps, expense_id)
    assert expense.deleted_at is not None
    assert "settled up" in await balance_text(deps, update_id=202)


async def add_action_of(deps, expense_id: int) -> Action:
    async with deps.session_factory() as session:
        expense = await session.get_one(Expense, expense_id)
        return await session.get_one(Action, expense.created_by_action_id)


async def test_redo_of_the_original_add_never_resurrects_an_explicitly_deleted_expense(deps):
    """The #5 flag: reapply of add_expense must not clear a /delete's deleted_at."""
    await setup_group(deps)
    expense_id, _ = await add_dinner(deps)
    add_action = await add_action_of(deps, expense_id)
    await delete_expense(deps, expense_id)

    # undo then redo the ADD via its own (older) message's buttons
    await dispatch(
        callback_update(update_id=7, chat_id=-42, data=f"v1:undo:{add_action.id}", from_user=BOB),
        deps,
    )
    await dispatch(
        callback_update(update_id=8, chat_id=-42, data=f"v1:redo:{add_action.id}", from_user=BOB),
        deps,
    )

    expense = await read_expense(deps, expense_id)
    assert expense.deleted_at is not None  # the explicit delete still stands (§8, §9)
    assert "settled up" in await balance_text(deps, update_id=201)


async def test_undoing_a_delete_while_the_add_is_undone_keeps_the_expense_hidden(deps):
    await setup_group(deps)
    expense_id, _ = await add_dinner(deps)
    add_action = await add_action_of(deps, expense_id)
    delete_action, _ = await delete_expense(deps, expense_id)

    # undo the ADD (materially a no-op: already deleted), then undo the DELETE
    await dispatch(
        callback_update(update_id=7, chat_id=-42, data=f"v1:undo:{add_action.id}", from_user=BOB),
        deps,
    )
    await dispatch(
        callback_update(
            update_id=8, chat_id=-42, data=f"v1:undo:{delete_action.id}", from_user=BOB
        ),
        deps,
    )

    expense = await read_expense(deps, expense_id)
    assert expense.deleted_at is not None  # the add is still undone (§9)
    assert "settled up" in await balance_text(deps, update_id=201)

    # redo the ADD: now nothing stands against the expense — it returns
    await dispatch(
        callback_update(update_id=9, chat_id=-42, data=f"v1:redo:{add_action.id}", from_user=BOB),
        deps,
    )
    expense = await read_expense(deps, expense_id)
    assert expense.deleted_at is None
    assert "Bob owes" in await balance_text(deps, update_id=202)


async def test_replying_to_the_expense_result_with_bare_delete_resolves_it(deps):
    await setup_group(deps)
    expense_id, result_message_id = await add_dinner(deps)

    [reply] = await dispatch(
        message_update(
            update_id=6,
            chat_id=-42,
            text="/delete",
            from_user=BOB,
            reply_to_message_id=result_message_id,
        ),
        deps,
    )

    assert f"#{expense_id}" in reply.text
    expense = await read_expense(deps, expense_id)
    assert expense.deleted_at is not None


async def test_a_reply_and_a_conflicting_id_are_refused_and_nothing_is_deleted(deps):
    await setup_group(deps)
    first_id, first_message_id = await add_dinner(deps, update_id=5)
    second_id, _ = await add_dinner(deps, update_id=6)

    [reply] = await dispatch(
        message_update(
            update_id=7,
            chat_id=-42,
            text=f"/delete {second_id}",
            from_user=BOB,
            reply_to_message_id=first_message_id,
        ),
        deps,
    )

    assert "can't tell which" in reply.text
    assert (await read_expense(deps, first_id)).deleted_at is None
    assert (await read_expense(deps, second_id)).deleted_at is None


async def test_a_reply_with_a_matching_id_deletes_that_expense(deps):
    await setup_group(deps)
    expense_id, result_message_id = await add_dinner(deps)

    [reply] = await dispatch(
        message_update(
            update_id=6,
            chat_id=-42,
            text=f"/delete #{expense_id}",  # the visible '#42' form works too (§11)
            from_user=BOB,
            reply_to_message_id=result_message_id,
        ),
        deps,
    )

    assert "Deleted" in reply.text
    assert (await read_expense(deps, expense_id)).deleted_at is not None


async def test_replying_to_a_non_expense_bot_message_falls_back_to_the_id(deps):
    await setup_group(deps)
    expense_id, _ = await add_dinner(deps)

    [reply] = await dispatch(
        message_update(
            update_id=6,
            chat_id=-42,
            text=f"/delete {expense_id}",
            from_user=BOB,
            reply_to_message_id=444,  # some bot message that is not an expense result
        ),
        deps,
    )

    assert "Deleted" in reply.text
    assert (await read_expense(deps, expense_id)).deleted_at is not None


async def test_delete_with_neither_reply_nor_id_gets_usage_guidance(deps):
    await setup_group(deps)
    expense_id, _ = await add_dinner(deps)

    [reply] = await dispatch(
        message_update(update_id=6, chat_id=-42, text="/delete", from_user=BOB), deps
    )

    assert "Usage" in reply.text
    assert (await read_expense(deps, expense_id)).deleted_at is None


async def test_deleting_an_unknown_id_is_refused_and_records_nothing(deps):
    await setup_group(deps)
    await add_dinner(deps)

    [reply] = await dispatch(
        message_update(update_id=6, chat_id=-42, text="/delete 9999", from_user=BOB), deps
    )

    assert "can't find" in reply.text
    async with deps.session_factory() as session:
        deletes = (
            (await session.execute(select(Action).where(Action.kind == "delete_expense")))
            .scalars()
            .all()
        )
        assert deletes == []  # a rejection commits nothing (§0.9)


async def test_another_groups_expense_reads_as_not_found_not_as_a_leak(deps):
    await setup_group(deps, chat_id=-42)
    await setup_group(deps, chat_id=-43)
    other_id, _ = await add_dinner(deps, chat_id=-43)

    [reply] = await dispatch(
        message_update(update_id=6, chat_id=-42, text=f"/delete {other_id}", from_user=BOB), deps
    )

    assert "can't find" in reply.text  # the bot stays silent about other groups (§0.10)
    assert (await read_expense(deps, other_id)).deleted_at is None


async def test_deleting_an_already_deleted_expense_noops_with_already_gone(deps):
    await setup_group(deps)
    expense_id, _ = await add_dinner(deps)
    await delete_expense(deps, expense_id)

    [reply] = await dispatch(
        message_update(update_id=7, chat_id=-42, text=f"/delete {expense_id}", from_user=BOB), deps
    )

    assert "already gone" in reply.text
    async with deps.session_factory() as session:
        deletes = (
            (await session.execute(select(Action).where(Action.kind == "delete_expense")))
            .scalars()
            .all()
        )
        assert len(deletes) == 1  # no second delete action stacked


async def switch_to_new_ledger(deps, chat_id: int, name: str) -> None:
    """Repoint the group's active ledger to a fresh one — /newledger is a later slice."""
    async with deps.session_factory() as session, session.begin():
        group = (
            await session.execute(select(Group).where(Group.platform_chat_id == chat_id))
        ).scalar_one()
        ledger = Ledger(group_id=group.id, name=name)
        session.add(ledger)
        await session.flush()
        group.active_ledger_id = ledger.id


async def test_a_cross_ledger_reference_is_refused_naming_the_other_ledger(deps):
    await setup_group(deps)  # first ledger is named after the group: "Japan Trip"
    expense_id, result_message_id = await add_dinner(deps)
    await switch_to_new_ledger(deps, chat_id=-42, name="Osaka Leg")

    by_id = await dispatch(
        message_update(update_id=6, chat_id=-42, text=f"/delete {expense_id}", from_user=BOB), deps
    )
    by_reply = await dispatch(
        message_update(
            update_id=7,
            chat_id=-42,
            text="/delete",
            from_user=BOB,
            reply_to_message_id=result_message_id,
        ),
        deps,
    )

    for [reply] in (by_id, by_reply):
        assert "📒 Japan Trip" in reply.text  # names the sealed ledger, points to switch (§0.10)
        assert "switch" in reply.text.lower()
    assert (await read_expense(deps, expense_id)).deleted_at is None


async def test_edit_changes_the_description_and_nothing_financial(deps):
    await setup_group(deps)
    expense_id, _ = await add_dinner(deps)
    balances_before = await balance_text(deps, update_id=200)

    [reply] = await dispatch(
        message_update(
            update_id=6, chat_id=-42, text=f"/edit {expense_id} team dinner", from_user=BOB
        ),
        deps,
    )

    assert f"#{expense_id}" in reply.text
    assert "team dinner" in reply.text
    expense = await read_expense(deps, expense_id)
    assert expense.description == "team dinner"
    assert expense.edited_at is not None
    assert expense.amount_minor == 6000  # amounts are not editable (§4)
    assert await balance_text(deps, update_id=201) == balances_before


async def test_undo_of_an_edit_restores_the_previous_fields_and_redo_reapplies(deps):
    await setup_group(deps)
    expense_id, _ = await add_dinner(deps)

    outbound = await dispatch(
        message_update(
            update_id=6,
            chat_id=-42,
            text=f"/edit {expense_id} 2026-07-01 team dinner",
            from_user=BOB,
        ),
        deps,
    )
    await execute(outbound, RecordingClient(first_message_id=800), deps.session_factory)
    async with deps.session_factory() as session:
        action = (
            await session.execute(select(Action).where(Action.kind == "edit_expense"))
        ).scalar_one()

    await dispatch(
        callback_update(
            update_id=7,
            chat_id=-42,
            data=f"v1:undo:{action.id}",
            from_user=ALICE,
            message_text=outbound[0].text,
        ),
        deps,
    )

    expense = await read_expense(deps, expense_id)
    assert expense.description == "dinner"
    assert expense.occurred_on is None
    assert expense.edited_at is None  # back to never-edited

    await dispatch(
        callback_update(
            update_id=8,
            chat_id=-42,
            data=f"v1:redo:{action.id}",
            from_user=ALICE,
            message_text=outbound[0].text,
        ),
        deps,
    )

    expense = await read_expense(deps, expense_id)
    assert expense.description == "team dinner"
    assert expense.occurred_on == "2026-07-01"
    assert expense.edited_at is not None


async def run_edit(deps, text: str, update_id: int, first_message_id: int) -> Action:
    """Run an /edit command, send its reply, and return its action row."""
    outbound = await dispatch(
        message_update(update_id=update_id, chat_id=-42, text=text, from_user=BOB), deps
    )
    await execute(
        outbound, RecordingClient(first_message_id=first_message_id), deps.session_factory
    )
    async with deps.session_factory() as session:
        return (
            await session.execute(
                select(Action)
                .where(Action.kind == "edit_expense")
                .order_by(Action.id.desc())
                .limit(1)
            )
        ).scalar_one()


async def test_undoing_an_older_edit_never_wipes_a_later_edits_untouched_field(deps):
    """Adversarial-review fix: before_image must be MINIMAL (§8) — undo restores
    only the fields that edit actually changed, so a standing later edit survives."""
    await setup_group(deps)
    expense_id, _ = await add_dinner(deps)
    edit_a = await run_edit(
        deps, f"/edit {expense_id} team dinner", update_id=6, first_message_id=810
    )
    await run_edit(deps, f"/edit {expense_id} 2026-07-01", update_id=7, first_message_id=811)

    # undo the OLDER description-only edit while the date-only edit still stands
    await dispatch(
        callback_update(update_id=8, chat_id=-42, data=f"v1:undo:{edit_a.id}", from_user=ALICE),
        deps,
    )

    expense = await read_expense(deps, expense_id)
    assert expense.description == "dinner"  # edit A's change is reverted
    assert expense.occurred_on == "2026-07-01"  # edit B's standing change SURVIVES
    assert expense.edited_at is not None  # edit B still stands: the expense IS edited

    # redo edit A: its description returns, the date is still intact
    await dispatch(
        callback_update(update_id=9, chat_id=-42, data=f"v1:redo:{edit_a.id}", from_user=ALICE),
        deps,
    )
    expense = await read_expense(deps, expense_id)
    assert expense.description == "team dinner"
    assert expense.occurred_on == "2026-07-01"


async def test_back_dating_by_replying_with_a_date_never_changes_any_balance(deps):
    await setup_group(deps)
    _, first_message_id = await add_dinner(deps, update_id=5)
    await add_dinner(deps, update_id=6)
    balances_before = await balance_text(deps, update_id=200)

    [reply] = await dispatch(
        message_update(
            update_id=7,
            chat_id=-42,
            text="/edit 2019-01-05",  # date only, expense from the reply target
            from_user=BOB,
            reply_to_message_id=first_message_id,
        ),
        deps,
    )

    assert "2019-01-05" in reply.text
    assert await balance_text(deps, update_id=201) == balances_before  # §0.4
    async with deps.session_factory() as session:
        expenses = (await session.execute(select(Expense).order_by(Expense.id))).scalars().all()
        assert [e.occurred_on for e in expenses] == ["2019-01-05", None]


async def test_a_date_buried_in_the_description_stays_part_of_the_description(deps):
    # a date is recognized only as the FIRST token after the id — prose that
    # merely mentions a date must not be truncated into a back-date
    await setup_group(deps)
    expense_id, _ = await add_dinner(deps)

    [reply] = await dispatch(
        message_update(
            update_id=6,
            chat_id=-42,
            text=f"/edit {expense_id} paid on 2026-07-01 invoice",
            from_user=BOB,
        ),
        deps,
    )

    assert "paid on 2026-07-01 invoice" in reply.text
    expense = await read_expense(deps, expense_id)
    assert expense.description == "paid on 2026-07-01 invoice"
    assert expense.occurred_on is None  # not back-dated


async def test_an_impossible_date_is_refused(deps):
    await setup_group(deps)
    expense_id, _ = await add_dinner(deps)

    [reply] = await dispatch(
        message_update(update_id=6, chat_id=-42, text=f"/edit {expense_id} 2026-13-40"), deps
    )

    assert "isn't a real date" in reply.text
    expense = await read_expense(deps, expense_id)
    assert expense.description == "dinner"
    assert expense.occurred_on is None


async def test_edit_with_only_an_id_gets_usage_guidance(deps):
    await setup_group(deps)
    expense_id, _ = await add_dinner(deps)

    [reply] = await dispatch(
        message_update(update_id=6, chat_id=-42, text=f"/edit {expense_id}"), deps
    )

    assert "Usage" in reply.text


async def test_editing_a_deleted_expense_is_refused_with_a_pointer_to_undo(deps):
    await setup_group(deps)
    expense_id, _ = await add_dinner(deps)
    await delete_expense(deps, expense_id)

    [reply] = await dispatch(
        message_update(update_id=7, chat_id=-42, text=f"/edit {expense_id} brunch"), deps
    )

    assert "deleted" in reply.text
    assert "↩️" in reply.text
    expense = await read_expense(deps, expense_id)
    assert expense.description == "dinner"


async def test_editing_across_the_ledger_seal_is_refused(deps):
    await setup_group(deps)
    expense_id, _ = await add_dinner(deps)
    await switch_to_new_ledger(deps, chat_id=-42, name="Osaka Leg")

    [reply] = await dispatch(
        message_update(update_id=6, chat_id=-42, text=f"/edit {expense_id} brunch"), deps
    )

    assert "📒 Japan Trip" in reply.text
    assert (await read_expense(deps, expense_id)).description == "dinner"


async def test_replying_to_a_delete_or_edit_result_still_resolves_the_expense(deps):
    await setup_group(deps)
    expense_id, _ = await add_dinner(deps)

    edit_outbound = await dispatch(
        message_update(update_id=6, chat_id=-42, text=f"/edit {expense_id} team dinner"), deps
    )
    client = RecordingClient(first_message_id=900)
    await execute(edit_outbound, client, deps.session_factory)

    # replying to the EDIT result deletes the same expense — the chain of result
    # messages keeps pointing home (§11)
    [reply] = await dispatch(
        message_update(
            update_id=7, chat_id=-42, text="/delete", from_user=BOB, reply_to_message_id=900
        ),
        deps,
    )

    assert "Deleted" in reply.text
    assert (await read_expense(deps, expense_id)).deleted_at is not None


async def test_edit_replying_with_a_conflicting_leading_id_is_refused_not_guessed(deps):
    # a leading integer always parses as the #id — when it disagrees with the
    # reply target we ask instead of guessing which expense (or description) was meant
    await setup_group(deps)
    _, first_message_id = await add_dinner(deps, update_id=5)
    second_id, _ = await add_dinner(deps, update_id=6)

    [reply] = await dispatch(
        message_update(
            update_id=7,
            chat_id=-42,
            text=f"/edit {second_id} team dinner",
            from_user=BOB,
            reply_to_message_id=first_message_id,
        ),
        deps,
    )

    assert "can't tell which" in reply.text
    async with deps.session_factory() as session:
        expenses = (await session.execute(select(Expense).order_by(Expense.id))).scalars().all()
        assert [e.description for e in expenses] == ["dinner", "dinner"]


async def test_an_anonymous_admin_cannot_delete(deps):
    await setup_group(deps)
    expense_id, _ = await add_dinner(deps)
    anonymous_admin = {"id": 1087968824, "is_bot": True, "first_name": "Group"}

    [reply] = await dispatch(
        message_update(
            update_id=6, chat_id=-42, text=f"/delete {expense_id}", from_user=anonymous_admin
        ),
        deps,
    )

    assert "anonymous" in reply.text.lower()
    assert (await read_expense(deps, expense_id)).deleted_at is None  # no actor, no audit (§11)
