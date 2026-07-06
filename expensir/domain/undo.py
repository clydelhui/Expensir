"""Undo / redo (§9): operates on the actions log itself, button-only, never via NL.

The only writer besides apply_intent (§0.2) — same per-group lock, same
transactional discipline. The toggle is idempotent: a double or stale tap
no-ops with an "already" answer instead of flipping state twice.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from expensir.core.locking import per_group_lock
from expensir.db.models import Action, Expense, Group, Identity, Ledger, User, utcnow

ToggleDirection = Literal["undo", "redo"]

# grows as slices add undoable kinds (§4); setup is permanent and never listed
REVERSIBLE_KINDS = {"add_expense", "delete_expense", "edit_expense", "set_home_currency"}


@dataclass
class ToggleOutcome:
    answer: str  # the callback answer — always shown to the presser
    # current state for syncing the message/keyboard; None -> leave the message alone
    undone: bool | None
    undone_by_name: str | None = None  # who undid it, for the message's "Undone by" line


async def toggle(
    session: AsyncSession,
    group: Group,
    action_id: int,
    direction: ToggleDirection,
    presser: User,
    presser_platform_id: int,
    operator_platform_id: int | None,
    window_hours: int,
) -> ToggleOutcome:
    await per_group_lock(session, group.id)
    await session.refresh(group)  # post-lock re-read (ADR-0003)

    action = await session.get(Action, action_id)
    if action is None:
        return ToggleOutcome("That button doesn't match anything I recorded.", None)
    ledger = await session.get_one(Ledger, action.ledger_id)
    if ledger.group_id != group.id:
        # a forged/foreign callback: never toggle across groups
        return ToggleOutcome("That button doesn't match anything I recorded.", None)
    if action.kind not in REVERSIBLE_KINDS:
        return ToggleOutcome("That action is permanent — it can't be undone.", None)

    now = utcnow()
    if _locked(action, now, window_hours) and presser_platform_id != operator_platform_id:
        operator = await _operator_name(session, operator_platform_id)
        return ToggleOutcome(
            f"🔒 Locked — over {window_hours}h old. Ask the operator, {operator}.", None
        )

    if action.undone_at is not None:
        if direction == "undo":
            name = await _display_name(session, action.undone_by)
            return ToggleOutcome("Already undone.", True, undone_by_name=name)
        await _reapply(session, action, group)
        action.undone_at = None
        action.undone_by = None
        return ToggleOutcome("↪️ Redone.", False)

    if direction == "redo":
        return ToggleOutcome("Already redone.", False)
    await _reverse(session, action, group, now)
    action.undone_at = now
    action.undone_by = presser.id
    return ToggleOutcome("↩️ Undone.", True, undone_by_name=presser.display_name)


def _locked(action: Action, now: datetime, window_hours: int) -> bool:
    """Computed on press — no scheduler (§9). After the window only the operator may toggle."""
    created_at = action.created_at
    if created_at.tzinfo is None:  # SQLite returns naive datetimes; storage is UTC (§16)
        created_at = created_at.replace(tzinfo=UTC)
    return now >= created_at + timedelta(hours=window_hours)


async def _reverse(session: AsyncSession, action: Action, group: Group, now: datetime) -> None:
    """Reverse the action (§8): soft-delete created rows, or restore the before-image."""
    if action.kind == "add_expense":
        await session.execute(
            update(Expense).where(Expense.created_by_action_id == action.id).values(deleted_at=now)
        )
    elif action.kind == "delete_expense":
        expense = await _target_expense(session, action)
        if await _expense_should_be_visible(session, expense, excluding_action_id=action.id):
            expense.deleted_at = None
    elif action.kind == "edit_expense":
        before = action.before_image
        assert before is not None
        expense = await _target_expense(session, action)
        # the before_image is MINIMAL (§8): restore only the fields this edit
        # changed, so a later standing edit's untouched fields survive
        if "description" in before:
            expense.description = before["description"]
        if "occurred_on" in before:
            expense.occurred_on = before["occurred_on"]
        # edited_at is derived, not restored: the newest OTHER standing edit keeps
        # the expense marked edited; none -> back to never-edited
        expense.edited_at = await _latest_standing_edit_at(
            session, expense.id, excluding_action_id=action.id
        )
    elif action.kind == "set_home_currency":
        assert action.before_image is not None
        group.home_currency = action.before_image["home_currency"]


async def _reapply(session: AsyncSession, action: Action, group: Group) -> None:
    """Redo: restore the rows the action created, or re-apply its field flip (§9)."""
    if action.kind == "add_expense":
        expenses = (
            (
                await session.execute(
                    select(Expense).where(Expense.created_by_action_id == action.id)
                )
            )
            .scalars()
            .all()
        )
        for expense in expenses:
            # an explicit /delete outlives the add's undo/redo cycle: redo must
            # never resurrect an expense a standing delete action removed (§8)
            if not await _active_delete_exists(session, expense.id):
                expense.deleted_at = None
    elif action.kind == "delete_expense":
        expense = await _target_expense(session, action)
        if expense.deleted_at is None:
            expense.deleted_at = utcnow()
    elif action.kind == "edit_expense":
        expense = await _target_expense(session, action)
        if action.intent_json["description"] is not None:
            expense.description = action.intent_json["description"]
        if action.intent_json["occurred_on"] is not None:
            expense.occurred_on = action.intent_json["occurred_on"]
        # derived like _reverse: this action stands again, so count it in
        others = await _latest_standing_edit_at(session, expense.id, excluding_action_id=action.id)
        expense.edited_at = max(filter(None, (others, action.created_at)))
    elif action.kind == "set_home_currency":
        group.home_currency = action.intent_json["currency"]


async def _target_expense(session: AsyncSession, action: Action) -> Expense:
    """The one expense a delete/edit action concerns, from its recorded intent (§8)."""
    expense_id = action.intent_json["expense_id"]
    assert isinstance(expense_id, int)
    return await session.get_one(Expense, expense_id)


async def _expense_should_be_visible(
    session: AsyncSession, expense: Expense, excluding_action_id: int
) -> bool:
    """Visibility is derived, never assumed (§0.4): restoring one action's effect
    must not override the others — the add must stand and no other delete may."""
    add_action = await session.get_one(Action, expense.created_by_action_id)
    if add_action.undone_at is not None:
        return False
    return not await _active_delete_exists(session, expense.id, excluding_action_id)


async def _latest_standing_edit_at(
    session: AsyncSession, expense_id: int, excluding_action_id: int
) -> datetime | None:
    """When the newest not-undone edit on this expense happened; None -> never edited."""
    return (
        await session.execute(
            select(Action.created_at)
            .where(
                Action.kind == "edit_expense",
                Action.undone_at.is_(None),
                Action.id != excluding_action_id,
                Action.intent_json["expense_id"].as_integer() == expense_id,
            )
            .order_by(Action.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def _active_delete_exists(
    session: AsyncSession, expense_id: int, excluding_action_id: int | None = None
) -> bool:
    """Is a not-undone delete_expense action standing against this expense?"""
    stmt = select(Action.id).where(
        Action.kind == "delete_expense",
        Action.undone_at.is_(None),
        Action.intent_json["expense_id"].as_integer() == expense_id,
    )
    if excluding_action_id is not None:
        stmt = stmt.where(Action.id != excluding_action_id)
    return (await session.execute(stmt.limit(1))).scalar_one_or_none() is not None


async def _operator_name(session: AsyncSession, operator_platform_id: int | None) -> str:
    """Name the operator by @username, falling back to display name, then a generic (§9)."""
    if operator_platform_id is None:
        return "the operator"
    identity = (
        await session.execute(
            select(Identity).where(
                Identity.platform == "telegram",
                Identity.platform_user_id == operator_platform_id,
            )
        )
    ).scalar_one_or_none()
    if identity is None:
        return "the operator"
    if identity.username:
        return f"@{identity.username}"
    operator = await session.get_one(User, identity.user_id)
    return operator.display_name


async def _display_name(session: AsyncSession, user_id: int | None) -> str | None:
    if user_id is None:
        return None
    user = await session.get(User, user_id)
    return user.display_name if user is not None else None
