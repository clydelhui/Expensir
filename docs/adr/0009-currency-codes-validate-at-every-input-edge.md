# Currency codes validate against circulating ISO 4217 at every input edge

## Status

accepted — amends ARCHITECTURE.md §3 (which said an expense "may specify **any** currency" and
implied unknown codes were tolerated everywhere) and generalizes ADR-0002's custom-settle
"real ISO" check to every currency input.

## Context

Slice 9 added ISO validation to settlements only: a settlement freezes its currency forever, so a
typo must not mint a bucket. Every other door still accepted any 3-letter token —
`/homecurrency FUN` stored FUN, `/equal 30 EUE dinner` silently booked an EUE bucket,
`/newledger Tokyo JPZ` silently created a JPZ-logging ledger. Balances are per-currency and never
cross buckets (§0), so one typo'd code splits the pool in a way only delete-and-re-add repairs.
The NL, OCR, and FX slices add more doors, none of which pass through the slash parsers.

## Decision

- **One invariant at the apply layer: no currency code crosses `apply_intent` unvalidated.** Every
  intent that persists a currency (`set_home_currency`, `set_logging_currency`, `new_ledger`,
  `add_expense` post-resolution, `settle_up`, and later `set_fx_rate`) rejects unrecognized codes.
  Parsers stay grammar-only; NL/OCR inherit the check for free.
- **Unknown codes reject loudly, never re-interpret.** `/newledger Tokyo JPZ` and
  `/equal 30 EUE dinner …` fail with a correction message; the token is never folded back into the
  ledger name or the description (§0.9 — never guess). The same token shape must not mean two
  things depending on a lookup table.
- **The list is circulating ISO 4217 only** — no XAU/XDR/XXX/XTS, no fund codes (BOV, CHE, USN, …).
  A group can only split what someone can actually pay. A static frozenset, maintained by hand as
  ISO changes.
- **Inputs only.** Stored rows, balance replay, rendering, and imported backups are never
  re-policed: a code retired from the standard (HRK, ZWL) keeps working from history, and
  `minor_digits` keeps defaulting unknown codes to 2 for exactly that case. Recognition guards
  keystrokes, not data.

## Consequences

- A ledger name can no longer *end* in an unrecognized uppercase 3-letter word ("Tokyo ABC" —
  lowercase it instead); a lone token is still always a name (`/newledger USD` names a ledger
  "USD" and sets nothing).
- Shouty words in an expense's currency slot now reject instead of minting buckets
  (`/equal 30 FUN dinner` → write `fun`).
- "Recognized currency" enters the glossary, deliberately distinct from FX-*supported*: a
  recognized code may still render `(≈ n/a)`.
- The list needs occasional manual updates (~one ISO change every couple of years). A stale list
  wrongly rejects a genuinely new currency at the door; history is unaffected either way.
