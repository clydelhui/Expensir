"""Settling (§7.3, issue #10): board [Settle] taps (WYSIWYG, ADR-0006) and the
ungated custom /settle (ADR-0002). One currency, one direction, one row, one action."""

from sqlalchemy import select

from expensir.core.handler import dispatch
from expensir.db.models import Action, Ledger, Settlement, User
from tests.factories import bot_added_update, callback_update, message_update, user
from tests.test_board import FakeBoardMessenger, board_edits

ALICE = user(1001, "Alice", "alice")
BOB = user(1002, "Bob", "bob")
CAROL = user(1003, "Carol", "carol")


async def setup_group(deps, chat_id: int = -42) -> None:
    await dispatch(bot_added_update(chat_id=chat_id, by=ALICE), deps)
    await dispatch(message_update(update_id=90, chat_id=chat_id, text="/homecurrency EUR"), deps)
    await dispatch(message_update(update_id=91, chat_id=chat_id, text="hi", from_user=BOB), deps)


async def bob_owes_alice_30(deps) -> None:
    await dispatch(
        message_update(update_id=92, chat_id=-42, text="/equal 60 dinner @alice @bob"), deps
    )


async def read_settlements(deps) -> list[Settlement]:
    async with deps.session_factory() as session:
        return list((await session.execute(select(Settlement).order_by(Settlement.id))).scalars())


async def read_actions(deps, kind: str) -> list[Action]:
    async with deps.session_factory() as session:
        return list(
            (
                await session.execute(select(Action).where(Action.kind == kind).order_by(Action.id))
            ).scalars()
        )


def replies_of(outbound) -> list:
    return [a for a in outbound if a.kind == "send_message"]


async def test_settle_records_the_stated_payment_and_settles_the_board(deps):
    deps.client = FakeBoardMessenger()
    await setup_group(deps)
    await bob_owes_alice_30(deps)

    outbound = await dispatch(
        message_update(update_id=93, chat_id=-42, text="/settle @alice 30 EUR", from_user=BOB),
        deps,
    )

    [settlement] = await read_settlements(deps)
    assert settlement.amount_minor == 3000
    assert settlement.currency == "EUR"
    assert settlement.deleted_at is None
    [action] = await read_actions(deps, "settle_up")
    assert settlement.created_by_action_id == action.id

    [reply] = replies_of(outbound)
    assert "Bob" in reply.text and "Alice" in reply.text and "EUR 30.00" in reply.text
    assert reply.reply_markup == {
        "inline_keyboard": [[{"text": "↩️ Undo", "callback_data": f"v1:undo:{action.id}"}]]
    }
    assert reply.records_result_for_action_id == action.id
    [board_edit] = board_edits(outbound)
    assert "All settled up." in board_edit.text


async def test_a_third_member_may_record_an_overpaying_reverse_settlement(deps):
    """ADR-0002: any direction, any positive amount, recorded by anyone — the
    action rows audit who. Alice pays Bob 100 although BOB owes HER; Carol records."""
    deps.client = FakeBoardMessenger()
    await setup_group(deps)
    await dispatch(message_update(update_id=95, chat_id=-42, text="yo", from_user=CAROL), deps)
    await bob_owes_alice_30(deps)

    outbound = await dispatch(
        message_update(
            update_id=96, chat_id=-42, text="/settle @alice @bob 100 EUR", from_user=CAROL
        ),
        deps,
    )

    [settlement] = await read_settlements(deps)
    assert settlement.amount_minor == 10000
    [action] = await read_actions(deps, "settle_up")
    async with deps.session_factory() as session:
        carol = (
            await session.execute(select(User).where(User.display_name == "Carol"))
        ).scalar_one()
    assert action.actor_user_id == carol.id  # the recorder, not a party to the payment
    [reply] = replies_of(outbound)
    assert "Alice paid Bob EUR 100.00" in reply.text
    # the pool now owes Alice 130: her 30 from dinner plus the 100 overpayment
    [board_edit] = board_edits(outbound)
    assert board_edit.text == "📒 Japan Trip • Board\nBob → Alice EUR 130.00"


async def test_settling_with_yourself_is_refused(deps):
    """The one directional validation that survives ADR-0002: from ≠ to."""
    deps.client = FakeBoardMessenger()
    await setup_group(deps)
    await bob_owes_alice_30(deps)

    outbound = await dispatch(
        message_update(update_id=93, chat_id=-42, text="/settle @bob 30 EUR", from_user=BOB),
        deps,
    )

    assert await read_settlements(deps) == []
    assert await read_actions(deps, "settle_up") == []
    [reply] = replies_of(outbound)
    assert "yourself" in reply.text or "themselves" in reply.text
    assert reply.reply_markup is None  # nothing recorded -> nothing to undo
    assert board_edits(outbound) == []  # a rejection never touches the board


async def test_a_made_up_currency_code_is_refused(deps):
    """§7.3: a settlement freezes a currency forever, so the code must be real ISO 4217."""
    deps.client = FakeBoardMessenger()
    await setup_group(deps)
    await bob_owes_alice_30(deps)

    outbound = await dispatch(
        message_update(update_id=93, chat_id=-42, text="/settle @alice 30 ZZZ", from_user=BOB),
        deps,
    )

    assert await read_settlements(deps) == []
    [reply] = replies_of(outbound)
    assert "ZZZ" in reply.text
    assert reply.reply_markup is None


async def test_the_remaining_custom_settle_rejections(deps):
    """§7.3's short validation list: registered members, positive amount, an amount at all."""
    deps.client = FakeBoardMessenger()
    await setup_group(deps)
    await bob_owes_alice_30(deps)

    rejected = {
        "/settle @stranger 30 EUR": "don't know @stranger",
        "/settle @alice 0 EUR": "positive",  # to_minor refuses, like /equal 0
        "/settle @alice EUR": "Usage",  # no amount: the settle sheet, slice 10
        "/settle 30 EUR": "Usage",  # nobody named
    }
    for text, expected in rejected.items():
        outbound = await dispatch(
            message_update(update_id=93, chat_id=-42, text=text, from_user=BOB), deps
        )
        [reply] = replies_of(outbound)
        assert expected in reply.text, text
        assert reply.reply_markup is None, text
    assert await read_settlements(deps) == []


async def user_id_of(deps, display_name: str) -> int:
    async with deps.session_factory() as session:
        return (
            await session.execute(select(User.id).where(User.display_name == display_name))
        ).scalar_one()


async def test_each_board_line_carries_a_wysiwyg_settle_button(deps):
    """ADR-0006: the button encodes the tuple AND the shown amount as a staleness token."""
    deps.client = FakeBoardMessenger()
    await setup_group(deps)

    outbound = await dispatch(
        message_update(update_id=92, chat_id=-42, text="/equal 60 dinner @alice @bob"), deps
    )

    alice, bob = await user_id_of(deps, "Alice"), await user_id_of(deps, "Bob")
    [board_edit] = board_edits(outbound)
    assert board_edit.text == "📒 Japan Trip • Board\nBob → Alice EUR 30.00"
    assert board_edit.reply_markup == {
        "inline_keyboard": [
            [
                {
                    "text": "🤝 Settle Bob → Alice EUR 30.00",
                    "callback_data": f"v1:st:{bob}:{alice}:EUR:3000",
                }
            ]
        ]
    }


async def test_a_settled_board_has_no_buttons(deps):
    deps.client = FakeBoardMessenger()
    await setup_group(deps)
    await bob_owes_alice_30(deps)

    outbound = await dispatch(
        message_update(update_id=93, chat_id=-42, text="/settle @alice 30 EUR", from_user=BOB),
        deps,
    )

    [board_edit] = board_edits(outbound)
    assert "All settled up." in board_edit.text
    assert board_edit.reply_markup is None


async def test_a_fresh_settle_tap_records_exactly_the_shown_amount(deps):
    """ADR-0006 WYSIWYG: shown == current -> one settlement, one action with its
    own Undo, board re-rendered. Any member may tap — Carol settles Bob's debt."""
    deps.client = FakeBoardMessenger()
    await setup_group(deps)
    await bob_owes_alice_30(deps)
    alice, bob = await user_id_of(deps, "Alice"), await user_id_of(deps, "Bob")

    outbound = await dispatch(
        callback_update(
            update_id=94,
            chat_id=-42,
            data=f"v1:st:{bob}:{alice}:EUR:3000",
            message_id=901,  # the pinned board itself
            from_user=CAROL,
        ),
        deps,
    )

    [settlement] = await read_settlements(deps)
    assert (settlement.from_user, settlement.to_user) == (bob, alice)
    assert (settlement.amount_minor, settlement.currency) == (3000, "EUR")
    [action] = await read_actions(deps, "settle_up")
    assert action.actor_user_id == await user_id_of(deps, "Carol")

    [ack] = [a for a in outbound if a.kind == "answer_callback_query"]
    assert "Recorded" in (ack.text or "")
    [reply] = replies_of(outbound)
    assert "Bob paid Alice EUR 30.00" in reply.text
    assert reply.records_result_for_action_id == action.id
    assert reply.reply_markup == {
        "inline_keyboard": [[{"text": "↩️ Undo", "callback_data": f"v1:undo:{action.id}"}]]
    }
    [board_edit] = board_edits(outbound)
    assert "All settled up." in board_edit.text


async def test_a_stale_tap_records_nothing_warns_and_refreshes_the_board(deps):
    """ADR-0006: shown ≠ current — never record an amount the presser didn't see."""
    deps.client = FakeBoardMessenger()
    await setup_group(deps)
    await bob_owes_alice_30(deps)
    alice, bob = await user_id_of(deps, "Alice"), await user_id_of(deps, "Bob")
    # the debt moves on: Bob now owes 40, so a button showing 30.00 is stale
    await dispatch(
        message_update(update_id=93, chat_id=-42, text="/equal 20 taxi @alice @bob"), deps
    )

    outbound = await dispatch(
        callback_update(
            update_id=94, chat_id=-42, data=f"v1:st:{bob}:{alice}:EUR:3000", message_id=901
        ),
        deps,
    )

    assert await read_settlements(deps) == []
    [ack] = [a for a in outbound if a.kind == "answer_callback_query"]
    assert "out of date" in (ack.text or "")
    assert replies_of(outbound) == []  # nothing recorded -> no result message
    [board_edit] = board_edits(outbound)  # refreshed against the truth
    assert board_edit.text == "📒 Japan Trip • Board\nBob → Alice EUR 40.00"
    assert f"v1:st:{bob}:{alice}:EUR:4000" in str(board_edit.reply_markup)


async def test_a_tap_on_a_settled_away_line_answers_already_settled(deps):
    deps.client = FakeBoardMessenger()
    await setup_group(deps)
    await bob_owes_alice_30(deps)
    alice, bob = await user_id_of(deps, "Alice"), await user_id_of(deps, "Bob")
    await dispatch(
        message_update(update_id=93, chat_id=-42, text="/settle @alice 30 EUR", from_user=BOB),
        deps,
    )

    outbound = await dispatch(  # the line is gone; an old button races the truth
        callback_update(
            update_id=94, chat_id=-42, data=f"v1:st:{bob}:{alice}:EUR:3000", message_id=901
        ),
        deps,
    )

    [settlement] = await read_settlements(deps)  # still just the /settle one
    [ack] = [a for a in outbound if a.kind == "answer_callback_query"]
    assert ack.text == "Already settled."
    [board_edit] = board_edits(outbound)
    assert "All settled up." in board_edit.text


async def test_a_settle_tap_that_matches_no_board_records_nothing(deps):
    """A forged or orphaned callback: no board message, no settlement (§9's spirit)."""
    deps.client = FakeBoardMessenger()
    await setup_group(deps)
    await bob_owes_alice_30(deps)
    alice, bob = await user_id_of(deps, "Alice"), await user_id_of(deps, "Bob")

    outbound = await dispatch(
        callback_update(  # message 555 is not any ledger's board
            update_id=94, chat_id=-42, data=f"v1:st:{bob}:{alice}:EUR:3000", message_id=555
        ),
        deps,
    )

    assert await read_settlements(deps) == []
    [ack] = [a for a in outbound if a.kind == "answer_callback_query"]
    assert ack.text == "That button doesn't match anything I recorded."
    assert board_edits(outbound) == []


async def test_undoing_a_settled_against_expense_keeps_the_payment_and_warns_of_the_credit(deps):
    """§9: payments are immutable facts — the undo never resizes the settlement;
    the now-excess payment surfaces as a credit with a warning."""
    deps.client = FakeBoardMessenger()
    await setup_group(deps)
    await bob_owes_alice_30(deps)
    await dispatch(
        message_update(update_id=93, chat_id=-42, text="/settle @alice 30 EUR", from_user=BOB),
        deps,
    )
    [add_action] = await read_actions(deps, "add_expense")

    outbound = await dispatch(
        callback_update(update_id=94, chat_id=-42, data=f"v1:undo:{add_action.id}"), deps
    )

    [settlement] = await read_settlements(deps)
    assert settlement.deleted_at is None  # the payment stands (§9)
    [ack] = [a for a in outbound if a.kind == "answer_callback_query"]
    assert "Undone" in (ack.text or "")
    assert "overpays" in (ack.text or "")
    assert "Bob has a EUR 30.00 credit" in (ack.text or "")
    # the board shows the flipped direction: the pool now owes Bob
    [board_edit] = board_edits(outbound)
    assert board_edit.text == "📒 Japan Trip • Board\nAlice → Bob EUR 30.00"


async def test_undoing_an_expense_no_settlement_touched_warns_of_nothing(deps):
    """The credit warning is earned, not blanket: no settlement, no warning."""
    deps.client = FakeBoardMessenger()
    await setup_group(deps)
    await bob_owes_alice_30(deps)
    [add_action] = await read_actions(deps, "add_expense")

    outbound = await dispatch(
        callback_update(update_id=94, chat_id=-42, data=f"v1:undo:{add_action.id}"), deps
    )

    [ack] = [a for a in outbound if a.kind == "answer_callback_query"]
    assert ack.text == "↩️ Undone."


async def test_an_anonymous_admin_settle_tap_records_nothing(deps):
    """§11: no auditable actor, no mutation — same rule as undo presses."""
    deps.client = FakeBoardMessenger()
    await setup_group(deps)
    await bob_owes_alice_30(deps)
    alice, bob = await user_id_of(deps, "Alice"), await user_id_of(deps, "Bob")
    anonymous = {"id": 1087968824, "is_bot": True, "first_name": "Group", "username": "GroupBot"}

    outbound = await dispatch(
        callback_update(
            update_id=94,
            chat_id=-42,
            data=f"v1:st:{bob}:{alice}:EUR:3000",
            message_id=901,
            from_user=anonymous,
        ),
        deps,
    )

    assert await read_settlements(deps) == []
    [ack] = [a for a in outbound if a.kind == "answer_callback_query"]
    assert "anonymous" in (ack.text or "")


async def test_a_ledger_holding_only_a_settlement_refuses_undo_of_its_creation(deps):
    """ADR-0004: a settlement is a transaction too — undoing /newledger must not
    orphan it in an archived shell."""
    deps.client = FakeBoardMessenger()
    await setup_group(deps)
    await dispatch(message_update(update_id=93, chat_id=-42, text="/newledger Tokyo"), deps)
    await dispatch(
        message_update(update_id=94, chat_id=-42, text="/settle @alice 5 EUR", from_user=BOB),
        deps,
    )
    [new_ledger_action] = await read_actions(deps, "new_ledger")

    outbound = await dispatch(
        callback_update(update_id=95, chat_id=-42, data=f"v1:undo:{new_ledger_action.id}"), deps
    )

    [ack] = [a for a in outbound if a.kind == "answer_callback_query"]
    assert "Tokyo has" in (ack.text or "")
    async with deps.session_factory() as session:
        tokyo = (await session.execute(select(Ledger).where(Ledger.name == "Tokyo"))).scalar_one()
    assert tokyo.status == "open"  # nothing toggled


async def test_undoing_a_settlement_restores_the_debt_and_redo_restores_the_payment(deps):
    """§8: settle_up is row-creating — undo soft-deletes its one row, redo revives it."""
    deps.client = FakeBoardMessenger()
    await setup_group(deps)
    await bob_owes_alice_30(deps)
    await dispatch(
        message_update(update_id=93, chat_id=-42, text="/settle @alice 30 EUR", from_user=BOB),
        deps,
    )
    [action] = await read_actions(deps, "settle_up")

    undone = await dispatch(
        callback_update(update_id=94, chat_id=-42, data=f"v1:undo:{action.id}"), deps
    )

    [settlement] = await read_settlements(deps)
    assert settlement.deleted_at is not None
    [board_edit] = board_edits(undone)
    assert board_edit.text == "📒 Japan Trip • Board\nBob → Alice EUR 30.00"

    redone = await dispatch(
        callback_update(update_id=95, chat_id=-42, data=f"v1:redo:{action.id}"), deps
    )

    [settlement] = await read_settlements(deps)
    assert settlement.deleted_at is None
    [board_edit] = board_edits(redone)
    assert "All settled up." in board_edit.text
