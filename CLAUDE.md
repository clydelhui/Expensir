# CLAUDE.md

Guidance for agents working in the Expensir repo — a Telegram bot for expense tracking in Telegram groups.

## Authoritative spec

**`ARCHITECTURE-v2.md`** at the repo root is the authoritative, self-contained architecture & build
spec — build from it. The decisions that shaped it are recorded in `docs/adr/` (ADR-0001–0009), and
domain vocabulary lives in `CONTEXT.md`. The original `docs/archive/ARCHITECTURE-v1.md` is **archived
and must not be used** — several of its locked decisions were reversed (see the ADRs).

## Agent skills

### Issue tracker

Issues and PRDs are tracked as GitHub issues (clydelhui/Expensir) via the `gh` CLI. See `docs/agents/issue-tracker.md`.

### Triage labels

Default vocabulary: needs-triage, needs-info, ready-for-agent, ready-for-human, wontfix. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: one `CONTEXT.md` + `docs/adr/` at the repo root. See `docs/agents/domain.md`.
