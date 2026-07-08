"""The /members roster (#22, ADR-0011): a slash-only container-inspection read."""

from sqlalchemy import select

from expensir.core.handler import dispatch
from expensir.db.models import Action
from tests.factories import (
    bot_added_update,
    left_member_update,
    message_update,
    user,
)

ALICE = user(1001, "Alice Tan", "alice")
BOB = user(1002, "Bob", None)
PRIYA = user(1003, "Priya Menon", "priya")


async def three_member_group(deps, chat_id: int = -42) -> None:
    await dispatch(bot_added_update(chat_id=chat_id, by=ALICE), deps)
    await dispatch(message_update(update_id=90, chat_id=chat_id, text="hi", from_user=BOB), deps)
    await dispatch(message_update(update_id=91, chat_id=chat_id, text="yo", from_user=PRIYA), deps)


async def test_members_lists_current_members_with_a_count_header_and_the_caller_marked(deps):
    await three_member_group(deps)

    [reply] = await dispatch(
        message_update(update_id=5, chat_id=-42, text="/members", from_user=ALICE), deps
    )

    assert reply.text.splitlines()[0] == "Members (3):"
    alice_line = next(line for line in reply.text.splitlines() if "Alice Tan" in line)
    assert alice_line.endswith("— you")
    # only the caller is marked
    assert reply.text.count("— you") == 1


async def test_members_shows_at_username_when_present_and_a_bare_name_when_absent(deps):
    await three_member_group(deps)

    [reply] = await dispatch(
        message_update(update_id=5, chat_id=-42, text="/members", from_user=PRIYA), deps
    )

    alice_line = next(line for line in reply.text.splitlines() if "Alice Tan" in line)
    bob_line = next(line for line in reply.text.splitlines() if "Bob" in line)
    assert "(@alice)" in alice_line
    assert "@" not in bob_line  # Bob has no username: no handle, no stray @


async def test_members_are_sorted_alphabetically_case_insensitively(deps):
    # registration order (by id) deliberately differs from alphabetical order, and
    # a naive ASCII sort would put uppercase "Bob" ahead of lowercase "alice"
    zoe = user(2001, "zoe", "zoe")
    bob = user(2002, "Bob", "bob")
    alice = user(2003, "alice", "alice")
    await dispatch(bot_added_update(chat_id=-42, by=zoe), deps)
    await dispatch(message_update(update_id=90, chat_id=-42, text="hi", from_user=bob), deps)
    await dispatch(message_update(update_id=91, chat_id=-42, text="yo", from_user=alice), deps)

    [reply] = await dispatch(
        message_update(update_id=5, chat_id=-42, text="/members", from_user=bob), deps
    )

    names = [line[2:].split(" (")[0].split(" —")[0] for line in reply.text.splitlines()[1:]]
    assert names == ["alice", "Bob", "zoe"]


async def test_departed_members_are_not_listed(deps):
    await three_member_group(deps)
    # Bob leaves: excluded from "everyone", so absent from the roster (§11)
    await dispatch(left_member_update(update_id=92, chat_id=-42, member=BOB), deps)

    [reply] = await dispatch(
        message_update(update_id=5, chat_id=-42, text="/members", from_user=ALICE), deps
    )

    assert reply.text.splitlines()[0] == "Members (2):"
    assert "Bob" not in reply.text
    assert "Alice Tan" in reply.text
    assert "Priya Menon" in reply.text


async def test_members_is_a_read_writing_no_action_and_carrying_no_buttons(deps):
    await three_member_group(deps)

    [reply] = await dispatch(
        message_update(update_id=5, chat_id=-42, text="/members", from_user=ALICE), deps
    )

    assert reply.reply_markup is None  # no Confirm/Undo/buttons on a read (§0.7)
    async with deps.session_factory() as session:
        actions = (await session.execute(select(Action))).scalars().all()
    assert actions == []  # a read appends no action row


async def test_members_with_arguments_is_rejected_with_usage_and_writes_nothing(deps):
    await three_member_group(deps)

    [reply] = await dispatch(
        message_update(update_id=5, chat_id=-42, text="/members everyone", from_user=ALICE), deps
    )

    assert "Usage" in reply.text
    assert "Members (" not in reply.text  # rejected: no roster rendered
    async with deps.session_factory() as session:
        actions = (await session.execute(select(Action))).scalars().all()
    assert actions == []
