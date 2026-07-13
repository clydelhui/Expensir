# Logs carry metadata at INFO, message content at DEBUG

## Status

accepted

## Context

Expensir lives in group chats and handles people's financial activity. Diagnostic logging
(ARCHITECTURE-v2.md §15.15, until now unbuilt) has to choose what a log line may contain:
the useful trace for debugging an NL misparse is exactly the sensitive part — the message
text, the LLM request/response, amounts, payee names. In prod (Cloud Run) everything
written to stdout/stderr is ingested into Cloud Logging and retained; a leaked log cannot
be unleaked.

## Decision

- **INFO logs metadata only**: ids (`update_id`, `chat_id`, `user_id`), update kind, intent
  type or command name, adapter latency, outcome. Never message text, LLM payloads,
  amounts, or names.
- **DEBUG adds content**: raw message text, LLM request/response, outbound payloads. Dev
  runs `LOG_LEVEL=DEBUG`; prod runs INFO.
- One knob, no redaction machinery. The level *is* the privacy boundary.
- **Accepted leak**: `logger.exception` tracebacks may carry content in exception messages.
  Tracebacks are the point of error logs; we do not scrub them.

## Consequences

- Prod Cloud Logging only ever sees ids and outcomes; debugging a prod NL misparse
  requires reproducing in dev at DEBUG (the LLM fixture recorder helps here).
- Any future log call site must sort its fields by this rule; a content field at INFO is a bug.
- Setting `LOG_LEVEL=DEBUG` in prod is the one way to violate the boundary — it is a
  deliberate operator act, not a default.
