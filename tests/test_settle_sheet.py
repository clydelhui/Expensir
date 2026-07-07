"""The settle sheet (§7.3, ADR-0007, issue #11): /settle with no amount is a READ
that lists every suggested transfer between the pair, each line its own WYSIWYG
[Settle] button. The sheet commits nothing; the taps do."""

from sqlalchemy import select

from expensir.core.handler import dispatch
from expensir.db.models import Group
from tests.factories import callback_update, message_update
from tests.test_board import FakeBoardMessenger, board_edits
from tests.test_settle import (
    ALICE,
    BOB,
    CAROL,
    bob_owes_alice_30,
    read_actions,
    read_settlements,
    replies_of,
    setup_group,
    user_id_of,
)


async def active_ledger_id(deps, chat_id: int = -42) -> int:
    async with deps.session_factory() as session:
        group = (
            await session.execute(select(Group).where(Group.platform_chat_id == chat_id))
        ).scalar_one()
        return group.active_ledger_id


async def test_settle_with_no_amount_replies_with_the_pairs_settle_sheet(deps):
    """ADR-0007: the sheet is a read — the suggested transfer between the pair,
    with a per-line WYSIWYG [Settle] button; no action row, no settlement."""
    deps.client = FakeBoardMessenger()
    await setup_group(deps)
    await bob_owes_alice_30(deps)
    alice, bob = await user_id_of(deps, "Alice"), await user_id_of(deps, "Bob")
    ledger = await active_ledger_id(deps)

    outbound = await dispatch(
        message_update(update_id=93, chat_id=-42, text="/settle @alice", from_user=BOB), deps
    )

    [reply] = replies_of(outbound)
    assert "Bob → Alice EUR 30.00" in reply.text
    assert reply.reply_markup == {
        "inline_keyboard": [
            [
                {
                    "text": "🤝 Settle Bob → Alice EUR 30.00",
                    "callback_data": f"v1:sh:{ledger}:{bob}:{alice}:EUR:3000",
                }
            ]
        ]
    }
    assert await read_actions(deps, "settle_up") == []  # a read writes no action row
    assert await read_settlements(deps) == []
    assert board_edits(outbound) == []  # a read never touches the board


async def test_opposite_directions_in_two_currencies_get_one_line_and_button_each(deps):
    """The sheet lists BOTH directions (ADR-0007): a directed bulk settle would
    silently leave the counter-direction line standing."""
    deps.client = FakeBoardMessenger()
    await setup_group(deps)
    await bob_owes_alice_30(deps)  # EUR: Bob → Alice 30.00
    await dispatch(  # JPY: Alice → Bob 500
        message_update(
            update_id=93, chat_id=-42, text="/equal 1000 JPY taxi @alice @bob", from_user=BOB
        ),
        deps,
    )
    alice, bob = await user_id_of(deps, "Alice"), await user_id_of(deps, "Bob")
    ledger = await active_ledger_id(deps)

    outbound = await dispatch(
        message_update(update_id=94, chat_id=-42, text="/settle @alice", from_user=BOB), deps
    )

    [reply] = replies_of(outbound)
    assert "Bob → Alice EUR 30.00" in reply.text
    assert "Alice → Bob JPY 500" in reply.text
    assert reply.reply_markup == {
        "inline_keyboard": [
            [
                {
                    "text": "🤝 Settle Bob → Alice EUR 30.00",
                    "callback_data": f"v1:sh:{ledger}:{bob}:{alice}:EUR:3000",
                }
            ],
            [
                {
                    "text": "🤝 Settle Alice → Bob JPY 500",
                    "callback_data": f"v1:sh:{ledger}:{alice}:{bob}:JPY:500",
                }
            ],
        ]
    }
    assert await read_settlements(deps) == []


async def test_a_fresh_sheet_tap_records_that_one_line_with_its_own_undo(deps):
    """ADR-0007: the sheet commits nothing — the tap does: one settlement, one
    action, its own Undo; the board re-renders and the sheet refreshes in place."""
    deps.client = FakeBoardMessenger()
    await setup_group(deps)
    await bob_owes_alice_30(deps)
    alice, bob = await user_id_of(deps, "Alice"), await user_id_of(deps, "Bob")
    ledger = await active_ledger_id(deps)

    outbound = await dispatch(
        callback_update(
            update_id=94,
            chat_id=-42,
            data=f"v1:sh:{ledger}:{bob}:{alice}:EUR:3000",
            message_id=555,  # the sheet message the button rides on
        ),
        deps,
    )

    [settlement] = await read_settlements(deps)
    assert (settlement.from_user, settlement.to_user) == (bob, alice)
    assert (settlement.amount_minor, settlement.currency) == (3000, "EUR")
    [action] = await read_actions(deps, "settle_up")
    assert settlement.created_by_action_id == action.id

    [ack] = [a for a in outbound if a.kind == "answer_callback_query"]
    assert "Recorded" in (ack.text or "")
    [reply] = replies_of(outbound)
    assert "Bob paid Alice EUR 30.00" in reply.text
    assert reply.records_result_for_action_id == action.id
    assert reply.reply_markup == {
        "inline_keyboard": [[{"text": "↩️ Undo", "callback_data": f"v1:undo:{action.id}"}]]
    }
    [board_edit] = board_edits(outbound)  # a mutation like any other (§13)
    assert "All settled up." in board_edit.text
    [sheet_edit] = [a for a in outbound if a.kind == "edit_message" and a.message_id == 555]
    assert "Nothing to settle between Alice and Bob." in sheet_edit.text
    assert sheet_edit.reply_markup is None


async def test_a_stale_sheet_tap_records_nothing_warns_and_refreshes_the_sheet(deps):
    """Same staleness guard as the board (ADR-0006 via ADR-0007): never record
    an amount the presser didn't see."""
    deps.client = FakeBoardMessenger()
    await setup_group(deps)
    await bob_owes_alice_30(deps)
    alice, bob = await user_id_of(deps, "Alice"), await user_id_of(deps, "Bob")
    ledger = await active_ledger_id(deps)
    # the debt moves on: Bob now owes 40, so a sheet line showing 30.00 is stale
    await dispatch(
        message_update(update_id=93, chat_id=-42, text="/equal 20 taxi @alice @bob"), deps
    )

    outbound = await dispatch(
        callback_update(
            update_id=94, chat_id=-42, data=f"v1:sh:{ledger}:{bob}:{alice}:EUR:3000", message_id=555
        ),
        deps,
    )

    assert await read_settlements(deps) == []
    assert await read_actions(deps, "settle_up") == []
    [ack] = [a for a in outbound if a.kind == "answer_callback_query"]
    assert "out of date" in (ack.text or "")
    assert replies_of(outbound) == []  # nothing recorded -> no result message
    [sheet_edit] = [a for a in outbound if a.kind == "edit_message" and a.message_id == 555]
    assert "Bob → Alice EUR 40.00" in sheet_edit.text  # refreshed against the truth
    assert f"v1:sh:{ledger}:{bob}:{alice}:EUR:4000" in str(sheet_edit.reply_markup)
    assert board_edits(outbound) == []  # nothing recorded -> the board stands


async def test_a_tap_on_a_settled_away_sheet_line_answers_already_settled(deps):
    deps.client = FakeBoardMessenger()
    await setup_group(deps)
    await bob_owes_alice_30(deps)
    alice, bob = await user_id_of(deps, "Alice"), await user_id_of(deps, "Bob")
    ledger = await active_ledger_id(deps)
    await dispatch(
        message_update(update_id=93, chat_id=-42, text="/settle @alice 30 EUR", from_user=BOB),
        deps,
    )

    outbound = await dispatch(  # the line is gone; an old sheet races the truth
        callback_update(
            update_id=94, chat_id=-42, data=f"v1:sh:{ledger}:{bob}:{alice}:EUR:3000", message_id=555
        ),
        deps,
    )

    [settlement] = await read_settlements(deps)  # still just the /settle one
    [ack] = [a for a in outbound if a.kind == "answer_callback_query"]
    assert ack.text == "Already settled."
    [sheet_edit] = [a for a in outbound if a.kind == "edit_message" and a.message_id == 555]
    assert "Nothing to settle between Alice and Bob." in sheet_edit.text
    assert sheet_edit.reply_markup is None


async def test_nothing_to_settle_when_simplify_routes_the_debt_elsewhere(deps):
    """ADR-0007: the sheet follows the solver, not pairwise history — Bob owes
    Alice from dinner, but his own fronting nets him out, so their sheet is empty
    and no reverse credit is invented."""
    deps.client = FakeBoardMessenger()
    await setup_group(deps)
    await dispatch(message_update(update_id=92, chat_id=-42, text="ciao", from_user=CAROL), deps)
    # Alice fronts 60 for Bob; Bob fronts 60 for Carol -> Bob nets zero;
    # simplify suggests only Carol → Alice
    await dispatch(message_update(update_id=93, chat_id=-42, text="/equal 60 dinner @bob"), deps)
    await dispatch(
        message_update(update_id=94, chat_id=-42, text="/equal 60 taxi @carol", from_user=BOB),
        deps,
    )

    outbound = await dispatch(
        message_update(update_id=95, chat_id=-42, text="/settle @alice", from_user=BOB), deps
    )

    [reply] = replies_of(outbound)
    assert reply.text == "📒 Japan Trip • Nothing to settle between Alice and Bob."
    assert reply.reply_markup is None
    assert await read_actions(deps, "settle_up") == []


async def test_the_pair_is_unordered_both_members_get_the_identical_sheet(deps):
    """ADR-0007: 'settle up with Alice' from Bob and 'settle up with Bob' from
    Alice are the same read."""
    deps.client = FakeBoardMessenger()
    await setup_group(deps)
    await bob_owes_alice_30(deps)

    from_bob = await dispatch(
        message_update(update_id=93, chat_id=-42, text="/settle @alice", from_user=BOB), deps
    )
    from_alice = await dispatch(
        message_update(update_id=94, chat_id=-42, text="/settle @bob", from_user=ALICE), deps
    )

    [bob_reply] = replies_of(from_bob)
    [alice_reply] = replies_of(from_alice)
    assert bob_reply.text == alice_reply.text
    assert bob_reply.reply_markup == alice_reply.reply_markup


async def test_a_sheet_for_an_unknown_member_is_refused(deps):
    deps.client = FakeBoardMessenger()
    await setup_group(deps)
    await bob_owes_alice_30(deps)

    outbound = await dispatch(
        message_update(update_id=93, chat_id=-42, text="/settle @stranger", from_user=BOB), deps
    )

    [reply] = replies_of(outbound)
    assert "don't know @stranger" in reply.text
    assert reply.reply_markup is None


async def test_a_sheet_tap_with_a_foreign_ledger_records_nothing(deps):
    """A forged or cross-group callback: the ledger in the data must be this
    group's (§9's spirit, same as an orphaned board tap)."""
    deps.client = FakeBoardMessenger()
    await setup_group(deps)
    await bob_owes_alice_30(deps)
    alice, bob = await user_id_of(deps, "Alice"), await user_id_of(deps, "Bob")
    foreign = await active_ledger_id(deps) + 999  # no such ledger here

    outbound = await dispatch(
        callback_update(
            update_id=94,
            chat_id=-42,
            data=f"v1:sh:{foreign}:{bob}:{alice}:EUR:3000",
            message_id=555,
        ),
        deps,
    )

    assert await read_settlements(deps) == []
    [ack] = [a for a in outbound if a.kind == "answer_callback_query"]
    assert ack.text == "That button doesn't match anything I recorded."
    assert [a for a in outbound if a.kind == "edit_message"] == []


async def test_an_anonymous_admin_sheet_tap_records_nothing(deps):
    """§11: no auditable actor, no mutation — same rule as board taps."""
    deps.client = FakeBoardMessenger()
    await setup_group(deps)
    await bob_owes_alice_30(deps)
    alice, bob = await user_id_of(deps, "Alice"), await user_id_of(deps, "Bob")
    ledger = await active_ledger_id(deps)
    anonymous = {"id": 1087968824, "is_bot": True, "first_name": "Group", "username": "GroupBot"}

    outbound = await dispatch(
        callback_update(
            update_id=94,
            chat_id=-42,
            data=f"v1:sh:{ledger}:{bob}:{alice}:EUR:3000",
            message_id=555,
            from_user=anonymous,
        ),
        deps,
    )

    assert await read_settlements(deps) == []
    [ack] = [a for a in outbound if a.kind == "answer_callback_query"]
    assert "anonymous" in (ack.text or "")
