from datetime import UTC, datetime

from sqlalchemy import select

from expensir.core.handler import dispatch
from expensir.db.models import Action, Expense, Group, Ledger
from tests.factories import bot_added_update, message_update, user

ALICE = user(1001, "Alice", "alice")
BOB = user(1002, "Bob", "bob")


async def setup_group(deps, chat_id: int = -42) -> None:
    await dispatch(bot_added_update(chat_id=chat_id, by=ALICE), deps)
    await dispatch(message_update(update_id=90, chat_id=chat_id, text="/homecurrency EUR"), deps)
    await dispatch(message_update(update_id=91, chat_id=chat_id, text="hi", from_user=BOB), deps)


async def test_balance_on_a_fresh_ledger_reports_all_settled(deps):
    await setup_group(deps)

    [reply] = await dispatch(message_update(update_id=5, chat_id=-42, text="/balance"), deps)

    assert "📒 Japan Trip" in reply.text
    assert "settled" in reply.text.lower()


async def test_balance_reads_back_an_equal_split(deps):
    await setup_group(deps)
    await dispatch(
        message_update(update_id=5, chat_id=-42, text="/equal 60 dinner @alice @bob"), deps
    )

    [reply] = await dispatch(message_update(update_id=6, chat_id=-42, text="/balance"), deps)

    # Alice paid 60 and owes her 30 share: net -30 (owed); Bob owes 30
    assert "Bob owes EUR 30.00" in reply.text
    assert "Alice is owed EUR 30.00" in reply.text


async def test_balance_shows_each_currency_as_its_own_bucket(deps):
    await setup_group(deps)
    await dispatch(
        message_update(update_id=5, chat_id=-42, text="/equal 60 dinner @alice @bob"), deps
    )
    await dispatch(
        message_update(update_id=6, chat_id=-42, text="/equal 500 JPY snacks @alice @bob"),
        deps,
    )

    [reply] = await dispatch(message_update(update_id=7, chat_id=-42, text="/balance"), deps)

    assert "Bob owes EUR 30.00" in reply.text
    assert "Bob owes JPY 250" in reply.text  # no cross-currency netting, ever


async def test_balance_me_shows_only_the_callers_position(deps):
    await setup_group(deps)
    await dispatch(
        message_update(update_id=5, chat_id=-42, text="/equal 60 dinner @alice @bob"), deps
    )

    [bob_view] = await dispatch(
        message_update(update_id=6, chat_id=-42, text="/balance me", from_user=BOB), deps
    )
    [alice_view] = await dispatch(
        message_update(update_id=7, chat_id=-42, text="/balance me"), deps
    )

    assert "You owe EUR 30.00" in bob_view.text
    assert "Alice" not in bob_view.text
    assert "You're owed EUR 30.00" in alice_view.text


async def test_balance_is_sealed_to_the_active_ledger(deps):
    await setup_group(deps)
    await dispatch(
        message_update(update_id=5, chat_id=-42, text="/equal 60 dinner @alice @bob"), deps
    )
    # /switch arrives in a later slice; repoint the active ledger directly
    async with deps.session_factory() as session, session.begin():
        group = (await session.execute(select(Group))).scalar_one()
        session.add(Ledger(group_id=group.id, name="Side Trip"))
        await session.flush()
        fresh = (
            await session.execute(select(Ledger).where(Ledger.name == "Side Trip"))
        ).scalar_one()
        group.active_ledger_id = fresh.id

    [reply] = await dispatch(message_update(update_id=6, chat_id=-42, text="/balance"), deps)

    assert "📒 Side Trip" in reply.text
    assert "settled" in reply.text.lower()  # says nothing about the other ledger (§0.10)
    assert "dinner" not in reply.text
    assert "30.00" not in reply.text


async def test_soft_deleted_expenses_are_excluded_from_replay(deps):
    await setup_group(deps)
    await dispatch(
        message_update(update_id=5, chat_id=-42, text="/equal 60 dinner @alice @bob"), deps
    )
    async with deps.session_factory() as session, session.begin():
        expense = (await session.execute(select(Expense))).scalar_one()
        expense.deleted_at = datetime.now(UTC)

    [reply] = await dispatch(message_update(update_id=6, chat_id=-42, text="/balance"), deps)

    assert "settled" in reply.text.lower()


async def test_balance_me_with_no_position_says_settled_without_leaking_others(deps):
    await setup_group(deps)
    await dispatch(
        message_update(update_id=5, chat_id=-42, text="/equal 60 dinner @alice @bob"), deps
    )
    charlie = user(1003, "Charlie", "charlie")

    [reply] = await dispatch(
        message_update(update_id=6, chat_id=-42, text="/balance me", from_user=charlie), deps
    )

    assert "You're all settled up" in reply.text
    assert "Alice" not in reply.text
    assert "30.00" not in reply.text


async def test_members_whose_nets_cancel_to_zero_are_omitted(deps):
    await setup_group(deps)
    # reciprocal dinners: both nets are literal zeros, not missing keys
    await dispatch(
        message_update(update_id=5, chat_id=-42, text="/equal 60 dinner @alice @bob"), deps
    )
    await dispatch(
        message_update(
            update_id=6, chat_id=-42, text="/equal 60 brunch @alice @bob", from_user=BOB
        ),
        deps,
    )

    [reply] = await dispatch(message_update(update_id=7, chat_id=-42, text="/balance"), deps)

    assert "settled" in reply.text.lower()
    assert "owes" not in reply.text
    assert "EUR 0.00" not in reply.text


async def test_soft_delete_exclusion_with_a_surviving_expense(deps):
    await setup_group(deps)
    await dispatch(
        message_update(update_id=5, chat_id=-42, text="/equal 60 dinner @alice @bob"), deps
    )
    await dispatch(
        message_update(update_id=6, chat_id=-42, text="/equal 10 coffee @alice @bob"), deps
    )
    async with deps.session_factory() as session, session.begin():
        dinner = (
            await session.execute(select(Expense).where(Expense.description == "dinner"))
        ).scalar_one()
        dinner.deleted_at = datetime.now(UTC)

    [reply] = await dispatch(message_update(update_id=7, chat_id=-42, text="/balance"), deps)

    assert "Bob owes EUR 5.00" in reply.text  # only the coffee survives
    assert "35.00" not in reply.text


async def test_balance_with_junk_arguments_replies_usage(deps):
    await setup_group(deps)

    [reply] = await dispatch(
        message_update(update_id=5, chat_id=-42, text="/balance everything"), deps
    )

    assert "Usage" in reply.text


async def test_balance_writes_no_action_row(deps):
    await setup_group(deps)
    await dispatch(
        message_update(update_id=5, chat_id=-42, text="/equal 60 dinner @alice @bob"), deps
    )

    async with deps.session_factory() as session:
        before = len((await session.execute(select(Action))).scalars().all())
    await dispatch(message_update(update_id=6, chat_id=-42, text="/balance"), deps)
    async with deps.session_factory() as session:
        after = len((await session.execute(select(Action))).scalars().all())

    assert after == before
