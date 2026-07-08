"""LLM wire result -> the shared Intent contract (§12).

Currency resolution and minor-unit conversion happen HERE, app-side, exactly as
on the slash path — the wire schema carries decimal strings because the LLM is
a parser, never the source of truth for math (§0).
"""

from dataclasses import dataclass

from expensir.domain.currency import require_known_currency, resolve_currency
from expensir.domain.money import to_minor
from expensir.intents.schema import (
    AddExpense,
    ArchiveLedger,
    Intent,
    NewLedger,
    SetHomeCurrency,
    SetLoggingCurrency,
    SettleUp,
    ShowBalance,
    SplitMember,
    SwitchLedger,
    UnarchiveLedger,
    Unknown,
)
from expensir.llm.wire import (
    WireAddExpense,
    WireArchiveLedger,
    WireNewLedger,
    WireResult,
    WireSetHomeCurrency,
    WireSetLoggingCurrency,
    WireSettleUp,
    WireShowBalance,
    WireSwitchLedger,
    WireUnarchiveLedger,
    WireUnknown,
)


@dataclass(frozen=True)
class ConvertedIntent:
    intent: Intent
    rounded_from: str | None  # the stated amount, when to_minor rounded it (§3, visible)


def to_intent(
    wire: WireResult, *, logging_currency: str | None, home_currency: str | None
) -> ConvertedIntent:
    if isinstance(wire, WireAddExpense):
        return _add_expense(wire, logging_currency, home_currency)
    if isinstance(wire, WireSettleUp):
        return _settle_up(wire, logging_currency, home_currency)
    if isinstance(wire, WireShowBalance):
        return ConvertedIntent(
            intent=ShowBalance(scope=wire.scope, convert_to=wire.convert_to), rounded_from=None
        )
    if isinstance(wire, WireSwitchLedger):
        return ConvertedIntent(intent=SwitchLedger(name_or_id=wire.name_or_id), rounded_from=None)
    if isinstance(wire, WireNewLedger):
        # validate at the NL input edge (ADR-0009): the proposal must never show
        # a code that would only reject at confirm
        currency = (
            require_known_currency(wire.logging_currency)
            if wire.logging_currency is not None
            else None
        )
        return ConvertedIntent(
            intent=NewLedger(name=wire.name, logging_currency=currency), rounded_from=None
        )
    if isinstance(wire, WireArchiveLedger):
        return ConvertedIntent(intent=ArchiveLedger(name_or_id=wire.name_or_id), rounded_from=None)
    if isinstance(wire, WireUnarchiveLedger):
        return ConvertedIntent(
            intent=UnarchiveLedger(name_or_id=wire.name_or_id), rounded_from=None
        )
    if isinstance(wire, WireSetHomeCurrency):
        return ConvertedIntent(
            intent=SetHomeCurrency(currency=require_known_currency(wire.currency)),
            rounded_from=None,
        )
    if isinstance(wire, WireSetLoggingCurrency):
        return ConvertedIntent(
            intent=SetLoggingCurrency(currency=require_known_currency(wire.currency)),
            rounded_from=None,
        )
    if isinstance(wire, WireUnknown):
        return ConvertedIntent(intent=Unknown(reason=wire.reason), rounded_from=None)
    # WireUndoRedo never reaches here: the handler answers it before conversion (§9)
    raise AssertionError(f"unhandled wire kind: {wire.kind}")


def _settle_up(
    wire: WireSettleUp, logging_currency: str | None, home_currency: str | None
) -> ConvertedIntent:
    """No amount -> the settle sheet, a read (ADR-0007). With one, the same §3
    resolution order as expenses — the proposal shows the resolved currency (§3)."""
    if wire.amount is None:
        intent = SettleUp(from_ref=wire.from_ref, to_ref=wire.to_ref)
        return ConvertedIntent(intent=intent, rounded_from=None)
    currency = resolve_currency(wire.currency, logging_currency, home_currency)
    amount_minor, was_rounded = to_minor(wire.amount, currency)
    intent = SettleUp(
        from_ref=wire.from_ref,
        to_ref=wire.to_ref,
        amount_minor=amount_minor,
        currency=currency,
    )
    return ConvertedIntent(intent=intent, rounded_from=wire.amount if was_rounded else None)


def _add_expense(
    wire: WireAddExpense, logging_currency: str | None, home_currency: str | None
) -> ConvertedIntent:
    currency = resolve_currency(wire.currency, logging_currency, home_currency)
    amount_minor, was_rounded = to_minor(wire.amount, currency)
    intent = AddExpense(
        payer_ref=wire.payer_ref,
        amount_minor=amount_minor,
        currency=currency,
        description=wire.description,
        occurred_on=wire.occurred_on,
        split_type=wire.split_type,
        participants=_split_members(wire, currency),
        confidence=wire.confidence,
    )
    return ConvertedIntent(intent=intent, rounded_from=wire.amount if was_rounded else None)


def _split_members(wire: WireAddExpense, currency: str) -> list[SplitMember]:
    """Per-split-type members, close to the slash path's handler._split_members.

    A valued split needs its field on EVERY participant, and 'exact' amounts are
    money that must convert with the resolved currency. A missing or too-fine
    value is a user-facing ValueError at the NL edge (ADR-0009) — never a
    downstream assert that would crash mid-proposal on a stray model output.

    One deliberate divergence from slash: /shares defaults an unstated weight to 1
    (parse_shares default_value='1'), but NL has no positional syntax, so an
    unweighted shares/percent split is an error here rather than a silent default —
    the model is expected to state each value. (A future slice may ask the user to
    clarify instead of rejecting.)"""
    if wire.split_type == "equal":
        # empty participants keeps the §4 convention: all REGISTERED members
        return [SplitMember(user_ref=p.user_ref) for p in wire.participants]
    if not wire.participants:
        raise ValueError(f"A {wire.split_type} split needs a value stated for each person.")
    if wire.split_type == "exact":
        members = []
        for p in wire.participants:
            if p.exact is None:
                raise ValueError("An exact split needs a stated amount for each person.")
            minor, was_rounded = to_minor(p.exact, currency)
            if was_rounded:
                # exact parts are the user's stated amounts (mirrors the slash path)
                raise ValueError(
                    f"{p.exact} doesn't land on the smallest {currency} unit — "
                    f"exact amounts can't be finer than that."
                )
            members.append(SplitMember(user_ref=p.user_ref, exact_minor=minor))
        return members
    if wire.split_type == "shares":
        for p in wire.participants:
            if p.weight is None:
                raise ValueError("A shares split needs a weight for each person.")
        return [SplitMember(user_ref=p.user_ref, weight=p.weight) for p in wire.participants]
    # percent
    for p in wire.participants:
        if p.percent is None:
            raise ValueError("A percent split needs a percentage for each person.")
    return [SplitMember(user_ref=p.user_ref, percent=p.percent) for p in wire.participants]
