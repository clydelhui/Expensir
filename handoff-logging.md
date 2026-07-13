# Handoff: diagnostic logging slice

Grilled 2026-07-13. All decisions below are settled — implement, don't re-litigate.
Branch: `worktree-logging`. Companion docs written during the grill: ADR-0015
(`docs/adr/0015-logs-carry-metadata-at-info-content-at-debug.md`), CONTEXT.md
disambiguation note under **Logging currency**.

## Problem

The bot has no logging mechanism. Stdlib loggers exist in 4 modules (10 call sites, all
WARNING/exception) but no root handler is configured, so they surface only via Python's
last-resort stderr handler. In dev (`MODE=poll`) a crashed update is dropped invisibly
(`transports/poll.py:23-32`). ARCHITECTURE-v2.md §15.15 parks "structured logging" as an
unbuilt Harden item.

## Decisions (from the grill)

1. **Scope**: dev-visibility slice, JSON-ready. Human-readable text output now; call sites
   carry structured fields so a future Cloud Run JSON formatter is a formatter swap, not a
   re-instrumentation. The full §15.15 item stays parked.
2. **Library**: stdlib `logging` only. No structlog. New setup module (suggested:
   `expensir/logsetup.py` — avoid the name `expensir/logging.py`, it shadows stdlib).
3. **Content policy (ADR-0015)**: metadata at INFO (ids, kinds, intent type, latency,
   outcome); message text / LLM payloads / amounts / names at DEBUG only. Tracebacks may
   leak content — accepted, no scrubbing.
4. **Trace shape**: event trace, ~3–5 INFO lines per update:
   - transport: `update <id> received  chat=<id> user=<id> kind=<message|callback|...>`
   - handler: `update <id> intent=<cmd:equal | nl:add_expense | ...>`
   - llm (when hit): `update <id> llm parse ok  model=<m> <n>ms`
   - executor: `update <id> effects sent  sendMessage=1 editMessageText=1 <n>ms`
   - handler: `update <id> done  outcome=<ok|rejection|error> <n>ms`
   The `outcome=error` line (with traceback) is the replacement for today's invisible
   poll-mode drop. Raw update JSON, message text, LLM request/response, outbound payloads:
   DEBUG lines alongside.
5. **Correlation**: `ContextVar` set at transport entry (both poll and webhook), stamped
   onto every record by a `logging.Filter` installed in setup; formatter prints it. No
   signature changes anywhere. Async-safe under concurrent webhook requests.
6. **Config knob**: new `log_level` field in `Settings` (env `LOG_LEVEL`, default `INFO`).
   Controls `expensir.*` loggers only. Third-party loggers (`httpx`, `httpcore`,
   `sqlalchemy`) pinned to WARNING in setup. Uvicorn: `log_config=None` so its records
   propagate into our root handler (one format), `access_log=False` (our trace supersedes
   it). Setup runs first thing in `main()` (`expensir/__main__.py`).
7. **Destination**: stderr StreamHandler always; plus optional file — new `log_file` field
   in `Settings` (env `LOG_FILE`, default unset). When set, add a `RotatingFileHandler`
   (~5 MB × 3 backups, same format/level). Add `*.log` to `.gitignore`. Never set in prod
   (Cloud Run ingests stdout/stderr). Document both vars in `.env.example`, with the dev
   section suggesting `LOG_LEVEL=DEBUG`.
8. **Scope boundary**: log-only. No user-facing "something went wrong" reply from the poll
   except-block — that belongs to the no-silent-replies sweep
   (`handoff-no-silent-replies.md`). This slice only makes the drop visible.
9. **Domain stays pure**: no logging imports in `expensir/domain/` (§16 convention).
   Instrument transports, core, and adapters (llm, fx, telegram client) only.
10. **Testing**: light. Unit tests for the setup module (filter stamps `update_id`,
    `LOG_LEVEL` respected, `LOG_FILE` adds handler) + `caplog` assertions that intent and
    outcome lines fire, including `outcome=error` when dispatch raises. Finish with a live
    `/verify` run to eyeball the trace.

## Files to touch

- `expensir/logsetup.py` (new): `setup_logging(settings)`, the ContextVar +
  `set_current_update_id()` helper, the Filter, the formatter, lib-logger pinning,
  optional file handler.
- `expensir/config.py`: `log_level`, `log_file` fields.
- `expensir/__main__.py`: call setup first; pass `log_config=None`, `access_log=False`
  to uvicorn.
- `expensir/transports/poll.py`, `expensir/transports/webhook.py`: set ContextVar, emit
  received/done lines. Webhook answers dispatch/send failures with an explicit 500 after
  a `logger.exception` outcome=error line (not a re-raise: uvicorn would print the
  traceback untagged, after the contextvar reset). Both error paths — dispatch (claim
  released) and post-commit send (claim kept) — log a done line, and the 403/dedupe
  early returns each log too, since `access_log=False` removed uvicorn's record of them.
- `expensir/core/handler.py`: intent + outcome lines (small additions at the dispatch
  seam; do not thread ids).
- `expensir/transports/executor.py`: effects-summary line + timing.
- `expensir/llm/openai_compat.py`, `expensir/fx/frankfurter.py`: latency INFO lines,
  payload DEBUG lines.
- `.env.example`, `.gitignore`, tests.

## Out of scope

JSON formatter, Cloud Logging severity mapping, metrics/tracing/Sentry, crash replies to
users, log-based alerting.
