"""Reply formatting (§6): results carry the active-ledger prefix and a visible #id."""

from dataclasses import dataclass

from expensir.domain.money import fmt
from expensir.format.board import BoardLine


@dataclass(frozen=True)
class LedgerLine:
    ledger_id: int  # shown as #id: names may repeat, /switch #id always lands (§11's spirit)
    name: str
    is_active: bool
    is_archived: bool
    logging_currency: str | None


def ledgers_reply(lines: list[LedgerLine]) -> str:
    """The /ledgers read (§0.7): the whole list, active marked, archived labeled."""
    rendered = []
    for line in lines:
        marks = [m for m in ("active" if line.is_active else "", line.logging_currency or "") if m]
        suffix = f" ({', '.join(marks)})" if marks else ""
        status = " — archived" if line.is_archived else ""
        rendered.append(f"• 📒 #{line.ledger_id} {line.name}{suffix}{status}")
    return "Ledgers\n" + "\n".join(rendered)


_SPLIT_LABEL = {
    "exact": "split exactly",
    "shares": "split by shares",
    "percent": "split by percent",
}


def expense_reply(
    *,
    ledger_name: str,
    expense_id: int,
    amount_minor: int,
    currency: str,
    description: str,
    payer_name: str,
    participant_names: list[str],
    rounded_from: str | None = None,
    split_type: str = "equal",
    shares: list[tuple[str, int]] | None = None,  # (name, owed minor), non-equal splits
) -> str:
    amount = fmt(amount_minor, currency)
    rounded = f" (rounded from {rounded_from})" if rounded_from is not None else ""
    if len(participant_names) == 1:
        split = f"owed entirely by {participant_names[0]}"
    elif split_type == "equal":
        split = f"split equally between {join_names(participant_names)}"
    else:
        assert shares is not None
        each = ", ".join(f"{name} {fmt(minor, currency)}" for name, minor in shares)
        split = f"{_SPLIT_LABEL[split_type]}: {each}"
    return (
        f"📒 {ledger_name} • #{expense_id} {description} — {amount}{rounded} "
        f"paid by {payer_name}, {split}."
    )


def settle_reply(
    *,
    ledger_name: str,
    from_name: str,
    to_name: str,
    amount_minor: int,
    currency: str,
    rounded_from: str | None = None,
) -> str:
    amount = fmt(amount_minor, currency)
    rounded = f" (rounded from {rounded_from})" if rounded_from is not None else ""
    return f"📒 {ledger_name} • 🤝 {from_name} paid {to_name} {amount}{rounded}. Balances updated."


def settle_sheet_reply(
    *,
    ledger_name: str,
    pair_names: tuple[str, str],  # the UNORDERED pair (ADR-0007), in stable order
    transfers: list[BoardLine],
) -> str:
    first, second = pair_names
    if not transfers:
        return f"📒 {ledger_name} • Nothing to settle between {first} and {second}."
    lines = (f"{t.from_name} → {t.to_name} {fmt(t.amount_minor, t.currency)}" for t in transfers)
    return "\n".join([f"📒 {ledger_name} • Settling up {first} ↔ {second}", *lines])


def delete_reply(
    *,
    ledger_name: str,
    expense_id: int,
    amount_minor: int,
    currency: str,
    description: str,
) -> str:
    amount = fmt(amount_minor, currency)
    return f"📒 {ledger_name} • 🗑 Deleted #{expense_id} {description} — {amount}. Balances updated."


def edit_reply(
    *,
    ledger_name: str,
    expense_id: int,
    amount_minor: int,
    currency: str,
    description: str,
    occurred_on: str | None,
) -> str:
    amount = fmt(amount_minor, currency)
    dated = f", dated {occurred_on}" if occurred_on is not None else ""
    return f"📒 {ledger_name} • ✏️ #{expense_id} is now: {description} — {amount}{dated}."


def join_names(names: list[str]) -> str:
    if len(names) <= 1:
        return "".join(names)
    return f"{', '.join(names[:-1])} and {names[-1]}"


def balance_reply(
    *,
    ledger_name: str,
    entries: list[tuple[str, dict[str, int]]],  # (name, currency -> net minor, + owes the pool)
    as_me: bool = False,  # scope=me: a single entry, phrased as "you"
) -> str:
    lines: list[str] = []
    for currency in sorted({c for _, by_ccy in entries for c in by_ccy}):
        nets = [(name, by_ccy[currency]) for name, by_ccy in entries if by_ccy.get(currency)]
        # debtors first, largest debt first; stable by name within ties
        for name, net in sorted(nets, key=lambda item: (-item[1], item[0])):
            amount = fmt(abs(net), currency)
            if as_me:
                lines.append(f"You owe {amount}" if net > 0 else f"You're owed {amount}")
            else:
                verb = "owes" if net > 0 else "is owed"
                lines.append(f"{name} {verb} {amount}")
    if not lines:
        settled = "You're all settled up" if as_me else "All settled up"
        return f"📒 {ledger_name} • {settled}."
    return f"📒 {ledger_name} • Balances\n" + "\n".join(lines)
