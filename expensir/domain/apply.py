"""apply_intent — THE forward write path (§8). Every mutation goes through here.

Runs under the per-group advisory lock (ADR-0003) and re-reads the group post-lock,
so a concurrent /switch or currency change can never interleave with this write.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from expensir.core.locking import per_group_lock
from expensir.db.models import Action, Expense, ExpenseSplit, Group, Ledger, User, utcnow
from expensir.domain.allocate import allocate
from expensir.domain.balances import net_positions
from expensir.domain.errors import Rejection
from expensir.domain.identity import registered_members, resolve_refs
from expensir.domain.ledgers import find_ledger, most_recent_open
from expensir.domain.money import fmt
from expensir.intents.schema import (
    AddExpense,
    ArchiveLedger,
    Intent,
    NewLedger,
    SetHomeCurrency,
    SetLoggingCurrency,
    SwitchLedger,
    UnarchiveLedger,
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
class AppliedLedgerOp:
    """A ledger lifecycle flip (§8, ADR-0004): the caller renders the announcement."""

    action_id: int
    ledger: Ledger
    repointed_to: Ledger | None = None  # archiving the active ledger repoints deterministically
    outstanding_balances: bool = False  # archive warns but proceeds (§17)


Applied = AppliedExpense | AppliedFlip | AppliedLedgerOp


async def apply_intent(intent: Intent, ctx: ApplyContext) -> Applied | None:
    await per_group_lock(ctx.session, ctx.group.id)
    await ctx.session.refresh(ctx.group)  # post-lock re-read (ADR-0003)
    if isinstance(intent, AddExpense):
        return await _apply_add_expense(intent, ctx)
    if isinstance(intent, SetHomeCurrency):
        return await _apply_set_home_currency(intent, ctx)
    if isinstance(intent, SetLoggingCurrency):
        return await _apply_set_logging_currency(intent, ctx)
    if isinstance(intent, NewLedger):
        return await _apply_new_ledger(intent, ctx)
    if isinstance(intent, SwitchLedger):
        return await _apply_switch_ledger(intent, ctx)
    if isinstance(intent, ArchiveLedger):
        return await _apply_archive_ledger(intent, ctx)
    if isinstance(intent, UnarchiveLedger):
        return await _apply_unarchive_ledger(intent, ctx)
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


async def _apply_set_home_currency(intent: SetHomeCurrency, ctx: ApplyContext) -> AppliedFlip:
    before = {"home_currency": ctx.group.home_currency}
    ctx.group.home_currency = intent.currency
    action = await _append_action(ctx, intent, before_image=before)
    return AppliedFlip(action_id=action.id)


async def _apply_set_logging_currency(
    intent: SetLoggingCurrency, ctx: ApplyContext
) -> AppliedLedgerOp:
    """Change the ACTIVE ledger's new-expense default (ADR-0001); existing expenses keep
    their frozen currency — this is a default-picker, never a re-denomination (§3)."""
    assert ctx.group.active_ledger_id is not None  # ensure_group invariant (ADR-0004)
    ledger = await ctx.session.get_one(Ledger, ctx.group.active_ledger_id)
    before = {"logging_currency": ledger.logging_currency}
    ledger.logging_currency = intent.currency
    action = await _append_action(ctx, intent, before_image=before, ledger_id=ledger.id)
    return AppliedLedgerOp(action_id=action.id, ledger=ledger)


async def _apply_new_ledger(intent: NewLedger, ctx: ApplyContext) -> AppliedLedgerOp:
    """Create + activate (§8, ADR-0004): undo restores the previous active pointer."""
    before = {"active_ledger_id": ctx.group.active_ledger_id}
    ledger = Ledger(
        group_id=ctx.group.id, name=intent.name, logging_currency=intent.logging_currency
    )
    ctx.session.add(ledger)
    await ctx.session.flush()
    ctx.group.active_ledger_id = ledger.id
    action = await _append_action(ctx, intent, before_image=before, ledger_id=ledger.id)
    return AppliedLedgerOp(action_id=action.id, ledger=ledger)


async def _apply_switch_ledger(intent: SwitchLedger, ctx: ApplyContext) -> AppliedLedgerOp:
    """Repoint the active ledger (ADR-0004): anyone may switch; the reply announces it."""
    ledger = await find_ledger(ctx.session, ctx.group.id, intent.name_or_id)
    if ledger.status == "archived":
        raise Rejection(
            f"📒 {ledger.name} is archived — /unarchive {ledger.name} first, then /switch."
        )
    if ledger.id == ctx.group.active_ledger_id:
        raise Rejection(f"📒 {ledger.name} is already the active ledger.")
    before = {"active_ledger_id": ctx.group.active_ledger_id}
    ctx.group.active_ledger_id = ledger.id
    action = await _append_action(ctx, intent, before_image=before, ledger_id=ledger.id)
    return AppliedLedgerOp(action_id=action.id, ledger=ledger)


async def _apply_archive_ledger(intent: ArchiveLedger, ctx: ApplyContext) -> AppliedLedgerOp:
    """Archive a ledger (ADR-0004): the active pointer repoints; the LAST open one refuses."""
    if intent.name_or_id is None:
        assert ctx.group.active_ledger_id is not None  # ensure_group invariant (ADR-0004)
        ledger = await ctx.session.get_one(Ledger, ctx.group.active_ledger_id)
    else:
        ledger = await find_ledger(ctx.session, ctx.group.id, intent.name_or_id)
    if ledger.status == "archived":
        raise Rejection(f"📒 {ledger.name} is already archived.")
    replacement = await most_recent_open(ctx.session, ctx.group.id, exclude_id=ledger.id)
    if replacement is None:
        raise Rejection(
            f"🚫 {ledger.name} is the only open ledger — "
            "create another with /newledger first, then archive this one."
        )
    before: dict[str, Any] = {"status": "open", "archived_at": _iso(ledger.archived_at)}
    repointed = None
    if ledger.id == ctx.group.active_ledger_id:
        before["active_ledger_id"] = ctx.group.active_ledger_id
        ctx.group.active_ledger_id = replacement.id
        repointed = replacement
    net = await net_positions(ctx.session, ledger.id)
    outstanding = any(minor for by_currency in net.values() for minor in by_currency.values())
    ledger.status = "archived"
    ledger.archived_at = utcnow()
    action = await _append_action(ctx, intent, before_image=before, ledger_id=ledger.id)
    return AppliedLedgerOp(
        action_id=action.id,
        ledger=ledger,
        repointed_to=repointed,
        outstanding_balances=outstanding,
    )


async def _apply_unarchive_ledger(intent: UnarchiveLedger, ctx: ApplyContext) -> AppliedLedgerOp:
    """Reopen an archived ledger; the active pointer is NOT touched (§17, ADR-0004)."""
    ledger = await find_ledger(ctx.session, ctx.group.id, intent.name_or_id)
    if ledger.status != "archived":
        raise Rejection(f"📒 {ledger.name} isn't archived — it's already open.")
    before = {"status": "archived", "archived_at": _iso(ledger.archived_at)}
    ledger.status = "open"
    ledger.archived_at = None
    action = await _append_action(ctx, intent, before_image=before, ledger_id=ledger.id)
    return AppliedLedgerOp(action_id=action.id, ledger=ledger)


def _iso(moment: datetime | None) -> str | None:
    return moment.isoformat() if moment is not None else None


async def _append_action(
    ctx: ApplyContext,
    intent: Intent,
    before_image: dict[str, Any] | None = None,
    ledger_id: int | None = None,  # ledger ops pin the action to their TARGET ledger
) -> Action:
    actor = _require_actor(ctx)
    if ledger_id is None:
        assert ctx.group.active_ledger_id is not None  # ensure_group invariant (ADR-0004)
        ledger_id = ctx.group.active_ledger_id
    action = Action(
        ledger_id=ledger_id,
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
