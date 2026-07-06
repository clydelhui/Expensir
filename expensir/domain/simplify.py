"""Minimum cash-flow simplification (§7.4, ADR-0006): pure, per currency, deterministic.

A display/solver aid only: it drives the board's suggested transfers and full
settle-up, but never gates a custom settle (ADR-0002).
"""

Transfer = tuple[int, int, int]  # (debtor, creditor, amount in minor units)


def simplify(net_ccy: dict[int, int]) -> list[Transfer]:
    """Suggested transfers for one currency's net positions (+ owes the pool, §7.2).

    Greedy: repeatedly match the largest debtor with the largest creditor, ties
    broken by ascending user id, so the output is stable across renders (§7.4).
    """
    debtors = {user: net for user, net in net_ccy.items() if net > 0}
    creditors = {user: -net for user, net in net_ccy.items() if net < 0}
    transfers: list[Transfer] = []
    while debtors and creditors:
        debtor = max(debtors, key=lambda user: (debtors[user], -user))
        creditor = max(creditors, key=lambda user: (creditors[user], -user))
        amount = min(debtors[debtor], creditors[creditor])
        transfers.append((debtor, creditor, amount))
        if debtors[debtor] == amount:
            del debtors[debtor]
        else:
            debtors[debtor] -= amount
        if creditors[creditor] == amount:
            del creditors[creditor]
        else:
            creditors[creditor] -= amount
    return transfers
