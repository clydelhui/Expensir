# Container-inspection reads are slash-only, not NL-reachable

## Status

accepted — carves out an exception to ARCHITECTURE-v2.md §0 ("Every other command is
NL-reachable"), and documents the behaviour `/ledgers` already shipped with.

## Context

§0 stated that only undo/redo are non-NL, and every other command is NL-reachable. But `/ledgers`
was already built slash-only: it has no intent kind in the `Intent` union, no branch in
`intents/nl.py`, and no few-shot in `llm/prompts.py` — it dispatches straight through `_RUNNERS`
as a plain read. Adding `/members` (slice grill, 2026-07-08) forced the question of whether a new
container-roster read should follow `/ledgers` (slash-only) or `/balance` (NL-reachable), and
surfaced that §0 was already not literally true.

The reads split cleanly by *what they inspect*. **Content reads** — `/balance`, `/convert` — answer
"who owes what?", the bot's whole reason to exist; people ask them conversationally ("what do I
owe?"), so NL-reachability is worth its cost. **Container-inspection reads** — `/ledgers`,
`/members` — answer "what's *in* this container?"; they are structural utilities, rarely phrased
conversationally, and their NL surface ("list everyone", "show ledgers") is both low-value and prone
to colliding with content intents during parse.

## Decision

- **Container-inspection reads (`/ledgers`, `/members`) are slash-only.** No `Intent` kind, no NL
  branch, no few-shot. They dispatch through `_RUNNERS` and return a `Reply` directly — no lock, no
  action row, like every read.
- **Content reads stay NL-reachable** (`/balance` → `ShowBalance`, with few-shots), unchanged.
- The rule is "**structural reads are slash-only; content reads are NL-reachable**" — applied to
  future reads too, not a one-off for these two.

## Consequences

- Skipping the intent contract for these reads is deliberate: routing "list members" through the
  LLM would spend a call and risk mis-parsing a roster request as an expense. The cost of *not*
  being NL-reachable is a user who types "who's in here?" and gets an `unknown` nudge toward
  `/members` — an acceptable trade for a structural utility.
- A future read that is genuinely *content* (e.g. an expense history) would be NL-reachable and go
  through the intent contract; the split is the deciding test, not "is it a read?". (Realized:
  ADR-0012's transaction history shipped NL-reachable from day one, as this test predicted.)
