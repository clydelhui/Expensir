Implemented in 2744c9c (local commit).

**What shipped**
- `domain/balances.py`: pure `replay(events)` (net[user][currency], + = owes the pool) plus `net_positions(session, ledger_id)` — splits fetched via a join that re-applies the seal + `deleted_at` predicates (an id `IN`-list would hit asyncpg's 32,767 bind-param cap on big ledgers)
- `/balance` and `/balance me` routed as reads: no advisory lock, no action row — both pinned by tests (a spy test proves /equal locks and /balance doesn't)
- `ShowBalance` added to the intent union; `format/render.balance_reply` renders per-currency buckets, debtors first, zero nets omitted, me-scope phrased as "you"

**Acceptance criteria all green** — 97 tests + 1 env-gated skip, mypy strict/ruff/black clean:
- order-independence + per-currency conservation proven by a 200-event shuffled property test over `allocate`-generated splits
- soft-deleted rows excluded (including the mixed live+deleted case)
- sealed to the active ledger (repointing `active_ledger_id` hides the other ledger entirely)
- live-verified through the webhook transport: group balance aggregated four real expenses across the prior slice's DB, zeroed JPY bucket omitted, me-scope leaked nothing, junk args → usage

**Review outcome:** trimmed 2-finder/2-vote adversarial panel confirmed zero defects; the four contested findings (asyncpg IN-cap, three unpinned behaviors) were all addressed anyway since each was a cheap join rewrite or regression pin.
