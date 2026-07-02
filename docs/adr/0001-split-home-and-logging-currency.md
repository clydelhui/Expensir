# Split currency into group-level home currency and ledger-level logging currency

## Status

accepted — amends ARCHITECTURE.md §0/§3, which originally locked "one group-wide currency, ledgers carry no currency."

## Context

The original spec made `groups.default_currency` serve two duties at once: the default currency for new expenses *and* the single target every `≈` equivalent converts to. Ledgers are used as per-trip containers (e.g. a "Japan Trip" ledger), and a trip naturally logs in its own currency. Forcing one group-wide currency meant every expense on a JPY trip had to override the default by hand, while the group default stayed pinned to the home currency.

## Decision

Separate the two duties into two concepts:

- **Home currency** — group-level (`groups.default_currency`), the `≈` display target. Set by `/homecurrency <ISO>` → `set_home_currency` intent. Rare; usually set once at onboarding.
- **Logging currency** — ledger-level, the default currency for new expenses in that ledger. Set by `/currency <ISO>` → `set_logging_currency` intent, or as an optional trailing ISO on `/newledger <name> [ISO]`. A ledger's logging currency is nullable and resolves to the group home currency when unset.

Per-expense currency override is unchanged. The `≈` equivalent and `/convert` still target the home currency only; FX remains display-only.

## Consequences

- `ledgers` regains a nullable `logging_currency` column (the spec had explicitly removed it).
- The single `SetCurrency` intent / `/currency` command splits in two; `/currency` now targets the active ledger, not the group. Onboarding copy must teach both commands so the old single-currency mental model doesn't mislead.
- Changing the home currency restyles every `≈` line across all ledgers; changing a ledger's logging currency affects only future expenses in that ledger.
