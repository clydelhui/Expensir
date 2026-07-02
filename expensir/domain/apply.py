"""apply_intent — THE forward write path (§8). Every mutation goes through here.

Runs under the per-group advisory lock (ADR-0003) and re-reads the group post-lock,
so a concurrent /switch or currency change can never interleave with this write.
"""

from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from expensir.core.locking import per_group_lock
from expensir.db.models import Action, Expense, ExpenseSplit, Group, User
from expensir.domain.allocate import allocate
from expensir.domain.errors import Rejection
from expensir.domain.identity import registered_members, resolve_refs
from expensir.intents.schema import AddExpense, Intent, SetHomeCurrency


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
    payer: User
    participants: list[User]  # the members the split actually used, in stable order


async def apply_intent(intent: Intent, ctx: ApplyContext) -> AppliedExpense | None:
    await per_group_lock(ctx.session, ctx.group.id)
    await ctx.session.refresh(ctx.group)  # post-lock re-read (ADR-0003)
    if isinstance(intent, AddExpense):
        return await _apply_add_expense(intent, ctx)
    if isinstance(intent, SetHomeCurrency):
        await _apply_set_home_currency(intent, ctx)
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

    shares = allocate(intent.amount_minor, {u.id: 1 for u in participants}, ctx.seed)

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
    return AppliedExpense(expense_id=expense.id, payer=payer, participants=participants)


async def _apply_set_home_currency(intent: SetHomeCurrency, ctx: ApplyContext) -> None:
    before = {"home_currency": ctx.group.home_currency}
    ctx.group.home_currency = intent.currency
    await _append_action(ctx, intent, before_image=before)


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
