"""Identity, registration & onboarding (§11). No ghosts: members exist only once seen.

Platform-agnostic: callers at the transport-aware edge extract primitives from the
Telegram update; nothing here imports Telegram shapes.
"""

from typing import Literal

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from expensir.db.models import Action, Expense, Group, GroupMember, Identity, Ledger, User, utcnow
from expensir.domain.errors import AmbiguousExpense, AmbiguousRef, Rejection


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
        if ref.startswith("id:"):
            # a pinned reference (issue #14): resolved by id, valid even after
            # the member departs — pinning survives departure (§10)
            member = await _member_by_id(session, group_id, ref.removeprefix("id:"))
            if member is None:
                unknown.append(ref)
            else:
                resolved[ref] = member
            continue
        member = await _member_by_ref(session, group_id, ref)
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


async def _member_by_ref(session: AsyncSession, group_id: int, ref: str) -> User | None:
    """One current member for a reference (§11): '@username' matches exactly; a
    bare name (NL) also matches display names — full name or first name,
    case-insensitive. Ambiguity rejects; the pick-list arrives in slice 13."""
    matches = {u.id: u for u in await _members_by_username(session, group_id, ref.lstrip("@"))}
    if not ref.startswith("@"):
        for member in await _members_by_display_name(session, group_id, ref):
            matches[member.id] = member
    if len(matches) > 1:
        # stale stored usernames can collide, and given names repeat: never
        # guess — proposals render the candidates as a pick-list (§10, §13)
        raise AmbiguousRef(ref, sorted(matches.values(), key=lambda u: u.id))
    return next(iter(matches.values()), None)


def ambiguous_guidance(ref: str) -> str:
    """The fallback for paths that cannot render a pick-list (reads, slash for
    now): same wording the pre-pick-list rejection used."""
    return (
        f"🤔 More than one member here matches {ref} — use their @username so I know who you mean."
    )


async def usernames_of(session: AsyncSession, user_ids: list[int]) -> dict[int, str | None]:
    rows = await session.execute(
        select(Identity.user_id, Identity.username).where(
            Identity.platform == "telegram", Identity.user_id.in_(user_ids)
        )
    )
    return {user_id: username for user_id, username in rows.all()}


async def _member_by_id(session: AsyncSession, group_id: int, raw_id: str) -> User | None:
    """One member (current OR departed) of this group by pinned user id."""
    if not raw_id.isdigit():
        return None  # a malformed pin is an unknown ref, never a crash
    return (
        await session.execute(
            select(User)
            .join(GroupMember, GroupMember.user_id == User.id)
            .where(GroupMember.group_id == group_id, User.id == int(raw_id))
        )
    ).scalar_one_or_none()


async def _members_by_username(session: AsyncSession, group_id: int, username: str) -> list[User]:
    return list(
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


async def _members_by_display_name(session: AsyncSession, group_id: int, name: str) -> list[User]:
    # groups are small: filter registered members in Python rather than fight
    # cross-DB first-token SQL
    wanted = name.lower()
    return [
        member
        for member in await registered_members(session, group_id)
        if member.display_name.lower() == wanted
        or member.display_name.lower().split()[:1] == [wanted]
    ]


async def resolve_expense_id(
    session: AsyncSession,
    platform_chat_id: int,
    reply_message_id: int | None,
    explicit_id: int | None,
) -> int | None:
    """Expense reference resolution (§11): reply-to-target primary, visible #id fallback.

    A reply to a bot result message resolves via the action's stored result
    message id. When both are given they must agree — deleting or editing the
    wrong expense is worse than asking again. None means nothing resolved.
    """
    replied = None
    if reply_message_id is not None:
        replied = await _expense_id_of_result_message(session, platform_chat_id, reply_message_id)
    if replied is not None and explicit_id is not None and replied != explicit_id:
        raise Rejection(
            f"🤔 That reply points at #{replied} but you wrote #{explicit_id} — "
            "I can't tell which you mean, so nothing was changed."
        )
    return replied if replied is not None else explicit_id


async def _expense_id_of_result_message(
    session: AsyncSession, platform_chat_id: int, message_id: int
) -> int | None:
    """Map a bot result message back to the expense it concerns (§11).

    add_expense results map via created_by_action_id; delete/edit results carry
    the expense id in their intent — replying to those keeps working too.
    """
    action = (
        await session.execute(
            select(Action).where(
                Action.result_chat_id == platform_chat_id,
                Action.result_message_id == message_id,
            )
        )
    ).scalar_one_or_none()
    if action is None:
        return None
    if action.kind == "add_expense":
        return (
            await session.execute(
                select(Expense.id).where(Expense.created_by_action_id == action.id)
            )
        ).scalar_one_or_none()
    if action.kind in ("delete_expense", "edit_expense"):
        expense_id = action.intent_json["expense_id"]
        assert isinstance(expense_id, int)
        return expense_id
    return None


async def match_expenses(session: AsyncSession, ledger_id: int, query: str) -> list[Expense]:
    """§11 tertiary tier: deterministic CPU matching, never the LLM (issue #14
    grill). Every query token must appear in the description, case-insensitive;
    candidates come back newest first."""
    expenses = (
        await session.execute(
            select(Expense)
            .where(Expense.ledger_id == ledger_id, Expense.deleted_at.is_(None))
            .order_by(Expense.id.desc())
        )
    ).scalars()
    tokens = query.lower().split()
    return [e for e in expenses if all(token in e.description.lower() for token in tokens)]


async def resolve_expense_match(session: AsyncSession, ledger_id: int, query: str) -> Expense:
    """One expense for a descriptive reference (§11): unique or nothing."""
    candidates = await match_expenses(session, ledger_id, query)
    if not candidates:
        raise Rejection(
            f"🤷 Nothing here matches “{query}” — reply to the expense's result "
            "message or give its #id."
        )
    if len(candidates) > 1:
        raise AmbiguousExpense(query, candidates)
    return candidates[0]


async def display_names(session: AsyncSession, user_ids: list[int]) -> dict[int, str]:
    users = (await session.execute(select(User).where(User.id.in_(user_ids)))).scalars()
    return {u.id: u.display_name for u in users}


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


async def registered_members_with_usernames(
    session: AsyncSession, group_id: int
) -> list[tuple[User, str | None]]:
    """Current members (§11) paired with their Telegram @username (None when unset —
    Telegram allows no username). The inner join is safe: no ghosts means every
    member has an identity row (§5, §11). For the /members roster (#22)."""
    rows = (
        await session.execute(
            select(User, Identity.username)
            .join(GroupMember, GroupMember.user_id == User.id)
            .join(Identity, Identity.user_id == User.id)
            .where(
                GroupMember.group_id == group_id,
                GroupMember.left_at.is_(None),
                Identity.platform == "telegram",
            )
            .order_by(User.id)
        )
    ).all()
    return [(member, username) for member, username in rows]


async def _identity_of(session: AsyncSession, platform_user_id: int) -> Identity | None:
    return (
        await session.execute(
            select(Identity).where(
                Identity.platform == "telegram",
                Identity.platform_user_id == platform_user_id,
            )
        )
    ).scalar_one_or_none()


async def _membership_of(session: AsyncSession, group_id: int, user_id: int) -> GroupMember | None:
    return (
        await session.execute(
            select(GroupMember).where(
                GroupMember.group_id == group_id, GroupMember.user_id == user_id
            )
        )
    ).scalar_one_or_none()


async def mark_left(session: AsyncSession, group_id: int, platform_user_id: int) -> None:
    """Leaving (§11): set left_at. Balances persist and history stays intact, but the
    member drops out of "everyone" and is no longer nameable until reactivation.

    A never-registered leaver is ignored — registering someone as they leave would
    mint a departed member with no history (no ghosts).
    """
    identity = await _identity_of(session, platform_user_id)
    if identity is None:
        return
    membership = await _membership_of(session, group_id, identity.user_id)
    if membership is not None:
        membership.left_at = utcnow()


SetupOutcome = Literal["registered", "already", "departed"]


async def register_setup_target(
    session: AsyncSession,
    group_id: int,
    platform_user_id: int,
    display_name: str,
    username: str | None,
) -> tuple[User, SetupOutcome]:
    """Register one /setup target from SNAPSHOT data (§11).

    Snapshots seed unknown people but never touch an existing member — no identity
    refresh, no reactivation; only the member's own live interaction does those.
    Returns the member and which per-entry reply line applies.
    """
    identity = await _identity_of(session, platform_user_id)
    if identity is not None:
        membership = await _membership_of(session, group_id, identity.user_id)
        if membership is not None:
            member = await session.get_one(User, identity.user_id)
            # reactivation is strictly self-triggered (§11): a departed member stays
            # departed no matter who names them in a /setup
            return member, "departed" if membership.left_at is not None else "already"
    member = await register_member(session, group_id, platform_user_id, display_name, username)
    return member, "registered"


async def register_member(
    session: AsyncSession,
    group_id: int,
    platform_user_id: int,
    display_name: str,
    username: str | None,
) -> User:
    """Register the author of an interaction: users + identities + group_members rows."""
    identity = await _identity_of(session, platform_user_id)
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
            identity = await _identity_of(session, platform_user_id)
            assert identity is not None  # the loser reads the winner's row
            member = await session.get_one(User, identity.user_id)
    else:
        member = await session.get_one(User, identity.user_id)
        # identity refresh (§11): every caller passes LIVE User data (the update's own
        # `from` or a new_chat_members entry — snapshots go through register_setup_target
        # instead), so stored names track reality and @resolution never goes stale
        identity.username = username
        member.display_name = display_name

    membership = await _membership_of(session, group_id, member.id)
    if membership is None:
        try:
            async with session.begin_nested():
                session.add(GroupMember(group_id=group_id, user_id=member.id))
                await session.flush()
        except IntegrityError:
            pass  # a concurrent update already created the membership
    elif membership.left_at is not None:
        # reactivation (§11): a departed member re-joining or interacting is back in
        # "everyone" — same row, balances intact. Lifecycle event, no action row.
        membership.left_at = None
    return member
