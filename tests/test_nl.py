"""NL text path (§12) + proposal/confirm loop (§10) — slice 12, issue #13.

Dispatch-seam tests: a fake LLMClient returns canned wire results; the LLM's
own parsing is covered separately by recorded fixtures (§16)."""

from datetime import timedelta

import pytest
from sqlalchemy import func, select

from expensir.core.handler import dispatch
from expensir.db.models import (
    Action,
    Expense,
    ExpenseSplit,
    Group,
    Identity,
    Ledger,
    PendingIntent,
    Settlement,
    utcnow,
)
from expensir.llm.base import LLMUnavailable
from expensir.llm.wire import (
    WireAddExpense,
    WireArchiveLedger,
    WireDeleteExpense,
    WireEditExpense,
    WireNewLedger,
    WireSetHomeCurrency,
    WireSetLoggingCurrency,
    WireSettleUp,
    WireSetup,
    WireShowBalance,
    WireSplitMember,
    WireSwitchLedger,
    WireUnarchiveLedger,
    WireUnknown,
)
from expensir.transports.executor import execute
from tests.factories import callback_update, message_update, user
from tests.fakes import FakeLLM
from tests.test_executor import FakeTelegramClient

ALICE = user(1001, "Alice", "alice")
SAM = user(1002, "Sam", "sam")


DINNER_WITH_SAM = WireAddExpense(
    payer_ref="me",
    amount="40",
    currency=None,
    description="dinner",
    split_type="equal",
    participants=[WireSplitMember(user_ref="me"), WireSplitMember(user_ref="Sam")],
)


async def arrange_group(deps) -> None:
    """Home currency set (SGD) and Sam registered, so refs and currency resolve."""
    await dispatch(message_update(update_id=1, text="/homecurrency SGD", from_user=ALICE), deps)
    await dispatch(
        message_update(update_id=2, text="hi", from_user=SAM, message_id=5),
        deps,
    )


async def mention(deps, text: str, *, update_id: int = 3, message_id: int = 10, from_user=None):
    return await dispatch(
        message_update(
            update_id=update_id,
            text=f"@expensir_bot {text}",
            from_user=from_user or ALICE,
            message_id=message_id,
        ),
        deps,
    )


async def user_id_of(session, tg_user: dict) -> int:
    identity = (
        await session.execute(select(Identity).where(Identity.platform_user_id == tg_user["id"]))
    ).scalar_one()
    return identity.user_id


def keyboard_buttons(markup) -> list[tuple[str, str]]:
    return [(b["text"], b["callback_data"]) for row in markup["inline_keyboard"] for b in row]


async def test_an_nl_expense_renders_a_pinned_proposal_and_commits_nothing(deps):
    await arrange_group(deps)
    deps.llm = FakeLLM([DINNER_WITH_SAM])

    actions = await mention(deps, "I paid 40 for dinner, split with Sam")

    (send,) = actions
    assert send.kind == "send_message"
    assert send.chat_id == -100500
    # the proposal names the pinned ledger (WYSIWYG, §10) and shows resolved money (§3)
    assert send.text.startswith("📒 Japan Trip • ")
    assert "SGD 40.00" in send.text
    assert "dinner" in send.text
    # shares preview == the shares that will commit (frozen seed, §7.1)
    assert send.text.count("SGD 20.00") == 2
    assert "Alice" in send.text and "Sam" in send.text
    assert "reply to correct" in send.text

    async with deps.session_factory() as session:
        pending = (await session.execute(select(PendingIntent))).scalar_one()
        group = (await session.execute(select(Group))).scalar_one()
        assert pending.ledger_id == group.active_ledger_id  # pinned at propose time
        assert pending.chat_id == -100500
        assert pending.message_id is None  # backfilled by the executor after the send
        assert pending.seed == 10  # the originating message id (§7.1 WYSIWYG shares)
        assert pending.expires_at > pending.created_at
        # nothing commits until Confirm (§0.7): no expense, no action
        assert (await session.execute(select(func.count()).select_from(Expense))).scalar() == 0

    labels = keyboard_buttons(send.reply_markup)
    assert labels == [
        ("✅ Confirm", f"v1:confirm:{pending.id}"),
        ("✖ Cancel", f"v1:cancel:{pending.id}"),
    ]
    assert send.records_message_for_pending_id == pending.id


async def propose_dinner(deps, **mention_kwargs) -> int:
    """Arrange + propose the 40-SGD dinner; returns the pending row's id."""
    await arrange_group(deps)
    deps.llm = FakeLLM([DINNER_WITH_SAM])
    await mention(deps, "I paid 40 for dinner, split with Sam", **mention_kwargs)
    async with deps.session_factory() as session:
        return (await session.execute(select(PendingIntent))).scalar_one().id


async def test_a_concurrent_switch_never_redirects_the_commit_off_the_pinned_ledger(deps):
    pending_id = await propose_dinner(deps)
    async with deps.session_factory() as session:
        pinned_ledger_id = (await session.execute(select(PendingIntent))).scalar_one().ledger_id
    # the active ledger moves between propose and confirm (§10)
    await dispatch(message_update(update_id=7, text="/newledger Tokyo", from_user=ALICE), deps)

    actions = await dispatch(
        callback_update(data=f"v1:confirm:{pending_id}", from_user=ALICE, message_id=555),
        deps,
    )

    _, edit = actions[0], actions[1]
    assert edit.text.startswith("📒 Japan Trip • ")  # committed where it was proposed
    async with deps.session_factory() as session:
        expense = (await session.execute(select(Expense))).scalar_one()
        group = (await session.execute(select(Group))).scalar_one()
        assert expense.ledger_id == pinned_ledger_id
        assert expense.ledger_id != group.active_ledger_id  # Tokyo stayed untouched


async def test_an_unknown_reference_rejects_the_whole_intent_with_setup_guidance(deps):
    await arrange_group(deps)
    deps.llm = FakeLLM(
        [
            WireAddExpense(
                amount="40",
                description="dinner",
                participants=[WireSplitMember(user_ref="me"), WireSplitMember(user_ref="Carol")],
            )
        ]
    )

    actions = await mention(deps, "I paid 40 for dinner, split with Carol")

    (send,) = actions
    assert "Carol" in send.text and "/setup" in send.text  # how to register them (§11)
    assert send.reply_markup is None
    async with deps.session_factory() as session:
        assert (
            await session.execute(select(func.count()).select_from(PendingIntent))
        ).scalar() == 0


async def test_a_bare_display_name_resolves_even_when_the_username_differs(deps):
    """§11: a bare name from NL matches display names, not just @usernames."""
    await arrange_group(deps)
    wanderer = user(1003, "Kim", "wanderer99")
    await dispatch(message_update(update_id=6, text="hi", from_user=wanderer, message_id=8), deps)
    deps.llm = FakeLLM(
        [
            WireAddExpense(
                amount="40",
                description="dinner",
                participants=[WireSplitMember(user_ref="me"), WireSplitMember(user_ref="Kim")],
            )
        ]
    )

    actions = await mention(deps, "I paid 40 for dinner, split with Kim")

    (send,) = actions
    assert "reply to correct" in send.text  # it proposed: the ref resolved
    assert "Kim" in send.text


async def test_an_ambiguous_display_name_rejects_with_username_guidance(deps):
    """Two members named Sam: no pick-list until slice 13 — reject with guidance."""
    await arrange_group(deps)
    other_sam = user(1004, "Sam", "sam_the_second")
    await dispatch(message_update(update_id=6, text="hi", from_user=other_sam, message_id=8), deps)
    deps.llm = FakeLLM([DINNER_WITH_SAM])

    actions = await mention(deps, "I paid 40 for dinner, split with Sam")

    (send,) = actions
    assert "@" in send.text  # point at @username disambiguation
    async with deps.session_factory() as session:
        assert (
            await session.execute(select(func.count()).select_from(PendingIntent))
        ).scalar() == 0
        assert (await session.execute(select(func.count()).select_from(Expense))).scalar() == 0


async def test_an_nl_ledger_switch_proposes_then_commits_on_confirm(deps):
    """Every NL mutation kind rides the same propose/confirm loop (§0.7, §12)."""
    await arrange_group(deps)
    await dispatch(message_update(update_id=7, text="/newledger Tokyo", from_user=ALICE), deps)
    deps.llm = FakeLLM([WireSwitchLedger(name_or_id="Japan Trip")])

    (proposal,) = await mention(deps, "switch back to the Japan Trip ledger")
    assert proposal.text.startswith("📒 Tokyo • ")  # pinned to the ledger of propose time
    assert "Japan Trip" in proposal.text and "reply to correct" in proposal.text
    async with deps.session_factory() as session:
        pending_id = (await session.execute(select(PendingIntent))).scalar_one().id
        group = (await session.execute(select(Group))).scalar_one()
        tokyo_id = group.active_ledger_id  # unchanged: nothing commits at propose

    ack, edit, *_ = await dispatch(
        callback_update(data=f"v1:confirm:{pending_id}", from_user=ALICE, message_id=555), deps
    )

    assert "Switched to Japan Trip" in edit.text
    (undo_button,) = keyboard_buttons(edit.reply_markup)
    assert undo_button[0] == "↩️ Undo"
    async with deps.session_factory() as session:
        group = (await session.execute(select(Group))).scalar_one()
        assert group.active_ledger_id != tokyo_id  # the switch committed


async def test_an_nl_settlement_with_an_amount_proposes_then_records_on_confirm(deps):
    await arrange_group(deps)
    deps.llm = FakeLLM([WireSettleUp(from_ref="me", to_ref="Sam", amount="20")])

    (proposal,) = await mention(deps, "I paid Sam 20")
    assert "SGD 20.00" in proposal.text  # §3: the resolved currency is visible
    assert "reply to correct" in proposal.text
    async with deps.session_factory() as session:
        pending_id = (await session.execute(select(PendingIntent))).scalar_one().id

    ack, edit, *_ = await dispatch(
        callback_update(data=f"v1:confirm:{pending_id}", from_user=ALICE, message_id=555), deps
    )

    assert "Alice paid Sam SGD 20.00" in edit.text
    async with deps.session_factory() as session:
        settlement = (await session.execute(select(Settlement))).scalar_one()
        assert settlement.amount_minor == 2000 and settlement.currency == "SGD"


async def commit_dinner(deps) -> int:
    """Propose + confirm the dinner; returns the expense id. The proposal message
    (555) becomes the result message, so replies can target it (§8, §11)."""
    pending_id = await propose_dinner(deps)
    await dispatch(
        callback_update(data=f"v1:confirm:{pending_id}", from_user=ALICE, message_id=555), deps
    )
    async with deps.session_factory() as session:
        return (await session.execute(select(Expense))).scalar_one().id


async def test_nl_delete_by_visible_id_proposes_then_soft_deletes_on_confirm(deps):
    expense_id = await commit_dinner(deps)
    deps.llm = FakeLLM([WireDeleteExpense(expense_id=expense_id)])

    (proposal,) = await mention(deps, f"delete #{expense_id}", update_id=9, message_id=30)
    assert f"#{expense_id}" in proposal.text and "reply to correct" in proposal.text
    async with deps.session_factory() as session:
        pending_id = (await session.execute(select(PendingIntent))).scalar_one().id
        expense = await session.get_one(Expense, expense_id)
        assert expense.deleted_at is None  # nothing until Confirm

    ack, edit, *_ = await dispatch(
        callback_update(data=f"v1:confirm:{pending_id}", from_user=ALICE, message_id=600), deps
    )

    assert "Deleted" in edit.text
    async with deps.session_factory() as session:
        expense = await session.get_one(Expense, expense_id)
        assert expense.deleted_at is not None


async def test_nl_edit_resolves_the_expense_from_the_replied_to_result_message(deps):
    expense_id = await commit_dinner(deps)
    deps.llm = FakeLLM([WireEditExpense(description="team dinner")])  # no #id: the reply names it

    (proposal,) = await dispatch(
        message_update(
            update_id=9,
            text="@expensir_bot make that team dinner",
            from_user=ALICE,
            message_id=30,
            reply_to_message_id=555,  # the committed result message (§11 primary tier)
        ),
        deps,
    )
    assert "team dinner" in proposal.text
    async with deps.session_factory() as session:
        pending_id = (await session.execute(select(PendingIntent))).scalar_one().id

    ack, edit, *_ = await dispatch(
        callback_update(data=f"v1:confirm:{pending_id}", from_user=ALICE, message_id=600), deps
    )

    assert "team dinner" in edit.text
    async with deps.session_factory() as session:
        expense = await session.get_one(Expense, expense_id)
        assert expense.description == "team dinner"


async def test_nl_delete_without_id_or_reply_gets_guidance_not_a_proposal(deps):
    await commit_dinner(deps)
    deps.llm = FakeLLM([WireDeleteExpense()])

    (send,) = await mention(deps, "delete the dinner one", update_id=9, message_id=30)

    # §11 guidance; descriptive matching arrives in slice 13
    assert "#id" in send.text or "reply" in send.text.lower()
    async with deps.session_factory() as session:
        assert (
            await session.execute(select(func.count()).select_from(PendingIntent))
        ).scalar() == 0


async def test_a_confirm_whose_reference_went_stale_fails_and_commits_nothing(deps):
    """The referenced expense vanished between propose and confirm (§10.3)."""
    expense_id = await commit_dinner(deps)
    deps.llm = FakeLLM([WireDeleteExpense(expense_id=expense_id)])
    await mention(deps, f"delete #{expense_id}", update_id=9, message_id=30)
    async with deps.session_factory() as session:
        pending_id = (await session.execute(select(PendingIntent))).scalar_one().id
    await dispatch(
        message_update(update_id=10, text=f"/delete {expense_id}", from_user=ALICE), deps
    )  # someone slash-deletes it first

    ack, edit = await dispatch(
        callback_update(data=f"v1:confirm:{pending_id}", from_user=ALICE, message_id=600), deps
    )

    assert edit.kind == "edit_message"
    assert "already gone" in edit.text or "changed while you were deciding" in edit.text
    assert edit.reply_markup is None
    async with deps.session_factory() as session:
        assert (
            await session.execute(select(func.count()).select_from(PendingIntent))
        ).scalar() == 0


async def test_nl_archive_proposes_then_archives_and_repoints_on_confirm(deps):
    await arrange_group(deps)
    await dispatch(message_update(update_id=7, text="/newledger Tokyo", from_user=ALICE), deps)
    deps.llm = FakeLLM([WireArchiveLedger(name_or_id="Tokyo")])

    (proposal,) = await mention(deps, "archive the Tokyo ledger", update_id=9, message_id=30)
    assert "Tokyo" in proposal.text and "reply to correct" in proposal.text
    async with deps.session_factory() as session:
        pending_id = (await session.execute(select(PendingIntent))).scalar_one().id

    ack, edit, *_ = await dispatch(
        callback_update(data=f"v1:confirm:{pending_id}", from_user=ALICE, message_id=600), deps
    )

    assert "Archived" in edit.text
    async with deps.session_factory() as session:
        ledgers = {
            ledger.name: ledger.status
            for ledger in (await session.execute(select(Ledger))).scalars()
        }
        assert ledgers["Tokyo"] == "archived"
        group = (await session.execute(select(Group))).scalar_one()
        active = await session.get_one(Ledger, group.active_ledger_id)
        assert active.name == "Japan Trip"  # archiving the active ledger repoints (ADR-0004)


NL_MUTATIONS = [
    (
        WireNewLedger(name="Osaka", logging_currency="JPY"),
        "new ledger Osaka in yen",
        "Osaka",
    ),
    (
        WireSetHomeCurrency(currency="EUR"),
        "set our home currency to euros",
        "EUR",
    ),
    (
        WireSetLoggingCurrency(currency="JPY"),
        "log this ledger in yen",
        "JPY",
    ),
]


@pytest.mark.parametrize(("wire", "utterance", "committed_marker"), NL_MUTATIONS)
async def test_every_nl_mutation_kind_rides_the_propose_confirm_loop(
    deps, wire, utterance, committed_marker
):
    await arrange_group(deps)
    deps.llm = FakeLLM([wire])

    (proposal,) = await mention(deps, utterance, update_id=9, message_id=30)
    assert "reply to correct" in proposal.text
    async with deps.session_factory() as session:
        pending_id = (await session.execute(select(PendingIntent))).scalar_one().id

    ack, edit, *_ = await dispatch(
        callback_update(data=f"v1:confirm:{pending_id}", from_user=ALICE, message_id=600), deps
    )

    assert committed_marker in edit.text
    (undo_button,) = keyboard_buttons(edit.reply_markup)
    assert undo_button[0] == "↩️ Undo"


async def test_nl_unarchive_reopens_without_switching(deps):
    await arrange_group(deps)
    await dispatch(message_update(update_id=7, text="/newledger Tokyo", from_user=ALICE), deps)
    await dispatch(message_update(update_id=8, text="/archive Tokyo", from_user=ALICE), deps)
    deps.llm = FakeLLM([WireUnarchiveLedger(name_or_id="Tokyo")])

    (proposal,) = await mention(deps, "reopen the Tokyo ledger", update_id=9, message_id=30)
    async with deps.session_factory() as session:
        pending_id = (await session.execute(select(PendingIntent))).scalar_one().id

    ack, edit, *_ = await dispatch(
        callback_update(data=f"v1:confirm:{pending_id}", from_user=ALICE, message_id=600), deps
    )

    assert "Reopened" in edit.text
    async with deps.session_factory() as session:
        tokyo = (await session.execute(select(Ledger).where(Ledger.name == "Tokyo"))).scalar_one()
        assert tokyo.status == "open"
        group = (await session.execute(select(Group))).scalar_one()
        assert group.active_ledger_id != tokyo.id  # reopening never switches (§17)


async def test_nl_setup_by_reply_proposes_then_registers_without_undo(deps):
    """Registration is permanent (§8): the committed result carries NO Undo button."""
    await arrange_group(deps)
    carol = user(1005, "Carol", "carol")
    deps.llm = FakeLLM([WireSetup()])

    (proposal,) = await dispatch(
        message_update(
            update_id=9,
            text="@expensir_bot add Carol",
            from_user=ALICE,
            message_id=30,
            reply_to_message_id=25,
            reply_to_from=carol,  # the reply target embeds the account (§11)
        ),
        deps,
    )
    assert "Carol" in proposal.text and "reply to correct" in proposal.text
    async with deps.session_factory() as session:
        pending_id = (await session.execute(select(PendingIntent))).scalar_one().id

    ack, edit, *_ = await dispatch(
        callback_update(data=f"v1:confirm:{pending_id}", from_user=ALICE, message_id=600), deps
    )

    assert "Registered Carol" in edit.text
    assert edit.reply_markup is None  # permanent — no Undo affordance (§8)
    async with deps.session_factory() as session:
        identity = (
            await session.execute(select(Identity).where(Identity.platform_user_id == 1005))
        ).scalar_one_or_none()
        assert identity is not None


class UnavailableLLM:
    async def extract_text(self, text: str):
        raise LLMUnavailable("connect timeout")


async def test_llm_outage_says_so_instead_of_blaming_the_sentence(deps):
    """Grilled (issue #13): a transport failure must not read as 'rephrase that'."""
    await arrange_group(deps)
    deps.llm = UnavailableLLM()

    actions = await mention(deps, "I paid 40 for dinner, split with Sam")

    (send,) = actions
    assert "rephras" not in send.text.lower()
    assert "slash command" in send.text  # the deterministic path still works
    async with deps.session_factory() as session:
        assert (
            await session.execute(select(func.count()).select_from(PendingIntent))
        ).scalar() == 0


async def test_unmappable_text_gets_a_rephrase_suggestion_and_no_proposal(deps):
    await arrange_group(deps)
    deps.llm = FakeLLM([WireUnknown(reason="not an expense or a question")])

    actions = await mention(deps, "purple monkey dishwasher")

    (send,) = actions
    assert "rephras" in send.text.lower()  # ask to rephrase (§4 Unknown)
    assert send.reply_markup is None
    async with deps.session_factory() as session:
        assert (
            await session.execute(select(func.count()).select_from(PendingIntent))
        ).scalar() == 0


async def test_an_nl_balance_read_runs_immediately_without_a_proposal(deps):
    pending_id = await propose_dinner(deps)
    await dispatch(
        callback_update(data=f"v1:confirm:{pending_id}", from_user=ALICE, message_id=555), deps
    )
    deps.llm = FakeLLM([WireShowBalance(scope="me")])

    actions = await mention(deps, "what do I owe?", update_id=9, message_id=30)

    (send,) = actions
    assert send.kind == "send_message"
    assert "You're owed SGD 20.00" in send.text  # Alice fronted Sam's half of dinner
    assert send.reply_markup is None  # a read: no Confirm/Cancel, no Undo (§0.7)
    async with deps.session_factory() as session:
        assert (
            await session.execute(select(func.count()).select_from(PendingIntent))
        ).scalar() == 0


async def test_nl_settle_up_without_an_amount_is_the_settle_sheet_a_read(deps):
    pending_id = await propose_dinner(deps)
    await dispatch(
        callback_update(data=f"v1:confirm:{pending_id}", from_user=ALICE, message_id=555), deps
    )
    deps.llm = FakeLLM([WireSettleUp(from_ref="me", to_ref="Sam", amount=None)])

    actions = await mention(deps, "settle up with Sam", update_id=9, message_id=30)

    (send,) = actions
    assert "Settling up" in send.text and "Sam" in send.text
    # the sheet carries its own per-line [Settle] buttons (ADR-0007), not Confirm/Cancel
    buttons = keyboard_buttons(send.reply_markup)
    assert all(data.startswith("v1:sh:") for _, data in buttons)
    async with deps.session_factory() as session:
        assert (
            await session.execute(select(func.count()).select_from(PendingIntent))
        ).scalar() == 0


async def test_confirming_against_an_archived_pinned_ledger_fails_and_commits_nothing(deps):
    pending_id = await propose_dinner(deps)
    await dispatch(message_update(update_id=7, text="/newledger Tokyo", from_user=ALICE), deps)
    await dispatch(message_update(update_id=8, text="/archive Japan Trip", from_user=ALICE), deps)

    actions = await dispatch(
        callback_update(data=f"v1:confirm:{pending_id}", from_user=ALICE, message_id=555),
        deps,
    )

    ack, edit = actions
    assert edit.kind == "edit_message" and edit.message_id == 555
    assert "archived" in edit.text
    assert edit.reply_markup is None  # failed re-validation: no Undo, nothing committed
    async with deps.session_factory() as session:
        assert (await session.execute(select(func.count()).select_from(Expense))).scalar() == 0
        # the failed confirm still consumes the row: replying starts fresh (slice 13)
        assert (
            await session.execute(select(func.count()).select_from(PendingIntent))
        ).scalar() == 0


async def test_anyone_may_confirm_but_me_still_means_the_proposer(deps):
    """Grilled (issue #13): the trust model is anyone-may-act with Undo as the
    guardrail (§17); the frozen proposer keeps owning "me"."""
    pending_id = await propose_dinner(deps)  # Alice proposed "I paid 40..."

    await dispatch(
        callback_update(data=f"v1:confirm:{pending_id}", from_user=SAM, message_id=555),
        deps,
    )

    async with deps.session_factory() as session:
        expense = (await session.execute(select(Expense))).scalar_one()
        action = (
            await session.execute(select(Action).where(Action.kind == "add_expense"))
        ).scalar_one()
        alice_id = await user_id_of(session, ALICE)
        sam_id = await user_id_of(session, SAM)
        assert expense.payer_id == alice_id  # "me" = the proposer, not the presser
        assert action.actor_user_id == sam_id  # ...while the audit names who committed it
        assert expense.created_by_user_id == sam_id


async def test_confirming_an_expired_proposal_edits_it_to_expired_and_commits_nothing(deps):
    pending_id = await propose_dinner(deps)
    async with deps.session_factory() as session, session.begin():
        pending = await session.get_one(PendingIntent, pending_id)
        pending.expires_at = utcnow() - timedelta(minutes=1)  # expiry computed on read (§10)

    actions = await dispatch(
        callback_update(data=f"v1:confirm:{pending_id}", from_user=ALICE, message_id=555),
        deps,
    )

    ack, edit = actions
    assert "Expired" in edit.text
    assert edit.reply_markup is None
    async with deps.session_factory() as session:
        assert (await session.execute(select(func.count()).select_from(Expense))).scalar() == 0
        assert (
            await session.execute(select(func.count()).select_from(PendingIntent))
        ).scalar() == 0


async def test_a_second_confirm_tap_finds_nothing_and_commits_nothing_more(deps):
    pending_id = await propose_dinner(deps)
    tap = callback_update(data=f"v1:confirm:{pending_id}", from_user=ALICE, message_id=555)
    await dispatch(tap, deps)

    actions = await dispatch(tap, deps)

    (ack,) = actions
    assert ack.kind == "answer_callback_query"
    assert ack.text == "This proposal was already handled."
    async with deps.session_factory() as session:
        assert (await session.execute(select(func.count()).select_from(Expense))).scalar() == 1


async def test_the_executor_reports_the_proposal_message_id_back_onto_the_pending_row(deps):
    """§10: the row is keyed by the proposal message, which exists only after the
    send — the executor closes the loop like it does for action result ids."""
    await arrange_group(deps)
    deps.llm = FakeLLM([DINNER_WITH_SAM])
    actions = await mention(deps, "I paid 40 for dinner, split with Sam")

    client = FakeTelegramClient()
    await execute(actions, client, deps.session_factory)

    async with deps.session_factory() as session:
        pending = (await session.execute(select(PendingIntent))).scalar_one()
        assert pending.message_id == 1  # the id FakeTelegramClient minted for the send


async def test_cancel_edits_the_proposal_and_leaves_nothing_behind(deps):
    pending_id = await propose_dinner(deps)

    actions = await dispatch(
        callback_update(data=f"v1:cancel:{pending_id}", from_user=ALICE, message_id=555),
        deps,
    )

    ack, edit = actions
    assert ack.kind == "answer_callback_query"
    assert edit.kind == "edit_message"
    assert edit.message_id == 555
    assert "Cancelled" in edit.text
    assert edit.reply_markup is None  # a dead proposal keeps no buttons

    async with deps.session_factory() as session:
        assert (
            await session.execute(select(func.count()).select_from(PendingIntent))
        ).scalar() == 0
        assert (await session.execute(select(func.count()).select_from(Expense))).scalar() == 0
        add_actions = (
            await session.execute(
                select(func.count()).select_from(Action).where(Action.kind == "add_expense")
            )
        ).scalar()
        assert add_actions == 0  # a cancelled proposal never audited anything (§10)


async def test_confirm_commits_to_the_pinned_ledger_and_edits_the_proposal(deps):
    pending_id = await propose_dinner(deps)

    actions = await dispatch(
        callback_update(data=f"v1:confirm:{pending_id}", from_user=ALICE, message_id=555),
        deps,
    )

    ack, edit = actions
    assert ack.kind == "answer_callback_query"
    # the proposal message ITSELF becomes the committed result, with Undo (§10)
    assert edit.kind == "edit_message"
    assert edit.message_id == 555
    assert "dinner" in edit.text and "SGD 40.00" in edit.text
    assert "#" in edit.text  # committed results show the visible expense id (§11)
    (undo_button,) = keyboard_buttons(edit.reply_markup)

    async with deps.session_factory() as session:
        expense = (await session.execute(select(Expense))).scalar_one()
        group = (await session.execute(select(Group))).scalar_one()
        assert expense.ledger_id == group.active_ledger_id
        assert expense.amount_minor == 4000 and expense.currency == "SGD"
        assert expense.source == "nl"
        owed = [
            split.owed_minor for split in (await session.execute(select(ExpenseSplit))).scalars()
        ]
        assert sorted(owed) == [2000, 2000]
        action = (
            await session.execute(select(Action).where(Action.kind == "add_expense"))
        ).scalar_one()
        assert undo_button == ("↩️ Undo", f"v1:undo:{action.id}")
        # the result message is the proposal message: undo can edit it, reply
        # can target it (§8) — recorded in-transaction, no executor round-trip
        assert action.result_chat_id == -100500 and action.result_message_id == 555
        # confirm consumed the row (§10): nothing pending remains
        assert (
            await session.execute(select(func.count()).select_from(PendingIntent))
        ).scalar() == 0
