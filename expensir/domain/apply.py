"""apply_intent — THE forward write path (§8). Every mutation goes through here.

Runs under the per-group advisory lock (ADR-0003) and re-reads the group post-lock,
so a concurrent /switch or currency change can never interleave with this write.
"""

from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from expensir.core.locking import per_group_lock
from expensir.db.models import Action, Expense, ExpenseSplit, Group, Ledger, User, utcnow
from expensir.domain.allocate import allocate
from expensir.domain.errors import Rejection
from expensir.domain.identity import registered_members, resolve_refs
from expensir.domain.money import fmt
from expensir.intents.schema import (
    AddExpense,
    DeleteExpense,
    EditExpense,
    Intent,
    SetHomeCurrency,
)


@dataclass
class ApplyContext:
    session: AsyncSession
    group: Group
    actor: User | None
    seed: int  # originating platform message id; drives the rotating tie-break (§7.1)
    source: str = "command"  # 'command' | 'nl' | 'ocr'


@dataclass
class AppliedExpense:
    expense_id: int
    action_id: int  # the one actions row this mutation appended (§0.2)
    payer: User
    participants: list[User]  # the members the split actually used, in stable order
    shares: dict[int, int]  # user id -> owed minor units, as committed


@dataclass
class AppliedFlip:
    """A field/pointer flip (§8): reversed by restoring the action's before_image."""

    action_id: int


@dataclass
class AppliedExpenseChange:
    """delete_expense / edit_expense: the acted-on expense, for the caller's render (§8)."""

    action_id: int
    expense: Expense


Applied = AppliedExpense | AppliedFlip | AppliedExpenseChange


async def apply_intent(intent: Intent, ctx: ApplyContext) -> Applied | None:
    await per_group_lock(ctx.session, ctx.group.id)
    await ctx.session.refresh(ctx.group)  # post-lock re-read (ADR-0003)
    if isinstance(intent, AddExpense):
        return await _apply_add_expense(intent, ctx)
    if isinstance(intent, DeleteExpense):
        return await _apply_delete_expense(intent, ctx)
    if isinstance(intent, EditExpense):
        return await _apply_edit_expense(intent, ctx)
    if isinstance(intent, SetHomeCurrency):
        return await _apply_set_home_currency(intent, ctx)
    return None


async def _apply_add_expense(intent: AddExpense, ctx: ApplyContext) -> AppliedExpense:
    actor = _require_actor(ctx)
    # the slash path resolves the currency before building the intent (§3); None is
    # only legal for NL/OCR intents, which are re-resolved at confirm time (later slice)
    assert intent.currency is not None

    refs = [intent.payer_ref] + [p.user_ref for p in intent.participants]
    resolved = await resolve_refs(ctx.session, ctx.group.id, refs, actor)
    payer = resolved[intent.payer_ref]
    participants = _unique([resolved[p.user_ref] for p in intent.participants])
    if not participants:
        participants = await registered_members(ctx.session, ctx.group.id)

    shares = _split(intent, resolved, participants, ctx.seed)

    action = await _append_action(ctx, intent)
    assert ctx.group.active_ledger_id is not None  # ensure_group invariant (ADR-0004)
    expense = Expense(
        ledger_id=ctx.group.active_ledger_id,
        payer_id=payer.id,
        amount_minor=intent.amount_minor,
        currency=intent.currency,
        description=intent.description,
        occurred_on=intent.occurred_on,
        split_type=intent.split_type,
        source=ctx.source,
        created_by_user_id=actor.id,
        created_by_action_id=action.id,
    )
    ctx.session.add(expense)
    await ctx.session.flush()
    ctx.session.add_all(
        ExpenseSplit(
            expense_id=expense.id,
            user_id=user.id,
            owed_minor=shares[user.id],
            created_by_action_id=action.id,
        )
        for user in participants
    )
    await ctx.session.flush()
    return AppliedExpense(
        expense_id=expense.id,
        action_id=action.id,
        payer=payer,
        participants=participants,
        shares=shares,
    )


def _split(
    intent: AddExpense, resolved: dict[str, User], participants: list[User], seed: int
) -> dict[int, int]:
    """Per-user minor-unit shares — validation by split type BEFORE allocate (§7.1)."""
    currency = intent.currency or ""
    if intent.split_type == "exact":
        shares = {u: int(v) for u, v in _per_user(intent, resolved, "exact_minor").items()}
        stated = sum(shares.values())
        if stated != intent.amount_minor:
            gap = fmt(abs(intent.amount_minor - stated), currency)
            direction = "short of" if stated < intent.amount_minor else "over"
            raise Rejection(
                f"Those parts add up to {fmt(stated, currency)} — "
                f"{gap} {direction} the {fmt(intent.amount_minor, currency)} total."
            )
        return shares
    if intent.split_type == "shares":
        return allocate(intent.amount_minor, _per_user(intent, resolved, "weight"), seed)
    if intent.split_type == "percent":
        percents = _per_user(intent, resolved, "percent")
        total_percent = sum(percents.values())
        if abs(total_percent - 100) > 1.0:
            raise Rejection(
                f"Those percents add up to {total_percent:g}, not 100 — "
                "they need to land within ±1 of 100."
            )
        # weights = the given percents; normalization absorbs the ±1.0 tolerance (§7.1)
        return allocate(intent.amount_minor, percents, seed)
    return allocate(intent.amount_minor, {u.id: 1 for u in participants}, seed)


def _per_user(intent: AddExpense, resolved: dict[str, User], attr: str) -> dict[int, int | float]:
    """Each participant's stated value keyed by resolved user id, one value per person."""
    values: dict[int, int | float] = {}
    for member in intent.participants:
        value = getattr(member, attr)
        assert value is not None  # the parser/extractor set the field this split type needs
        user_id = resolved[member.user_ref].id
        if user_id in values:
            # distinct refs can land on one person (e.g. "me" and "@alice"); never guess
            raise Rejection(
                f"{member.user_ref} appears more than once in the split — name each person once."
            )
        values[user_id] = value
    return values


async def _apply_delete_expense(intent: DeleteExpense, ctx: ApplyContext) -> AppliedExpenseChange:
    _require_actor(ctx)
    expense = await _sealed_expense(intent.expense_id, ctx)
    if expense.deleted_at is not None:
        raise Rejection(f"🤷 #{expense.id} is already gone — nothing to delete.")
    action = await _append_action(ctx, intent)
    expense.deleted_at = utcnow()
    await ctx.session.flush()
    return AppliedExpenseChange(action_id=action.id, expense=expense)


async def _apply_edit_expense(intent: EditExpense, ctx: ApplyContext) -> AppliedExpenseChange:
    """Non-financial fields only (§4, §8): display changes, never a balance change (§0.4)."""
    _require_actor(ctx)
    expense = await _sealed_expense(intent.expense_id, ctx)
    if expense.deleted_at is not None:
        raise Rejection(
            f"🚫 #{expense.id} is deleted — tap ↩️ Undo on its delete first if you want it back."
        )
    if intent.description is None and intent.occurred_on is None:
        raise Rejection("Nothing to change — give a new description and/or a YYYY-MM-DD date.")

    # MINIMAL before_image (§8): only the fields this edit changes — undoing this
    # edit later must not clobber another standing edit's untouched fields.
    # edited_at is not captured: it is derived from the standing edit actions on undo/redo.
    before: dict[str, Any] = {"expense_id": expense.id}
    if intent.description is not None:
        before["description"] = expense.description
    if intent.occurred_on is not None:
        before["occurred_on"] = expense.occurred_on
    action = await _append_action(ctx, intent, before_image=before)
    if intent.description is not None:
        expense.description = intent.description
    if intent.occurred_on is not None:
        expense.occurred_on = intent.occurred_on
    expense.edited_at = utcnow()
    await ctx.session.flush()
    return AppliedExpenseChange(action_id=action.id, expense=expense)


async def _sealed_expense(expense_id: int, ctx: ApplyContext) -> Expense:
    """Load an expense reference, enforcing the ledger seal (§0.10, §11).

    Another group's expense reads as not-found — the bot stays silent about
    other groups; another ledger of THIS group is refused with a pointer to switch.
    """
    not_found = Rejection(f"🚫 I can't find expense #{expense_id} here — check the #id.")
    expense = await ctx.session.get(Expense, expense_id)
    if expense is None:
        raise not_found
    ledger = await ctx.session.get_one(Ledger, expense.ledger_id)
    if ledger.group_id != ctx.group.id:
        raise not_found
    if expense.ledger_id != ctx.group.active_ledger_id:
        raise Rejection(
            f"🔒 #{expense_id} is in 📒 {ledger.name} — switch there first, then try again."
        )
    return expense


async def _apply_set_home_currency(intent: SetHomeCurrency, ctx: ApplyContext) -> AppliedFlip:
    before = {"home_currency": ctx.group.home_currency}
    ctx.group.home_currency = intent.currency
    action = await _append_action(ctx, intent, before_image=before)
    return AppliedFlip(action_id=action.id)


async def _append_action(
    ctx: ApplyContext, intent: Intent, before_image: dict[str, Any] | None = None
) -> Action:
    actor = _require_actor(ctx)
    assert ctx.group.active_ledger_id is not None  # ensure_group invariant (ADR-0004)
    action = Action(
        ledger_id=ctx.group.active_ledger_id,
        actor_user_id=actor.id,
        kind=intent.kind,
        intent_json=intent.model_dump(mode="json"),
        before_image=before_image,
    )
    ctx.session.add(action)
    await ctx.session.flush()
    return action


def _require_actor(ctx: ApplyContext) -> User:
    if ctx.actor is None:
        # GroupAnonymousBot / service accounts: no author to audit, so no mutation (§11)
        raise Rejection(
            "I can't tell who sent that — anonymous admins can't record changes. "
            "Turn off 'Remain anonymous' and try again."
        )
    return ctx.actor


def _unique(users: list[User]) -> list[User]:
    return list({u.id: u for u in users}.values())
