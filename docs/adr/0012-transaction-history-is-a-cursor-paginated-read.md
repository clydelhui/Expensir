# Transaction history is a cursor-paginated read, NL-reachable from day one

## Status

accepted — realizes the "expense history" content read ADR-0011 anticipated, and introduces
**Transaction** (= Expense | Settlement) as the domain umbrella term (CONTEXT.md).

## Context

There is no way to see a ledger's history: no "list transactions" query exists, and a ledger's
transactions are two row types (`Expense`, `Settlement`) with no shared table. A listing reply is a
static snapshot in a shared group chat while new transactions keep arriving underneath it, so
offset-based paging repeats rows on insert and skips rows on delete exactly when the group is
active. Pager buttons are inline callbacks, so whatever position state exists must fit Telegram's
64-byte `callback_data` with the handler staying stateless. ADR-0011's test says a content read
("what did we spend?") must be NL-reachable through the intent contract.

## Decision

- **`/transactions` and a `show_transactions` intent, both from day one.** The intent is
  **parameterless** — every history ask renders the same first page; filters ("only food", "last
  3", "only Bob's") are future work with their own design pass. Both doors share one runner: a
  plain read — no lock, no action row (ADR-0011 template).
- **One merged, totally-ordered stream.** Expenses and settlements merge ordered by
  `created_at DESC`, tiebroken by `(kind, id)` — ids alone can collide across the two tables.
  `occurred_on` is display-only (§7.2); order is always `created_at`. Soft-deleted rows are
  excluded. `Expense` gains a `(ledger_id, deleted_at, created_at)` index to match `Settlement`'s.
- **Keyset cursor paging, page size 10.** The pager anchors on the edge row:
  `v1:tx:<ledger_id>:<n|p>:<epoch_us>:<kind>:<row_id>` (~40 bytes). ▶ = strictly older than the
  anchor, ◀ = strictly newer (ascending, reversed). Inserts land above a forward anchor, so
  forward paging never repeats or skips. The ledger id is pinned at render time, like the settle
  sheet's — a pager tap keeps paging the ledger it was rendered for even if the group switched.
- **Snapshot semantics.** The reply re-renders only on pager taps, never on mutations — the feed
  (ADR-0013) is the live surface. A ▶ landing past the end after deletions renders "no older
  transactions" with ◀ still offered. The pager is shared: anyone's tap moves everyone's view,
  the same property every group surface already has.
- **Rendering.** Header: ledger name + total count, newest-first note — no page numbers (keyset
  has none). Each transaction is two lines via the shared formatter: description/direction and
  native amount (`fmt`, no ≈ equivalents) on the first; date, split summary, and an edited marker
  on the second. Empty ledger: a friendly "no transactions yet" nudge.
- **Shared primitives, built once, consumed by both surfaces** (this listing and the feed):
  - `expensir/domain/transactions.py::list_transactions(session, ledger_id, *, limit, cursor=None,
    direction="older") -> TransactionPage` — rows plus `has_newer`/`has_older`/`total`.
  - `expensir/format/transactions.py::transaction_line(tx) -> str` — the two-line rendering, so
    the listing and the feed can never drift apart.

## Consequences

- A displayed page goes stale the moment someone writes — by design; liveness is the feed's job.
- No page numbers; the total count in the header is the only size cue.
- The `v1:tx` grammar is versioned like every callback; filters would need a new verb or version,
  since filter state must also fit the 64-byte budget.
- `show_transactions` few-shots must teach the LLM to collapse qualified asks ("last 3 expenses")
  to the plain listing rather than hallucinate parameters.
