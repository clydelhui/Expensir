"""Reply-to-correct loop on live proposals (§10.2) — slice 13, issue #14.

Dispatch-seam tests like test_nl.py: a fake LLMClient returns canned wire
results; the real refine prompt is covered by recorded fixtures (§16)."""

from datetime import timedelta

from sqlalchemy import select

from expensir.core.handler import dispatch
from expensir.db.models import Expense, PendingIntent, Settlement, utcnow
from expensir.llm.wire import (
    WireAddExpense,
    WireSettleUp,
    WireSetup,
    WireShowBalance,
    WireSplitMember,
    WireUndoRedo,
    WireUnknown,
)
from expensir.transports.executor import execute
from tests.factories import callback_update, message_update
from tests.fakes import FakeLLM, UnavailableLLM
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

DINNER_FOR_45 = WireAddExpense(
    payer_ref="me",
    amount="45",
    currency=None,
    description="dinner",
    split_type="equal",
    participants=[WireSplitMember(user_ref="me"), WireSplitMember(user_ref="Sam")],
)


async def deliver_dinner_proposal(deps) -> tuple[int, int]:
    """Propose the 40-SGD dinner and deliver it: the executor backfills the
    proposal message id the reply router needs. Returns (pending_id, message_id)."""
    await arrange_group(deps)
    deps.llm = FakeLLM([DINNER_WITH_SAM])
    actions = await mention(deps, "I paid 40 for dinner, split with Sam")
    await execute(actions, FakeTelegramClient(), deps.session_factory)
    async with deps.session_factory() as session:
        pending = (await session.execute(select(PendingIntent))).scalar_one()
        assert pending.message_id is not None
        return pending.id, pending.message_id


async def reply_to(deps, message_id: int, text: str, *, from_user=None, update_id: int = 9):
    return await dispatch(
        message_update(
            update_id=update_id,
            text=text,
            from_user=from_user or ALICE,
            message_id=30 + update_id,
            reply_to_message_id=message_id,
        ),
        deps,
    )


async def test_a_reply_corrects_a_live_proposal_in_place_and_confirm_commits_it(deps):
    """Acceptance #1 (issue #14): "make it 45" updates the proposal in place;
    the eventual Confirm commits the corrected values."""
    pending_id, proposal_mid = await deliver_dinner_proposal(deps)
    fake = FakeLLM([], refinements=[DINNER_FOR_45])
    deps.llm = fake

    actions = await reply_to(deps, proposal_mid, "make it 45")

    (edit,) = actions
    assert edit.kind == "edit_message"
    assert edit.message_id == proposal_mid  # edited in place, not resent (§10.2)
    assert "SGD 45.00" in edit.text
    assert edit.text.count("SGD 22.50") == 2  # shares re-previewed WYSIWYG (§7.1)
    assert "reply to correct" in edit.text  # still a proposal, still correctable
    labels = keyboard_buttons(edit.reply_markup)
    assert labels == [
        ("✅ Confirm", f"v1:confirm:{pending_id}"),
        ("✖ Cancel", f"v1:cancel:{pending_id}"),
    ]
    # the refine seam saw the correction verbatim; nothing committed yet
    [(_, correction, _)] = fake.refined
    assert correction == "make it 45"
    async with deps.session_factory() as session:
        assert (await session.execute(select(Expense))).scalar_one_or_none() is None

    await dispatch(
        callback_update(data=f"v1:confirm:{pending_id}", from_user=ALICE, message_id=proposal_mid),
        deps,
    )

    async with deps.session_factory() as session:
        expense = (await session.execute(select(Expense))).scalar_one()
        assert expense.amount_minor == 4500  # the corrected amount, not the proposed one
        assert expense.currency == "SGD"


async def test_a_read_reply_answers_inline_and_leaves_the_proposal_untouched(deps):
    """Grilled (issue #14): "what does Sam owe?" mid-decision is a legitimate
    read — it runs, but it is not a correction, so nothing about the proposal moves."""
    _, proposal_mid = await deliver_dinner_proposal(deps)
    async with deps.session_factory() as session:
        before = (await session.execute(select(PendingIntent))).scalar_one()
        before_intent, before_expiry = before.intent_json, before.expires_at
    deps.llm = FakeLLM([], refinements=[WireShowBalance(scope="group")])

    actions = await reply_to(deps, proposal_mid, "wait, what are the balances?")

    (send,) = actions
    assert send.kind == "send_message"  # a fresh reply, NOT an edit of the proposal
    assert "owes" in send.text or "settled" in send.text.lower()
    async with deps.session_factory() as session:
        pending = (await session.execute(select(PendingIntent))).scalar_one()
        assert pending.intent_json == before_intent  # proposal untouched
        assert pending.expires_at == before_expiry  # a read is not active editing: no refresh


async def test_an_unreadable_correction_guides_and_leaves_the_proposal_standing(deps):
    _, proposal_mid = await deliver_dinner_proposal(deps)
    async with deps.session_factory() as session:
        before = (await session.execute(select(PendingIntent))).scalar_one()
        before_intent, before_expiry = before.intent_json, before.expires_at
    deps.llm = FakeLLM([], refinements=[WireUnknown(reason="not a correction")])

    actions = await reply_to(deps, proposal_mid, "asdfgh")

    (send,) = actions
    assert send.kind == "send_message"
    # correction-specific guidance, not the generic "rephrase" text: the
    # proposal is still standing and still correctable
    assert "correction" in send.text.lower()
    assert "unchanged" in send.text.lower() or "still" in send.text.lower()
    async with deps.session_factory() as session:
        pending = (await session.execute(select(PendingIntent))).scalar_one()
        assert pending.intent_json == before_intent
        assert pending.expires_at == before_expiry


async def test_an_llm_outage_during_a_correction_says_so_and_touches_nothing(deps):
    """A transport failure is NOT the user's sentence (issue #13 grill): no
    rephrase-guidance, and the proposal stays exactly as it was."""
    _, proposal_mid = await deliver_dinner_proposal(deps)
    async with deps.session_factory() as session:
        before_expiry = (await session.execute(select(PendingIntent))).scalar_one().expires_at
    deps.llm = UnavailableLLM()

    actions = await reply_to(deps, proposal_mid, "make it 45")

    (send,) = actions
    assert send.kind == "send_message"
    assert "couldn't reach" in send.text
    async with deps.session_factory() as session:
        pending = (await session.execute(select(PendingIntent))).scalar_one()
        assert pending.expires_at == before_expiry


async def test_a_correction_may_turn_the_expense_into_a_settlement(deps):
    """Grilled (issue #14): a correction may change the intent kind, mutation to
    mutation — "I was paying Sam back, not splitting" is a natural fix."""
    pending_id, proposal_mid = await deliver_dinner_proposal(deps)
    deps.llm = FakeLLM([], refinements=[WireSettleUp(from_ref="me", to_ref="Sam", amount="40")])

    actions = await reply_to(deps, proposal_mid, "actually I was paying Sam back, not splitting")

    (edit,) = actions
    assert edit.message_id == proposal_mid
    assert "🤝" in edit.text and "Sam" in edit.text  # re-rendered as a settlement proposal
    assert "reply to correct" in edit.text

    await dispatch(
        callback_update(data=f"v1:confirm:{pending_id}", from_user=ALICE, message_id=proposal_mid),
        deps,
    )

    async with deps.session_factory() as session:
        settlement = (await session.execute(select(Settlement))).scalar_one()
        assert settlement.amount_minor == 4000
        assert (await session.execute(select(Expense))).scalar_one_or_none() is None


async def test_me_in_a_correction_means_the_corrections_author_not_the_proposer(deps):
    """Grilled decision (issue #14): Sam replying "actually I paid" makes Sam
    the payer — a first-person ref anchors to whoever introduced it, and a
    later confirm tap by anyone never re-anchors it."""
    pending_id, proposal_mid = await deliver_dinner_proposal(deps)  # Alice proposed
    fake = FakeLLM(
        [],
        refinements=[
            WireAddExpense(
                payer_ref="me",  # Sam's "me", introduced by Sam's correction
                amount="40",
                description="dinner",
                split_type="equal",
                participants=[WireSplitMember(user_ref="me"), WireSplitMember(user_ref="Alice")],
            )
        ],
    )
    deps.llm = fake

    actions = await reply_to(deps, proposal_mid, "actually I paid, split with Alice", from_user=SAM)

    (edit,) = actions
    assert "paid by Sam" in edit.text
    # the prior intent the LLM saw carries Alice PINNED, not a floating "me" the
    # model could echo back re-anchored (the park-time half of the same decision)
    async with deps.session_factory() as session:
        alice_id = await user_id_of(session, ALICE)
        sam_id = await user_id_of(session, SAM)
    [(prior, _, _)] = fake.refined
    assert prior["payer_ref"] == f"id:{alice_id}"

    # Alice confirms — and the payer is still Sam: confirm never re-anchors
    await dispatch(
        callback_update(data=f"v1:confirm:{pending_id}", from_user=ALICE, message_id=proposal_mid),
        deps,
    )

    async with deps.session_factory() as session:
        expense = (await session.execute(select(Expense))).scalar_one()
        assert expense.payer_id == sam_id


async def test_a_correction_naming_an_unknown_member_guides_and_keeps_the_proposal(deps):
    """Unknown refs reject at the input edge (§11) — but a bad correction must
    not kill a good proposal: it stands unchanged, TTL untouched."""
    _, proposal_mid = await deliver_dinner_proposal(deps)
    async with deps.session_factory() as session:
        before = (await session.execute(select(PendingIntent))).scalar_one()
        before_intent, before_expiry = before.intent_json, before.expires_at
    deps.llm = FakeLLM(
        [],
        refinements=[
            WireAddExpense(
                payer_ref="me",
                amount="40",
                description="dinner",
                participants=[WireSplitMember(user_ref="me"), WireSplitMember(user_ref="Carol")],
            )
        ],
    )

    actions = await reply_to(deps, proposal_mid, "split it with Carol instead")

    (send,) = actions
    assert send.kind == "send_message"  # guidance, not an edit
    assert "Carol" in send.text and "/setup" in send.text
    async with deps.session_factory() as session:
        pending = (await session.execute(select(PendingIntent))).scalar_one()
        assert pending.intent_json == before_intent  # the good proposal survived
        assert pending.expires_at == before_expiry


COFFEE_WITH_SAM = WireAddExpense(
    payer_ref="me",
    amount="10",
    currency=None,
    description="coffee",
    split_type="equal",
    participants=[WireSplitMember(user_ref="me"), WireSplitMember(user_ref="Sam")],
)


async def test_a_reply_to_a_dead_proposal_is_a_fresh_intent_no_mention_needed(deps):
    """Acceptance #5 (issue #14): after the first interaction, replying to the
    bot IS addressing it — a confirmed result, the board, any bot message."""
    pending_id, proposal_mid = await deliver_dinner_proposal(deps)
    await dispatch(
        callback_update(data=f"v1:confirm:{pending_id}", from_user=ALICE, message_id=proposal_mid),
        deps,
    )  # the proposal is dead: confirmed and consumed
    fake = FakeLLM([COFFEE_WITH_SAM])
    deps.llm = fake

    actions = await reply_to(deps, proposal_mid, "I paid 10 for coffee, split with Sam")

    (send,) = actions
    assert send.kind == "send_message"
    assert "coffee" in send.text and "reply to correct" in send.text  # a FRESH proposal
    assert fake.seen == ["I paid 10 for coffee, split with Sam"]  # extract, not refine
    assert fake.refined == []
    async with deps.session_factory() as session:
        fresh = (await session.execute(select(PendingIntent))).scalar_one()
        assert fresh.intent_json["amount_minor"] == 1000  # the coffee, not the dinner
        assert fresh.message_id is None  # a brand-new proposal awaiting its send
        assert send.records_message_for_pending_id == fresh.id


async def test_a_reply_to_a_humans_message_without_a_mention_stays_ignored(deps):
    """The mention tax only lifts for replies to the BOT's messages: quoting a
    person while chatting about money must not summon proposals."""
    await arrange_group(deps)
    deps.llm = FakeLLM([COFFEE_WITH_SAM])

    actions = await dispatch(
        message_update(
            update_id=9,
            text="I paid 10 for coffee, split with Sam",
            from_user=ALICE,
            message_id=40,
            reply_to_message_id=5,  # Sam's "hi", a human message
            reply_to_from=SAM,
        ),
        deps,
    )

    assert actions == []
    async with deps.session_factory() as session:
        assert (await session.execute(select(PendingIntent))).scalar_one_or_none() is None


async def test_a_reply_to_an_expired_proposal_buries_it_and_starts_fresh(deps):
    """Acceptance #2 (issue #14): no resend dead-end — the same reply that
    discovers expiry marks the proposal Expired AND becomes a fresh intent."""
    _, proposal_mid = await deliver_dinner_proposal(deps)
    async with deps.session_factory() as session, session.begin():
        pending = (await session.execute(select(PendingIntent))).scalar_one()
        pending.expires_at = utcnow() - timedelta(minutes=1)  # expiry computed on read (§10)
    fake = FakeLLM([COFFEE_WITH_SAM])
    deps.llm = fake

    actions = await reply_to(deps, proposal_mid, "I paid 10 for coffee, split with Sam")

    edit, send = actions
    assert edit.kind == "edit_message"
    assert edit.message_id == proposal_mid
    assert "Expired" in edit.text
    assert edit.reply_markup is None  # a dead proposal keeps no buttons
    assert send.kind == "send_message"
    assert "coffee" in send.text and "reply to correct" in send.text  # the fresh proposal
    assert fake.refined == []  # never treated as a correction to the dead one
    async with deps.session_factory() as session:
        fresh = (await session.execute(select(PendingIntent))).scalar_one()  # old row consumed
        assert fresh.intent_json["amount_minor"] == 1000


async def test_a_successful_refine_refreshes_the_proposal_ttl(deps):
    """Active editing keeps the proposal live (§10.2): expires_at moves forward."""
    _, proposal_mid = await deliver_dinner_proposal(deps)
    async with deps.session_factory() as session:
        before = (await session.execute(select(PendingIntent))).scalar_one().expires_at
    deps.llm = FakeLLM([], refinements=[DINNER_FOR_45])

    await reply_to(deps, proposal_mid, "make it 45")

    async with deps.session_factory() as session:
        pending = (await session.execute(select(PendingIntent))).scalar_one()
        assert pending.expires_at > before
        assert pending.intent_json["amount_minor"] == 4500  # the correction stuck


async def test_an_undo_reply_points_at_the_button_and_leaves_the_proposal(deps):
    """Review finding (slice 13): the refine prompt teaches undo_redo, so the
    model may emit it — detected, never honored (§9), and never a correction."""
    pending_id, proposal_mid = await deliver_dinner_proposal(deps)
    deps.llm = FakeLLM([], refinements=[WireUndoRedo()])

    actions = await reply_to(deps, proposal_mid, "undo that")

    (send,) = actions
    assert send.kind == "send_message"
    assert "↩️ Undo button" in send.text  # the templated pointer, not a crash
    async with deps.session_factory() as session:
        pending = await session.get(PendingIntent, pending_id)
        assert pending is not None  # the proposal stands exactly as it was
        assert pending.intent_json["amount_minor"] == 4000


async def test_a_setup_reply_without_targets_guides_and_leaves_the_proposal(deps):
    """Review finding (slice 13): a correction reading as setup has no account
    ids to work with (the reply target is the BOT's message) — guidance, and
    the proposal stands."""
    pending_id, proposal_mid = await deliver_dinner_proposal(deps)
    deps.llm = FakeLLM([], refinements=[WireSetup()])

    actions = await reply_to(deps, proposal_mid, "register Carol please")

    (send,) = actions
    assert send.kind == "send_message"
    assert "register" in send.text.lower()  # the setup guidance
    async with deps.session_factory() as session:
        assert (await session.get(PendingIntent, pending_id)) is not None
