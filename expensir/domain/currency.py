"""Currency resolution order for a new expense (§3, ADR-0001).

explicit override -> ledger logging currency -> group home currency -> reject.
The resolved code freezes onto the expense at creation and is never re-denominated.
"""

from expensir.domain.errors import Rejection


class CannotResolveCurrency(Rejection):
    def __init__(self) -> None:
        super().__init__(
            "Set a currency first: /currency <ISO> for this ledger, "
            "or /homecurrency <ISO> for the group"
        )


def resolve_currency(
    override: str | None, logging_currency: str | None, home_currency: str | None
) -> str:
    resolved = override or logging_currency or home_currency
    if resolved is None:
        raise CannotResolveCurrency()
    return resolved.upper()
