"""Ledger lifecycle (§8, ADR-0004): new/switch/archive/unarchive/currency — slice 7 (#8)."""

from sqlalchemy import select

from expensir.core.handler import dispatch
from expensir.db.models import Action, Expense, Group, Ledger
from tests.factories import bot_added_update, callback_update, message_update, user

ALICE = user(1001, "Alice", "alice")
BOB = user(1002, "Bob", "bob")


async def setup_group(deps, chat_id: int = -42, home: str | None = "EUR") -> None:
    await dispatch(bot_added_update(chat_id=chat_id, by=ALICE), deps)
    if home is not None:
        await dispatch(
            message_update(update_id=90, chat_id=chat_id, text=f"/homecurrency {home}"), deps
        )
    await dispatch(message_update(update_id=91, chat_id=chat_id, text="hello", from_user=BOB), deps)


async def read_group(deps, chat_id: int = -42) -> Group:
    async with deps.session_factory() as session:
        return (
            await session.execute(select(Group).where(Group.platform_chat_id == chat_id))
        ).scalar_one()


async def ledger_named(deps, name: str) -> Ledger:
    async with deps.session_factory() as session:
        return (await session.execute(select(Ledger).where(Ledger.name == name))).scalar_one()


async def test_newledger_without_a_name_replies_usage_and_creates_nothing(deps):
    await setup_group(deps)

    [reply] = await dispatch(message_update(update_id=5, chat_id=-42, text="/newledger"), deps)

    assert "Usage" in reply.text
    async with deps.session_factory() as session:
        ledgers = (await session.execute(select(Ledger))).scalars().all()
    assert len(ledgers) == 1  # only the onboarding ledger


async def test_a_lone_iso_looking_token_is_the_ledgers_name_not_its_currency(deps):
    await setup_group(deps)

    await dispatch(message_update(update_id=5, chat_id=-42, text="/newledger JPY"), deps)

    ledger = await ledger_named(deps, "JPY")
    assert ledger.logging_currency is None


async def test_new_expenses_land_in_the_new_ledger_and_default_to_its_logging_currency(deps):
    await setup_group(deps)
    await dispatch(message_update(update_id=5, chat_id=-42, text="/newledger Tokyo JPY"), deps)

    [reply] = await dispatch(
        message_update(update_id=6, chat_id=-42, text="/equal 6000 ramen @alice @bob"), deps
    )

    tokyo = await ledger_named(deps, "Tokyo")
    async with deps.session_factory() as session:
        expense = (await session.execute(select(Expense))).scalar_one()
    assert expense.ledger_id == tokyo.id
    assert expense.currency == "JPY"  # the ledger's logging default, not the group home EUR
    assert reply.text.startswith("📒 Tokyo •")


async def test_newledger_creates_activates_and_announces(deps):
    await setup_group(deps)

    [reply] = await dispatch(
        message_update(update_id=5, chat_id=-42, text="/newledger Tokyo JPY"), deps
    )

    tokyo = await ledger_named(deps, "Tokyo")
    assert tokyo.status == "open"
    assert tokyo.logging_currency == "JPY"
    assert (await read_group(deps)).active_ledger_id == tokyo.id
    assert "Tokyo" in reply.text
    assert "JPY" in reply.text


async def test_switch_repoints_the_active_ledger_and_announces(deps):
    await setup_group(deps)
    await dispatch(message_update(update_id=5, chat_id=-42, text="/newledger Tokyo JPY"), deps)

    [reply] = await dispatch(
        message_update(update_id=6, chat_id=-42, text="/switch Japan Trip", from_user=BOB), deps
    )

    japan = await ledger_named(deps, "Japan Trip")
    assert (await read_group(deps)).active_ledger_id == japan.id
    assert "Japan Trip" in reply.text
    # the flip is undoable via before-image (§8): the log captures the prior pointer
    async with deps.session_factory() as session:
        action = (
            await session.execute(select(Action).where(Action.kind == "switch_ledger"))
        ).scalar_one()
    tokyo = await ledger_named(deps, "Tokyo")
    assert action.before_image == {"active_ledger_id": tokyo.id}


async def test_switch_to_an_unknown_ledger_is_refused_pointing_at_ledgers(deps):
    await setup_group(deps)

    [reply] = await dispatch(message_update(update_id=5, chat_id=-42, text="/switch Nowhere"), deps)

    assert "No ledger called Nowhere" in reply.text
    assert "/ledgers" in reply.text
    async with deps.session_factory() as session:
        kinds = [a.kind for a in (await session.execute(select(Action))).scalars()]
    assert "switch_ledger" not in kinds  # a refusal writes nothing


async def test_switch_to_the_already_active_ledger_noops_without_an_action(deps):
    await setup_group(deps)

    [reply] = await dispatch(
        message_update(update_id=5, chat_id=-42, text="/switch Japan Trip"), deps
    )

    assert "already the active ledger" in reply.text
    async with deps.session_factory() as session:
        kinds = [a.kind for a in (await session.execute(select(Action))).scalars()]
    assert "switch_ledger" not in kinds


async def test_balances_stay_sealed_to_the_active_ledger_across_switches(deps):
    await setup_group(deps)
    await dispatch(
        message_update(update_id=5, chat_id=-42, text="/equal 60 dinner @alice @bob"), deps
    )
    await dispatch(message_update(update_id=6, chat_id=-42, text="/newledger Tokyo JPY"), deps)

    [fresh] = await dispatch(message_update(update_id=7, chat_id=-42, text="/balance"), deps)
    assert "settled up" in fresh.text  # Tokyo is empty; Japan Trip's debts never leak in (§0.10)

    await dispatch(message_update(update_id=8, chat_id=-42, text="/switch Japan Trip"), deps)
    [back] = await dispatch(message_update(update_id=9, chat_id=-42, text="/balance"), deps)
    assert "Bob owes" in back.text


async def test_archiving_the_active_ledger_repoints_to_the_most_recently_created_open_one(deps):
    await setup_group(deps)  # Japan Trip (oldest, open)
    await dispatch(message_update(update_id=5, chat_id=-42, text="/newledger Tokyo"), deps)
    await dispatch(message_update(update_id=6, chat_id=-42, text="/newledger Osaka"), deps)
    await dispatch(message_update(update_id=7, chat_id=-42, text="/switch Tokyo"), deps)

    [reply] = await dispatch(message_update(update_id=8, chat_id=-42, text="/archive"), deps)

    tokyo = await ledger_named(deps, "Tokyo")
    osaka = await ledger_named(deps, "Osaka")
    assert tokyo.status == "archived"
    assert tokyo.archived_at is not None
    # most-recently-created open ledger, NOT the oldest (ADR-0004)
    assert (await read_group(deps)).active_ledger_id == osaka.id
    assert "Tokyo" in reply.text
    assert "Osaka is now the active ledger" in reply.text


async def test_archiving_the_only_open_ledger_is_refused(deps):
    await setup_group(deps)

    [reply] = await dispatch(message_update(update_id=5, chat_id=-42, text="/archive"), deps)

    assert "only open ledger" in reply.text
    assert "/newledger" in reply.text
    assert (await ledger_named(deps, "Japan Trip")).status == "open"


async def test_archiving_with_outstanding_balances_warns_but_proceeds(deps):
    await setup_group(deps)
    await dispatch(
        message_update(update_id=5, chat_id=-42, text="/equal 60 dinner @alice @bob"), deps
    )
    await dispatch(message_update(update_id=6, chat_id=-42, text="/newledger Tokyo"), deps)
    await dispatch(message_update(update_id=7, chat_id=-42, text="/switch Japan Trip"), deps)

    [reply] = await dispatch(message_update(update_id=8, chat_id=-42, text="/archive"), deps)

    assert (await ledger_named(deps, "Japan Trip")).status == "archived"  # warn, allow (§17)
    assert "⚠️" in reply.text
    assert "outstanding balances" in reply.text


async def test_archiving_a_non_active_ledger_leaves_the_active_pointer_alone(deps):
    await setup_group(deps)
    await dispatch(message_update(update_id=5, chat_id=-42, text="/newledger Tokyo"), deps)

    [reply] = await dispatch(
        message_update(update_id=6, chat_id=-42, text="/archive Japan Trip"), deps
    )

    assert (await ledger_named(deps, "Japan Trip")).status == "archived"
    tokyo = await ledger_named(deps, "Tokyo")
    assert (await read_group(deps)).active_ledger_id == tokyo.id
    assert "is now the active ledger" not in reply.text


async def test_switch_to_an_archived_ledger_is_refused_with_unarchive_guidance(deps):
    await setup_group(deps)
    await dispatch(message_update(update_id=5, chat_id=-42, text="/newledger Tokyo"), deps)
    await dispatch(message_update(update_id=6, chat_id=-42, text="/archive Japan Trip"), deps)

    [reply] = await dispatch(
        message_update(update_id=7, chat_id=-42, text="/switch Japan Trip"), deps
    )

    assert "archived" in reply.text
    assert "/unarchive Japan Trip" in reply.text  # unarchive-then-switch, two steps (§17)
    tokyo = await ledger_named(deps, "Tokyo")
    assert (await read_group(deps)).active_ledger_id == tokyo.id


async def test_unarchive_reopens_without_touching_the_active_pointer(deps):
    await setup_group(deps)
    await dispatch(message_update(update_id=5, chat_id=-42, text="/newledger Tokyo"), deps)
    await dispatch(message_update(update_id=6, chat_id=-42, text="/archive Japan Trip"), deps)

    [reply] = await dispatch(
        message_update(update_id=7, chat_id=-42, text="/unarchive Japan Trip"), deps
    )

    japan = await ledger_named(deps, "Japan Trip")
    assert japan.status == "open"
    assert japan.archived_at is None
    tokyo = await ledger_named(deps, "Tokyo")
    # reopening and activating are separate, deliberate steps (§17, ADR-0004)
    assert (await read_group(deps)).active_ledger_id == tokyo.id
    assert "Japan Trip" in reply.text
    assert "/switch" in reply.text


async def test_unarchiving_an_open_ledger_noops_without_an_action(deps):
    await setup_group(deps)

    [reply] = await dispatch(
        message_update(update_id=5, chat_id=-42, text="/unarchive Japan Trip"), deps
    )

    assert "isn't archived" in reply.text
    async with deps.session_factory() as session:
        kinds = [a.kind for a in (await session.execute(select(Action))).scalars()]
    assert "unarchive_ledger" not in kinds


async def test_currency_changes_the_default_for_new_expenses_without_redenominating(deps):
    await setup_group(deps)  # home EUR
    await dispatch(
        message_update(update_id=5, chat_id=-42, text="/equal 60 dinner @alice @bob"), deps
    )

    [reply] = await dispatch(message_update(update_id=6, chat_id=-42, text="/currency jpy"), deps)
    await dispatch(
        message_update(update_id=7, chat_id=-42, text="/equal 6000 ramen @alice @bob"), deps
    )

    assert "JPY" in reply.text
    assert (await ledger_named(deps, "Japan Trip")).logging_currency == "JPY"
    async with deps.session_factory() as session:
        expenses = (await session.execute(select(Expense).order_by(Expense.id))).scalars().all()
    # the earlier expense keeps its frozen EUR; only the default for NEW ones changed (§3)
    assert [e.currency for e in expenses] == ["EUR", "JPY"]
    # the flip is undoable: the log captured the prior value on the ledger's action
    async with deps.session_factory() as session:
        action = (
            await session.execute(select(Action).where(Action.kind == "set_logging_currency"))
        ).scalar_one()
    assert action.before_image == {"logging_currency": None}
    assert action.ledger_id == (await ledger_named(deps, "Japan Trip")).id


async def latest_action(deps, kind: str) -> Action:
    async with deps.session_factory() as session:
        return (
            await session.execute(
                select(Action).where(Action.kind == kind).order_by(Action.id.desc()).limit(1)
            )
        ).scalar_one()


async def press(deps, action_id: int, direction: str = "undo", update_id: int = 60) -> list:
    return await dispatch(
        callback_update(
            update_id=update_id, chat_id=-42, data=f"v1:{direction}:{action_id}", from_user=BOB
        ),
        deps,
    )


async def test_undoing_an_empty_new_ledger_archives_it_and_restores_the_previous_active(deps):
    await setup_group(deps)
    japan = await ledger_named(deps, "Japan Trip")
    await dispatch(message_update(update_id=5, chat_id=-42, text="/newledger Tokyo JPY"), deps)
    action = await latest_action(deps, "new_ledger")

    [answer, _] = await press(deps, action.id)

    assert "undone" in answer.text.lower()
    tokyo = await ledger_named(deps, "Tokyo")
    assert tokyo.status == "archived"  # the empty shell is archived, not erased (ADR-0004)
    assert (await read_group(deps)).active_ledger_id == japan.id

    [answer2, _] = await press(deps, action.id, "redo", update_id=61)

    assert "redone" in answer2.text.lower()
    tokyo = await ledger_named(deps, "Tokyo")
    assert tokyo.status == "open"
    assert tokyo.archived_at is None
    assert (await read_group(deps)).active_ledger_id == tokyo.id


async def test_undo_of_new_ledger_is_refused_once_it_holds_transactions(deps):
    await setup_group(deps)
    await dispatch(message_update(update_id=5, chat_id=-42, text="/newledger Tokyo JPY"), deps)
    action = await latest_action(deps, "new_ledger")
    await dispatch(
        message_update(update_id=6, chat_id=-42, text="/equal 6000 ramen @alice @bob"), deps
    )

    [answer] = await press(deps, action.id)  # no edit: the button stays

    assert "expenses" in answer.text.lower()
    tokyo = await ledger_named(deps, "Tokyo")
    assert tokyo.status == "open"
    assert (await read_group(deps)).active_ledger_id == tokyo.id
    assert (await latest_action(deps, "new_ledger")).undone_at is None


async def test_undo_of_new_ledger_succeeds_again_once_its_expenses_are_soft_deleted(deps):
    await setup_group(deps)
    japan = await ledger_named(deps, "Japan Trip")
    await dispatch(message_update(update_id=5, chat_id=-42, text="/newledger Tokyo JPY"), deps)
    new_ledger_action = await latest_action(deps, "new_ledger")
    await dispatch(
        message_update(update_id=6, chat_id=-42, text="/equal 6000 ramen @alice @bob"), deps
    )
    expense_action = await latest_action(deps, "add_expense")

    await press(deps, expense_action.id)  # soft-deletes the only expense
    [answer, _] = await press(deps, new_ledger_action.id, update_id=61)

    # only NON-DELETED transactions gate the undo (ADR-0004): the shell is empty again
    assert "undone" in answer.text.lower()
    assert (await ledger_named(deps, "Tokyo")).status == "archived"
    assert (await read_group(deps)).active_ledger_id == japan.id


async def test_archiving_an_already_archived_ledger_noops_without_an_action(deps):
    await setup_group(deps)
    await dispatch(message_update(update_id=5, chat_id=-42, text="/newledger Tokyo"), deps)
    await dispatch(message_update(update_id=6, chat_id=-42, text="/archive Japan Trip"), deps)
    japan = await ledger_named(deps, "Japan Trip")
    first_archived_at = japan.archived_at

    [reply] = await dispatch(
        message_update(update_id=7, chat_id=-42, text="/archive Japan Trip"), deps
    )

    assert "already archived" in reply.text
    japan = await ledger_named(deps, "Japan Trip")
    assert japan.archived_at == first_archived_at  # not overwritten by a double archive
    async with deps.session_factory() as session:
        kinds = [a.kind for a in (await session.execute(select(Action))).scalars()]
    assert kinds.count("archive_ledger") == 1  # the refusal wrote nothing


async def test_undoing_a_new_ledger_whose_previous_active_was_archived_repoints_to_open(deps):
    await setup_group(deps)  # Japan Trip (oldest, open)
    await dispatch(message_update(update_id=5, chat_id=-42, text="/newledger Osaka"), deps)
    await dispatch(message_update(update_id=6, chat_id=-42, text="/newledger Tokyo"), deps)
    action = await latest_action(deps, "new_ledger")  # Tokyo; before-image points at Osaka
    await dispatch(message_update(update_id=7, chat_id=-42, text="/archive Osaka"), deps)

    [answer, _] = await press(deps, action.id)

    assert "undone" in answer.text.lower()
    # the before-image target is archived: repoint by the archive rule, never onto
    # an archived ledger (ADR-0004)
    japan = await ledger_named(deps, "Japan Trip")
    assert (await ledger_named(deps, "Tokyo")).status == "archived"
    assert (await read_group(deps)).active_ledger_id == japan.id


async def test_undoing_the_only_open_new_ledger_is_refused(deps):
    await setup_group(deps)
    await dispatch(message_update(update_id=5, chat_id=-42, text="/newledger Tokyo"), deps)
    action = await latest_action(deps, "new_ledger")
    await dispatch(message_update(update_id=6, chat_id=-42, text="/archive Japan Trip"), deps)

    [answer] = await press(deps, action.id)  # undoing would leave zero open ledgers

    assert "only open ledger" in answer.text
    tokyo = await ledger_named(deps, "Tokyo")
    assert tokyo.status == "open"
    assert (await read_group(deps)).active_ledger_id == tokyo.id
    assert (await latest_action(deps, "new_ledger")).undone_at is None


async def test_undo_and_redo_of_switch_toggle_the_active_pointer(deps):
    await setup_group(deps)
    await dispatch(message_update(update_id=5, chat_id=-42, text="/newledger Tokyo"), deps)
    await dispatch(message_update(update_id=6, chat_id=-42, text="/switch Japan Trip"), deps)
    action = await latest_action(deps, "switch_ledger")
    tokyo = await ledger_named(deps, "Tokyo")
    japan = await ledger_named(deps, "Japan Trip")

    await press(deps, action.id)
    assert (await read_group(deps)).active_ledger_id == tokyo.id

    await press(deps, action.id, "redo", update_id=61)
    assert (await read_group(deps)).active_ledger_id == japan.id


async def test_undoing_a_switch_whose_previous_ledger_was_archived_repoints_deterministically(
    deps,
):
    await setup_group(deps)  # Japan Trip
    await dispatch(message_update(update_id=5, chat_id=-42, text="/newledger Tokyo"), deps)
    await dispatch(message_update(update_id=6, chat_id=-42, text="/newledger Osaka"), deps)
    await dispatch(message_update(update_id=7, chat_id=-42, text="/switch Japan Trip"), deps)
    action = await latest_action(deps, "switch_ledger")
    await dispatch(message_update(update_id=8, chat_id=-42, text="/archive Osaka"), deps)

    [answer, _] = await press(deps, action.id)  # before-image points at now-archived Osaka

    assert "undone" in answer.text.lower()
    tokyo = await ledger_named(deps, "Tokyo")
    # the most-recently-created OPEN ledger, never the archived before-image (ADR-0004)
    assert (await read_group(deps)).active_ledger_id == tokyo.id


async def test_redoing_a_switch_to_a_now_archived_ledger_is_refused(deps):
    await setup_group(deps)
    await dispatch(message_update(update_id=5, chat_id=-42, text="/newledger Tokyo"), deps)
    await dispatch(message_update(update_id=6, chat_id=-42, text="/switch Japan Trip"), deps)
    action = await latest_action(deps, "switch_ledger")
    await press(deps, action.id)  # back on Tokyo
    await dispatch(message_update(update_id=7, chat_id=-42, text="/archive Japan Trip"), deps)

    [answer] = await press(deps, action.id, "redo", update_id=61)

    assert "archived" in answer.text.lower()
    tokyo = await ledger_named(deps, "Tokyo")
    assert (await read_group(deps)).active_ledger_id == tokyo.id
    assert (await latest_action(deps, "switch_ledger")).undone_at is not None  # still undone


async def test_undo_of_archive_reopens_and_restores_the_pointer_and_redo_rearchives(deps):
    await setup_group(deps)
    await dispatch(message_update(update_id=5, chat_id=-42, text="/newledger Tokyo"), deps)
    await dispatch(message_update(update_id=6, chat_id=-42, text="/switch Japan Trip"), deps)
    await dispatch(message_update(update_id=7, chat_id=-42, text="/archive"), deps)
    action = await latest_action(deps, "archive_ledger")
    japan = await ledger_named(deps, "Japan Trip")
    tokyo = await ledger_named(deps, "Tokyo")
    assert (await read_group(deps)).active_ledger_id == tokyo.id

    await press(deps, action.id)

    japan = await ledger_named(deps, "Japan Trip")
    assert japan.status == "open"
    assert japan.archived_at is None
    assert (await read_group(deps)).active_ledger_id == japan.id

    await press(deps, action.id, "redo", update_id=61)

    japan = await ledger_named(deps, "Japan Trip")
    assert japan.status == "archived"
    assert (await read_group(deps)).active_ledger_id == tokyo.id


async def test_redoing_an_archive_that_would_leave_no_open_ledger_is_refused(deps):
    await setup_group(deps)
    await dispatch(message_update(update_id=5, chat_id=-42, text="/newledger Tokyo"), deps)
    await dispatch(message_update(update_id=6, chat_id=-42, text="/archive Japan Trip"), deps)
    action = await latest_action(deps, "archive_ledger")
    await press(deps, action.id)  # Japan Trip reopened
    await dispatch(message_update(update_id=7, chat_id=-42, text="/archive Tokyo"), deps)

    [answer] = await press(deps, action.id, "redo", update_id=61)  # would leave none open

    assert "only open ledger" in answer.text
    assert (await ledger_named(deps, "Japan Trip")).status == "open"
    japan = await ledger_named(deps, "Japan Trip")
    assert (await read_group(deps)).active_ledger_id == japan.id


async def test_undo_of_unarchive_rearchives_and_redo_reopens(deps):
    await setup_group(deps)
    await dispatch(message_update(update_id=5, chat_id=-42, text="/newledger Tokyo"), deps)
    await dispatch(message_update(update_id=6, chat_id=-42, text="/archive Japan Trip"), deps)
    await dispatch(message_update(update_id=7, chat_id=-42, text="/unarchive Japan Trip"), deps)
    action = await latest_action(deps, "unarchive_ledger")

    await press(deps, action.id)

    japan = await ledger_named(deps, "Japan Trip")
    assert japan.status == "archived"
    assert japan.archived_at is not None  # the before-image timestamp came back

    await press(deps, action.id, "redo", update_id=61)

    japan = await ledger_named(deps, "Japan Trip")
    assert japan.status == "open"
    assert japan.archived_at is None


async def test_undoing_an_unarchive_of_the_now_active_ledger_is_refused(deps):
    await setup_group(deps)
    await dispatch(message_update(update_id=5, chat_id=-42, text="/newledger Tokyo"), deps)
    await dispatch(message_update(update_id=6, chat_id=-42, text="/archive Japan Trip"), deps)
    await dispatch(message_update(update_id=7, chat_id=-42, text="/unarchive Japan Trip"), deps)
    action = await latest_action(deps, "unarchive_ledger")
    await dispatch(message_update(update_id=8, chat_id=-42, text="/switch Japan Trip"), deps)

    [answer] = await press(deps, action.id)  # re-archiving the ACTIVE ledger: invariant

    assert "active" in answer.text.lower()
    assert (await ledger_named(deps, "Japan Trip")).status == "open"
    assert (await latest_action(deps, "unarchive_ledger")).undone_at is None


async def test_undo_and_redo_of_currency_toggle_the_logging_default(deps):
    await setup_group(deps)
    await dispatch(message_update(update_id=5, chat_id=-42, text="/currency JPY"), deps)
    await dispatch(message_update(update_id=6, chat_id=-42, text="/currency KRW"), deps)
    action = await latest_action(deps, "set_logging_currency")

    await press(deps, action.id)
    assert (await ledger_named(deps, "Japan Trip")).logging_currency == "JPY"

    await press(deps, action.id, "redo", update_id=61)
    assert (await ledger_named(deps, "Japan Trip")).logging_currency == "KRW"


async def test_undo_of_currency_lands_on_the_actions_ledger_even_after_a_switch(deps):
    await setup_group(deps)
    await dispatch(message_update(update_id=5, chat_id=-42, text="/currency JPY"), deps)
    action = await latest_action(deps, "set_logging_currency")
    await dispatch(message_update(update_id=6, chat_id=-42, text="/newledger Tokyo KRW"), deps)

    await press(deps, action.id)  # active is Tokyo now; the undo targets Japan Trip

    assert (await ledger_named(deps, "Japan Trip")).logging_currency is None
    assert (await ledger_named(deps, "Tokyo")).logging_currency == "KRW"  # untouched


async def test_ledgers_labels_archived_ledgers_and_does_not_mark_them_active(deps):
    await setup_group(deps)
    await dispatch(message_update(update_id=5, chat_id=-42, text="/newledger Tokyo"), deps)
    await dispatch(message_update(update_id=6, chat_id=-42, text="/archive Japan Trip"), deps)

    [reply] = await dispatch(message_update(update_id=7, chat_id=-42, text="/ledgers"), deps)

    japan_line = next(line for line in reply.text.splitlines() if "Japan Trip" in line)
    assert "archived" in japan_line
    assert "active" not in japan_line
    tokyo_line = next(line for line in reply.text.splitlines() if "Tokyo" in line)
    assert "active" in tokyo_line


async def test_duplicate_ledger_names_refuse_the_switch_and_ids_resolve_it(deps):
    await setup_group(deps)
    await dispatch(message_update(update_id=5, chat_id=-42, text="/newledger Tokyo"), deps)
    await dispatch(message_update(update_id=6, chat_id=-42, text="/newledger Tokyo"), deps)

    [refusal] = await dispatch(message_update(update_id=7, chat_id=-42, text="/switch Tokyo"), deps)
    assert "More than one ledger" in refusal.text

    # /ledgers shows each ledger's id so the guidance is actionable
    [listing] = await dispatch(message_update(update_id=8, chat_id=-42, text="/ledgers"), deps)
    async with deps.session_factory() as session:
        first_tokyo = (
            await session.execute(
                select(Ledger).where(Ledger.name == "Tokyo").order_by(Ledger.id).limit(1)
            )
        ).scalar_one()
    assert f"#{first_tokyo.id}" in listing.text

    [switched] = await dispatch(
        message_update(update_id=9, chat_id=-42, text=f"/switch #{first_tokyo.id}"), deps
    )
    assert "Switched to Tokyo" in switched.text
    assert (await read_group(deps)).active_ledger_id == first_tokyo.id


async def test_ledgers_lists_every_ledger_marking_the_active_one_and_writes_no_action(deps):
    await setup_group(deps)  # onboarding ledger "Japan Trip"
    await dispatch(message_update(update_id=5, chat_id=-42, text="/newledger Tokyo JPY"), deps)

    [reply] = await dispatch(message_update(update_id=6, chat_id=-42, text="/ledgers"), deps)

    assert "Japan Trip" in reply.text
    tokyo_line = next(line for line in reply.text.splitlines() if "Tokyo" in line)
    assert "active" in tokyo_line
    assert reply.reply_markup is None  # a read: no Undo button (§0.7)
    async with deps.session_factory() as session:
        actions = (await session.execute(select(Action))).scalars().all()
    assert [a.kind for a in actions] == ["set_home_currency", "new_ledger"]  # no read row
