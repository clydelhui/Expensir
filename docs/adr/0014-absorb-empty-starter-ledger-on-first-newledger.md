# Absorb the empty starter ledger on the group's first /newledger

## Status

accepted — amends ADR-0004 (active-ledger invariant maintenance).

## Context

`ensure_group` auto-creates a ledger named after the chat (the **starter ledger**, CONTEXT.md) so the
active-ledger invariant holds from the group's first message. The 2026-07-11 live UX review found the
trap this sets: a group creates a real ledger (`/newledger Japan JPY`), uses it all trip, and archives
it at trip end — ADR-0004's repoint rule then lands on the forgotten, near-empty starter ledger, and
the next expense silently misfiles there, disconnected from the trip's balances.

Alternatives considered: a warning line on the first expense after any active-ledger switch (treats
the symptom; the misfiled expense still happens), and lazy ledger creation (kills the root cause but
breaks the "a group always has exactly one active ledger" invariant, forcing a no-ledger state onto
every read path).

## Decision

- When `/newledger` runs and the currently-active ledger is the starter ledger with zero non-deleted
  transactions, archive it **within the same action**, and say so in the confirmation.
- A starter ledger that holds any transaction is an ordinary ledger — never absorbed.
- WELCOME announces the starter ledger by name, so its existence is never a secret.
- Undo of an absorbing `/newledger` reverses the whole action: the created ledger is removed per
  ADR-0004 and the starter ledger is unarchived and made active again. This falls out of the action
  model (undo reverts everything the action wrote) and keeps the invariant — undo can never leave the
  group with zero open ledgers.

## Consequences

- After absorption the group has exactly one open ledger, so a trip-end `/archive` hits ADR-0004's
  deliberate "create another ledger first" refusal instead of silently repointing to a ledger nobody
  chose. The failure mode flips from silent misfiling to an explicit, guided step.
- Groups that logged expenses into the starter ledger first keep it as a real ledger; for them the
  repoint trap remains reachable and is mitigated only by the announced switch (ADR-0004).
- ADR-0004's repoint rule is unchanged; this decision only removes the standing empty target that
  made it dangerous.
