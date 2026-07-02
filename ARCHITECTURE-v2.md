# Expensir — Architecture & Build Spec (v2)

A self-hostable **Telegram bot for Splitwise-style expense splitting** in group chats.
It responds only when **@mentioned, replied to, or commanded**; understands slash commands,
natural language, and **receipt photos**; runs **serverless** and scales to zero when idle.

**This document is authoritative and self-contained.** Build directly from it. Read **§0
Invariants** first; they constrain everything else. When two readings are possible, prefer the one
that keeps the domain pure and routes all writes through `apply_intent`.

> **This is v2**, superseding the original `ARCHITECTURE.md`. It incorporates the decisions in
> `docs/adr/0001`–`0006`; each affected section links the ADR that governs it. The glossary in
> `CONTEXT.md` is the source of truth for domain vocabulary.

> **Decisions locked in this spec (call out if you disagree):**
> - No cross-currency settlement. Each currency settles on its own. FX is **display-only**.
> - **Two currency layers** (ADR-0001): a group-wide **home currency** (`groups.home_currency`) that
>   every `≈` equivalent converts to, and a per-ledger **logging currency** (`ledgers.logging_currency`,
>   nullable → resolves to home) that is the default for new expenses in that ledger. An expense
>   **freezes its currency at creation**; later currency changes never re-denominate it.
> - **Ledgers are sealed.** Balances and settlements never cross ledger boundaries. `/balance` and
>   `settle up` act on the active ledger only, and say nothing about other ledgers.
> - **No ghosts.** Only registered members may appear in a transaction. Any reference to an
>   unregistered person **rejects the whole intent** with guidance on how to register them. (Import is
>   the one exception — it registers members from the backup file, ADR-0005.)
> - **Undo/redo are button-only**, never NL-triggered. Every other command is NL-reachable.
> - **Settlements are recorded stated facts** (ADR-0002), and every settlement is **one line** —
>   one currency, one direction, one action (ADR-0007). The board `[Settle]` button and the settle
>   sheet follow the solver; `/settle` with an amount is an ungated escape hatch (any direction,
>   overpayment OK).
> - Money is integer **minor units** — the currency's smallest circulating unit: cents, yen, fils
>   (ADR-0008). Allocation distributes whole smallest units uniformly for every currency (no
>   per-currency formula), so JPY splits in whole yen — no fake cents. The leftover unit rotates
>   deterministically per expense — never systematically the payer.

---

## 0. Invariants (non-negotiable)

1. **One Intent contract.** Slash commands, natural language, and receipt vision all produce the
   *same* `Intent` discriminated union (§4). Everything downstream is shared and never branches on
   "where did this come from." The LLM is a **parser only** — never the source of truth for math
   or persistence.

2. **Two write entrypoints, both transactional and audited.**
   - `apply_intent(intent, ctx)` (§8) — the forward path. Every mutation goes through it, in one DB
     transaction. It appends exactly **one `actions` row**, and **every data row it writes carries
     that row's `action_id`** (`created_by_action_id`). Undo then = "soft-delete every row created
     by this action."
   - `undo` / `redo` (§9) — operate on the `actions` log itself, not through a new intent. Same
     transactional discipline.
   No other code path writes to the database.

3. **Money is integer minor units** — a currency's *smallest circulating* unit (cent, yen, fils;
   ADR-0008). **No floats** in storage or math. Allocation distributes whole smallest units,
   identically for every currency; the per-currency `minor_digits` (§3) is used **only** to parse
   the major-unit string a person types and to format output. Round only at the smallest-unit
   boundary — at parse time, via `Decimal` — surfaced visibly when it happens.

4. **Balances are derived, never stored.** Computed by **replaying** non-deleted
   `expenses + settlements` (§7.2). Deleting / undoing = soft-delete + recompute. There are no
   stored balance columns and no frozen snapshots. The pooled net is **order-independent** (a sum of
   deltas), so `occurred_on` is display-only and back-dating never changes a balance (§7.2, §17).

5. **Transport-agnostic core.** `core.handler.dispatch(update_dict) -> list[OutboundAction]` knows
   nothing about webhook vs polling, and the domain layer imports no Telegram/LLM/network types. A
   future web dashboard reads the same tables and replays the same way.

6. **Privacy-respecting invocation.** The bot acts only on: slash commands, @mentions of itself,
   replies to its own messages, button taps (callback queries), photos that mention/reply to it,
   document uploads, and member/chat service events. It never reads general group chatter.

7. **Confirm policy (single rule, no exceptions).**
   - **Deterministic + fully specified** (a slash command with everything it needs) → **commit
     immediately**, reply carries an ↩️ Undo button. No confirm tap.
   - **Fuzzy** (anything from NL or OCR, or ambiguous reference resolution) → **propose + confirm**
     first. `confidence` is **cosmetic only** — it never changes this branch; NL/OCR always confirm
     regardless (§4, §12).
   - **Reads never confirm** (`/balance`, `/convert`, `/ledgers`, `/rates`, `/export`, and the
     settle sheet — `/settle` with no amount, ADR-0007).

8. **Pure, testable domain.** Allocation, balance replay, simplification, and settlement math are
   pure functions with no I/O — unit-tested in isolation. The flaky/paid parts (Telegram, LLM, FX)
   sit at the edges behind protocols and are faked in tests.

9. **Registered members only.** A transaction may reference only members the bot has registered in
   this group (§11). A reference to anyone unknown **rejects the entire intent** with guidance.
   Nothing partial commits. (Import registers members from the backup file — the sole exception,
   ADR-0005.)

10. **Ledgers are sealed** (ADR-0004, §3). Balances, settlements, the board, and "settle up" are all
    scoped to a single ledger. Cross-ledger netting does not exist; the bot stays silent about other
    ledgers.

11. **Writes are serialized per group** (ADR-0003). Every mutating transaction takes a per-group
    Postgres advisory lock so the read-modify-write of balances → board → active-pointer is atomic.
    Reads take no lock — except that a read-triggered board `≈` refresh (§13) takes it for the
    render + edit only.

12. **Side effects are returned as data.** The core never sends a Telegram message directly; it
    returns `OutboundAction`s and a thin executor at the transport edge performs them.

---

## 1. Tech stack

| Concern | Choice | Notes |
|---|---|---|
| Language | **Python 3.12** | type hints everywhere |
| Web framework | **FastAPI + uvicorn** | thin webhook handler only |
| Telegram | **`httpx`** calling the Bot API directly | do **not** use `python-telegram-bot` |
| Models/validation | **Pydantic v2** + **pydantic-settings** | the Intent contract is Pydantic |
| ORM / migrations | **SQLAlchemy 2.x (async)** + **Alembic** | |
| DB | **Postgres** (Neon in prod) / **SQLite** locally | use Neon's **pooled** connection string; the per-group advisory lock (§0.11) needs Postgres, and is a harmless no-op on SQLite (which serializes writes globally) |
| LLM (text) | **Cloudflare Workers AI**, OpenAI-compatible endpoint | swappable: Groq, Gemini |
| LLM (vision) | **Cloudflare Workers AI** vision model | swappable: Gemini |
| FX rates (**display only**) | **Frankfurter** | free, no key, ECB daily, EUR-based (triangulate) |
| Deploy | **Google Cloud Run** (one container) | portable: Fly.io / Railway / laptop |
| Lint/format/type | **ruff** + **black** + **mypy** | |
| Tests | **pytest** | pure-domain tests need no network; LLM/FX use recorded fixtures |

Provider model IDs are volatile — verify in the provider dashboard before relying on them. The FX
provider is used **only** for `≈` equivalent lines and `/convert`; it never touches ledger math, so
an FX outage degrades display only (§3, §7.5).

---

## 2. Repository layout

```
expensir/
├── expensir/
│   ├── config.py                 # pydantic-settings: tokens, DB url, providers, OPERATOR_USER_ID, MODE
│   ├── transports/
│   │   ├── webhook.py            # FastAPI app + Cloud Run entry; secret-header check; update_id dedupe
│   │   ├── poll.py               # getUpdates loop for local dev
│   │   └── executor.py           # performs the list[OutboundAction] the core returns
│   ├── telegram/
│   │   ├── client.py            # httpx wrapper (sendMessage, editMessageText/ReplyMarkup, getFile,
│   │   │                        #   sendDocument, pinChatMessage, answerCallbackQuery, setMyCommands)
│   │   ├── types.py             # minimal Pydantic Update/Message/CallbackQuery/Document/User
│   │   └── keyboards.py         # inline keyboards: confirm/cancel, split-type, member picker, settle, pick-list
│   ├── core/
│   │   ├── handler.py           # dispatch(update) -> list[OutboundAction]; transport-agnostic
│   │   ├── router.py            # classify an update into exactly one path (§6)
│   │   ├── outbound.py          # OutboundAction model
│   │   ├── locking.py           # per-group advisory lock helper (§0.11, ADR-0003)
│   │   └── pending.py           # park / fetch / expire unconfirmed intents (DB-backed)
│   ├── intents/
│   │   ├── schema.py            # the shared Intent contract (Pydantic discriminated union)
│   │   ├── commands.py          # deterministic slash parsers (CPU, no LLM)
│   │   └── nl.py                # LLM text + vision -> Intent; refine(intent, correction)
│   ├── llm/
│   │   ├── base.py              # LLMClient protocol: extract_text / extract_vision / refine
│   │   ├── cloudflare.py        # default (OpenAI-compatible)
│   │   ├── groq.py / gemini.py  # swap-ins
│   │   └── prompts.py           # system prompt + few-shot covering EVERY intent kind (§12)
│   ├── domain/
│   │   ├── apply.py             # apply_intent — THE forward write path (§8)
│   │   ├── undo.py              # undo/redo on the actions log (§9)
│   │   ├── allocate.py          # split a total into per-person minor-unit shares (rotating tiebreak)
│   │   ├── balances.py          # replay -> per-user net per currency
│   │   ├── simplify.py          # minimum cash-flow per currency (deterministic)
│   │   ├── settle.py            # same-currency settlement (board / full / custom, §7.3)
│   │   ├── fx.py                # Frankfurter fetch + cache + triangulation — DISPLAY ONLY
│   │   ├── convert.py           # /convert and ≈ equivalents (pure reads)
│   │   ├── money.py             # minor-unit parsing/formatting; per-currency minor_digits (§3)
│   │   ├── currency.py          # home/logging resolution order (§3, ADR-0001)
│   │   └── identity.py          # resolve refs -> registered member, or fail (§11)
│   ├── db/
│   │   ├── models.py            # SQLAlchemy models
│   │   ├── session.py           # async engine / session factory (pooled)
│   │   └── repo.py              # data-access functions
│   ├── backup/
│   │   ├── export.py            # DB -> JSON (scope + schema_version stamped)
│   │   └── import_.py           # JSON -> DB (validate, replace|merge, confirm, pre-snapshot, operator-only)
│   └── format/
│       ├── render.py            # reply formatting (each bucket + ≈ equivalent)
│       └── board.py             # the pinned balance board (build + edit-in-place)
├── migrations/                  # Alembic
├── tests/
├── Dockerfile · pyproject.toml · .env.example
├── ARCHITECTURE-v2.md           # this file (authoritative)
├── CONTEXT.md · docs/adr/       # glossary + decision records
├── CLAUDE.md                    # short agent guide
└── README.md
```

---

## 3. Money & currency model (`domain/money.py`, `domain/currency.py`) — ADR-0001

**Storage unit.** All amounts are integer **minor units** — the currency's smallest circulating
unit: cent, yen, fils (ADR-0008). `$60.00 → 6000`, `¥6000 → 6000`, `BHD 6.000 → 6000`. No floats
in storage or math; whole-unit-ness is guaranteed by the representation itself (there is no
sub-unit precision to police).

**Minor digits.** Each ISO currency has a `minor_digits` count (most 2; some 0 or 3). Maintain a
small table; default unknown codes to 2. It is used **only** to parse a typed major-unit amount and
to place the decimal point when formatting — it does **not** affect allocation granularity.

| `minor_digits` | examples | smallest unit | 1 major unit |
|---|---|---|---|
| 0 | JPY, KRW, VND, CLP, ISK | yen/etc. | `1` |
| 2 | USD, EUR, SGD, GBP (default) | cent | `100` |
| 3 | BHD, KWD, OMR, TND, JOD | fils/etc. | `1_000` |

`1 major unit = 10**minor_digits` minor units.

Functions:
- `to_minor(amount_str, currency) -> (minor: int, was_rounded: bool)`: parse with `Decimal`, scale
  by `10**minor_digits`, round **half-up at the minor-unit boundary**. `"60" USD → 6000`,
  `"60.50" USD → 6050`, `"6000" JPY → 6000`. Input more precise than the currency's minor unit
  rounds **visibly**: the proposal/result shows the rounded figure (e.g. `¥6000.50 → ¥6001`).
- `fmt(minor, currency) -> str`: format with `minor_digits` decimals and the code/symbol. Display
  only.

**Two currency layers (ADR-0001).**
- **Home currency** (`groups.home_currency`, group-wide): the single currency every `≈` equivalent
  and `/convert` total is shown in. The "what's this worth back home" frame. Display only — never
  used in ledger math. Set via `/homecurrency <ISO>` → `set_home_currency` intent. Usually set once
  at onboarding.
- **Logging currency** (`ledgers.logging_currency`, per ledger, **nullable**): the default currency
  for *new* expenses in that ledger. Set via `/currency <ISO>` → `set_logging_currency`, or as a
  trailing ISO on `/newledger <name> [ISO]`. When null it **resolves to the home currency at read
  time** (so a never-set ledger follows a later home change).

**Currency resolution order for a new expense:**
```
explicit per-expense override (ISO after amount / NL "30 SGD" / "¥6000")
  else active ledger's logging_currency (if set)
    else group home_currency (if set)
      else: cannot resolve → reject with
            "Set a currency first: /currency <ISO> for this ledger, or /homecurrency <ISO> for the group"
```
The resolved currency is **frozen onto the expense row at creation** (`expenses.currency`). Later
changes to logging or home currency **never re-denominate an existing transaction** (§18 non-goal:
no automatic FX on stored expenses). Logging currency is a default-picker only.

**Per-expense override.** An expense may specify any currency via an ISO code immediately after the
amount (`/equal 30 SGD trains @a`) or via NL words/symbols. The proposal/result always shows the
resolved currency so a wrong default is visible.

**`≈` equivalents.** Board lines and balance buckets in a non-home currency show the home-currency
equivalent, labeled approximate. Whenever the board or a balance is rendered and the cached **API**
rate was not fetched today, fetch a fresh rate first; **manually pinned rates are used as-is and
never auto-refreshed** (§7.5). If no rate is available (FX down or unsupported and no manual pin),
show the amount followed by **`(≈ n/a)`** and do not block anything. Equivalents are pure reads,
never stored (§7.5).

---

## 4. The Intent contract (`intents/schema.py`)

```python
from typing import Annotated, Literal, Union
from pydantic import BaseModel, Field

class SplitMember(BaseModel):
    user_ref: str                  # "@alice" or a display name as seen in chat
    weight: float | None = None    # split_type="shares"
    exact_minor: int | None = None # split_type="exact"
    percent: float | None = None   # split_type="percent"

class AddExpense(BaseModel):
    kind: Literal["add_expense"] = "add_expense"
    payer_ref: str
    amount_minor: int
    currency: str | None = None    # None -> resolution order (§3)
    description: str
    occurred_on: str | None = None # ISO date; DISPLAY ONLY (replay is order-independent, §7.2)
    split_type: Literal["equal", "exact", "shares", "percent"] = "equal"
    participants: list[SplitMember] = []   # empty -> all REGISTERED members (payer included)
    confidence: float | None = None        # LLM self-report; COSMETIC ONLY (§0.7)

class SettleUp(BaseModel):
    kind: Literal["settle_up"] = "settle_up"
    from_ref: str                  # without an amount the pair is UNORDERED (ADR-0007)
    to_ref: str
    amount_minor: int | None = None  # None -> settle sheet: a READ; per-line [Settle] buttons (ADR-0007)
    currency: str | None = None    # required when amount given
    # With an amount, this is the CUSTOM path: ungated — any direction, overpayment allowed (ADR-0002)

class ShowBalance(BaseModel):      # /balance and /convert — active ledger only (sealed, §0.10)
    kind: Literal["show_balance"] = "show_balance"
    scope: Literal["me", "group"] = "group"
    convert_to: str | None = None  # /convert <TARGET>: consolidate all buckets into one currency

class DeleteExpense(BaseModel):
    kind: Literal["delete_expense"] = "delete_expense"
    expense_id: int                # resolved via reply-to-target / #id / descriptive match (§11)

class EditExpense(BaseModel):      # non-financial fields ONLY
    kind: Literal["edit_expense"] = "edit_expense"
    expense_id: int
    description: str | None = None
    occurred_on: str | None = None

class NewLedger(BaseModel):
    kind: Literal["new_ledger"] = "new_ledger"
    name: str
    logging_currency: str | None = None    # optional trailing ISO on /newledger

class SwitchLedger(BaseModel):
    kind: Literal["switch_ledger"] = "switch_ledger"
    name_or_id: str

class ArchiveLedger(BaseModel):
    kind: Literal["archive_ledger"] = "archive_ledger"
    name_or_id: str | None = None  # None -> active ledger

class UnarchiveLedger(BaseModel):  # reopens; does NOT switch (orthogonal verbs, §17)
    kind: Literal["unarchive_ledger"] = "unarchive_ledger"
    name_or_id: str

class SetHomeCurrency(BaseModel):  # group-wide ≈ target (ADR-0001)
    kind: Literal["set_home_currency"] = "set_home_currency"
    currency: str

class SetLoggingCurrency(BaseModel):   # active ledger's new-expense default (ADR-0001)
    kind: Literal["set_logging_currency"] = "set_logging_currency"
    currency: str

class SetFxRate(BaseModel):        # pin a DISPLAY rate for ≈ / convert
    kind: Literal["set_fx_rate"] = "set_fx_rate"
    base: str
    quote: str
    rate: float | None = None      # None -> fetch from Frankfurter

class Setup(BaseModel):            # register pre-existing members (§11)
    kind: Literal["setup"] = "setup"
    # populated from reply target and/or text_mention entities by the router; see §11

class Unknown(BaseModel):          # LLM couldn't map it -> ask to rephrase
    kind: Literal["unknown"] = "unknown"
    reason: str

Intent = Annotated[
    Union[AddExpense, SettleUp, ShowBalance, DeleteExpense, EditExpense,
          NewLedger, SwitchLedger, ArchiveLedger, UnarchiveLedger, SetHomeCurrency,
          SetLoggingCurrency, SetFxRate, Setup, Unknown],
    Field(discriminator="kind"),
]
```

**Not Intents.** Undo, redo, `[Settle]`-line taps (board & settle sheet), and pick-list disambiguation are **callback actions**
(§9, §6, §13). **Export/import** are document/file flows in `backup/`, not through `apply_intent`
(import takes its own pre-snapshot and is not on the per-action undo stack).

**Undoability of each kind.** `add_expense`, `settle_up` (with amount; the no-amount settle sheet
is a read), `delete_expense`, `edit_expense`,
`new_ledger`, `switch_ledger`, `archive_ledger`, `unarchive_ledger`, `set_home_currency`, `set_logging_currency`,
`set_fx_rate` all carry an Undo button. **`setup` (registration) is permanent and carries no Undo
button.** `show_balance` and `unknown` are reads/no-ops and write no action row.

---

## 5. Data model (`db/models.py`)

All money is integer minor units (ADR-0008). Core is Telegram-agnostic; Telegram identity lives
only in `identities`.

```
users            (id, display_name)
identities       (user_id, platform, platform_user_id, username)   -- platform='telegram'
                   -- a member EXISTS only once we have their identity row (no ghosts)
groups           (id, platform_chat_id, name, home_currency, active_ledger_id)
                   -- home_currency = group-wide ≈ target (ADR-0001); nullable until set
group_members    (group_id, user_id, joined_at, left_at)
                   -- left_at set on leave; cleared on re-join/interaction (reactivation, §11)

ledgers          (id, group_id, name, status['open'|'archived'], logging_currency,
                  created_at, archived_at, board_message_id, board_chat_id)
                   -- logging_currency NULLABLE -> resolves to home at read time (ADR-0001)
                   -- board_message_id UNIQUE (create-board-once guard, ADR-0003)

expenses         (id, ledger_id, payer_id, amount_minor, currency, description, occurred_on,
                  split_type, source['command'|'nl'|'ocr'], created_by_user_id,
                  created_by_action_id, created_at, edited_at, deleted_at)
                   -- currency is FROZEN at creation (§3); never re-denominated
expense_splits   (expense_id, user_id, owed_minor)        -- exact minor-unit share per participant

settlements      (id, ledger_id, from_user, to_user, amount_minor, currency,
                  created_by_action_id, created_at, deleted_at)
                   -- always single-currency, concrete amount; a recorded stated fact (ADR-0002)

fx_rates         (id, base_currency, quote_currency, rate,
                  source['manual'|'api'], fetched_at, set_by)   -- group-wide; manual beats api
                   -- DISPLAY ONLY

actions          (id, ledger_id, actor_user_id, kind, intent_json JSON, before_image JSON,
                  result_chat_id, result_message_id, created_at, undone_at, undone_by)
                   -- before_image: only for pointer/field flips (switch, currency, fx, edit);
                   --   row-creating ops are reversed via created_by_action_id, no before_image.

pending_intents  (id, chat_id, message_id, ledger_id, intent_json JSON, created_at, expires_at)
                   -- keyed by the PROPOSAL message_id; stores the UNRESOLVED intent (§10)
                   -- ledger_id = pinned at propose time; confirm commits THERE, not to the
                   --   current active ledger (WYSIWYG, §10)

processed_updates(update_id PK, seen_at)                 -- webhook idempotency
```

Indices: `expenses(ledger_id, deleted_at)`, `settlements(ledger_id, deleted_at, created_at)`,
`actions(ledger_id, undone_at)`, `identities(platform, username)`,
`identities(platform, platform_user_id)`, `expenses(created_by_action_id)`,
`settlements(created_by_action_id)`. Unique: `ledgers(board_message_id)`.

Rules:
- `groups.active_ledger_id` must always point to an **open** ledger of that group. Operations that
  could orphan it repoint deterministically or refuse (§8, ADR-0004).
- Soft-deleted rows (`deleted_at IS NOT NULL`) are excluded from all balance/list queries.
- A user is "registered in this group" iff they have an `identities` row **and** a `group_members`
  row for this group with `left_at IS NULL`. A member who left keeps balances and remains
  referenceable by id; a re-join or interaction clears `left_at` (reactivation, §11).

---

## 6. Request lifecycle (`core/`)

```
Telegram update (webhook push OR getUpdates poll)
  -> transport: validate secret header (webhook) · dedupe by update_id
  -> core.handler.dispatch(update_dict)            [transport-agnostic from here down]
  -> core.router classifies into exactly ONE:
       callback_query .................. confirm/cancel · undo/redo · board-settle · split-type/picker · pick-list
       reply to a LIVE pending proposal  nl.refine(pending, text) -> edit proposal in place
       reply to any OTHER bot message .. new NL intent (as if @mentioned) — incl. expired proposals & board
       slash command ................... intents.commands (CPU parse)
       @mention (text) ................. intents.nl.extract_text -> Intent
       photo (mention in caption / reply) intents.nl.extract_vision -> Intent
       document upload ................. backup.import_ (operator)
       service events .................. onboarding/registration/reactivation/migration (§11)
  -> resolve references (read-only preview for proposals): refs -> registered members
       any unknown ref  -> REJECT whole intent with registration guidance (§11); STOP
       ambiguous ref    -> pick-list as a pre-confirm stage on the proposal message (§13); STOP
       expense ref      -> reply-to-target / #id / descriptive match; cross-ledger -> refuse (§11)
  -> confirm? (§0.7)
       fuzzy  -> render proposal + Confirm/Cancel keyboard; store UNRESOLVED pending_intent keyed by
                 the PROPOSAL message_id, pinned to the active ledger (§10); STOP (await tap or reply)
       reads  -> render result directly; no action row
       deterministic mutation -> straight to apply
  -> [MUTATIONS] take per-group advisory lock (§0.11, ADR-0003), then in ONE transaction:
       domain.apply.apply_intent(intent)
         re-resolve + re-validate refs (may fail on staleness, §10); perform write;
         tag every row with created_by_action_id; append ONE actions row
       recompute balances (replay) -> render + edit-in-place the pinned board (content consistent)
  -> return OutboundAction(s); result reply carries ↩️ Undo and is prefixed with the active
       ledger ("📒 Japan Trip • …"). The board editMessageText call itself is best-effort.
```

**Stateless note.** Cloud Run is stateless: pending intents, undo state, and board ids all live in
the DB. `callback_data` is capped at **64 bytes**. Most buttons carry an id + namespace+version
(`"v1:undo:123"`, `"v1:confirm:678"`, `"v1:pick:9:42"`); the board `[Settle]` button carries the
full tuple + amount inline (`"v1:st:<from>:<to>:<ccy>:<amount_minor>"`, §13, ADR-0006).

---

## 7. Domain algorithms (pure — build and unit-test first)

### 7.1 Allocation (`domain/allocate.py`) — ADR-0008
Splits a total into per-person integer **minor-unit** shares, identically for every currency. Used
for `equal`, `shares`, `percent`. `exact` skips this.

```
def allocate(total_minor, weights, seed):
    W = sum(weights.values())
    raw   = {u: Fraction(total_minor * w, W) for u, w in weights.items()}
    base  = {u: floor(raw[u]) for u in weights}
    short = total_minor - sum(base.values())
    # hand out the `short` leftover minor units to the largest fractional remainders;
    # ties rotate deterministically per expense — never systematically the payer (ADR-0008)
    order = sorted(weights, key=lambda u: (-(raw[u] - base[u]), stable_hash(seed, u)))
    for u in order[:short]:
        base[u] += 1
    assert sum(base.values()) == total_minor
    return base
```

`seed` is the originating platform message id: an opaque int handed to the domain in `ctx` and
**frozen into the pending intent** for the confirm path, so the shares a proposal shows are exactly
the shares that commit (WYSIWYG). `stable_hash` must be deterministic across processes and runs
(e.g. sha256 over `f"{seed}:{user_id}"`) — never Python's builtin `hash`.

Validation by split type, BEFORE allocate:
- `equal`: weights all 1 over the participant set (default = all registered members, payer included).
- `shares`: weights = given positive weights.
- `percent`: reject if `abs(sum(percent) - 100) > 1.0`; else weights = the given percents
  (normalization absorbs the ±1.0 tolerance).
- `exact`: require `sum(exact_minor) == total_minor`; else reject and show the difference.

### 7.2 Balance replay (`domain/balances.py`)
Per-user net per currency (pooled model): `net[user][ccy]` in minor units, positive = user owes the pool.
Conservation holds per currency: `sum over users == 0`. **The result is a sum of deltas and thus
order-independent** — chronological ordering below is for stable display only; `occurred_on` and
back-dating never change a balance.

```
def balances(ledger_id) -> dict[user, dict[ccy, int]]:
    events = ordered(expenses ∪ settlements where deleted_at IS NULL, by (created_at, id))
    net = defaultdict(lambda: defaultdict(int))
    for ev in events:
        if isinstance(ev, Expense):
            net[ev.payer][ev.currency] -= ev.amount_minor
            for s in ev.splits:
                net[s.user][ev.currency] += s.owed_minor
        else:  # Settlement (always single-currency, concrete amount)
            net[ev.from_user][ev.currency] -= ev.amount_minor
            net[ev.to_user][ev.currency]   += ev.amount_minor
    return net
```

### 7.3 Settlements (`domain/settle.py`) — same-currency, one line at a time (ADR-0002, 0006, 0007)
Every settlement records exactly **one currency, one direction, one row, one action** through
`apply_intent`, and is individually undoable. Balances absorb whatever is recorded (§7.2); a
settlement is a **recorded stated fact**, not policed against the pool.

- **Board `[Settle]` button** — WYSIWYG full settle of one currency line (ADR-0006, §13). The button
  carries `(from, to, ccy, amount_minor)`. On tap, under the per-group lock, recompute the current
  simplified `from→to` amount for that currency:
  - **shown == current** → record a settlement for the shown amount; re-render the board.
  - **shown ≠ current** (board was stale) → **do not record**; warn and refresh the board (edit in
    place; if the message is gone, post + re-pin). User re-taps against the truth.
  - **line gone** → refresh + "Already settled."
- **Settle sheet** (`settle_up`, `amount_minor = None`; "settle up with X", `/settle @x`) — a **read**
  (ADR-0007): render every currency where simplify emits a transfer between the pair (**either
  direction**) as its own line `from → to  AMT CCY` with a WYSIWYG `[Settle]` button carrying the
  same amount-token + staleness guard as the board. The sheet commits nothing and writes no action
  row; each tap records that one line (its own action + Undo). No lines between the pair →
  "Nothing to settle" (no reverse credit).
- **Custom settle** (`settle_up` with amount + currency; `/settle`, NL) — **fully ungated**: any
  direction, any positive amount (overpayment allowed). Only validation: `from ≠ to`, positive
  amount, both registered members, real ISO currency. Overpayment surfaces later as a credit.

Payments are **immutable facts**. Undoing an earlier expense never resizes a later settlement; it
recomputes by replay, and any now-excess payment surfaces as a credit with a warning (§9).

### 7.4 Simplify (`domain/simplify.py`) — minimum cash-flow per currency, deterministic
Run independently per currency on the net positions. Greedy, with a **stable tiebreaker** so output
is deterministic (board stability + golden tests). It is a **display/solver aid**: it drives the
board's suggested transfers and full settle-up, but never gates a custom settle (ADR-0002).

```
def simplify(net_ccy: dict[user, int]) -> list[(debtor, creditor, minor)]:
    debtors   = sorted([(u, v)  for u, v in net_ccy.items() if v > 0], key=lambda x: (-x[1], x[0]))
    creditors = sorted([(u, -v) for u, v in net_ccy.items() if v < 0], key=lambda x: (-x[1], x[0]))
    # repeatedly match largest debtor with largest creditor (ties by ascending id); emit transfers
    ...
    return transfers
```
Board "who owes whom" = union over currencies of `simplify(net[·][ccy])`.

### 7.5 FX (`domain/fx.py`) — DISPLAY ONLY
- Resolve `FROM→TO`: latest manual `fx_rates` row wins (never auto-refreshed); else Frankfurter,
  cached group-wide with a **same-calendar-day TTL** — a render that finds `fetched_at` isn't today
  refetches (ECB publishes daily). If the refetch fails, fall back to the cached rate and surface
  its date in display.
- Frankfurter is EUR-based; **triangulate** non-EUR pairs via EUR.
- Unsupported currency, or API down with nothing cached → no rate; callers render `(≈ n/a)`.
  Never guess, never block.
- FX never participates in `apply_intent` or settlement math.

### 7.6 Convert & equivalents (`domain/convert.py`) — pure reads, active ledger only
- `≈` equivalent: convert a bucket to the group **home currency** at the current rate, format to the
  home currency's `minor_digits`. Nothing stored.
- `/convert <TARGET>`: convert every bucket of the active ledger to TARGET and sum. Read-only.
- `/balance`: each currency bucket + its `≈ home` line + a total `≈ home` line, labeled approximate.
  Sealed to the active ledger; says nothing about other ledgers (§0.10).

---

## 8. apply_intent (`domain/apply.py`) — the forward write path

Single function, single transaction, **under the per-group advisory lock** (§0.11, ADR-0003). For
each `Intent` kind it: authoritatively resolves refs to registered members (re-validating, §10),
performs the write, **stamps every new row with the `action_id`**, appends **one `actions` row**,
recomputes balances, and renders + edits the board in place. Returns the rendered reply + keyboard
so the caller can send it and store `result_chat_id/message_id` back on the action.

Reversal model:
- Row-creating ops (`add_expense`, `settle_up` — always a single settlement row, ADR-0007 —
  `delete_expense` which flips `deleted_at`) → undo = soft-delete / restore **all rows where
  `created_by_action_id = me`**.
- Field/pointer flips (`switch_ledger`, `archive_ledger`, `unarchive_ledger`, `new_ledger`'s
  active-pointer change, `set_home_currency`, `set_logging_currency`, `set_fx_rate`,
  `edit_expense`) → store a minimal `before_image` and restore it on undo.

Active-ledger invariant maintenance (ADR-0004):
- `new_ledger` sets active = new; undo restores the previous `active_ledger_id` (or repoints per the
  archive rule if that ledger is now archived) and marks the created ledger `archived`. **Undo is
  refused if the created ledger has since gained non-deleted transactions.**
- `archive_ledger` of the active ledger repoints active to the **most-recently-created open ledger**
  and announces it. Archiving the **only** open ledger is **forbidden**.
- `switch_ledger` to an **archived** ledger is **refused** with guidance
  ("📒 Japan Trip is archived — `/unarchive Japan Trip` first, then `/switch`").
  `unarchive_ledger` flips it back to `open` (undo restores `archived`) and does **not** touch the
  active pointer — reopening and activating are separate, deliberate steps.
- `setup` writes registration rows but appends an action with **no Undo affordance** (permanent).
- `show_balance` / `unknown` write no action row.

---

## 9. Undo / redo (`domain/undo.py`) — callback actions, button-only

- Each undoable result message carries an **↩️ Undo** button; `callback_data` holds the `action_id`.
  State lives in `actions`, so the button survives cold starts and arbitrarily old messages. **The
  button persists permanently.**
- **Anyone may press within `UNDO_WINDOW_HOURS` (default 24).** After that the action **locks**: only
  the **operator** may undo/redo. The lock is **computed on press** (`now ≥ created_at + window`) —
  no scheduler. A non-operator press after lock gets
  `answerCallbackQuery("🔒 Locked — over 24h old. Ask the operator, @<operator>.")` and the button
  stays. The operator is named by resolving `OPERATOR_USER_ID` to their @username (fallback display
  name; if unresolvable, a generic "the operator").
- **Idempotent toggle:** set `undone_at`/`undone_by` only if currently null, in one transaction under
  the per-group lock; a stale or double tap no-ops ("already undone").
- On undo, reverse the action (§8), then **edit the same message** and flip the button to **↪️ Redo**.
- The undo is the **DB transaction**; `editMessageText` is cosmetic and best-effort. **Always edit,
  never delete.**
- **Undoing a settled-against expense** is allowed; the result notes any resulting credit
  (e.g. `"⚠️ a later settlement now overpays — Alice has a JPY 200 credit"`). Note that overpayment
  credits can also originate at settle time via the ungated custom path (ADR-0002).
- **NL "undo"/"redo" is not honored.** It maps to `Unknown` with a templated reply pointing to the
  ↩️ button.
- Import is **not** on the per-action undo stack; it takes its own pre-snapshot (§13, ADR-0005).

---

## 10. Confirm + reply-to-correct loop (`core/pending.py`)

1. Fuzzy intents (NL/OCR, ambiguous) are **proposed**: render a summary + `[✅ Confirm] [✖ Cancel]`
   keyboard with footer `↳ reply to correct`; store the **unresolved** intent in `pending_intents`
   keyed by the **proposal message_id**, **pinned to the ledger active at propose time**
   (`pending_intents.ledger_id`); TTL `PENDING_TTL_MINUTES` (15). The proposal is prefixed with the
   pinned ledger (`📒 Japan Trip • …`) — what you see is where it commits. Resolution that *creates*
   state is deferred to confirm, so a cancelled proposal leaves nothing behind. (Unknown-member
   rejection and ambiguous-reference pick-lists still happen at propose time via read-only preview,
   §11, §13.)
2. **Correcting a LIVE proposal** — the user **replies** with free text. The router matches
   `reply_to_message.message_id` to a live pending intent → `nl.refine(pending, text)` → **edit the
   proposal in place**. A successful refine **refreshes `expires_at`** so active editing stays in
   place. Repeat as needed. (Reading A, discussion resolved.)
3. **Confirm** → take the per-group lock, **re-resolve + re-validate** the intent in the transaction
   **against the pinned ledger** — a concurrent `/switch` never redirects a pending proposal; if the
   pinned ledger was archived meanwhile, the confirm fails re-validation — then `apply_intent`, then
   **edit that same message** into the committed result with the ↩️ Undo button. Confirm **consumes and deletes** the pending row in the transaction, so a double-tap finds
   nothing → "This proposal was already handled." **Cancel** → drop pending, edit to "Cancelled".
   - **Re-validation can fail** (§6): if a referenced expense was deleted or a structural precondition
     no longer holds, the confirm fails with "That changed while you were deciding — please resend"
     and commits nothing. (A departed *member* referenced in the proposal is still valid — they stay
     referenceable by id.)
4. **Expiry is computed on read** (like the undo lock): a Confirm/refine on an expired proposal edits
   it to "Expired." **There is no resend dead-end** — a reply to an expired/confirmed/cancelled
   proposal is a **new NL intent** (§10.5 / §6), which for a mutation produces a **fresh proposal**.
5. Reply to any **non-pending** bot message (the pinned board, an old result, an expired proposal) is
   a **new NL intent**, exactly as if @mentioned — removing the @mention tax after the first
   interaction.

**Ambiguous-reference pick-list** (§11, §13): when a reference matches more than one member, the
proposal renders the ambiguous slot as pick buttons `[Sam A] [Sam B]` (+ Cancel); a tap
(`v1:pick:<pending_id>:<user_id>`) or a refining reply writes the choice into the pending intent and
re-renders. Multiple ambiguous refs resolve one at a time; only once every reference is pinned does
the message show `[✅ Confirm] [✖ Cancel]`.

---

## 11. Identity, registration & onboarding (`domain/identity.py`)

**No ghosts. Registered members only.** A user exists to the bot only once it has seen their
Telegram `User` object and created `users` + `identities` + `group_members` rows.

Registration happens when the bot sees an account:
- **Join:** `new_chat_members` carries full `User` objects → auto-register.
- **Any interaction:** a message, @mention, reply, or button tap carries `from` → register the
  author if new.
- **`/setup`:** seeds pre-existing members the bot can identify (reply to their message; or
  `text_mention` entities that embed `user.id`). A bare `@username` cannot be resolved by the Bot
  API, so `/setup @carol` is **rejected** for that entry with guidance.

**Reactivation.** A member with `left_at` set who **re-joins** (`new_chat_members`) or simply
**interacts** again has `left_at` **cleared** (same `user_id`, balances intact, back in "everyone").
Reactivation is a lifecycle event — no `actions` row, not undoable, like the original join.

**Reference resolution (`identity.resolve`).** `@username`/`text_mention` are exact; a bare name from
NL is a fuzzy match over registered members' display names/usernames.
- **Unknown reference → reject the whole intent** with how to register them.
- **Ambiguous reference** → pick-list (§10, §13).

**Expense reference resolution** (for `delete_expense` / `edit_expense`):
- **Reply-to-target (primary):** reply to the bot's original expense result message; the `actions`
  row's `result_message_id` maps message → action → expense. Unambiguous, no ids.
- **Visible `#id` (fallback):** every expense result and balance/list line shows `#42`; `/delete 42`,
  `/edit 42 …`, and NL containing a bare id work.
- **Descriptive NL (tertiary):** "the dinner one" → match within the active ledger; if not unique,
  pick-list; if nothing, `Unknown` with guidance.
- **Sealing:** a reference to an expense in another ledger is **refused** ("that's in 📒 Other; switch
  there first"), never acted across the seal (§0.10).

**"Everyone" / empty participants** = all currently-registered members (`left_at IS NULL`), payer
included. Any "everyone" proposal **lists the names it used**.

**Onboarding (`my_chat_member`, bot added):** register the group, create the first ledger named after
the group (fallback `"General"`, `logging_currency = null`), and post a welcome that (a) asks the
operator to set the **home currency** `/homecurrency <ISO>` (the universal `≈`/default fallback),
(b) mentions `/currency <ISO>` for a ledger's logging currency, (c) explains `/setup` and that bare
usernames can't be added, (d) notes photos/NL need an @mention or a reply. A new expense whose
currency resolves to nothing (§3) is rejected with the set-a-currency prompt.

**Leaving.** On `left_chat_member`, set `group_members.left_at`. Balances persist; the user stays
referenceable by id but is excluded from future "everyone" — until reactivation.

**Identity refresh.** Usernames and display names change. Every update carrying a Telegram `User`
object refreshes that member's stored `username`/`display_name` so `@username` resolution never
works from stale data. Lifecycle behavior — no action row, not undoable.

**Supergroup migration.** Telegram upgrades a basic group to a supergroup when certain settings
change — granting the bot admin rights so it can pin the board is a common trigger — and the chat
gets a **new chat_id**, announced by a `migrate_to_chat_id` service message. On it, update
`groups.platform_chat_id` in place: same group row, ledgers, members, and balances. Clear every
ledger's `board_message_id`/`board_chat_id` (the pin does not survive) so the board is lazily
re-created and re-pinned on the next mutation. Lifecycle event — no action row, not undoable.

---

## 12. Natural-language coverage (`intents/nl.py`, `llm/prompts.py`)

**Every command is reachable by NL.** The prompt's few-shot set must cover all kinds. Examples:

| NL example | Intent |
|---|---|
| "I paid 40 for dinner, split with Sam" | `add_expense` |
| "Bob owes me 15 for the taxi" | `add_expense` (exact, payer=me) |
| "settle up with Alex" | `settle_up` (no amount → settle sheet, a read; ADR-0007) |
| "I paid Alex 30 SGD" | `settle_up` (custom, ungated) |
| "what do I owe?" / "show balances" | `show_balance` (read — runs immediately) |
| "convert everything to USD" | `show_balance(convert_to=USD)` (read) |
| "set our home currency to euros" | `set_home_currency` |
| "log this ledger in yen" | `set_logging_currency` |
| "new ledger called Tokyo in JPY" | `new_ledger(logging_currency=JPY)` |
| "switch to Japan" / "archive this ledger" / "reopen the Japan ledger" | ledger intents |
| "delete the dinner expense" / reply "delete this" | `delete_expense` (resolved via §11) |
| "pin the rate 1 usd = 1.35 sgd" | `set_fx_rate` |
| "add Carol" (by reply/tap) | `setup` (bare username → guidance) |
| "export everything" | export flow (operator for `all`) |
| "undo that" | `unknown` → reply pointing to the ↩️ button |

Routing rules: **reads run immediately**; **NL/OCR mutations always propose+confirm** (§0.7);
**undo/redo are never performed from NL**. `confidence` is **cosmetic** and never forces a branch.
Ambiguous resolution triggers a pick-list (§10). `nl.refine(prior_intent_json, correction_text)`
returns a refined intent for the reply-to-correct loop.

---

## 13. Telegram specifics & gotchas (`telegram/`)

- **Privacy mode (default ON)** delivers only: slash commands, @mentions, replies to the bot,
  callback queries, and member/service messages. Keep it on.
- **`callback_data` ≤ 64 bytes.** Most buttons carry ids only (`"v1:undo:123"`). The board `[Settle]`
  button is the exception: it carries `"v1:st:<from>:<to>:<ccy>:<amount_minor>"` inline (~25 bytes), so
  the board stays a **stateless projection** with no per-render transfer table (ADR-0006).
- **Editing vs deleting:** bot messages with an inline keyboard can be edited at any age; deletion is
  limited to 48h. Undo and the board edit in place, never delete.
- **Files:** `sendDocument` up to ~50 MB (export); inbound download via `getFile` then
  `…/file/bot<token>/<file_path>`, ~20 MB (import).
- **Pinning** the board requires the bot to be a **group admin**; if not, post the board unpinned and
  warn once.
- **Supergroup migration:** changing group settings (including making the bot admin) can upgrade the
  chat to a supergroup with a **new chat_id** (`migrate_to_chat_id`). Remap
  `groups.platform_chat_id` in place and let the board re-create (§11) — never treat the migrated
  chat as a new group.
- **Command menu:** call `setMyCommands` on startup so `/equal`, `/exact`, `/currency`,
  `/homecurrency`, `/setup`, etc. are discoverable.
- **Webhook:** validate `X-Telegram-Bot-Api-Secret-Token`; dedupe by `update_id`; process
  synchronously and return 200.
- **Entities:** parse `mention` (text only — may be unresolvable → reject) and `text_mention`
  (embeds `user.id` — resolvable/registerable).
- **Board lifecycle (ADR-0003, ADR-0006):** created+pinned on a ledger's first mutation (or on
  `/newledger`), guarded by the per-group lock + the `board_message_id` unique constraint so only one
  board is ever created. Thereafter rendered from post-write balances **inside** the locked
  transaction and edited in place (content consistent even under concurrent writes; the API call is
  best-effort). Each debt line: `from → to  AMT CCY (≈ home)  [Settle]`. `[Settle]` is WYSIWYG
  (ADR-0006): it records the shown amount when the board is fresh, or warns + refreshes when stale.
  Board lines themselves never carry Undo; the short result a settle posts does.
  **Read-triggered `≈` refresh:** ledger reads (`/balance`, `/convert`, the settle sheet) that find
  the board's API rates weren't fetched today re-render the board with fresh rates — fetch outside
  the lock, then render + edit **under the per-group lock** so it can't race a concurrent write's
  board edit. Manually pinned rates never trigger this.

---

## 14. Config (`.env.example`)

```
MODE=webhook                 # or "poll" for local dev
BOT_TOKEN=
TELEGRAM_WEBHOOK_SECRET=     # validated on every webhook request
PUBLIC_URL=                  # for setWebhook (prod)
DATABASE_URL=                # Neon POOLED url in prod (advisory locks need Postgres);
                             #   sqlite+aiosqlite:///./expensir.db locally
OPERATOR_USER_ID=            # telegram user id; runs /import, full /export, undo locked actions
UNDO_WINDOW_HOURS=24
PENDING_TTL_MINUTES=15

LLM_TEXT_PROVIDER=cloudflare # cloudflare | groq | gemini
LLM_VISION_PROVIDER=cloudflare
CF_ACCOUNT_ID=
CF_API_TOKEN=
GROQ_API_KEY=                # only if swapping the text path
GEMINI_API_KEY=              # only if swapping the vision path

FX_API_BASE=https://api.frankfurter.dev   # no key; ECB daily; same-day cache (§7.5); DISPLAY ONLY
```

Deploy: run Alembic migrations + `setWebhook` as a one-shot release step, not on every cold start.

---

## 15. Build milestones (each green — tests + smoke — before the next)

1. **Skeleton + transport + outbound.** `webhook.py` + `poll.py` both call `dispatch`; `executor.py`
   performs `OutboundAction`s. *Done when:* `/start` echoes via webhook (secret checked) and polling.
2. **DB + models + Alembic.** All tables from §5 incl. `groups.home_currency`,
   `ledgers.logging_currency`, unique `board_message_id`. *Done when:* migrations apply clean on
   Postgres and SQLite; `/start` registers group + caller + first ledger.
3. **Money & currency (pure).** `money.py` (`to_minor`, `fmt`, minor-digits table) +
   `currency.py` resolution order (§3). *Done when:* unit tests cover 2/0/3-decimal currencies,
   rounding visibility, and home/logging/override resolution + freezing.
4. **Ledgers.** `/newledger [ISO]`, `/ledgers`, `/switch`, `/archive`, `/unarchive`; active-ledger
   wiring + invariant maintenance (ADR-0004); switch announced; archive warns on non-zero balances.
   *Done when:* switching redirects new expenses; archiving the active ledger repoints; archiving
   the last ledger is refused; switching to an archived ledger is refused with unarchive guidance.
5. **Slash expense path (no LLM).** `/add`, `/equal`, `/exact`, `/shares`, `/percent`, `/balance`,
   `/settle` (sheet + custom), `/currency`, `/homecurrency`. `setMyCommands`. *Done when:*
   `/equal 60 dinner @A @B` splits correctly and `/balance` reads it back; bare `/add` shows
   split-type buttons → member picker.
6. **Allocation + balances + simplify (pure).** *Done when:* golden tests pass — 3-way $10.00 →
   334/333/333 cents; JPY ¥6001 → 2001/2000/2000; every share a whole minor unit; `sum(owed)==total`;
   the member receiving the extra unit varies with the seed (rotation, ADR-0008) but is stable for a
   fixed seed; simplify reduces A→B→C to A→C deterministically; replay proven order-independent.
7. **Provenance + undo/redo.** `apply_intent` under the per-group lock appends `actions`, tags rows,
   soft-deletes; persistent per-message Undo; idempotent toggle; 24h lock on press; operator override
   + named. *Done when:* undo/redo flips balances; double-tap no-ops; a simulated >24h action is
   operator-only.
8. **Confirm + reply-to-correct.** `pending_intents` keyed by proposal message_id; Confirm/Cancel;
   reply refines live proposals in place (TTL refreshed); confirm re-validates + consumes; reply to a
   dead proposal starts a fresh one (no resend). *Done when:* a proposed expense is corrected by
   reply, confirmed, and only then commits; a stale reference fails at confirm.
9. **Registration & /setup + reactivation.** Auto-register on join/interaction; `/setup` via reply +
   text_mention; bare-username rejection; unknown-reference whole-intent rejection; `left_at` cleared
   on re-join/interaction. *Done when:* an expense naming an unknown person is rejected, a
   reply-`/setup` then lets it commit, and a returned member is back in "everyone".
10. **NL text path (all intents).** `llm/cloudflare.py` + prompts → any `Intent` kind incl.
    `set_home_currency`/`set_logging_currency`; reads run, mutations confirm; ambiguous-ref pick-list;
    expense-ref resolution; `nl.refine`. *Done when:* "@bot I paid 40 for dinner, split with Sam"
    proposes and disambiguates two Sams; "set home currency to USD" / "switch to Tokyo" map right.
11. **Vision path.** Receipt photo (mention/reply) → `extract_vision` → same `AddExpense` → same
    confirm. *Done when:* a sample receipt proposes a plausible expense (mocked).
12. **Currency display: FX + convert + ≈.** `fx.py` (Frankfurter + cache + triangulation, display
    only), `/setrate`, `/rates`, `/convert`, `≈ home` lines on board + balance. *Done when:* board
    shows each currency with its home equivalent; `(≈ n/a)` when FX unavailable; settlement math never
    calls FX.
13. **Pinned balance board.** `format/board.py`; one edited-in-place message per ledger under the
    lock; WYSIWYG `[Settle]` buttons with the amount-token staleness guard (ADR-0006). *Done when:*
    adding/settling updates the board in place; a fresh tap records the shown amount; a stale tap
    warns + refreshes without recording.
14. **Export / import.** `/export [ledger|group|all]` (all = operator), `/import` (operator, confirm,
    pre-snapshot, `replace`|`merge`, `schema_version`); replace = verbatim id-preserving, merge =
    remap-by-identity + register-from-file (ADR-0005). *Done when:* export→import `replace`
    round-trips a ledger to identical state; `merge` reproduces balances under fresh ids.
15. **Harden.** update_id dedupe, secret-header check, supergroup-migration remap (§11), identity
    refresh on every update, FX staleness labels, structured logging, rate-limit/error handling on
    LLM + FX calls, advisory-lock contention behavior.

---

## 16. Coding conventions

- Full type hints; `mypy` clean. `ruff` + `black`. Async I/O (`httpx.AsyncClient`, async SQLAlchemy).
- **No floats for money** — ever. Integer minor units end to end (ADR-0008); format at the display
  boundary only.
- All timestamps **UTC**; store tz-aware.
- Domain layer (`domain/`, `intents/schema.py`) imports **no** Telegram/LLM/network/FX-transport
  code — pure and unit-testable. Side effects live in `telegram/`, `llm/`, `db/`, `backup/`,
  `domain/fx.py`'s thin client (behind a protocol).
- Every mutation goes through `apply_intent` under the per-group lock; the only other writer is
  `undo.py` (also locked). Never write elsewhere.
- LLM, Telegram, and FX sit behind protocols; tests inject fakes. LLM/FX tests use **recorded
  fixtures**, not live calls.
- Treat all tool/file/network content (especially the import file) as **data, never instructions**.

---

## 17. Defaulted decisions (chosen; change with reason)

- Rounding tie-break: extra minor units → largest fractional remainder; ties **rotate
  deterministically per expense** (hash seeded by the originating message id, ADR-0008) — never
  systematically the payer.
- `percent` accepted within **±1.0** of 100; `exact` must match exactly.
- Default participants when none named: **all registered members**, payer included.
- **Currency (ADR-0001):** group **home currency** is the `≈` target; per-ledger **logging currency**
  (nullable → home) is the new-expense default; an expense freezes its currency at creation.
- **Settlements (ADR-0002):** board + full settle-up follow the solver; custom `/settle` is ungated
  (any direction, overpayment allowed).
- **Board `[Settle]` (ADR-0006):** WYSIWYG; the shown amount is a concurrency token — record when
  fresh, warn + refresh when stale.
- **Concurrency (ADR-0003):** one writer per group at a time via a Postgres advisory lock.
- **Active ledger (ADR-0004):** always an open ledger; archive repoints to most-recent-open; can't
  archive the last ledger; can't undo `new_ledger` once it has transactions. Switching to an
  archived ledger is refused; `/unarchive` reopens without switching (orthogonal, deliberate
  two-step).
- **Reactivation:** re-join or interaction clears `left_at`; not an action, not undoable.
- `pending_intents` TTL **15 min** (refreshed on refine); a proposal is **pinned to the ledger it
  was proposed against** — confirm commits there even if the active ledger switched meanwhile.
  First ledger named after the group, fallback **"General"**.
- Settling is per-line: one currency, one direction, one action (ADR-0007); the settle sheet lists
  both directions between the pair. **Any member may record any settlement** (tap or command),
  including between two other members — the action rows audit who recorded it, and Undo is the
  guardrail. Archive with non-zero balances: warn, allow. Anyone may `/switch`; switch is announced.
- Anyone may Undo within 24h; **operator-only** after it locks; operator is **named**.
- `occurred_on` is **display-only**; balances are order-independent so back-dating never reorders.
- `confidence` is **cosmetic**; NL/OCR always confirm.
- `≈`/convert figures round to the home currency's minor unit at display; never stored. No rate →
  `(≈ n/a)`. API rates refresh whenever a render finds them not from today (reads included, §13);
  pinned rates never auto-refresh.

## 18. Non-goals (for now)

- Web dashboard (data model is kept web-ready; no UI built).
- Cross-currency settlement and automatic FX on stored expenses (expenses keep their frozen
  currency; FX is display-only).
- Cross-ledger balances / netting / a group-wide "what do I owe across everything" view (ledgers are
  sealed, §0.10).
- Ghosts / pre-resolving bare usernames (registered members only; import is the one file-based
  registration path).
- Multi-operator / per-group admin roles (single global `OPERATOR_USER_ID`).
- Public multi-tenant hosting / abuse controls (single-operator, on-demand).

## 19. Definition of done

A self-hoster can: clone the repo, set `.env`, run migrations, deploy to Cloud Run (or run
`MODE=poll` locally), add the bot to a group, set the **home currency** (and per-ledger **logging
currencies** as trips demand), register members (by their interacting or via `/setup`), and — by
talking, tapping, or commanding — add expenses (equal/exact/shares/percent across currencies), settle
each currency line-by-line (board WYSIWYG, the settle sheet, or ungated custom), see an
always-current pinned board with simplified debts and home-currency equivalents, undo/redo within
24h via the button, convert views, and export/import the ledger as JSON. All domain math is covered
by passing unit tests;
references to unknown people are rejected with guidance; ledgers stay sealed; concurrent writes are
serialized per group; and the bot never acts on un-mentioned group chatter.
