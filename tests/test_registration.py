from sqlalchemy import select

from expensir.core.handler import dispatch
from expensir.db.models import Group, GroupMember, Identity, User
from tests.factories import bot_added_update, message_update, user


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
