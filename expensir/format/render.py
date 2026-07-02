"""Reply formatting (§6): results carry the active-ledger prefix and a visible #id."""

from expensir.domain.money import fmt


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
) -> str:
    amount = fmt(amount_minor, currency)
    rounded = f" (rounded from {rounded_from})" if rounded_from is not None else ""
    split = (
        f"owed entirely by {participant_names[0]}"
        if len(participant_names) == 1
        else f"split equally between {join_names(participant_names)}"
    )
    return (
        f"📒 {ledger_name} • #{expense_id} {description} — {amount}{rounded} "
        f"paid by {payer_name}, {split}."
    )


def join_names(names: list[str]) -> str:
    if len(names) <= 1:
        return "".join(names)
    return f"{', '.join(names[:-1])} and {names[-1]}"
