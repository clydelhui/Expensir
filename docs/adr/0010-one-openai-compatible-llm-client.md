# One OpenAI-compatible LLM client, provider chosen by base URL

## Status

accepted — amends ARCHITECTURE-v2.md §1/§2/§14, which sketched per-provider modules
(`llm/cloudflare.py`, `groq.py`, `gemini.py`) selected by `LLM_TEXT_PROVIDER`.

## Context

Every text provider on the table — Cloudflare Workers AI (`…/ai/v1`), Groq, OpenRouter,
DigitalOcean — exposes the same OpenAI-compatible chat-completions API; only the base URL, key,
and model id differ. Per-provider modules would make each "new provider" a code change for what
is actually the same wire protocol, and the operator (slice 12 grill, 2026-07-07) explicitly
wants to try OpenRouter/DigitalOcean later without one.

## Decision

- **One client, `llm/openai_compat.py`,** implements the `LLMClient` protocol for the text path.
- **Config is endpoint-shaped, not provider-shaped:** `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL`
  replace `LLM_TEXT_PROVIDER`/`CF_ACCOUNT_ID`/`CF_API_TOKEN`/`GROQ_API_KEY`. Swapping provider is
  an `.env` edit. Cloudflare stays the documented default.
- **Recorded fixtures stay provider-agnostic** for the same reason: they capture the
  chat-completion response shape, so one fixture set exercises the parse path regardless of
  which endpoint produced it.

## Consequences

- A provider that is *not* OpenAI-compatible (Gemini's native API, considered as a vision
  swap-in) would be the first genuine second module — the protocol seam (`llm/base.py`) is where
  it plugs in, unchanged.
- Model ids remain volatile and provider-specific; `LLM_MODEL` must be verified against the
  chosen provider's dashboard (§1 note).
