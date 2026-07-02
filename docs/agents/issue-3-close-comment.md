Implemented in a927847 (local commit).

**What shipped**
- `Expense`/`ExpenseSplit`/`Action` models + migration `69c2b0dc2e9d` (indices per §5; splits also carry `created_by_action_id` per AC4)
- `core/locking.py`: `pg_advisory_xact_lock(hashtext('group:<id>'))` on Postgres, no-op on SQLite; acquired by both the /equal handler path (before currency resolution, so a concurrent /switch can't redirect it) and `apply_intent` itself, which also re-reads the group post-lock (ADR-0003)
- `intents/schema.py`: `AddExpense`/`SetHomeCurrency` discriminated-union start; the slash path resolves the currency **before** building the intent (minor-unit scaling needs the resolved currency), so command intents always carry a concrete currency
- `domain/apply.py`: one action row per mutation, rows stamped, refs re-resolved inside the lock; `set_home_currency` stores a `before_image` for slice-7 undo
- Rejections (`domain/errors.Rejection`) roll back via a savepoint that preserves the author's registration; unknown refs reject the whole intent with /setup guidance
- Reply: 📒 ledger prefix, visible #id, rounded-figure note, participant names always listed (covers the everyone case)

**All acceptance criteria green** — 83 tests + 1 env-gated skip, mypy strict/ruff/black clean; live-verified end-to-end through the webhook transport against a Bot API stub (migrated SQLite, secret header, curl updates): expense/dust/overflow/unknown-ref/everyone/rounding paths plus update_id dedupe.

**Adversarial review findings fixed (each reproduced first, now regression-tested)**
- amounts > 10^15 minor units rejected politely (previously an uncaught OverflowError at flush — crash loop in poll mode, retry storm in webhook)
- amounts rounding to 0 minor units rejected with a smallest-unit message (previously leaked `allocate`'s internal error text)
- webhook send-failure after commit keeps the dedupe claim (previously Telegram's retry double-recorded the expense; the reply is sacrificed instead)
- duplicate stored usernames (Telegram handle reuse + no identity refresh yet) get an ambiguity rejection instead of `MultipleResultsFound`
- per-group lock wiring pinned by a dispatch-level spy test

**Notes for later slices**
- `actions.result_chat_id/message_id` backfill needs the executor to report sent message ids (slice-7 prerequisite)
- poll transport still has no dedupe: a crash between dispatch and offset confirm re-applies committed mutations on restart — belongs with #19 hardening alongside backoff
- identity refresh on interaction (§11) deferred; the duplicate-username rejection above is the stopgap until the pick-list slice
