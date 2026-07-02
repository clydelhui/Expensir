# Board [Settle] button: WYSIWYG, with the shown amount as a concurrency token

## Status

accepted — refines ARCHITECTURE.md §6 (`callback_data` shape) and §13 (`[Settle]` = full settle of a line). Builds on ADR-0002 (ungated settlements) and ADR-0003 (per-group lock). Since ADR-0008 the amount token is denominated in integer minor units (`amount_minor`, not `amount_e2`); since ADR-0007 the same button also appears on settle-sheet lines.

## Context

Board lines are derived from `simplify` and never stored, so a `[Settle]` button has no persisted "suggested transfer" row to reference. Balances can also shift between when the board is rendered and when someone taps. The question is whether a tap settles the amount *shown* or the amount *current at tap time*. Recomputing to the current amount would silently record a payment different from what the user tapped — and would quietly re-introduce the amount-gating that ADR-0002 removed, since a settlement is a recorded stated payment, not a policed one.

## Decision

The board `[Settle]` button is WYSIWYG: tapping a line that shows ¥5000 records a ¥5000 settlement. The shown amount doubles as an optimistic-concurrency token.

- The button encodes the tuple **and** the amount: `v1:st:<from_id>:<to_id>:<ccy>:<amount_e2>` (well within 64 bytes). This is a deliberate departure from §6's "buttons carry only an id" — for the board, the tuple + amount *is* the payload, keeping the board a stateless projection with no per-render transfer table.
- On tap, under the per-group lock, recompute the current simplified `from→to` amount for that currency and compare to the token:
  - **shown == current** — record a settlement for the shown amount, re-render the board.
  - **shown ≠ current** — do **not** record; `answerCallbackQuery` warns the board was out of date, and the board is refreshed (edit in place; if the message is gone, post + re-pin a fresh one). The user re-taps against the truth.
  - **line gone** — treated as stale: refresh + "Already settled."

## Consequences

- Because the board is edited in place inside the locked transaction on every write, "stale" is an exception (a failed Telegram edit), not the steady state — so the warn-and-refresh path fires rarely.
- The `[Settle]` button stays a clean settle of real debt (shown == owed); `/settle` remains the deliberate-overpayment escape hatch. The two settlement paths keep distinct meanings.
- A tap never records a payment that contradicts the amount the user saw.
