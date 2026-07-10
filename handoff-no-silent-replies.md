# Handoff: no silent replies — sweep the silent-fail points

## The rule (owner's decision, 2026-07-11)

The bot must never answer an interaction **addressed to it** with silence. When it can't or
won't do what was asked — a closed feature door, an ambiguous request, a refused actor — it
replies with guidance ("here's why nothing happened, here's what to do instead").

Silence stays correct in exactly one case: the message was **not addressed to the bot**
(no mention, no reply to the bot, no command of ours, no tap). That line matches Telegram
privacy mode (§13): unaddressed messages usually aren't even delivered, and the bot must
not butt into human conversation. Lifecycle/service events (joins, leaves, migrations) are
not "addressed" either — they keep writing no reply.

## The task

Sweep every path that returns `[]` (or a bare callback ack) after the user addressed the
bot, decide each against the rule, and add guidance copy + a dispatch-level test per point.
Grep leads: `return []` in `expensir/core/handler.py`, plus the transports' error paths.

Known/suspected silent points as of this note (worktree `slice-vision-receipt-photo`,
after the issue #15 review fixes):

1. **Vision door closed** (`LLM_VISION_MODEL` unset): a photo that mentions the bot in its
   caption, or replies to a bot message / live proposal, returns `[]`
   (`_handle_group_message` photo gates; `_vision_door_open`). This was a deliberate slice
   decision ("photos invisible when unconfigured") — the rule now overrides it: reply
   "I can't read photos on this deployment — type it instead, e.g. …".
2. **Album strays**: caption-less items of a media group are invisible (`_album_stray`).
   Mostly right (N items must not fire N replies), but the case where the WHOLE album is
   caption-less and replies to a proposal now yields total silence — the review fix chose
   this deliberately (stateless, no media_group dedup table). One guidance reply for the
   album without N-times firing likely needs the first-item claim the fix avoided; weigh
   a `media_group_id` claim (ProcessedUpdate-style) vs answering every item idempotently.
3. **Text LLM unconfigured** (`deps.llm is None`): @mentions and replies to bot messages
   are left unanswered (`Deps.llm` comment says so explicitly). Pre-dates #15.
4. **Anonymous admin correction**: `_handle_reply_to_pending` returns `[]` when
   `actor is None`. The proposer chose to talk to the bot — say why nothing happened.
5. **Callback near-silence**: audit bare `answerCallbackQuery` acks (no toast text) on
   refused/stale/forged taps — a spinner that just stops is silence with extra steps.
   Many already carry toasts; verify each.
6. **`bot_username` unknown** (getMe failed at startup): mention/reply detection silently
   never matches. Probably a startup-failure concern rather than per-message guidance —
   decide, and at minimum log loudly.

Each fix: smallest change at the routing site, guidance copy in the voice of the existing
strings (`UNDO_POINTER`, `SETUP_GUIDANCE`, `PHOTO_FETCH_FAILURE`), one test per point in
the matching dispatch-level test file. Several existing tests ASSERT silence (e.g.
`test_without_a_vision_model_photos_are_left_unanswered`,
`test_only_the_captioned_item_of_an_album_corrects` in `tests/test_vision.py`) — repoint
them to assert the guidance instead; they encode the old policy, not the new rule.

Also record the rule durably: add it to `ARCHITECTURE-v2.md` §0 (invariants) or §13, and
check whether the unaddressed-photo line in `CONTEXT.md`'s **Receipt photo** entry stays
accurate ("ignored" remains right — it's unaddressed). An ADR is probably overkill (one
principle, cheaply reversible) unless the album fix ends up adding schema.

## Boundary cases to NOT "fix"

- Unaddressed messages (no mention/reply/command): stay silent — that's the rule's line.
- Service/lifecycle events: no reply, by design (§11).
- Other bots' commands (`/foo@other_bot`): not ours, stay silent.
