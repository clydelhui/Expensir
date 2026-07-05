# Serialize mutating writes per group with a Postgres advisory lock

## Status

accepted — fills a gap in ARCHITECTURE.md §6/§13, which specified `update_id` dedupe and idempotent undo toggles but nothing for two *distinct* concurrent writes to the same ledger.

## Context

Cloud Run runs N stateless instances with no per-group affinity, so two updates for the same group can be handled concurrently. Balances are safe (derived by order-independent replay), but three things race: the pinned board (each writer renders it from its own read, so the surviving `editMessageText` can reflect only one of two concurrent expenses), the `active_ledger_id` pointer (an `add` can resolve the active ledger before a concurrent `switch` commits), and board creation (two writers each see "no board yet" and create two boards).

## Decision

Take `pg_advisory_xact_lock(hashtext(group_key))` at the start of every mutating transaction (`apply_intent`, undo, redo), making the read-modify-write of balances → board → active pointer atomic per group. The lock releases automatically at transaction end. Reads (`/balance`, `/convert`, `/rates`, `/export`) take no lock.

- The board is rendered from post-write balances **inside** the locked transaction, so its content is consistent; the `editMessageText` API call stays best-effort/cosmetic.
- Board creation is guarded by the same lock plus a composite unique constraint on `ledgers.(board_chat_id, board_message_id)`. Composite, not single-column: Telegram message_ids are unique only per chat, so two groups' boards would routinely collide on a bare `board_message_id`.
- SQLite (local dev) serializes writes globally, so the lock is a harmless no-op there.

## Consequences

- Throughput for a single very chatty group is capped to one writer at a time. Acceptable for an expense bot.
- Introduces a hard dependency on Postgres advisory locks in production; a different write store would need its own per-group serialization.
- The board can be trusted as always-current after any write, rather than being eventually consistent.
