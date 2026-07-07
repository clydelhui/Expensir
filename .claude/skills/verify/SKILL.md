---
name: verify
description: Live-verify Expensir end-to-end — run the bot in webhook mode against a local Telegram API stub, post update dicts, and read the stub's JSONL log of outbound API calls. Use before committing a slice, or whenever asked to confirm a change works in the real app (not just tests).
---

# Live-verify Expensir

The surface is the Telegram Bot API: inbound updates hit `POST /webhook`; every
observable effect is an outbound API call (`sendMessage`, `editMessageText`,
`answerCallbackQuery`, `pinChatMessage`, …). So: stub the API, run the real app,
post updates, assert on the stub's JSONL log.

## Recipe

1. **Stub** (~45 lines, stdlib only): an `HTTPServer` answering
   `POST /bot<token>/<method>` with `{"ok": true, "result": …}` and appending
   `{"method", "payload"}` JSONL per call. Methods to answer: `getMe`
   (`{"id": 999999, "is_bot": true, "username": "expensir_bot"}`), `sendMessage`
   (incrementing `message_id` from 1000 — board creation stores it), `editMessageText`
   / `editMessageReplyMarkup` (echo `message_id`), everything else `true`.
   Write it to the session scratchpad; a known-good copy existed at
   `scratchpad/tg_stub.py` of session 9b3d1d14 (2026-07-07).

2. **Migrate a scratch DB** (also proves the migration chain):
   `DATABASE_URL="sqlite+aiosqlite:///$SCRATCH/live.db" uv run alembic upgrade head`

3. **Run the app** (background):
   `MODE=webhook BOT_TOKEN=stubtoken TELEGRAM_WEBHOOK_SECRET=s3cret TELEGRAM_API_BASE=http://127.0.0.1:18643 DATABASE_URL="sqlite+aiosqlite:///$SCRATCH/live.db" PORT=18642 uv run python -m expensir`

4. **Drive** with a Python script (urllib is fine) posting update dicts to
   `http://127.0.0.1:18642/webhook` with header
   `X-Telegram-Bot-Api-Secret-Token: s3cret` and a fresh `update_id` per post
   (the webhook dedupes on it). Sleep ~0.1s between posts. Update shapes match
   `tests/factories.py` (`message`, `callback_query`, `my_chat_member`).
   Chain steps by parsing the JSONL: e.g. take `callback_data` out of the last
   board `editMessageText`'s `reply_markup` and post it back as a tap.

5. **Read the JSONL** and compare each step's outbound calls to the spec.

## Flows worth driving

Always: bot-added → `/homecurrency EUR` → a second user speaks → `/equal` →
whatever the slice adds (buttons via extracted `callback_data`, undo taps,
rejection probes like a bad currency or a stale token).

## Gotchas

- A 500 from `/webhook` usually means the stub is down, not an app bug — the
  app's own crash traceback lands in its uvicorn output file.
- Wrong secret → 403 (free probe; confirms the app is up).
- macOS: no `timeout`; kill by port `lsof -ti tcp:18642 tcp:18643 | xargs kill`.
- `getMe` runs at startup — the app won't boot if the stub isn't up first.
