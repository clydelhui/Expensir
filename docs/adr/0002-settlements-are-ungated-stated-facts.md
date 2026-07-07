# Custom settlements are ungated stated facts

## Status

accepted — amends ARCHITECTURE.md §7.3, which required validating that `from` owes `to` and rejecting with "Nothing to settle in C." Partially amended by ADR-0007: full settle-up as a bulk commit no longer exists — "settle up with X" renders a per-line settle sheet instead. The "real ISO" check this ADR kept on the custom path is generalized by ADR-0009 to every currency input.

## Context

Balances are pooled: members hold net positions against the pool, not pairwise debts. The only thing that manufactures pairwise "A owes B" amounts is `simplify`, which returns one of possibly several minimal solutions — so any debt gate is solver-dependent. The original spec gated a with-amount settlement on "`from` owes `to` per simplify" and rejected otherwise; it also capped nothing explicitly, which combined with the gate to leave magnitude ambiguous.

## Decision

Distinguish two settlement paths:

- **Solver-following paths** — the board `[Settle]` button (one currency line) and full settle-up (`settle up with X`, no amount). These settle exactly the `simplify`-suggested transfers, so they only ever pay down real debts. Unchanged.
- **Custom path** — `/settle` or NL with an explicit amount + currency. This is **fully ungated**: any direction, any positive amount (overpayment included). The bot records the stated payment and lets balance replay absorb it. The only validation kept is `from ≠ to`, positive amount, both registered members, and a real ISO currency.

The "Nothing to settle in C" rejection and any magnitude cap are removed from the custom path.

## Consequences

- A typo'd `/settle` can move balances in a surprising direction with no guardrail; the result carries an Undo button, which is the only safety net.
- Overpayment credits can now originate at settle time, not only from undoing a past expense (as §9 had assumed was the sole source).
- The board and full settle-up remain the guided paths for users who want to follow the solver; `/settle` is the raw "record this payment" escape hatch.
