"""Vision path (issue #15): a receipt photo enters the same proposal loop.

Dispatch-seam tests like test_nl.py: a fake LLMClient returns canned wire
results and a fake FileSource stands in for Telegram's getFile — the real
client's request/parse shape is covered by recorded fixtures (§16)."""

from datetime import timedelta

from sqlalchemy import func, select

from expensir.core.handler import dispatch
from expensir.db.models import Expense, PendingIntent
from expensir.llm.wire import WireAddExpense, WireSettleUp, WireShowBalance, WireUnknown
from tests.factories import callback_update, message_update, photo_update
from tests.fakes import FakeFiles, FakeLLM, UnavailableLLM
from tests.test_nl import ALICE, SAM, arrange_group, keyboard_buttons, user_id_of

RAMEN_RECEIPT = WireAddExpense(
    payer_ref="me",
    amount="34.50",
    currency=None,
    description="Ichiran Ramen",
    split_type="equal",
    participants=[],
)


async def test_a_captioned_receipt_photo_proposes_an_expense(deps):
    await arrange_group(deps)
    deps.llm = FakeLLM([], visions=[RAMEN_RECEIPT])
    deps.files = FakeFiles(b"receipt-jpeg")

    actions = await dispatch(
        photo_update(update_id=3, caption="@expensir_bot", from_user=ALICE, message_id=10),
        deps,
    )

    (send,) = actions
    assert send.kind == "send_message"
    # the same proposal rendering as the NL text door (§10): pinned ledger,
    # resolved currency, nothing committed until Confirm (§0.7)
    assert send.text.startswith("📒 Japan Trip • ")
    assert "SGD 34.50" in send.text
    assert "Ichiran Ramen" in send.text
    assert "reply to correct" in send.text
    buttons = [data for _, data in keyboard_buttons(send.reply_markup)]
    assert any(data.startswith("v1:confirm:") for data in buttons)
    assert any(data.startswith("v1:cancel:") for data in buttons)

    # the LARGEST PhotoSize was downloaded and handed to the model with the
    # mention-stripped caption
    assert deps.files.requested == ["photo-big-1"]
    assert deps.llm.seen_images == [(b"receipt-jpeg", "")]

    async with deps.session_factory() as session:
        pending = (await session.execute(select(PendingIntent))).scalar_one()
        assert pending.intent_json["kind"] == "add_expense"
        assert (await session.execute(select(func.count()).select_from(Expense))).scalar() == 0


async def test_a_photo_without_mention_or_reply_is_ignored(deps):
    """Privacy invocation rules (§13): even when a transport delivers every
    photo (poll mode, admin bot), an unaddressed one is invisible — nothing
    downloaded, no model call, no reply."""
    await arrange_group(deps)
    deps.llm = FakeLLM([], visions=[RAMEN_RECEIPT])
    deps.files = FakeFiles()

    bare = await dispatch(photo_update(update_id=3, from_user=ALICE), deps)
    chatty_caption = await dispatch(
        photo_update(update_id=4, caption="look at this receipt lol", from_user=ALICE), deps
    )

    assert bare == [] and chatty_caption == []
    assert deps.files.requested == []
    assert deps.llm.seen_images == []


async def test_an_uncaptioned_photo_replying_to_a_bot_message_proposes(deps):
    """The reply door (§6): replying to any non-pending bot message — an old
    result, the board — is an invocation, photo exactly like text (§10.5)."""
    await arrange_group(deps)
    deps.llm = FakeLLM([], visions=[RAMEN_RECEIPT])
    deps.files = FakeFiles(b"receipt-jpeg")

    actions = await dispatch(
        photo_update(update_id=3, from_user=ALICE, reply_to_message_id=777),
        deps,
    )

    (send,) = actions
    assert send.kind == "send_message"
    assert "Ichiran Ramen" in send.text
    assert deps.llm.seen_images == [(b"receipt-jpeg", "")]  # no caption -> empty steer
    assert deps.llm.seen == []  # the text extractor never saw a phantom ""


async def test_without_a_vision_model_photos_are_left_unanswered(deps):
    """LLM_VISION_MODEL unset -> the door is closed (issue #15 grill): addressed
    photos behave exactly like text mentions with no LLM configured at all."""
    await arrange_group(deps)
    deps.llm = FakeLLM([], visions=[RAMEN_RECEIPT])
    deps.llm.supports_vision = False
    deps.files = FakeFiles()

    mentioned = await dispatch(
        photo_update(update_id=3, caption="@expensir_bot", from_user=ALICE), deps
    )
    replied = await dispatch(
        photo_update(update_id=4, from_user=ALICE, reply_to_message_id=777), deps
    )

    assert mentioned == [] and replied == []
    assert deps.files.requested == []
    assert deps.llm.seen_images == []


async def test_a_vision_proposal_confirms_exactly_like_a_text_one(deps):
    """The identical loop (§0.7): any member may tap Confirm; the sender's "me"
    was pinned as payer at propose time and never re-anchors to the presser."""
    await arrange_group(deps)
    deps.llm = FakeLLM([], visions=[RAMEN_RECEIPT])
    deps.files = FakeFiles()

    await dispatch(photo_update(update_id=3, caption="@expensir_bot", from_user=ALICE), deps)
    async with deps.session_factory() as session:
        pending_id = (await session.execute(select(PendingIntent))).scalar_one().id

    ack, edit, *_ = await dispatch(
        callback_update(data=f"v1:confirm:{pending_id}", from_user=SAM, message_id=555), deps
    )

    assert "Ichiran Ramen" in edit.text
    async with deps.session_factory() as session:
        expense = (await session.execute(select(Expense))).scalar_one()
        assert expense.amount_minor == 3450 and expense.currency == "SGD"
        assert expense.payer_id == await user_id_of(session, ALICE)


async def test_a_text_reply_corrects_a_vision_proposal_in_place(deps):
    """Acceptance: correctable by reply exactly like NL text proposals (§10.2).
    The parked intent is door-agnostic, so the ordinary text refine applies."""
    await arrange_group(deps)
    corrected = RAMEN_RECEIPT.model_copy(update={"amount": "40"})
    deps.llm = FakeLLM([], visions=[RAMEN_RECEIPT], refinements=[corrected])
    deps.files = FakeFiles()

    await dispatch(photo_update(update_id=3, caption="@expensir_bot", from_user=ALICE), deps)
    async with deps.session_factory() as session:
        pending = (await session.execute(select(PendingIntent))).scalar_one()
        pending_id, proposal_message_id = pending.id, 900
        pending.message_id = proposal_message_id  # the executor's backfill
        await session.commit()

    (edit,) = await dispatch(
        message_update(
            update_id=4,
            text="the total was 40",
            from_user=ALICE,
            message_id=11,
            reply_to_message_id=proposal_message_id,
        ),
        deps,
    )

    assert edit.kind == "edit_message" and edit.message_id == proposal_message_id
    assert "SGD 40.00" in edit.text
    async with deps.session_factory() as session:
        pending = (await session.execute(select(PendingIntent))).scalar_one()
        assert pending.id == pending_id  # refined in place, not reproposed
        assert pending.intent_json["amount_minor"] == 4000


async def test_a_transfer_screenshot_proposes_a_settlement(deps):
    """Issue #15 grill: the vision door covers both money-moving kinds — a
    payment screenshot rides the same propose+confirm as a receipt."""
    await arrange_group(deps)
    deps.llm = FakeLLM([], visions=[WireSettleUp(from_ref="me", to_ref="Sam", amount="20")])
    deps.files = FakeFiles()

    (proposal,) = await dispatch(
        photo_update(update_id=3, caption="@expensir_bot paid Sam back", from_user=ALICE), deps
    )

    assert proposal.kind == "send_message"
    assert "SGD 20.00" in proposal.text and "reply to correct" in proposal.text
    async with deps.session_factory() as session:
        pending = (await session.execute(select(PendingIntent))).scalar_one()
        assert pending.intent_json["kind"] == "settle_up"


async def test_an_amountless_settle_up_from_a_photo_renders_the_settle_sheet(deps):
    """A blurry screenshot may read names but no figure: amountless settle_up
    is a READ (ADR-0007) and runs immediately, exactly as it does from text —
    to_intent never knows which door the wire came through (§0)."""
    await arrange_group(deps)
    deps.llm = FakeLLM([], visions=[WireSettleUp(from_ref="me", to_ref="Sam", amount=None)])
    deps.files = FakeFiles()

    (sheet,) = await dispatch(
        photo_update(update_id=3, caption="@expensir_bot", from_user=ALICE), deps
    )

    assert sheet.kind == "send_message"
    assert "Nothing to settle between Alice and Sam." in sheet.text
    async with deps.session_factory() as session:  # a read: no proposal parked
        assert (
            await session.execute(select(func.count()).select_from(PendingIntent))
        ).scalar() == 0


DINNER = WireAddExpense(
    payer_ref="me",
    amount="30",
    currency=None,
    description="dinner",
    split_type="equal",
    participants=[],
)


async def park_text_proposal(deps, wire, *, message_id: int = 900) -> int:
    """Propose via the text door and backfill the proposal's message id
    (normally the executor's job); returns the pending row's id."""
    (send,) = await dispatch(
        message_update(
            update_id=3, text="@expensir_bot dinner with Sam", from_user=ALICE, message_id=10
        ),
        deps,
    )
    assert send.kind == "send_message"
    async with deps.session_factory() as session:
        pending = (await session.execute(select(PendingIntent))).scalar_one()
        pending.message_id = message_id
        pending_id = pending.id
        await session.commit()
    return pending_id


async def test_a_photo_reply_to_a_live_proposal_is_a_vision_correction(deps):
    """Issue #15 grill: a photo correction MERGES — the model gets the parked
    intent AND the image, so what the receipt shows updates the proposal and
    what it doesn't show (the thread's participants) survives."""
    await arrange_group(deps)
    dinner = DINNER
    from_receipt = dinner.model_copy(update={"amount": "34.50", "description": "Ichiran Ramen"})
    deps.llm = FakeLLM([dinner], refinements=[from_receipt])
    deps.files = FakeFiles(b"receipt-jpeg")
    pending_id = await park_text_proposal(deps, dinner)

    (edit,) = await dispatch(
        photo_update(update_id=4, from_user=ALICE, message_id=11, reply_to_message_id=900),
        deps,
    )

    assert edit.kind == "edit_message" and edit.message_id == 900
    assert "SGD 34.50" in edit.text and "Ichiran Ramen" in edit.text
    # the model saw the PRIOR intent alongside the image: merge, never restart
    assert deps.files.requested == ["photo-big-1"]
    assert deps.llm.refine_images == [b"receipt-jpeg"]
    ((prior, correction, _),) = deps.llm.refined
    assert prior["kind"] == "add_expense"
    assert correction == ""  # uncaptioned: the image is the whole correction
    async with deps.session_factory() as session:
        pending = (await session.execute(select(PendingIntent))).scalar_one()
        assert pending.id == pending_id  # corrected in place, still live


async def test_a_photo_reply_to_an_expired_proposal_starts_fresh(deps):
    """No resend dead-end (§10.4): the reply that discovers expiry buries the
    proposal AND is processed as a fresh intent — through the VISION door."""
    await arrange_group(deps)
    deps.llm = FakeLLM([DINNER], visions=[RAMEN_RECEIPT])
    deps.files = FakeFiles(b"receipt-jpeg")
    await park_text_proposal(deps, DINNER)
    async with deps.session_factory() as session:
        pending = (await session.execute(select(PendingIntent))).scalar_one()
        pending.expires_at = pending.created_at - timedelta(seconds=1)
        await session.commit()

    buried, proposal = await dispatch(
        photo_update(update_id=4, from_user=ALICE, message_id=11, reply_to_message_id=900),
        deps,
    )

    assert buried.kind == "edit_message" and "Expired" in buried.text
    assert proposal.kind == "send_message" and "Ichiran Ramen" in proposal.text
    assert deps.llm.seen_images == [(b"receipt-jpeg", "")]  # the vision door, not text


async def test_an_unreadable_receipt_gets_receipt_guidance_not_rephrase(deps):
    """The text door's "try rephrasing" makes no sense for a photo: guidance
    points at telling the bot the amount instead (issue #15 grill)."""
    await arrange_group(deps)
    deps.llm = FakeLLM([], visions=[WireUnknown(reason="not a receipt")])
    deps.files = FakeFiles()

    (guidance,) = await dispatch(
        photo_update(update_id=3, caption="@expensir_bot", from_user=ALICE), deps
    )

    assert "couldn't read that receipt" in guidance.text
    assert "rephras" not in guidance.text
    async with deps.session_factory() as session:  # nothing parked, nothing committed
        assert (
            await session.execute(select(func.count()).select_from(PendingIntent))
        ).scalar() == 0


async def test_vision_transport_failure_says_so_instead_of_blaming_the_photo(deps):
    """LLMUnavailable means the receipt was never read (§12): don't imply the
    user's photo was at fault."""
    await arrange_group(deps)
    deps.llm = UnavailableLLM()
    deps.files = FakeFiles()

    (reply,) = await dispatch(
        photo_update(update_id=3, caption="@expensir_bot", from_user=ALICE), deps
    )

    assert "couldn't reach my vision model" in reply.text


async def test_a_failed_photo_download_replies_transiently(deps):
    """getFile trouble is Telegram-side: ask to resend, never call the model."""
    await arrange_group(deps)
    deps.llm = FakeLLM([], visions=[RAMEN_RECEIPT])
    deps.files = FakeFiles(None)

    (reply,) = await dispatch(
        photo_update(update_id=3, caption="@expensir_bot", from_user=ALICE), deps
    )

    assert "couldn't fetch that photo" in reply.text
    assert deps.llm.seen_images == []


async def test_a_failed_download_during_a_correction_leaves_the_proposal_alone(deps):
    await arrange_group(deps)
    deps.llm = FakeLLM([DINNER])
    deps.files = FakeFiles(None)
    await park_text_proposal(deps, DINNER)

    (reply,) = await dispatch(
        photo_update(update_id=4, from_user=ALICE, message_id=11, reply_to_message_id=900),
        deps,
    )

    assert reply.kind == "send_message" and "couldn't fetch that photo" in reply.text
    assert "nothing was changed" in reply.text
    assert deps.llm.refined == []  # the model was never asked
    async with deps.session_factory() as session:  # still live, still SGD 30
        pending = (await session.execute(select(PendingIntent))).scalar_one()
        assert pending.intent_json["amount_minor"] == 3000


async def test_a_caption_slash_command_runs_the_command_not_vision(deps):
    """Review fix: a slash command typed as a caption is deterministic input —
    it runs through the command path; the photo is never read."""
    await arrange_group(deps)
    deps.llm = FakeLLM([], visions=[RAMEN_RECEIPT])
    deps.files = FakeFiles()

    reply, *_ = await dispatch(
        photo_update(update_id=3, caption="/homecurrency USD", from_user=ALICE), deps
    )

    assert reply.kind == "send_message" and "USD" in reply.text
    assert deps.files.requested == []
    assert deps.llm.seen_images == []


async def test_vision_never_routes_a_kind_the_photo_door_forbids(deps):
    """Review fix (§12, §0): the add_expense/settle_up/unknown restriction is
    enforced app-side — a misbehaving model can't widen its own door."""
    await arrange_group(deps)
    deps.llm = FakeLLM([], visions=[WireShowBalance(scope="me")])
    deps.files = FakeFiles()

    (guidance,) = await dispatch(
        photo_update(update_id=3, caption="@expensir_bot", from_user=ALICE), deps
    )

    assert "couldn't read that receipt" in guidance.text
    async with deps.session_factory() as session:  # no read ran, nothing parked
        assert (
            await session.execute(select(func.count()).select_from(PendingIntent))
        ).scalar() == 0


async def test_a_photo_reply_still_buries_an_expired_proposal_when_vision_is_off(deps):
    """Review fix: burial is state hygiene, not an answer — the deliberate
    'photos unanswered when unconfigured' rule must not skip it."""
    await arrange_group(deps)
    deps.llm = FakeLLM([DINNER])
    deps.llm.supports_vision = False
    deps.files = FakeFiles()
    await park_text_proposal(deps, DINNER)
    async with deps.session_factory() as session:
        pending = (await session.execute(select(PendingIntent))).scalar_one()
        pending.expires_at = pending.created_at - timedelta(seconds=1)
        await session.commit()

    (buried,) = await dispatch(
        photo_update(update_id=4, from_user=ALICE, message_id=11, reply_to_message_id=900),
        deps,
    )

    assert buried.kind == "edit_message" and "Expired" in buried.text
    assert deps.files.requested == []  # buried without ever fetching the photo
    async with deps.session_factory() as session:
        assert (
            await session.execute(select(func.count()).select_from(PendingIntent))
        ).scalar() == 0


async def test_only_the_captioned_item_of_an_album_corrects(deps):
    """Review fix: an album fires one update per photo — reading every item
    would refine N times for one user action. Caption-less strays are invisible."""
    await arrange_group(deps)
    corrected = DINNER.model_copy(update={"amount": "34.50"})
    deps.llm = FakeLLM([DINNER], refinements=[corrected])
    deps.files = FakeFiles(b"receipt-jpeg")
    await park_text_proposal(deps, DINNER)

    stray = await dispatch(
        photo_update(
            update_id=4,
            from_user=ALICE,
            message_id=11,
            reply_to_message_id=900,
            media_group_id="album-1",
        ),
        deps,
    )
    (edit,) = await dispatch(
        photo_update(
            update_id=5,
            from_user=ALICE,
            message_id=12,
            reply_to_message_id=900,
            media_group_id="album-1",
            caption="the receipt",
        ),
        deps,
    )

    assert stray == []
    assert edit.kind == "edit_message"
    assert deps.files.requested == ["photo-big-1"]  # one download, one refine
    assert deps.llm.refine_images == [b"receipt-jpeg"]


async def test_a_malformed_photo_array_replies_transiently_instead_of_crashing(deps):
    """Review fix: the webhook validates only the secret header, so 'photo': []
    (or sizes without file_id) must not escape as a ValueError/KeyError."""
    await arrange_group(deps)
    deps.llm = FakeLLM([], visions=[RAMEN_RECEIPT])
    deps.files = FakeFiles()

    empty = await dispatch(
        photo_update(update_id=3, caption="@expensir_bot", from_user=ALICE, sizes=[]), deps
    )
    no_file_id = await dispatch(
        photo_update(
            update_id=4,
            caption="@expensir_bot",
            from_user=ALICE,
            sizes=[{"width": 90, "height": 120}],
        ),
        deps,
    )

    for (reply,) in (empty, no_file_id):
        assert "couldn't fetch that photo" in reply.text
    assert deps.files.requested == []
    assert deps.llm.seen_images == []
