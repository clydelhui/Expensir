"""Identity, registration & onboarding (§11). No ghosts: members exist only once seen.

Platform-agnostic: callers at the transport-aware edge extract primitives from the
Telegram update; nothing here imports Telegram shapes.
"""

from sqlalchemy import Select, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from expensir.db.models import Group, GroupMember, Identity, Ledger, User
from expensir.domain.errors import Rejection


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


async def resolve_refs(
    session: AsyncSession, group_id: int, refs: list[str], actor: User | None
) -> dict[str, User]:
    """Resolve refs to REGISTERED members; any unknown rejects the whole intent (§0.9, §11).

    "@username" is exact (case-insensitive); "me" is the interaction's author.
    """
    resolved: dict[str, User] = {}
    unknown: list[str] = []
    for ref in refs:
        if ref == "me" and actor is not None:
            resolved[ref] = actor
            continue
        member = await _member_by_username(session, group_id, ref.removeprefix("@"))
        if member is None:
            unknown.append(ref)
        else:
            resolved[ref] = member
    if unknown:
        raise Rejection(
            f"🚫 I don't know {join_refs(unknown)} yet — nothing was recorded. "
            "They need to send a message here once, or someone can reply to one of "
            "their messages with /setup; then try again."
        )
    return resolved


def join_refs(refs: list[str]) -> str:
    return ", ".join(ref if ref.startswith("@") else f"@{ref}" for ref in refs)


async def _member_by_username(session: AsyncSession, group_id: int, username: str) -> User | None:
    members = list(
        (
            await session.execute(
                select(User)
                .join(Identity, Identity.user_id == User.id)
                .join(GroupMember, GroupMember.user_id == User.id)
                .where(
                    Identity.platform == "telegram",
                    func.lower(Identity.username) == username.lower(),
                    GroupMember.group_id == group_id,
                    GroupMember.left_at.is_(None),
                )
            )
        ).scalars()
    )
    if len(members) > 1:
        # Telegram reassigns freed handles and stored usernames can go stale, so two
        # members may share one; stopgap until the pick-list slice (§10, §13)
        raise Rejection(
            f"🤔 More than one member here matches @{username} — I can't tell who you "
            "mean. Ask them to send a message so I can tell them apart, then try again."
        )
    return members[0] if members else None


async def registered_members(session: AsyncSession, group_id: int) -> list[User]:
    """Everyone currently registered here (§11): the meaning of empty participants."""
    return list(
        (
            await session.execute(
                select(User)
                .join(GroupMember, GroupMember.user_id == User.id)
                .where(GroupMember.group_id == group_id, GroupMember.left_at.is_(None))
                .order_by(User.id)
            )
        ).scalars()
    )


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
