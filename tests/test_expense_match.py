"""Descriptive expense references (§11 tertiary tier) — slice 13, issue #14.

"Delete the dinner one": the LLM extracts a match query; the app resolves it
CPU-side against the pinned ledger's non-deleted descriptions. Unique -> the
proposal names it; several -> an expense pick-list; none -> guidance."""

from sqlalchemy import select

from expensir.core.handler import dispatch
from expensir.db.models import Expense, PendingIntent, utcnow
from expensir.llm.wire import WireDeleteExpense
from expensir.transports.executor import execute
from tests.factories import callback_update, message_update
from tests.fakes import FakeLLM
from tests.test_executor import FakeTelegramClient
from tests.test_nl import ALICE, arrange_group, keyboard_buttons, mention
from tests.test_refine import reply_to

DINNER_QUERY = WireDeleteExpense(expense_id=None, match="dinner")


async def arrange_two_expenses(deps) -> tuple[int, int]:
    """A dinner and a taxi on the books; returns their expense ids."""
    await arrange_group(deps)
    await dispatch(
        message_update(update_id=11, text="/equal 40 dinner @sam", from_user=ALICE, message_id=21),
        deps,
    )
    await dispatch(
        message_update(update_id=12, text="/equal 20 taxi @sam", from_user=ALICE, message_id=22),
        deps,
    )
    async with deps.session_factory() as session:
        expenses = list((await session.execute(select(Expense).order_by(Expense.id))).scalars())
        assert [e.description for e in expenses] == ["dinner", "taxi"]
        return expenses[0].id, expenses[1].id


async def test_a_unique_description_match_proposes_and_commits_the_delete(deps):
    """Issue #14 scope addition: a descriptive reference that matches exactly
    one expense proposes its deletion; confirm commits it."""
    dinner_id, taxi_id = await arrange_two_expenses(deps)
    deps.llm = FakeLLM([DINNER_QUERY])

    actions = await mention(deps, "delete the dinner one", update_id=13, message_id=30)

    (send,) = actions
    assert send.kind == "send_message"
    assert f"#{dinner_id}" in send.text and "dinner" in send.text  # named, WYSIWYG
    assert "reply to correct" in send.text
    pending_id = send.records_message_for_pending_id
    assert pending_id is not None

    await dispatch(
        callback_update(data=f"v1:confirm:{pending_id}", from_user=ALICE, message_id=555),
        deps,
    )

    async with deps.session_factory() as session:
        dinner = await session.get_one(Expense, dinner_id)
        taxi = await session.get_one(Expense, taxi_id)
        assert dinner.deleted_at is not None  # the dinner, gone
        assert taxi.deleted_at is None  # the taxi, untouched


async def arrange_second_dinner(deps) -> int:
    """A second expense whose description also matches "dinner"; returns its id."""
    await dispatch(
        message_update(
            update_id=13, text="/equal 30 dinner drinks @sam", from_user=ALICE, message_id=23
        ),
        deps,
    )
    async with deps.session_factory() as session:
        return (
            await session.execute(select(Expense.id).where(Expense.description == "dinner drinks"))
        ).scalar_one()


async def test_an_ambiguous_description_parks_an_expense_pick_stage(deps):
    """Grilled (issue #14): several matches never guess — the proposal renders
    expense pick buttons, newest first, and no Confirm until the slot is pinned."""
    dinner_id, taxi_id = await arrange_two_expenses(deps)
    drinks_id = await arrange_second_dinner(deps)
    deps.llm = FakeLLM([DINNER_QUERY])

    actions = await mention(deps, "delete the dinner one", update_id=14, message_id=30)

    (send,) = actions
    assert send.kind == "send_message"
    assert "More than one expense matches “dinner”" in send.text
    pending_id = send.records_message_for_pending_id
    assert pending_id is not None
    assert keyboard_buttons(send.reply_markup) == [
        (f"#{drinks_id} dinner drinks — SGD 30.00", f"v1:pickx:{pending_id}:{drinks_id}"),
        (f"#{dinner_id} dinner — SGD 40.00", f"v1:pickx:{pending_id}:{dinner_id}"),
        ("✖ Cancel", f"v1:cancel:{pending_id}"),
    ]
    assert "✅" not in str(send.reply_markup)  # no Confirm while the slot is open
    async with deps.session_factory() as session:
        for expense_id in (dinner_id, taxi_id, drinks_id):
            expense = await session.get_one(Expense, expense_id)
            assert expense.deleted_at is None  # nothing committed (§0.7)


async def deliver_ambiguous_delete(deps) -> tuple[int, int, int, int, int]:
    """The ambiguous "delete the dinner one", delivered: returns
    (dinner_id, taxi_id, drinks_id, pending_id, proposal message_id)."""
    dinner_id, taxi_id = await arrange_two_expenses(deps)
    drinks_id = await arrange_second_dinner(deps)
    deps.llm = FakeLLM([DINNER_QUERY])
    actions = await mention(deps, "delete the dinner one", update_id=14, message_id=30)
    await execute(actions, FakeTelegramClient(), deps.session_factory)
    async with deps.session_factory() as session:
        pending = (await session.execute(select(PendingIntent))).scalar_one()
        assert pending.message_id is not None
        return dinner_id, taxi_id, drinks_id, pending.id, pending.message_id


async def test_a_pickx_tap_pins_the_expense_and_confirm_deletes_it(deps):
    """Grilled (issue #14): the tap pins the slot to one concrete expense, the
    proposal re-renders with Confirm, and the commit deletes exactly that one."""
    dinner_id, taxi_id, drinks_id, pending_id, proposal_mid = await deliver_ambiguous_delete(deps)
    async with deps.session_factory() as session:
        before_expiry = (await session.execute(select(PendingIntent))).scalar_one().expires_at

    actions = await dispatch(
        callback_update(
            data=f"v1:pickx:{pending_id}:{dinner_id}", from_user=ALICE, message_id=proposal_mid
        ),
        deps,
    )

    ack, edit = actions
    assert ack.kind == "answer_callback_query" and ack.text == "Got it."
    assert edit.kind == "edit_message" and edit.message_id == proposal_mid
    assert f"#{dinner_id}" in edit.text and "dinner" in edit.text  # the pick, named
    labels = [b[0] for b in keyboard_buttons(edit.reply_markup)]
    assert labels == ["✅ Confirm", "✖ Cancel"]  # the slot pinned -> Confirm appears
    async with deps.session_factory() as session:
        pending = (await session.execute(select(PendingIntent))).scalar_one()
        assert pending.intent_json["expense_id"] == dinner_id  # pinned into the parked intent
        assert pending.expires_at > before_expiry  # a pick is active editing: TTL restarts

    await dispatch(
        callback_update(data=f"v1:confirm:{pending_id}", from_user=ALICE, message_id=proposal_mid),
        deps,
    )

    async with deps.session_factory() as session:
        assert (await session.get_one(Expense, dinner_id)).deleted_at is not None
        assert (await session.get_one(Expense, taxi_id)).deleted_at is None
        assert (await session.get_one(Expense, drinks_id)).deleted_at is None


async def test_a_reply_can_resolve_the_open_expense_slot(deps):
    """Grilled decision 5 (issue #14): one refine seam — the reply gets the open
    expense slot's candidates, and echoing the chosen expense_id pins the slot."""
    dinner_id, _, drinks_id, _, proposal_mid = await deliver_ambiguous_delete(deps)
    fake = FakeLLM([], refinements=[WireDeleteExpense(expense_id=drinks_id, match="dinner")])
    deps.llm = fake

    actions = await reply_to(deps, proposal_mid, "the drinks one")

    (edit,) = actions
    assert edit.kind == "edit_message" and edit.message_id == proposal_mid
    assert f"#{drinks_id}" in edit.text  # the choice, previewed
    labels = [b[0] for b in keyboard_buttons(edit.reply_markup)]
    assert labels == ["✅ Confirm", "✖ Cancel"]  # the slot pinned -> Confirm appears
    _, correction, candidates = fake.refined[0]
    assert correction == "the drinks one"
    assert candidates == [
        f"expense_id:{drinks_id} = dinner drinks — SGD 30.00",
        f"expense_id:{dinner_id} = dinner — SGD 40.00",
    ]
    async with deps.session_factory() as session:
        pending = (await session.execute(select(PendingIntent))).scalar_one()
        assert pending.intent_json["expense_id"] == drinks_id  # pinned, like a tap


async def test_a_crowded_match_shows_only_the_five_newest_with_id_guidance(deps):
    """Grilled decision 6 (issue #14): the pick-list caps at the 5 newest and
    says what was dropped — an older expense is reachable by its #id."""
    await arrange_group(deps)
    for n in range(6):
        await dispatch(
            message_update(
                update_id=20 + n,
                text=f"/equal 1{n} dinner {n} @sam",
                from_user=ALICE,
                message_id=40 + n,
            ),
            deps,
        )
    deps.llm = FakeLLM([DINNER_QUERY])

    actions = await mention(deps, "delete the dinner one", update_id=30, message_id=50)

    (send,) = actions
    assert "showing the 5 newest of 6 — use its #id" in send.text
    buttons = keyboard_buttons(send.reply_markup)
    assert len(buttons) == 6  # 5 candidates + Cancel
    assert [b[0] for b in buttons[:5]] == [
        f"#{6 - n} dinner {5 - n} — SGD 1{5 - n}.00" for n in range(5)
    ]  # newest first; "dinner 0" (the oldest) dropped


async def test_no_match_gets_guidance_and_no_proposal(deps):
    """Grilled decision 6 (issue #14): nothing matches -> guidance toward a
    reply or #id; no proposal parks."""
    await arrange_two_expenses(deps)
    deps.llm = FakeLLM([WireDeleteExpense(expense_id=None, match="karaoke")])

    actions = await mention(deps, "delete the karaoke one", update_id=14, message_id=30)

    (send,) = actions
    assert "Nothing here matches “karaoke”" in send.text
    assert send.reply_markup is None
    async with deps.session_factory() as session:
        assert (await session.execute(select(PendingIntent))).scalar_one_or_none() is None


async def test_a_refine_to_a_unique_match_pins_it_against_retargeting(deps):
    """WYSIWYG (§10.3): a correction's descriptive reference pins NOW when
    unique — the previewed #id is the one that commits, even if another
    matching expense arrives before Confirm."""
    _, taxi_id, _, _, proposal_mid = await deliver_ambiguous_delete(deps)
    deps.llm = FakeLLM([], refinements=[WireDeleteExpense(expense_id=None, match="taxi")])

    actions = await reply_to(deps, proposal_mid, "the taxi one actually")

    (edit,) = actions
    assert f"#{taxi_id}" in edit.text  # previewed by its pinned id
    labels = [b[0] for b in keyboard_buttons(edit.reply_markup)]
    assert labels == ["✅ Confirm", "✖ Cancel"]
    async with deps.session_factory() as session:
        pending = (await session.execute(select(PendingIntent))).scalar_one()
        assert pending.intent_json["expense_id"] == taxi_id  # pinned, not re-matched later


async def test_a_pickx_tap_after_every_match_vanished_fails_gracefully(deps):
    """Review finding (slice 13): both candidates soft-deleted under an open
    pick stage — the tap must answer and bury the proposal, never crash into
    a Telegram retry loop."""
    dinner_id, _, drinks_id, pending_id, proposal_mid = await deliver_ambiguous_delete(deps)
    async with deps.session_factory() as session, session.begin():
        for expense_id in (dinner_id, drinks_id):
            (await session.get_one(Expense, expense_id)).deleted_at = utcnow()

    actions = await dispatch(
        callback_update(
            data=f"v1:pickx:{pending_id}:{dinner_id}", from_user=ALICE, message_id=proposal_mid
        ),
        deps,
    )

    ack, edit = actions
    assert ack.kind == "answer_callback_query" and ack.text == "Nothing was recorded."
    assert "nothing was recorded" in edit.text.lower()
    assert edit.reply_markup is None  # a dead proposal keeps no buttons
    async with deps.session_factory() as session:
        assert (await session.get(PendingIntent, pending_id)) is None  # consumed


async def test_a_reply_after_every_match_vanished_still_reaches_the_llm(deps):
    """Review finding (slice 13): the open slot can stop rendering (all its
    candidates deleted) — a reply must still be treated as a correction, not
    crash while re-deriving the slot."""
    dinner_id, taxi_id, drinks_id, _, proposal_mid = await deliver_ambiguous_delete(deps)
    async with deps.session_factory() as session, session.begin():
        for expense_id in (dinner_id, drinks_id):
            (await session.get_one(Expense, expense_id)).deleted_at = utcnow()
    fake = FakeLLM([], refinements=[WireDeleteExpense(expense_id=None, match="taxi")])
    deps.llm = fake

    actions = await reply_to(deps, proposal_mid, "the taxi one instead")

    (edit,) = actions
    assert edit.kind == "edit_message"
    assert f"#{taxi_id}" in edit.text  # the correction re-aimed the delete
    _, correction, candidates = fake.refined[0]
    assert correction == "the taxi one instead"
    assert candidates is None  # the dead slot offers no choices; the reply stands alone


async def test_a_foreign_groups_id_previews_as_not_found_never_leaked(deps):
    """Review finding (slice 13): the preview enforces the ledger seal (§0.10)
    exactly like the commit does — another group's #id reads as not-found here,
    and its description never renders in this chat."""
    dinner_id, _ = await arrange_two_expenses(deps)
    other_chat = -100600
    await dispatch(
        message_update(
            update_id=30,
            text="/homecurrency SGD",
            from_user=ALICE,
            chat_id=other_chat,
            message_id=60,
        ),
        deps,
    )
    deps.llm = FakeLLM([WireDeleteExpense(expense_id=dinner_id, match=None)])

    actions = await dispatch(
        message_update(
            update_id=31,
            text=f"@expensir_bot delete #{dinner_id}",
            from_user=ALICE,
            chat_id=other_chat,
            message_id=61,
        ),
        deps,
    )

    (send,) = actions
    assert "can't find" in send.text and f"#{dinner_id}" in send.text
    assert "dinner" not in send.text  # the other group's description, never leaked
    async with deps.session_factory() as session:
        assert (await session.execute(select(PendingIntent))).scalar_one_or_none() is None


async def test_a_confirm_racing_an_open_expense_slot_commits_nothing(deps):
    """Grilled decision 8 (issue #14): a Confirm that lands while the expense
    slot is still ambiguous commits nothing and re-renders the pick stage."""
    dinner_id, taxi_id, drinks_id, pending_id, proposal_mid = await deliver_ambiguous_delete(deps)

    actions = await dispatch(
        callback_update(data=f"v1:confirm:{pending_id}", from_user=ALICE, message_id=proposal_mid),
        deps,
    )

    ack, edit = actions
    assert edit.kind == "edit_message"
    assert "More than one expense matches “dinner”" in edit.text
    assert f"v1:pickx:{pending_id}:{drinks_id}" in str(edit.reply_markup)
    async with deps.session_factory() as session:
        assert (await session.get(PendingIntent, pending_id)) is not None  # the row survives
        for expense_id in (dinner_id, taxi_id, drinks_id):
            assert (await session.get_one(Expense, expense_id)).deleted_at is None
