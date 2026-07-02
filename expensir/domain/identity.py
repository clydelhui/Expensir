"""Identity, registration & onboarding (§11). No ghosts: members exist only once seen.

Platform-agnostic: callers at the transport-aware edge extract primitives from the
Telegram update; nothing here imports Telegram shapes.
"""

from sqlalchemy import Select, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from expensir.db.models import Group, GroupMember, Identity, Ledger, User


async def ensure_group(session: AsyncSession, platform_chat_id: int, title: str | None) -> Group:
    """Get or create the group and its first ledger (named after the group, fallback 'General').

    Telegram delivers a bot-add as multiple concurrent updates, so losing the
    insert race to another handler is normal: recover by re-reading the winner's row.
    """
    group = (
        await session.execute(select(Group).where(Group.platform_chat_id == platform_chat_id))
    ).scalar_one_or_none()
    if group is not None:
        return group

    name = title or "General"
    try:
        async with session.begin_nested():
            group = Group(platform_chat_id=platform_chat_id, name=name)
            session.add(group)
            await session.flush()
            ledger = Ledger(group_id=group.id, name=name)
            session.add(ledger)
            await session.flush()
            group.active_ledger_id = ledger.id
        return group
    except IntegrityError:
        return (
            await session.execute(select(Group).where(Group.platform_chat_id == platform_chat_id))
        ).scalar_one()


async def register_member(
    session: AsyncSession,
    group_id: int,
    platform_user_id: int,
    display_name: str,
    username: str | None,
) -> User:
    """Register the author of an interaction: users + identities + group_members rows."""

    def identity_query() -> Select[tuple[Identity]]:
        return select(Identity).where(
            Identity.platform == "telegram",
            Identity.platform_user_id == platform_user_id,
        )

    identity = (await session.execute(identity_query())).scalar_one_or_none()
    if identity is None:
        try:
            async with session.begin_nested():
                member = User(display_name=display_name)
                session.add(member)
                await session.flush()
                identity = Identity(
                    user_id=member.id,
                    platform="telegram",
                    platform_user_id=platform_user_id,
                    username=username,
                )
                session.add(identity)
                await session.flush()
        except IntegrityError:  # lost the race to a concurrent update for the same person
            identity = (await session.execute(identity_query())).scalar_one()
            member = await session.get_one(User, identity.user_id)
    else:
        member = await session.get_one(User, identity.user_id)

    membership = (
        await session.execute(
            select(GroupMember).where(
                GroupMember.group_id == group_id, GroupMember.user_id == member.id
            )
        )
    ).scalar_one_or_none()
    if membership is None:
        try:
            async with session.begin_nested():
                session.add(GroupMember(group_id=group_id, user_id=member.id))
                await session.flush()
        except IntegrityError:
            pass  # a concurrent update already created the membership
    return member
