# Expensir

A self-hostable **Telegram bot for Splitwise-style expense splitting** in group chats. It responds
only when @mentioned, replied to, or commanded; understands slash commands, natural language, and
receipt photos; runs serverless and scales to zero when idle.

## Documentation

- **[`ARCHITECTURE-v2.md`](./ARCHITECTURE-v2.md)** — the authoritative, self-contained architecture &
  build spec. Start here.
- **[`CONTEXT.md`](./CONTEXT.md)** — the domain glossary (source of truth for vocabulary).
- **[`docs/adr/`](./docs/adr/)** — architecture decision records (ADR-0001–0009) explaining the
  choices that shaped v2 and refined it during the build.
- **[`docs/archive/ARCHITECTURE-v1.md`](./docs/archive/ARCHITECTURE-v1.md)** — the original spec,
  **archived**; kept for provenance only. Do not build from it.

## Status

In progress: the deterministic core is built and tested (slices 1–11) — transports, slash-command
expenses and split types, ledger lifecycle, balances, settling (board `[Settle]`, settle sheet,
ungated custom), undo/redo, and registration/`/setup`. Still to come: natural language, receipt
vision, FX display (`≈` / `/convert`), and export/import.
