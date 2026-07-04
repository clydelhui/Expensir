"""Slice 5: /exact, /shares, /percent — issue #6, §7.1."""

from sqlalchemy import select

from expensir.core.handler import dispatch
from expensir.db.models import Action, Expense, ExpenseSplit
from tests.factories import bot_added_update, message_update, user

ALICE = user(1001, "Alice", "alice")
BOB = user(1002, "Bob", "bob")
CAROL = user(1003, "Carol", "carol")


async def setup_group(deps, chat_id: int = -42, home: str = "EUR") -> None:
    await dispatch(bot_added_update(chat_id=chat_id, by=ALICE), deps)
    await dispatch(
        message_update(update_id=90, chat_id=chat_id, text=f"/homecurrency {home}"), deps
    )
    await dispatch(message_update(update_id=91, chat_id=chat_id, text="hi", from_user=BOB), deps)
    await dispatch(message_update(update_id=92, chat_id=chat_id, text="hi", from_user=CAROL), deps)


async def read_expenses(deps) -> list[Expense]:
    async with deps.session_factory() as session:
        return list((await session.execute(select(Expense).order_by(Expense.id))).scalars())


async def read_splits(deps, expense_id: int) -> dict[int, int]:
    async with deps.session_factory() as session:
        splits = (
            await session.execute(select(ExpenseSplit).where(ExpenseSplit.expense_id == expense_id))
        ).scalars()
        return {s.user_id: s.owed_minor for s in splits}


async def test_exact_commits_the_stated_per_person_amounts(deps):
    await setup_group(deps)

    [reply] = await dispatch(
        message_update(update_id=5, chat_id=-42, text="/exact 60 dinner @alice=21.50 @bob=38.50"),
        deps,
    )

    [expense] = await read_expenses(deps)
    assert expense.amount_minor == 6000
    assert expense.split_type == "exact"
    splits = await read_splits(deps, expense.id)
    assert sorted(splits.values()) == [2150, 3850]
    assert reply.kind == "send_message"
    assert "📒 Japan Trip" in reply.text
    assert f"#{expense.id}" in reply.text


async def test_exact_rejects_parts_short_of_the_total_showing_the_difference(deps):
    await setup_group(deps)

    [reply] = await dispatch(
        message_update(update_id=5, chat_id=-42, text="/exact 60 dinner @alice=20 @bob=30"), deps
    )

    assert "EUR 10.00" in reply.text  # the difference, not just "doesn't add up"
    assert "short" in reply.text.lower()
    assert await read_expenses(deps) == []
    async with deps.session_factory() as session:
        actions = await session.execute(select(Action).where(Action.kind == "add_expense"))
        assert actions.scalars().all() == []  # nothing partial commits (§0.9)


async def test_exact_rejects_parts_over_the_total_showing_the_difference(deps):
    await setup_group(deps)

    [reply] = await dispatch(
        message_update(update_id=5, chat_id=-42, text="/exact 60 dinner @alice=45 @bob=25"), deps
    )

    assert "EUR 10.00" in reply.text
    assert "over" in reply.text.lower()
    assert await read_expenses(deps) == []


async def test_shares_splits_by_weight_conserving_the_total(deps):
    await setup_group(deps)

    await dispatch(
        message_update(update_id=5, chat_id=-42, text="/shares 60 dinner @alice=2 @bob=1"), deps
    )

    [expense] = await read_expenses(deps)
    assert expense.split_type == "shares"
    splits = await read_splits(deps, expense.id)
    assert sorted(splits.values()) == [2000, 4000]
    assert sum(splits.values()) == expense.amount_minor == 6000


async def test_shares_bare_mention_defaults_to_weight_one(deps):
    await setup_group(deps)

    await dispatch(
        message_update(update_id=5, chat_id=-42, text="/shares 60 dinner @alice=2 @bob"), deps
    )

    [expense] = await read_expenses(deps)
    splits = await read_splits(deps, expense.id)
    assert sorted(splits.values()) == [2000, 4000]


async def test_percent_splits_within_tolerance_normalizing_to_the_total(deps):
    await setup_group(deps)

    # 33.4 + 33.3 + 33.4 = 100.1 — inside ±1.0; normalization absorbs it (§7.1)
    await dispatch(
        message_update(
            update_id=5,
            chat_id=-42,
            text="/percent 10 lunch @alice=33.4 @bob=33.3 @carol=33.4",
        ),
        deps,
    )

    [expense] = await read_expenses(deps)
    assert expense.split_type == "percent"
    splits = await read_splits(deps, expense.id)
    assert sum(splits.values()) == expense.amount_minor == 1000
    assert sorted(splits.values(), reverse=True) == [334, 333, 333]


async def test_percent_rejects_beyond_the_tolerance(deps):
    await setup_group(deps)

    [reply] = await dispatch(
        message_update(update_id=5, chat_id=-42, text="/percent 60 dinner @alice=60 @bob=38"),
        deps,
    )

    assert "98" in reply.text  # says what the percents summed to
    assert "100" in reply.text
    assert await read_expenses(deps) == []


async def test_each_type_commits_with_ledger_prefix_id_undo_and_one_action_row(deps):
    await setup_group(deps)
    commands = [
        "/exact 60 dinner @alice=21.50 @bob=38.50",
        "/shares 60 taxi @alice=2 @bob=1",
        "/percent 60 hotel @alice=50 @bob=50",
    ]

    replies = [
        (await dispatch(message_update(update_id=10 + i, chat_id=-42, text=text), deps))[0]
        for i, text in enumerate(commands)
    ]

    async with deps.session_factory() as session:
        actions = (
            (await session.execute(select(Action).where(Action.kind == "add_expense")))
            .scalars()
            .all()
        )
        expenses = list((await session.execute(select(Expense).order_by(Expense.id))).scalars())
    assert len(actions) == len(expenses) == 3  # one action row per commit (§0.2)
    for reply, expense, action in zip(replies, expenses, actions, strict=True):
        assert reply.text.startswith("📒 Japan Trip • ")
        assert f"#{expense.id}" in reply.text
        assert expense.created_by_action_id == action.id
        [[button]] = reply.reply_markup["inline_keyboard"]
        assert button["callback_data"] == f"v1:undo:{action.id}"


async def test_valued_split_replies_show_each_persons_share(deps):
    await setup_group(deps)

    [reply] = await dispatch(
        message_update(update_id=5, chat_id=-42, text="/shares 60 dinner @alice=2 @bob=1"), deps
    )

    assert "Alice EUR 40.00" in reply.text
    assert "Bob EUR 20.00" in reply.text


async def test_a_person_named_twice_with_values_rejects_instead_of_guessing(deps):
    await setup_group(deps)

    [reply] = await dispatch(
        message_update(update_id=5, chat_id=-42, text="/shares 60 dinner @alice=2 @alice=1 @bob=1"),
        deps,
    )

    assert "@alice" in reply.text
    assert "once" in reply.text.lower()
    assert await read_expenses(deps) == []


async def test_a_zero_weight_rejects_and_commits_nothing(deps):
    await setup_group(deps)

    [reply] = await dispatch(
        message_update(update_id=5, chat_id=-42, text="/shares 60 dinner @alice=0 @bob=1"), deps
    )

    assert "positive" in reply.text.lower()
    assert await read_expenses(deps) == []


async def test_valued_splits_without_participants_or_description_reply_usage(deps):
    await setup_group(deps)
    bad = [
        "/exact 60 dinner",  # no participants
        "/exact 60 @alice=20 @bob=40",  # no description
        "/exact 60 dinner @alice=20 @bob",  # exact requires a value per person
        "/percent 60 dinner @alice=50 @bob",  # so does percent
        "/shares",  # no amount
    ]

    for i, text in enumerate(bad):
        [reply] = await dispatch(message_update(update_id=20 + i, chat_id=-42, text=text), deps)
        assert "Usage" in reply.text, text
    assert await read_expenses(deps) == []


async def test_exact_parts_convert_in_the_resolved_currency(deps):
    await setup_group(deps)

    await dispatch(
        message_update(update_id=5, chat_id=-42, text="/exact 600 JPY snacks @alice=200 @bob=400"),
        deps,
    )

    [expense] = await read_expenses(deps)
    assert expense.currency == "JPY"
    splits = await read_splits(deps, expense.id)
    assert sorted(splits.values()) == [200, 400]  # whole yen, not scaled by 100


async def test_exact_part_below_the_smallest_unit_rejects_instead_of_rounding(deps):
    await setup_group(deps)

    # 21.505 + 38.495 = 60 as typed, but neither lands on a whole cent; rounding
    # them would either mis-report the sum or silently commit altered amounts (§3)
    [reply] = await dispatch(
        message_update(update_id=5, chat_id=-42, text="/exact 60 dinner @alice=21.505 @bob=38.495"),
        deps,
    )

    assert "21.505" in reply.text  # names the offending part
    assert "cent" in reply.text.lower() or "smallest" in reply.text.lower()
    assert await read_expenses(deps) == []


async def test_two_refs_resolving_to_one_person_reject_at_the_domain_layer(deps):
    # the parser blocks a repeated @ref, but the domain must reject on its own when
    # DISTINCT refs land on one user (e.g. NL's "me" + "@alice"), never overwrite (§0.9)
    from expensir.db.models import Group, User
    from expensir.domain.apply import ApplyContext, apply_intent
    from expensir.domain.errors import Rejection
    from expensir.intents.schema import AddExpense, SplitMember

    await setup_group(deps)

    raised = None
    async with deps.session_factory() as session, session.begin():
        group = (await session.execute(select(Group))).scalar_one()
        actor = (
            await session.execute(select(User).where(User.display_name == "Alice"))
        ).scalar_one()
        intent = AddExpense(
            payer_ref="me",
            amount_minor=6000,
            currency="EUR",
            description="dinner",
            split_type="shares",
            participants=[
                SplitMember(user_ref="me", weight=2),  # resolves to the actor: alice
                SplitMember(user_ref="@alice", weight=1),  # ...and so does this
            ],
        )
        ctx = ApplyContext(session=session, group=group, actor=actor, seed=1)
        try:
            await apply_intent(intent, ctx)
        except Rejection as exc:
            raised = str(exc)

    assert raised is not None and "once" in raised.lower()
    assert await read_expenses(deps) == []


async def test_weighted_shares_are_stable_for_a_fixed_message_id(deps):
    await setup_group(deps)
    command = "/percent 10 lunch @alice=33.4 @bob=33.3 @carol=33.3"

    # the allocation seed is the originating message id (§7.1) — same id, same shares
    await dispatch(message_update(update_id=5, chat_id=-42, text=command, message_id=77), deps)
    await dispatch(message_update(update_id=6, chat_id=-42, text=command, message_id=77), deps)

    first, second = await read_expenses(deps)
    assert (await read_splits(deps, first.id)) == (await read_splits(deps, second.id))
