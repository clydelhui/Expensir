# Export/import: replace vs merge, and identity reconciliation

## Status

accepted — expands ARCHITECTURE.md §15.14, which named `replace|merge`, `schema_version`, and "round-trips to identical state" without defining id or identity handling.

## Context

A backup file carries primary keys and identity rows (`users.id`, `identities.platform_user_id`, `ledgers.id`, `created_by_action_id`, …). When those meet a live database, we must decide whether to trust the file's ids and how to reconcile people who already exist in the target group. The "no ghosts, registered members only" invariant complicates this: a backup can reference people not currently in the target group.

## Decision

- **Scope** is declared in the file header. `replace` swaps exactly that scope; `merge` adds a single-ledger file into the current group. Group/`all` files are operator-only and **replace-only** (deployment restore).
- **`replace`** clears the target scope and loads the file's rows **verbatim, preserving ids** — this is what makes round-trip *identical state* (ids, `created_by_action_id` links, and undo history intact).
- **`merge`** **never preserves incoming ids**. It reconciles members by `identities(platform, platform_user_id)` — existing identity → map to the existing `user_id`, otherwise mint a new user — then inserts ledgers/expenses/settlements under fresh ids, rewriting every foreign key through the remap. A merged ledger is a new ledger (name suffixed on collision).
- **No-ghosts on import:** import creates `identities` + `group_members` rows from the file for everyone it references, registering them in the target group. This is the one place registration happens from a file rather than a live Telegram `User`; the confirm screen discloses "will register N members from the backup."
- **`schema_version`:** refuse files newer than the running code; forward-migrate older files or refuse with a clear message. No silent best-effort load.

## Consequences

- Only `replace` gives a true identical round-trip. `merge` produces equivalent balances under new ids, and imported actions are **not** on the live undo stack (consistent with §9).
- Import is trusted to register members, a deliberate carve-out from the live-Telegram-account registration rule; it treats the file as data, never instructions.
