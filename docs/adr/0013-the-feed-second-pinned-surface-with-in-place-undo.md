# The feed: a second pinned surface with in-place undo

## Status

accepted — extends ADR-0003/0006/0007's framing: the board stays balances-only; recent activity
gets its own pinned surface. Also records the multi-surface undo model (D3).

## Context

Undo lives only on each mutation's scattered reply message, and there is no glanceable "what just
happened" view. Bolting history onto the board would overload the balances projection the ADRs
deliberately kept pure. Telegram allows many pinned messages: the header bar shows the most
recently pinned, the rest sit one tap away in the pin carousel, and each pin emits a one-time
service message. The undo handler today edits `callback["message"]` — the message hosting the
pressed button — which is correct only while the reply is the *only* undo surface;
`Action.result_chat_id`/`result_message_id` are recorded but were never read anywhere.

## Decision

- **The feed** is a per-ledger pinned message showing the last `feed_size` (Deps, default 5)
  standing transactions, newest first, rendered with the shared `transaction_line()` formatter
  (ADR-0012), each with its own WYSIWYG-labeled Undo button ("↩️ Undo Dinner ¥12,000"), callback
  `v1:fu:<action_id>`.
- **Lifecycle mirrors the board** (§13, ADR-0003): `feed_message_id`/`feed_chat_id` on the Ledger
  row with a composite unique index and the same savepoint guard; created and pinned on the
  ledger's first mutation; never deleted; edits best-effort. The feed is created and pinned
  **before** the board, so the board wins the chat-header slot — balances stay the headline, the
  feed sits in the carousel.
- **`sync_feed` runs beside every `sync_board` call site**, inside the locked write transaction —
  including the undo/redo path. Uniformity over selectivity: mutations that can't change the last
  5 still sync; Telegram's "message is not modified" 400 is swallowed like every board edit.
- **Multi-surface undo (D3).** State flips through the single `toggle()` entrypoint no matter
  which surface was pressed. The surface is identified by the callback verb, and each tap heals
  *both* surfaces:
  - a **feed tap** (`v1:fu`) re-renders the feed and flips the original reply's keyboard to Redo
    via the first-ever read of `Action.result_chat_id`/`result_message_id` — markup-only
    (`editMessageReplyMarkup`), since the reply's text isn't available from a feed callback; the
    "Undone by X" text line is skipped on this path. Honest button, slightly less annotated text.
  - a **reply tap** (`v1:undo`/`v1:redo`) keeps its existing full edit and now also `sync_feed`s.
- **Undone transactions drop out of the feed.** The feed is a projection of the current books,
  like the board: an undone row vanishes and an older one slides up. Redo lives where it always
  has — on the reply message whose keyboard the tap just flipped. No strikethrough, no dual redo
  surface.
- **The 24h window is not rendered.** Buttons appear regardless of age; `toggle()` decides on
  press (§9, computed-on-press) — the operator keeps a working button, everyone else gets the
  existing 🔒 answer. Rendered eligibility would go stale with no re-render to fix it.
- **Edited-then-undone expenses: no cascade.** Undoing an add whose expense was later edited
  soft-deletes the expense; the standing edit actions remain recorded against the hidden row, and
  a redo revives it with its edited values. Named, accepted.

## Consequences

- Two pin service messages per ledger (one-time), and roughly two best-effort edits per mutation
  under the per-group lock — acceptable for an expense bot; no rate-limit handling exists today
  and this does not add any.
- If a mutation's reply message was deleted from the chat, a feed undo still works but the redo
  button strands (the feed dropped the row; the reply's flipped keyboard is gone; undo/redo is
  button-only, §9). Accepted edge — before the feed, the *undo* itself was unreachable in that
  case.
- The feed widens the undo surface to the last `feed_size` transactions for everyone, at any age,
  gated only by `toggle()`'s window — a deliberate trust-model choice consistent with
  "anyone-may-act, Undo as the guardrail".
