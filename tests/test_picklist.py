"""Ambiguity pick-lists (§10, §13) — slice 13, issue #14.

A reference matching more than one member renders as a pre-confirm pick stage
on the proposal: tap or reply pins the slot, Confirm appears only once every
reference is pinned, and the bot never guesses."""

from sqlalchemy import func, select

from expensir.core.handler import dispatch
from expensir.db.models import Expense, ExpenseSplit, PendingIntent
from expensir.llm.wire import WireAddExpense, WireSplitMember
from expensir.transports.executor import execute
from tests.factories import callback_update, message_update, user
from tests.fakes import FakeLLM
from tests.test_executor import FakeTelegramClient
from tests.test_nl import (
    ALICE,
    DINNER_WITH_SAM,
    SAM,
    arrange_group,
    keyboard_buttons,
    mention,
    user_id_of,
)

OTHER_SAM = user(1004, "Sam", "sam_the_second")


async def arrange_two_sams(deps) -> None:
    """Alice + two members both displaying as "Sam" — a bare "Sam" is ambiguous."""
    await arrange_group(deps)
    await dispatch(message_update(update_id=6, text="hi", from_user=OTHER_SAM, message_id=8), deps)


async def test_an_ambiguous_reference_parks_a_pick_stage_proposal(deps):
    """Acceptance #3 (issue #14): two Sams produce pick buttons — labelled apart
    by @username — and no Confirm until the slot is pinned."""
    await arrange_two_sams(deps)
    deps.llm = FakeLLM([DINNER_WITH_SAM])

    actions = await mention(deps, "I paid 40 for dinner, split with Sam")

    (send,) = actions
    assert send.kind == "send_message"
    assert send.text.startswith("📒 Japan Trip • ")  # still pinned WYSIWYG (§10)
    assert "Sam" in send.text  # the ambiguous slot, named
    async with deps.session_factory() as session:
        pending = (await session.execute(select(PendingIntent))).scalar_one()
        sam_a = await user_id_of(session, SAM)
        sam_b = await user_id_of(session, OTHER_SAM)
        # parked, pinned, nothing committed (§0.7)
        assert (await session.execute(select(func.count()).select_from(Expense))).scalar() == 0
    assert keyboard_buttons(send.reply_markup) == [
        ("Sam (@sam)", f"v1:pick:{pending.id}:{sam_a}"),
        ("Sam (@sam_the_second)", f"v1:pick:{pending.id}:{sam_b}"),
        ("✖ Cancel", f"v1:cancel:{pending.id}"),
    ]
    assert "Confirm" not in send.text and "✅" not in str(send.reply_markup)
    assert send.records_message_for_pending_id == pending.id


async def deliver_ambiguous_dinner(deps) -> tuple[int, int]:
    """Two Sams + the dinner proposal, delivered: returns (pending_id, message_id)."""
    await arrange_two_sams(deps)
    deps.llm = FakeLLM([DINNER_WITH_SAM])
    actions = await mention(deps, "I paid 40 for dinner, split with Sam")
    await execute(actions, FakeTelegramClient(), deps.session_factory)
    async with deps.session_factory() as session:
        pending = (await session.execute(select(PendingIntent))).scalar_one()
        assert pending.message_id is not None
        return pending.id, pending.message_id


async def test_one_pick_pins_every_occurrence_of_the_same_string(deps):
    """Grilled (issue #14): one slot per distinct ref string — "Sam paid, split
    with me and Sam" needs ONE tap, pinning payer and participant together."""
    await arrange_two_sams(deps)
    deps.llm = FakeLLM(
        [
            WireAddExpense(
                payer_ref="Sam",
                amount="40",
                description="dinner",
                participants=[WireSplitMember(user_ref="me"), WireSplitMember(user_ref="Sam")],
            )
        ]
    )
    actions = await mention(deps, "Sam paid 40 for dinner, split with me and Sam")
    await execute(actions, FakeTelegramClient(), deps.session_factory)
    async with deps.session_factory() as session:
        pending = (await session.execute(select(PendingIntent))).scalar_one()
        sam_b = await user_id_of(session, OTHER_SAM)

    ack, edit = await dispatch(
        callback_update(
            data=f"v1:pick:{pending.id}:{sam_b}", from_user=ALICE, message_id=pending.message_id
        ),
        deps,
    )

    labels = [b[0] for b in keyboard_buttons(edit.reply_markup)]
    assert labels == ["✅ Confirm", "✖ Cancel"]  # nothing left to pick after ONE tap
    async with deps.session_factory() as session:
        stored = (await session.execute(select(PendingIntent))).scalar_one().intent_json
        assert stored["payer_ref"] == f"id:{sam_b}"
        assert f"id:{sam_b}" in [p["user_ref"] for p in stored["participants"]]


async def test_two_ambiguous_refs_resolve_one_slot_at_a_time(deps):
    """§10: multiple ambiguous refs pick one at a time; Confirm only appears
    once EVERY reference is pinned."""
    await arrange_two_sams(deps)
    for kim in (user(1005, "Kim", "kim_one"), user(1006, "Kim", "kim_two")):
        await dispatch(message_update(update_id=7, text="hi", from_user=kim, message_id=9), deps)
    deps.llm = FakeLLM(
        [
            WireAddExpense(
                payer_ref="me",
                amount="40",
                description="dinner",
                participants=[WireSplitMember(user_ref="Sam"), WireSplitMember(user_ref="Kim")],
            )
        ]
    )
    actions = await mention(deps, "I paid 40 for dinner, split between Sam and Kim")
    await execute(actions, FakeTelegramClient(), deps.session_factory)
    async with deps.session_factory() as session:
        pending = (await session.execute(select(PendingIntent))).scalar_one()
        sam_a = await user_id_of(session, SAM)

    # stage 1: Sam's slot is open, Kim's isn't offered yet
    _, edit = await dispatch(
        callback_update(
            data=f"v1:pick:{pending.id}:{sam_a}", from_user=ALICE, message_id=pending.message_id
        ),
        deps,
    )
    labels = [b[0] for b in keyboard_buttons(edit.reply_markup)]
    assert labels == ["Kim (@kim_one)", "Kim (@kim_two)", "✖ Cancel"]  # stage 2: Kim's turn
    assert "Kim" in edit.text

    async with deps.session_factory() as session:
        kim_one = await user_id_of(session, user(1005, "Kim", "kim_one"))
    _, edit = await dispatch(
        callback_update(
            data=f"v1:pick:{pending.id}:{kim_one}", from_user=ALICE, message_id=pending.message_id
        ),
        deps,
    )
    labels = [b[0] for b in keyboard_buttons(edit.reply_markup)]
    assert labels == ["✅ Confirm", "✖ Cancel"]  # every slot pinned


async def test_a_reply_can_resolve_the_open_pick_slot(deps):
    """Grilled (issue #14): refine receives the open slot's candidates as
    pinned id-refs, so "the first one" / "Adams" can answer the pick question."""
    pending_id, proposal_mid = await deliver_ambiguous_dinner(deps)
    async with deps.session_factory() as session:
        sam_a = await user_id_of(session, SAM)
        sam_b = await user_id_of(session, OTHER_SAM)
    fake = FakeLLM(
        [],
        refinements=[
            WireAddExpense(
                payer_ref="me",
                amount="40",
                description="dinner",
                participants=[
                    WireSplitMember(user_ref="me"),
                    WireSplitMember(user_ref=f"id:{sam_b}"),  # "the second one", as instructed
                ],
            )
        ],
    )
    deps.llm = fake

    actions = await dispatch(
        message_update(
            update_id=9,
            text="the second one",
            from_user=ALICE,
            message_id=40,
            reply_to_message_id=proposal_mid,
        ),
        deps,
    )

    # the refine seam was handed the open slot's candidates, labelled and pinned
    [(_, correction, candidates)] = fake.refined
    assert correction == "the second one"
    assert candidates == [
        f"id:{sam_a} = Sam (@sam)",
        f"id:{sam_b} = Sam (@sam_the_second)",
    ]
    (edit,) = actions
    assert edit.kind == "edit_message" and edit.message_id == proposal_mid
    labels = [b[0] for b in keyboard_buttons(edit.reply_markup)]
    assert labels == ["✅ Confirm", "✖ Cancel"]  # the slot is pinned: Confirm appears


async def test_a_pick_tap_on_an_expired_proposal_edits_it_to_expired(deps):
    from datetime import timedelta

    from expensir.db.models import utcnow

    pending_id, proposal_mid = await deliver_ambiguous_dinner(deps)
    async with deps.session_factory() as session, session.begin():
        pending = (await session.execute(select(PendingIntent))).scalar_one()
        pending.expires_at = utcnow() - timedelta(minutes=1)
        sam_b = await user_id_of(session, OTHER_SAM)

    ack, edit = await dispatch(
        callback_update(
            data=f"v1:pick:{pending_id}:{sam_b}", from_user=ALICE, message_id=proposal_mid
        ),
        deps,
    )

    assert "Expired" in edit.text
    assert edit.reply_markup is None
    async with deps.session_factory() as session:
        assert (
            await session.execute(select(func.count()).select_from(PendingIntent))
        ).scalar() == 0


async def test_cancel_works_from_the_pick_stage(deps):
    pending_id, proposal_mid = await deliver_ambiguous_dinner(deps)

    ack, edit = await dispatch(
        callback_update(data=f"v1:cancel:{pending_id}", from_user=ALICE, message_id=proposal_mid),
        deps,
    )

    assert "Cancelled" in edit.text
    async with deps.session_factory() as session:
        assert (
            await session.execute(select(func.count()).select_from(PendingIntent))
        ).scalar() == 0


async def test_a_slash_command_with_a_colliding_username_parks_a_pick_stage_proposal(deps):
    """§0.7: ambiguous reference resolution makes ANY intent fuzzy — the slash
    door parks a proposal too, with candidates told apart by display name."""
    await arrange_group(deps)
    sam_clone = user(1004, "Sam B", "sam")  # Telegram reassigned the freed @sam
    await dispatch(message_update(update_id=6, text="hi", from_user=sam_clone, message_id=8), deps)

    actions = await dispatch(
        message_update(update_id=7, text="/equal 60 dinner @sam", from_user=ALICE, message_id=12),
        deps,
    )

    (send,) = actions
    assert send.kind == "send_message"
    assert "@sam" in send.text  # the ambiguous slot, named
    async with deps.session_factory() as session:
        pending = (await session.execute(select(PendingIntent))).scalar_one()
        sam_a = await user_id_of(session, SAM)
        sam_b = await user_id_of(session, sam_clone)
        assert (await session.execute(select(func.count()).select_from(Expense))).scalar() == 0
    assert keyboard_buttons(send.reply_markup) == [
        ("Sam (@sam)", f"v1:pick:{pending.id}:{sam_a}"),
        ("Sam B (@sam)", f"v1:pick:{pending.id}:{sam_b}"),
        ("✖ Cancel", f"v1:cancel:{pending.id}"),
    ]
    assert send.records_message_for_pending_id == pending.id

    # pick Sam B, then confirm: the expense commits with the choice
    await execute([send], FakeTelegramClient(), deps.session_factory)
    await dispatch(
        callback_update(data=f"v1:pick:{pending.id}:{sam_b}", from_user=ALICE, message_id=1),
        deps,
    )
    await dispatch(
        callback_update(data=f"v1:confirm:{pending.id}", from_user=ALICE, message_id=1),
        deps,
    )
    async with deps.session_factory() as session:
        expense = (await session.execute(select(Expense))).scalar_one()
        assert expense.amount_minor == 6000
        split_user_ids = {
            s.user_id for s in (await session.execute(select(ExpenseSplit))).scalars()
        }
        assert sam_b in split_user_ids and sam_a not in split_user_ids


async def test_late_ambiguity_at_confirm_rerenders_to_the_pick_stage(deps):
    """Grilled (issue #14): "Sam" was unique at propose; a second Sam registered
    before the tap. Confirm commits nothing and the proposal returns to the
    pick stage — no dead-end, never a guess."""
    await arrange_group(deps)  # exactly one Sam so far
    deps.llm = FakeLLM([DINNER_WITH_SAM])
    actions = await mention(deps, "I paid 40 for dinner, split with Sam")
    await execute(actions, FakeTelegramClient(), deps.session_factory)
    async with deps.session_factory() as session:
        pending = (await session.execute(select(PendingIntent))).scalar_one()
        pending_id, proposal_mid = pending.id, pending.message_id
        before_expiry = pending.expires_at
    # the second Sam appears between propose and confirm
    await dispatch(message_update(update_id=8, text="hi", from_user=OTHER_SAM, message_id=9), deps)

    ack, edit = await dispatch(
        callback_update(data=f"v1:confirm:{pending_id}", from_user=ALICE, message_id=proposal_mid),
        deps,
    )

    assert edit.kind == "edit_message" and edit.message_id == proposal_mid
    async with deps.session_factory() as session:
        pending = (await session.execute(select(PendingIntent))).scalar_one()  # NOT consumed
        sam_a = await user_id_of(session, SAM)
        sam_b = await user_id_of(session, OTHER_SAM)
        assert (await session.execute(select(func.count()).select_from(Expense))).scalar() == 0
        assert pending.expires_at > before_expiry  # the stage change restarts the clock
    assert keyboard_buttons(edit.reply_markup) == [
        ("Sam (@sam)", f"v1:pick:{pending_id}:{sam_a}"),
        ("Sam (@sam_the_second)", f"v1:pick:{pending_id}:{sam_b}"),
        ("✖ Cancel", f"v1:cancel:{pending_id}"),
    ]


async def test_a_pick_tap_pins_the_slot_and_confirm_takes_the_picked_member(deps):
    """Acceptance #3 (issue #14): the tap pins the slot, the proposal re-renders
    with Confirm now that everything is pinned, and the commit uses the choice."""
    pending_id, proposal_mid = await deliver_ambiguous_dinner(deps)
    async with deps.session_factory() as session:
        sam_b = await user_id_of(session, OTHER_SAM)
        alice = await user_id_of(session, ALICE)
        before_expiry = (await session.execute(select(PendingIntent))).scalar_one().expires_at

    actions = await dispatch(
        callback_update(
            data=f"v1:pick:{pending_id}:{sam_b}", from_user=ALICE, message_id=proposal_mid
        ),
        deps,
    )

    ack, edit = actions
    assert ack.kind == "answer_callback_query"
    assert edit.kind == "edit_message" and edit.message_id == proposal_mid
    assert edit.text.count("SGD 20.00") == 2  # WYSIWYG shares render once pinned (§7.1)
    labels = [b[0] for b in keyboard_buttons(edit.reply_markup)]
    assert labels == ["✅ Confirm", "✖ Cancel"]  # every slot pinned -> Confirm appears
    async with deps.session_factory() as session:
        pending = (await session.execute(select(PendingIntent))).scalar_one()
        refs = [p["user_ref"] for p in pending.intent_json["participants"]]
        assert f"id:{sam_b}" in refs  # the choice, pinned into the parked intent
        assert pending.expires_at > before_expiry  # a pick is active editing: TTL restarts

    await dispatch(
        callback_update(data=f"v1:confirm:{pending_id}", from_user=ALICE, message_id=proposal_mid),
        deps,
    )

    async with deps.session_factory() as session:
        (await session.execute(select(Expense))).scalar_one()
        split_user_ids = {
            s.user_id for s in (await session.execute(select(ExpenseSplit))).scalars()
        }
        assert split_user_ids == {alice, sam_b}  # the picked Sam, not the other one


async def test_a_pick_tap_that_unmasks_an_unknown_ref_fails_gracefully(deps):
    """Review finding (slice 13): "split with Sam and Carol" (two Sams, Carol
    unregistered) parks in the pick stage because ambiguity surfaces before the
    unknown-ref check. The tap that pins Sam then unmasks Carol — the answer
    must be a graceful burial, never a crash into a Telegram retry loop."""
    await arrange_two_sams(deps)
    deps.llm = FakeLLM(
        [
            WireAddExpense(
                payer_ref="me",
                amount="40",
                description="dinner",
                participants=[WireSplitMember(user_ref="Sam"), WireSplitMember(user_ref="Carol")],
            )
        ]
    )
    actions = await mention(deps, "I paid 40 for dinner, split with Sam and Carol")
    await execute(actions, FakeTelegramClient(), deps.session_factory)
    async with deps.session_factory() as session:
        pending = (await session.execute(select(PendingIntent))).scalar_one()
        pending_id, proposal_mid = pending.id, pending.message_id
        sam_a = await user_id_of(session, SAM)

    ack, edit = await dispatch(
        callback_update(
            data=f"v1:pick:{pending_id}:{sam_a}", from_user=ALICE, message_id=proposal_mid
        ),
        deps,
    )

    assert ack.kind == "answer_callback_query" and ack.text == "Nothing was recorded."
    assert "nothing was recorded" in edit.text.lower() and "Carol" in edit.text
    assert edit.reply_markup is None  # a dead proposal keeps no buttons
    async with deps.session_factory() as session:
        assert (await session.get(PendingIntent, pending_id)) is None  # consumed
        assert (await session.execute(select(func.count()).select_from(Expense))).scalar() == 0
