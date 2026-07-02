from sqlalchemy import select

from expensir.core.handler import dispatch
from expensir.db.models import Action, Expense, ExpenseSplit, Ledger
from tests.factories import bot_added_update, message_update, user

ALICE = user(1001, "Alice", "alice")
BOB = user(1002, "Bob", "bob")
CAROL = user(1003, "Carol", "carol")


async def setup_group(deps, chat_id: int = -42, home: str | None = "EUR") -> None:
    await dispatch(bot_added_update(chat_id=chat_id, by=ALICE), deps)
    if home is not None:
        await dispatch(
            message_update(update_id=90, chat_id=chat_id, text=f"/homecurrency {home}"), deps
        )
    # Bob becomes registered by interacting once (§11)
    await dispatch(message_update(update_id=91, chat_id=chat_id, text="hello", from_user=BOB), deps)


async def read_expenses(deps) -> list[Expense]:
    async with deps.session_factory() as session:
        return list((await session.execute(select(Expense).order_by(Expense.id))).scalars())


async def read_splits(deps, expense_id: int) -> dict[int, int]:
    async with deps.session_factory() as session:
        splits = (
            await session.execute(select(ExpenseSplit).where(ExpenseSplit.expense_id == expense_id))
        ).scalars()
        return {s.user_id: s.owed_minor for s in splits}


async def test_equal_commits_an_expense_split_between_the_named_members(deps):
    await setup_group(deps)

    [reply] = await dispatch(
        message_update(update_id=5, chat_id=-42, text="/equal 60 dinner @alice @bob"), deps
    )

    [expense] = await read_expenses(deps)
    assert expense.amount_minor == 6000
    assert expense.currency == "EUR"
    assert expense.description == "dinner"
    assert expense.split_type == "equal"
    assert expense.source == "command"
    splits = await read_splits(deps, expense.id)
    assert sorted(splits.values()) == [3000, 3000]
    assert expense.payer_id in splits  # Alice both paid and shares

    assert reply.kind == "send_message"
    assert "📒 Japan Trip" in reply.text
    assert f"#{expense.id}" in reply.text
    assert "EUR 60.00" in reply.text
    assert "dinner" in reply.text


async def test_equal_writes_one_action_row_stamped_onto_expense_and_splits(deps):
    await setup_group(deps)

    await dispatch(
        message_update(update_id=5, chat_id=-42, text="/equal 60 dinner @alice @bob"), deps
    )

    async with deps.session_factory() as session:
        actions = (
            (await session.execute(select(Action).where(Action.kind == "add_expense")))
            .scalars()
            .all()
        )
        [action] = actions  # exactly ONE action row for the whole mutation (§0.2)
        [expense] = (await session.execute(select(Expense))).scalars().all()
        splits = (await session.execute(select(ExpenseSplit))).scalars().all()

    assert expense.created_by_action_id == action.id
    assert len(splits) == 2
    assert all(s.created_by_action_id == action.id for s in splits)
    assert action.intent_json["amount_minor"] == 6000
    assert action.ledger_id == expense.ledger_id


async def test_three_way_split_distributes_whole_cents_conserving_the_total(deps):
    await setup_group(deps)
    await dispatch(message_update(update_id=92, chat_id=-42, text="hi", from_user=CAROL), deps)

    await dispatch(
        message_update(update_id=5, chat_id=-42, text="/equal 10 lunch @alice @bob @carol"), deps
    )

    [expense] = await read_expenses(deps)
    splits = await read_splits(deps, expense.id)
    assert sorted(splits.values(), reverse=True) == [334, 333, 333]
    assert sum(splits.values()) == expense.amount_minor == 1000


async def test_the_extra_cent_recipient_is_stable_for_a_fixed_message_id(deps):
    await setup_group(deps)
    await dispatch(message_update(update_id=92, chat_id=-42, text="hi", from_user=CAROL), deps)
    command = "/equal 10 lunch @alice @bob @carol"

    # the allocation seed is the originating message id (§7.1) — same id, same shares
    await dispatch(message_update(update_id=5, chat_id=-42, text=command, message_id=77), deps)
    await dispatch(message_update(update_id=6, chat_id=-42, text=command, message_id=77), deps)

    first, second = await read_expenses(deps)
    assert (await read_splits(deps, first.id)) == (await read_splits(deps, second.id))


async def test_an_unregistered_reference_rejects_the_whole_intent(deps):
    await setup_group(deps)

    [reply] = await dispatch(
        message_update(update_id=5, chat_id=-42, text="/equal 60 dinner @alice @carol"), deps
    )

    assert "@carol" in reply.text
    assert "/setup" in reply.text
    async with deps.session_factory() as session:
        assert (await session.execute(select(Expense))).scalars().all() == []
        assert (await session.execute(select(ExpenseSplit))).scalars().all() == []
        actions = await session.execute(select(Action).where(Action.kind == "add_expense"))
        assert actions.scalars().all() == []  # nothing partial commits (§0.9)


async def test_empty_participants_means_everyone_and_the_reply_names_them(deps):
    await setup_group(deps)

    [reply] = await dispatch(
        message_update(update_id=5, chat_id=-42, text="/equal 60 dinner"), deps
    )

    [expense] = await read_expenses(deps)
    splits = await read_splits(deps, expense.id)
    assert sorted(splits.values()) == [3000, 3000]  # Alice and Bob, payer included
    assert "Alice" in reply.text
    assert "Bob" in reply.text


async def test_unresolvable_currency_rejects_with_set_a_currency_guidance(deps):
    await setup_group(deps, home=None)

    [reply] = await dispatch(
        message_update(update_id=5, chat_id=-42, text="/equal 60 dinner @alice"), deps
    )

    assert "/homecurrency" in reply.text
    assert await read_expenses(deps) == []


async def test_an_iso_after_the_amount_overrides_and_freezes_onto_the_expense(deps):
    await setup_group(deps, home="EUR")

    [reply] = await dispatch(
        message_update(update_id=5, chat_id=-42, text="/equal 30 SGD trains @alice"), deps
    )

    [expense] = await read_expenses(deps)
    assert expense.currency == "SGD"
    assert expense.amount_minor == 3000
    assert "SGD 30.00" in reply.text


async def test_ledger_logging_currency_beats_the_group_home_currency(deps):
    await setup_group(deps, home="EUR")
    async with deps.session_factory() as session, session.begin():
        ledger = (await session.execute(select(Ledger))).scalar_one()
        ledger.logging_currency = "JPY"  # /currency arrives in a later slice (§3)

    [reply] = await dispatch(
        message_update(update_id=5, chat_id=-42, text="/equal 600 snacks @alice"), deps
    )

    [expense] = await read_expenses(deps)
    assert expense.currency == "JPY"
    assert expense.amount_minor == 600  # 0-minor-digit currency: whole yen
    assert "JPY 600" in reply.text


async def test_overprecise_input_rounds_half_up_and_the_reply_shows_it(deps):
    await setup_group(deps)

    [reply] = await dispatch(
        message_update(update_id=5, chat_id=-42, text="/equal 6000.50 JPY snacks @alice"), deps
    )

    [expense] = await read_expenses(deps)
    assert expense.amount_minor == 6001
    assert "JPY 6001" in reply.text
    assert "rounded" in reply.text.lower()


async def test_usernames_resolve_case_insensitively(deps):
    await setup_group(deps)

    await dispatch(
        message_update(update_id=5, chat_id=-42, text="/equal 60 dinner @Alice @Bob"), deps
    )

    [expense] = await read_expenses(deps)
    assert len(await read_splits(deps, expense.id)) == 2


async def test_an_anonymous_admin_cannot_record_an_expense(deps):
    await setup_group(deps)
    anonymous = {
        "id": 1087968824,
        "is_bot": True,
        "first_name": "Group",
        "username": "GroupAnonymousBot",
    }

    [reply] = await dispatch(
        message_update(update_id=5, chat_id=-42, text="/equal 60 dinner", from_user=anonymous),
        deps,
    )

    assert "anonymous" in reply.text.lower()
    assert await read_expenses(deps) == []


async def test_equal_without_a_description_replies_usage(deps):
    await setup_group(deps)

    [reply] = await dispatch(message_update(update_id=5, chat_id=-42, text="/equal 60"), deps)

    assert "Usage" in reply.text
    assert await read_expenses(deps) == []


async def test_a_too_large_amount_is_rejected_politely(deps):
    await setup_group(deps)

    [reply] = await dispatch(
        message_update(update_id=5, chat_id=-42, text="/equal 100000000000000000 yacht @alice"),
        deps,
    )

    assert "too large" in reply.text
    assert await read_expenses(deps) == []


async def test_an_amount_below_the_smallest_unit_is_rejected_helpfully(deps):
    await setup_group(deps)

    [reply] = await dispatch(
        message_update(update_id=5, chat_id=-42, text="/equal 0.004 gum @alice"), deps
    )

    assert "smallest" in reply.text
    assert await read_expenses(deps) == []


async def test_an_ambiguous_stored_username_rejects_instead_of_crashing(deps):
    await setup_group(deps)
    # Telegram reassigns freed usernames, and identity refresh is a later slice —
    # so two registered members can legitimately share a stored @handle
    sam_a = user(1003, "Sam A", "sam")
    sam_b = user(1004, "Sam B", "sam")
    await dispatch(message_update(update_id=92, chat_id=-42, text="hi", from_user=sam_a), deps)
    await dispatch(message_update(update_id=93, chat_id=-42, text="hi", from_user=sam_b), deps)

    [reply] = await dispatch(
        message_update(update_id=5, chat_id=-42, text="/equal 60 dinner @sam"), deps
    )

    assert "more than one" in reply.text.lower()
    assert await read_expenses(deps) == []


async def test_a_single_participant_reply_reads_naturally(deps):
    await setup_group(deps)

    [reply] = await dispatch(
        message_update(update_id=5, chat_id=-42, text="/equal 12 taxi @bob"), deps
    )

    assert "owed entirely by Bob" in reply.text
    assert "between Bob" not in reply.text
