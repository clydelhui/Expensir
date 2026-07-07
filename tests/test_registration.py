from sqlalchemy import select

from expensir.core.handler import dispatch
from expensir.db.models import ExpenseSplit, Group, GroupMember, Identity, User
from tests.factories import (
    bot_added_update,
    joined_members_update,
    left_member_update,
    message_update,
    user,
)

ALICE = user(1001, "Alice", "alice")
BOB = user(1002, "Bob", "bob")


async def alice_and_bob_group(deps, chat_id: int = -42) -> None:
    await dispatch(bot_added_update(chat_id=chat_id, by=ALICE), deps)
    await dispatch(
        message_update(update_id=90, chat_id=chat_id, text="/homecurrency EUR", from_user=ALICE),
        deps,
    )
    await dispatch(message_update(update_id=91, chat_id=chat_id, text="hello", from_user=BOB), deps)


async def read_membership(deps, platform_user_id: int) -> GroupMember:
    async with deps.session_factory() as session:
        identity = (
            await session.execute(
                select(Identity).where(Identity.platform_user_id == platform_user_id)
            )
        ).scalar_one()
        return (
            await session.execute(
                select(GroupMember).where(GroupMember.user_id == identity.user_id)
            )
        ).scalar_one()


async def test_member_who_added_the_bot_is_registered(deps):
    alice = user(user_id=7, first_name="Alice", username="alice")
    await dispatch(bot_added_update(chat_id=-42, by=alice), deps)

    async with deps.session_factory() as session:
        registered = (await session.execute(select(User))).scalar_one()
        assert registered.display_name == "Alice"

        identity = (await session.execute(select(Identity))).scalar_one()
        assert identity.user_id == registered.id
        assert identity.platform == "telegram"
        assert identity.platform_user_id == 7
        assert identity.username == "alice"

        group = (await session.execute(select(Group))).scalar_one()
        membership = (await session.execute(select(GroupMember))).scalar_one()
        assert membership.group_id == group.id
        assert membership.user_id == registered.id
        assert membership.left_at is None


async def test_first_interaction_auto_registers_the_author(deps):
    await dispatch(bot_added_update(chat_id=-42, by=user(user_id=7)), deps)

    bob = user(user_id=8, first_name="Bob", username="bob")
    await dispatch(message_update(update_id=2, chat_id=-42, text="/start", from_user=bob), deps)

    async with deps.session_factory() as session:
        identities = (await session.execute(select(Identity))).scalars().all()
        assert {i.platform_user_id for i in identities} == {7, 8}
        memberships = (await session.execute(select(GroupMember))).scalars().all()
        assert len(memberships) == 2


async def test_leaving_departs_the_member_but_keeps_their_history(deps):
    """Leaving (§11): left_at set, no reply; departed members drop out of "everyone"
    and are no longer nameable — strict departure, decided in slice 11 grilling."""
    await alice_and_bob_group(deps)

    out = await dispatch(left_member_update(update_id=4, chat_id=-42, member=BOB), deps)
    assert out == []  # a lifecycle event, not a conversation

    membership = await read_membership(deps, 1002)
    assert membership.left_at is not None

    # "everyone" now means Alice alone: an empty-participants /equal splits to just her
    await dispatch(
        message_update(update_id=5, chat_id=-42, text="/equal 60 dinner", from_user=ALICE), deps
    )
    async with deps.session_factory() as session:
        splits = (await session.execute(select(ExpenseSplit))).scalars().all()
        assert len(splits) == 1

    # and Bob can no longer be named in new transactions
    [rejected] = await dispatch(
        message_update(update_id=6, chat_id=-42, text="/equal 30 taxi @bob", from_user=ALICE),
        deps,
    )
    assert "@bob" in rejected.text
    assert "don't know" in rejected.text


async def test_a_never_registered_leaver_is_ignored(deps):
    """No ghosts (§11): registering someone as they leave would mint a departed
    member with no history."""
    await dispatch(bot_added_update(chat_id=-42, by=ALICE), deps)

    ghost = user(1077, "Ghost", "ghost")
    out = await dispatch(left_member_update(update_id=4, chat_id=-42, member=ghost, by=ALICE), deps)
    assert out == []

    async with deps.session_factory() as session:
        identities = (await session.execute(select(Identity))).scalars().all()
        assert {i.platform_user_id for i in identities} == {1001}  # only Alice


async def test_rejoining_reactivates_a_departed_member(deps):
    """Reactivation (§11): same membership row, balances intact, back in "everyone"."""
    await alice_and_bob_group(deps)
    await dispatch(left_member_update(update_id=4, chat_id=-42, member=BOB), deps)

    out = await dispatch(
        joined_members_update(update_id=5, chat_id=-42, members=[BOB], by=ALICE), deps
    )
    assert out == []  # a lifecycle event, like the original join

    membership = await read_membership(deps, 1002)
    assert membership.left_at is None

    # back in "everyone": an empty-participants /equal splits between both again
    await dispatch(
        message_update(update_id=6, chat_id=-42, text="/equal 60 dinner", from_user=ALICE), deps
    )
    async with deps.session_factory() as session:
        splits = (await session.execute(select(ExpenseSplit))).scalars().all()
        assert len(splits) == 2


async def test_new_joiners_auto_register_and_bots_are_skipped(deps):
    await dispatch(bot_added_update(chat_id=-42, by=ALICE), deps)

    carol = user(1003, "Carol", "carol")
    other_bot = {"id": 555, "is_bot": True, "first_name": "OtherBot", "username": "other_bot"}
    out = await dispatch(
        joined_members_update(update_id=5, chat_id=-42, members=[carol, other_bot], by=ALICE),
        deps,
    )
    assert out == []

    async with deps.session_factory() as session:
        identities = (await session.execute(select(Identity))).scalars().all()
        assert {i.platform_user_id for i in identities} == {1001, 1003}  # no bot rows


async def test_a_departed_members_own_interaction_reactivates_them(deps):
    await alice_and_bob_group(deps)
    await dispatch(left_member_update(update_id=4, chat_id=-42, member=BOB), deps)

    await dispatch(message_update(update_id=5, chat_id=-42, text="i'm back", from_user=BOB), deps)

    membership = await read_membership(deps, 1002)
    assert membership.left_at is None


async def test_a_username_change_is_picked_up_from_the_next_interaction(deps):
    """Identity refresh (§11): live User data keeps @-resolution current — the new
    username resolves, the stale one stops matching."""
    await alice_and_bob_group(deps)

    renamed = user(1002, "Bobby", "bobby")  # same account, new username + display name
    await dispatch(
        message_update(update_id=5, chat_id=-42, text="hello again", from_user=renamed), deps
    )

    async with deps.session_factory() as session:
        identity = (
            await session.execute(select(Identity).where(Identity.platform_user_id == 1002))
        ).scalar_one()
        assert identity.username == "bobby"
        member = (
            await session.execute(select(User).where(User.id == identity.user_id))
        ).scalar_one()
        assert member.display_name == "Bobby"

    # the new handle resolves in a transaction...
    [committed] = await dispatch(
        message_update(
            update_id=6, chat_id=-42, text="/equal 60 dinner @alice @bobby", from_user=ALICE
        ),
        deps,
    )
    assert "Bobby" in committed.text

    # ...and the stale one no longer matches anyone
    [rejected] = await dispatch(
        message_update(update_id=7, chat_id=-42, text="/equal 30 taxi @bob", from_user=ALICE),
        deps,
    )
    assert "@bob" in rejected.text
    assert "don't know" in rejected.text
