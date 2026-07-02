# Settle-up is a per-line sheet, not a bulk action

## Status

accepted — amends ADR-0002's "solver-following paths" (which kept full settle-up as a bulk
multi-currency commit) and ARCHITECTURE-v2.md §7.3.

## Context

Full settle-up (`settle up with X`, no amount) recorded one settlement per shared currency, all
tagged with a single `action_id` — the only multi-row action in the system. But the solver's
suggested transfers between a pair can point in *different directions per currency*, and the spec
was internally inconsistent about it: CONTEXT.md defined full settle-up pair-symmetrically
("between two members … every currency they share") while §7.3 keyed on the intent's `from→to`
direction only. A directed bulk commit silently leaves counter-direction lines standing; a
pair-symmetric one bundles payments in *opposite directions* under a single Undo.

## Decision

Every settlement affordance corresponds to exactly **one suggested transfer: one currency, one
direction, one settlement row, one action, one Undo**.

"Settle up with X" (NL, or `/settle` with no amount) is therefore a **read**, not a mutation: it
replies with a **settle sheet** — the suggested transfers between the two members, both directions,
one line per currency, each with its own WYSIWYG `[Settle]` button using the same amount-token +
staleness guard as the board (ADR-0006). The sheet commits nothing and writes no action row; the
taps do the committing. No suggested transfers between the pair → "Nothing to settle."

## Consequences

- The multi-row-per-action path disappears from `apply_intent`: every action now creates at most
  one settlement row, simplifying the reversal model.
- The settle-up verb is a read, so NL "settle up with Alice" no longer needs propose+confirm — the
  sheet itself is the confirmation surface.
- Squaring a pair across N currencies takes N taps instead of one confirm.
- `from_ref`/`to_ref` carry direction only on the custom (with-amount) path; for the sheet the pair
  is unordered.
