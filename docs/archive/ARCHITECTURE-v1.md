# Expensir — Architecture & Build Spec (v1, ARCHIVED)

> ⚠️ **ARCHIVED — superseded by `/ARCHITECTURE-v2.md`.** This is the original spec, kept only for
> provenance. **Do not build from it.** Several of its locked decisions were reversed during a
> design review (see `docs/adr/0001`–`0006`): currency is now split into a group home currency +
> per-ledger logging currency; custom settlements are ungated; ledgers are sealed; writes are
> serialized per group; the board `[Settle]` button is WYSIWYG; and export/import id/identity
> semantics are pinned. `CONTEXT.md` is the source of truth for vocabulary. Read `ARCHITECTURE-v2.md`
> instead.

A self-hostable **Telegram bot for Splitwise-style expense splitting** in group chats.
It responds only when **@mentioned, replied to, or commanded**; understands slash commands,
natural language, and **receipt photos**; runs **serverless** and scales to zero when idle.

**This document is authoritative and self-contained.** Build directly from it. There is no
"overrides elsewhere" — every rule lives in one place. Read **§0 Invariants** first; they
constrain everything else. When two readings are possible, prefer the one that keeps the
domain pure and routes all writes through `apply_intent`.

> **Decisions locked in this spec (call out if you disagree):**
> - No cross-currency settlement. Each currency settles on its own. FX is **display-only**.
> - **One group-wide currency** (`groups.default_currency`): the default for new expenses *and*
>   the currency all `≈` equivalents convert to. Ledgers do **not** carry their own currency.
> - **No ghosts.** Only registered members may appear in a transaction. Any reference to an
>   unregistered person **rejects the whole intent** with guidance on how to register them.
> - **Undo/redo are button-only**, never NL-triggered. Every other command is NL-reachable.
> - Money is integer `e2` = **1/100 of the currency's smallest circulating unit** (one whole
>   smallest unit = 100 e2). Allocation snaps to whole smallest units uniformly for every currency
>   (no per-currency formula), so JPY splits in whole yen — no fake cents.

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
     by this action"; nothing else is needed for the common cases.
   - `undo` / `redo` (§9) — operate on the `actions` log itself, not through a new intent. Same
     transactional discipline.
   No other code path writes to the database.

3. **Money is integer `e2`** = 1/100 of a currency's *smallest circulating* unit (so one whole
   smallest unit = **100 e2**, for every currency). **No floats** in storage or math. Allocation
   distributes whole smallest units (multiples of 100), identically for every currency; the
   per-currency `minor_digits` (§3) is used **only** to parse the major-unit string a person types
   and to format output. Round only at the smallest-unit boundary, surfaced visibly when it happens.

4. **Balances are derived, never stored.** Computed by **replaying** non-deleted
   `expenses + settlements` in chronological order (§7.2). Deleting / undoing = soft-delete +
   recompute. There are no stored balance columns and no frozen snapshots.

5. **Transport-agnostic core.** `core.handler.dispatch(update_dict) -> list[OutboundAction]` knows
   nothing about webhook vs polling, and the domain layer imports no Telegram/LLM/network types. A
   future web dashboard reads the same tables and replays the same way.

6. **Privacy-respecting invocation.** The bot acts only on: slash commands, @mentions of itself,
   replies to its own messages, button taps (callback queries), photos that mention/reply to it,
   document uploads, and member/chat service events. It never reads general group chatter.

7. **Confirm policy (single rule, no exceptions).**
   - **Deterministic + fully specified** (a slash command with everything it needs) → **commit
     immediately**, reply carries an ↩️ Undo button. No confirm tap.
   - **Fuzzy** (anything from NL or OCR, low `confidence`, or ambiguous reference resolution) →
     **propose + confirm** first.
   - **Reads never confirm** (`/balance`, `/convert`, `/ledgers`, `/rates`, `/export`).
   (Cross-currency settlement was the old exception to this; it no longer exists.)

8. **Pure, testable domain.** Allocation, balance replay, simplification, and settlement math are
   pure functions with no I/O — unit-tested in isolation. The flaky/paid parts (Telegram, LLM, FX)
   sit at the edges behind protocols and are faked in tests.

9. **Registered members only.** A transaction may reference only members the bot has registered in
   this group (§11). A reference to anyone unknown **rejects the entire intent** with a message
   explaining how that person can register. Nothing partial commits.

10. **Side effects are returned as data.** The core never sends a Telegram message directly; it
    returns `OutboundAction`s and a thin executor at the transport edge performs them. This makes a
    full conversation testable end-to-end with no network.

---

## 1. Tech stack

| Concern | Choice | Notes |
|---|---|---|
| Language | **Python 3.12** | type hints everywhere |
| Web framework | **FastAPI + uvicorn** | thin webhook handler only |
| Telegram | **`httpx`** calling the Bot API directly | do **not** use `python-telegram-bot` (too heavy for cold-start serverless) |
| Models/validation | **Pydantic v2** + **pydantic-settings** | the Intent contract is Pydantic |
| ORM / migrations | **SQLAlchemy 2.x (async)** + **Alembic** | |
| DB | **Postgres** (Neon in prod) / **SQLite** locally | use Neon's **pooled** connection string in serverless |
| LLM (text) | **Cloudflare Workers AI**, OpenAI-compatible endpoint | swappable: Groq, Gemini |
| LLM (vision) | **Cloudflare Workers AI** vision model | swappable: Gemini |
| FX rates (**display only**) | **Frankfurter** | free, no key, ECB daily, EUR-based (triangulate) |
| Deploy | **Google Cloud Run** (one container) | portable: Fly.io / Railway / laptop |
| Lint/format/type | **ruff** + **black** + **mypy** | |
| Tests | **pytest** | pure-domain tests need no network; LLM/FX use recorded fixtures |

Provider model IDs are volatile — verify in the provider dashboard before relying on them. The FX
provider is now used **only** for the `≈` equivalent lines and `/convert`; it never touches ledger
math, so an FX outage degrades display only (see §3, §7.5).

---

## 2. Repository layout

```
expensir/
├── expensir/
│   ├── config.py                 # pydantic-settings: tokens, DB url, providers, OPERATOR_USER_ID, MODE
│   ├── transports/
│   │   ├── webhook.py            # FastAPI app + Cloud Run entry; secret-header check; update_id dedupe
│   │   ├── poll.py               # getUpdates loop for local dev (no public URL needed)
│   │   └── executor.py           # performs the list[OutboundAction] the core returns
│   ├── telegram/
│   │   ├── client.py            # httpx wrapper (sendMessage, editMessageText/ReplyMarkup, getFile,
│   │   │                        #   sendDocument, pinChatMessage, answerCallbackQuery, setMyCommands)
│   │   ├── types.py             # minimal Pydantic Update/Message/CallbackQuery/Document/User
│   │   └── keyboards.py         # inline keyboards: confirm/cancel, split-type, member picker, settle
│   ├── core/
│   │   ├── handler.py           # dispatch(update) -> list[OutboundAction]; transport-agnostic
│   │   ├── router.py            # classify an update into exactly one path (§6)
│   │   ├── outbound.py          # OutboundAction model (send/edit/answer-callback/pin/send-document)
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
│   │   ├── allocate.py          # split a total into per-person e2 shares at the currency's unit
│   │   ├── balances.py          # chronological replay -> per-user net per currency
│   │   ├── simplify.py          # minimum cash-flow per currency (deterministic)
│   │   ├── settle.py            # same-currency settlement consumption (with-amount + full)
│   │   ├── fx.py               # Frankfurter fetch + cache + triangulation — DISPLAY ONLY
│   │   ├── convert.py          # /convert and ≈ equivalents (pure reads)
│   │   ├── money.py            # e2 parsing/formatting; per-currency minor units (§3)
│   │   └── identity.py         # resolve refs -> registered member, or fail (§11)
│   ├── db/
│   │   ├── models.py            # SQLAlchemy models
│   │   ├── session.py           # async engine / session factory (pooled)
│   │   └── repo.py              # data-access functions
│   ├── backup/
│   │   ├── export.py            # DB -> JSON (schema_version stamped)
│   │   └── import_.py           # JSON -> DB (validate, replace|merge, confirm, pre-snapshot, operator-only)
│   └── format/
│       ├── render.py            # reply formatting (each bucket + ≈ equivalent)
│       └── board.py             # the pinned balance board (build + edit-in-place)
├── migrations/                  # Alembic
├── tests/
├── Dockerfile · pyproject.toml · .env.example
├── CLAUDE.md                    # short agent guide pointing at this file
└── README.md
```

---

## 3. Money & currency model (`domain/money.py`)

**Storage unit.** All amounts are integer `e2` = **1/100 of the currency's smallest (minor) unit**.
Equivalently, one whole smallest unit — cent, yen, fils — is always **`100 e2`**, for *every*
currency. The two extra digits are sub-unit headroom for intermediate precision; real expense and
settlement amounts are whole smallest units (multiples of `UNIT_E2 = 100`). This makes allocation
currency-independent (§7.1): there is no per-currency unit, only the constant `UNIT_E2 = 100`.

**Minor units.** Each ISO currency has a `minor_digits` count (most 2; some 0 or 3). Maintain a
small table; default unknown codes to 2. It is used **only** to parse a typed major-unit amount and
to place the decimal point when formatting — it does **not** affect allocation granularity.

| `minor_digits` | examples | smallest unit | 1 major unit |
|---|---|---|---|
| 0 | JPY, KRW, VND, CLP, ISK | yen/etc. = `100 e2` | `100 e2` |
| 2 | USD, EUR, SGD, GBP (default) | cent = `100 e2` | `10_000 e2` |
| 3 | BHD, KWD, OMR, TND, JOD | fils/etc. = `100 e2` | `100_000 e2` |

`1 major unit = 10**(minor_digits + 2) e2`; the smallest spendable unit is always `UNIT_E2 = 100`.

Functions:
- `to_e2(amount_str, currency) -> int`: `round(Decimal(amount_str) * 10**(minor_digits + 2))`.
  `"60" USD → 600_000`, `"60.50" USD → 605_000`, `"6000" JPY → 600_000`, `"6000.50" JPY → 600_050`.
  Reject more than `minor_digits + 2` decimal places at parse time.
- `quantize_e2(e2, currency) -> (rounded_e2, was_rounded)`: snap to the nearest `UNIT_E2` (100),
  half-up. Used after currency is resolved; real amounts are already whole smallest units. If
  `was_rounded`, the proposal/result **shows the rounded figure** so it is visible
  (e.g. `¥6000.50 → ¥6001`).
- `fmt(e2, currency) -> str`: `smallest_units = e2 // 100`, then format with `minor_digits` decimals
  and the code/symbol. Display only.

**Group home currency.** `groups.default_currency` is the single currency for the whole group. It
is (a) the default currency for a new expense when none is specified, and (b) the currency every
`≈` equivalent and `/convert` total is shown in. There is no per-ledger or per-user currency.
Set/changed via the `set_currency` intent (`/currency <ISO>` or NL).

**Per-expense override.** An expense may specify any currency via an ISO code immediately after the
amount (`/equal 30 SGD trains @a`) or via NL words/symbols (`"30 SGD"`, `"¥6000"`). The
proposal/result always shows the resolved currency so a wrong default is visible.

**`≈` equivalents.** Board lines and balance buckets in a non-home currency show the home-currency
equivalent at *today's* rate, labeled approximate. If no rate is available for a pair (FX down or
currency unsupported and no manual pin), show the amount followed by **`(≈ n/a)`** and do not block
anything. Equivalents are pure reads, never stored (§7.5).

---

## 4. The Intent contract (`intents/schema.py`)

```python
from typing import Annotated, Literal, Union
from pydantic import BaseModel, Field

class SplitMember(BaseModel):
    user_ref: str                  # "@alice" or a display name as seen in chat
    weight: float | None = None    # split_type="shares"
    exact_e2: int | None = None    # split_type="exact"
    percent: float | None = None   # split_type="percent"

class AddExpense(BaseModel):
    kind: Literal["add_expense"] = "add_expense"
    payer_ref: str
    amount_e2: int
    currency: str | None = None    # None -> group home currency
    description: str
    occurred_on: str | None = None # ISO date; DISPLAY ONLY (replay orders by created_at)
    split_type: Literal["equal", "exact", "shares", "percent"] = "equal"
    participants: list[SplitMember] = []   # empty -> all REGISTERED members (payer included)
    confidence: float | None = None        # LLM self-report; low -> force confirm

class SettleUp(BaseModel):
    kind: Literal["settle_up"] = "settle_up"
    from_ref: str
    to_ref: str
    amount_e2: int | None = None   # None -> full settle-up (clear every currency from owes to)
    currency: str | None = None    # required when amount given; must be a currency from owes to

class ShowBalance(BaseModel):      # /balance and /convert
    kind: Literal["show_balance"] = "show_balance"
    scope: Literal["me", "group"] = "group"
    convert_to: str | None = None  # /convert <TARGET>: consolidate all buckets into one currency

class DeleteExpense(BaseModel):
    kind: Literal["delete_expense"] = "delete_expense"
    expense_id: int

class EditExpense(BaseModel):      # non-financial fields ONLY
    kind: Literal["edit_expense"] = "edit_expense"
    expense_id: int
    description: str | None = None
    occurred_on: str | None = None

class NewLedger(BaseModel):
    kind: Literal["new_ledger"] = "new_ledger"
    name: str

class SwitchLedger(BaseModel):
    kind: Literal["switch_ledger"] = "switch_ledger"
    name_or_id: str

class ArchiveLedger(BaseModel):
    kind: Literal["archive_ledger"] = "archive_ledger"
    name_or_id: str | None = None  # None -> active ledger

class SetCurrency(BaseModel):      # group home currency
    kind: Literal["set_currency"] = "set_currency"
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
          NewLedger, SwitchLedger, ArchiveLedger, SetCurrency, SetFxRate, Setup, Unknown],
    Field(discriminator="kind"),
]
```

**Not Intents.** Undo, redo, and board-`[Settle]` are **callback actions** on already-committed
rows (§9, §6). **Export/import** are document/file flows handled in `backup/`, not through
`apply_intent` (import takes its own pre-snapshot and is not on the per-action undo stack).

**Undoability of each kind.** `add_expense`, `settle_up`, `delete_expense`, `edit_expense`,
`new_ledger`, `switch_ledger`, `archive_ledger`, `set_currency`, `set_fx_rate` all carry an Undo
button on their result. **`setup` (registration) is permanent and carries no Undo button** —
registering a person is treated like a join event, not a ledger mutation. `show_balance` and
`unknown` are reads/no-ops and write no action row.

---

## 5. Data model (`db/models.py`)

All money is integer `e2`. Core is Telegram-agnostic; Telegram identity lives only in `identities`.

```
users            (id, display_name)
identities       (user_id, platform, platform_user_id, username)   -- platform='telegram'
                   -- a member EXISTS only once we have their identity row (no ghosts)
groups           (id, platform_chat_id, name, default_currency, active_ledger_id)
                   -- default_currency = group home currency (expense default + ≈ target)
group_members    (group_id, user_id, joined_at, left_at)
                   -- left_at set if they leave; balances persist, excluded from future "everyone"

ledgers          (id, group_id, name, status['open'|'archived'],
                  created_at, archived_at, board_message_id, board_chat_id)
                   -- NO currency column (group-level only)

expenses         (id, ledger_id, payer_id, amount_e2, currency, description, occurred_on,
                  split_type, source['command'|'nl'|'ocr'], created_by_user_id,
                  created_by_action_id, created_at, edited_at, deleted_at)
expense_splits   (expense_id, user_id, owed_e2)           -- exact e2 share per participant

settlements      (id, ledger_id, from_user, to_user, amount_e2, currency,
                  created_by_action_id, created_at, deleted_at)
                   -- always single-currency, concrete amount (no NULL amount, no rate snapshot)

fx_rates         (id, base_currency, quote_currency, rate,
                  source['manual'|'api'], fetched_at, set_by)   -- group-wide; manual beats api
                   -- DISPLAY ONLY

actions          (id, ledger_id, actor_user_id, kind, intent_json JSON, before_image JSON,
                  result_chat_id, result_message_id, created_at, undone_at, undone_by)
                   -- before_image: only for pointer/field flips (switch ledger, currency, fx);
                   --   row-creating ops are reversed by created_by_action_id, no before_image.

pending_intents  (id, chat_id, message_id, intent_json JSON, created_at, expires_at)
                   -- keyed by the PROPOSAL message_id; for confirm + reply-to-correct

processed_updates(update_id PK, seen_at)                 -- webhook idempotency
```

Indices: `expenses(ledger_id, deleted_at)`, `settlements(ledger_id, deleted_at, created_at)`,
`actions(ledger_id, undone_at)`, `identities(platform, username)`,
`identities(platform, platform_user_id)`, `expenses(created_by_action_id)`,
`settlements(created_by_action_id)`.

Rules:
- `groups.active_ledger_id` must point to an `open` ledger of that group.
- Soft-deleted rows (`deleted_at IS NOT NULL`) are excluded from all balance/list queries.
- A user is "registered in this group" iff they have an `identities` row **and** a `group_members`
  row for this group with `left_at IS NULL`. (A member who left keeps balances and remains
  referenceable by id for settle/undo, but is excluded from "everyone".)

---

## 6. Request lifecycle (`core/`)

```
Telegram update (webhook push OR getUpdates poll)
  -> transport: validate secret header (webhook) · dedupe by update_id
  -> core.handler.dispatch(update_dict)            [transport-agnostic from here down]
  -> core.router classifies into exactly ONE:
       callback_query .................. confirm/cancel · undo/redo · board-settle · split-type/picker
       reply to a PENDING proposal ..... nl.refine(pending, text) -> edit proposal in place
       reply to any OTHER bot message .. new NL intent (as if @mentioned) — e.g. reply to the board
       slash command ................... intents.commands (CPU parse)
       @mention (text) ................. intents.nl.extract_text -> Intent
       photo (mention in caption / reply) intents.nl.extract_vision -> Intent
       document upload ................. backup.import_ (operator)
       service events .................. onboarding/registration (§11)
  -> resolve references (read-only preview for proposals): refs -> registered members
       any unknown ref -> REJECT whole intent with registration guidance (§11); STOP
       ambiguous ref (two "Sam"s) -> force a pick-list confirm; STOP
  -> confirm? (§0.7)
       fuzzy  -> render proposal + Confirm/Cancel keyboard; store pending_intent keyed by the
                 PROPOSAL message_id; STOP (await tap or reply-correct)
       reads  -> render result directly; no action row
       deterministic mutation -> straight to apply
  -> domain.apply.apply_intent(intent)   [one transaction]
       authoritative resolve (may register nothing new — unknown already rejected)
       perform write; tag every row with created_by_action_id; append ONE actions row
  -> recompute balances (replay) -> update pinned board (edit-in-place)
  -> return OutboundAction(s); result reply carries ↩️ Undo and is prefixed with the active
       ledger ("📒 Japan Trip • …")
```

**Stateless note.** Cloud Run is stateless: pending intents, undo state, and board ids all live in
the DB. `callback_data` is capped at **64 bytes**, so buttons carry only an id + namespace+version
(`"v1:undo:123"`, `"v1:settle:45"`, `"v1:confirm:678"`, `"v1:pick:9:42"`); payloads are fetched
from the table on tap.

---

## 7. Domain algorithms (pure — build and unit-test first)

### 7.1 Allocation (`domain/allocate.py`)
Splits a total into per-person integer `e2` shares **in whole smallest units** (multiples of
`UNIT_E2 = 100`), identically for every currency. Used for `equal`, `shares`, `percent`. `exact`
skips this.

```
UNIT_E2 = 100   # one smallest currency unit; identical for every currency

def allocate(total_e2, weights, payer):
    # weights: equal -> all 1; shares -> given; percent -> given (any positive scale)
    units = total_e2 // UNIT_E2
    dust  = total_e2 - units * UNIT_E2                 # 0..99 sub-unit remainder (0 for real amounts)
    W = sum(weights.values())
    raw   = {u: Fraction(units * w, W) for u, w in weights.items()}
    base  = {u: floor(raw[u]) for u in weights}
    short = units - sum(base.values())
    # hand out the `short` leftover smallest-units to the largest fractional remainders;
    # tie-break: payer first, then ascending stable user id
    order = sorted(weights, key=lambda u: (-(raw[u] - base[u]), 0 if u == payer else 1, u))
    for u in order[:short]:
        base[u] += 1
    owed = {u: base[u] * UNIT_E2 for u in weights}
    # sub-unit dust -> payer if participant, else largest share (ties: ascending id)
    target = payer if payer in owed else max(owed, key=lambda u: (owed[u], -u))
    owed[target] += dust
    assert sum(owed.values()) == total_e2
    return owed
```

Validation by split type, BEFORE allocate:
- `equal`: weights all 1 over the participant set (default = all registered members, payer included).
- `shares`: weights = given positive weights.
- `percent`: reject if `abs(sum(percent) - 100) > 1.0`; else weights = the given percents. The ±1.0
  tolerance is absorbed by normalization (`W = sum(percents)`); there is no separate "remainder to
  payer" step (that earlier wording is dropped).
- `exact`: require `sum(exact_e2) == total_e2` exactly after quantizing each to `UNIT_E2` (100);
  else reject and show the difference. No remainder logic.

### 7.2 Balance replay (`domain/balances.py`)
Per-user net per currency (pooled model): `net[user][ccy]` in `e2`, positive = user owes the pool.
Conservation holds per currency: `sum over users == 0`.

```
def balances(ledger_id) -> dict[user, dict[ccy, int]]:
    events = ordered(expenses ∪ settlements where deleted_at IS NULL, by (created_at, id))
    net = defaultdict(lambda: defaultdict(int))
    for ev in events:
        if isinstance(ev, Expense):
            net[ev.payer][ev.currency] -= ev.amount_e2
            for s in ev.splits:
                net[s.user][ev.currency] += s.owed_e2
        else:  # Settlement (always single-currency, concrete amount)
            net[ev.from_user][ev.currency] -= ev.amount_e2
            net[ev.to_user][ev.currency]   += ev.amount_e2
    return net
```

### 7.3 Settlements (`domain/settle.py`) — same-currency only
- **With amount** `A` in currency `C`: validate that `from` owes `to` in `C` (per simplify, §7.4);
  reject if not (`"Nothing to settle in C"`). Materialize one `settlements` row `(from, to, A, C)`.
- **Full settle-up** (no amount): compute simplified `from→to` debts (§7.4); for **each** currency
  where `from` owes `to`, materialize one concrete `settlements` row in that currency, **all tagged
  with the same `action_id`**. If `from` owes `to` nothing in any currency → `"Nothing to settle"`
  (do not create a reverse credit).
- Payments are **immutable facts**. Undoing an earlier expense never resizes a later settlement; it
  recomputes balances by replay, and any now-excess payment surfaces as a credit with a warning
  (§9). This is intentional and replaces any "dynamically shrink the settle-up" reading.

### 7.4 Simplify (`domain/simplify.py`) — minimum cash-flow per currency, deterministic
Run independently per currency on the net positions. Greedy, with a **stable tiebreaker** so output
is deterministic (important for board stability and golden tests):

```
def simplify(net_ccy: dict[user, int]) -> list[(debtor, creditor, e2)]:
    debtors   = sorted([(u, v)  for u, v in net_ccy.items() if v > 0], key=lambda x: (-x[1], x[0]))
    creditors = sorted([(u, -v) for u, v in net_ccy.items() if v < 0], key=lambda x: (-x[1], x[0]))
    # repeatedly match largest debtor with largest creditor (ties by ascending id); emit transfers
    ...
    return transfers
```
Board "who owes whom" = union over currencies of `simplify(net[·][ccy])`. A pair can appear on more
than one line (one per currency they owe across).

### 7.5 FX (`domain/fx.py`) — DISPLAY ONLY
- Resolve `FROM→TO`: latest manual `fx_rates` row wins; else Frankfurter; cache group-wide with a
  12–24h TTL; stamp `fetched_at`; surface staleness in display.
- Frankfurter is EUR-based; **triangulate** non-EUR pairs via EUR.
- Unsupported currency or API down → no rate; callers render `(≈ n/a)`. Never guess, never block.
- FX is used only by §7.6; it never participates in `apply_intent` or settlement math.

### 7.6 Convert & equivalents (`domain/convert.py`) — pure reads
- `≈` equivalent: convert a bucket to the group home currency at the current rate, format to the
  home currency's `minor_digits`. Nothing stored.
- `/convert <TARGET>`: convert every bucket to TARGET and sum. Read-only.
- `/balance`: each currency bucket + its `≈ home` line + a total `≈ home` line, labeled approximate.

---

## 8. apply_intent (`domain/apply.py`) — the forward write path

Single function, single transaction. For each `Intent` kind it: authoritatively resolves refs to
registered members (the router already rejected unknowns), performs the write, **stamps every new
row with the `action_id`**, and appends **one `actions` row**. Returns the rendered reply + keyboard
so the caller can send it and store `result_chat_id/message_id` back on the action (for the Undo
button).

Reversal model:
- Row-creating ops (`add_expense`, `settle_up` incl. multi-row full settle, `delete_expense` which
  flips `deleted_at`) → undo = soft-delete / restore **all rows where `created_by_action_id = me`**.
  No per-kind inverse needed.
- Field/pointer flips (`switch_ledger`, `archive_ledger`, `new_ledger`'s active-pointer change,
  `set_currency`, `set_fx_rate`, `edit_expense`) → store a minimal `before_image` (prev value of the
  changed fields) and restore it on undo.
- `new_ledger` undo restores the previous `active_ledger_id`; the created (empty) ledger is marked
  `archived` rather than hard-deleted.
- `setup` writes registration rows but appends an action with **no Undo affordance** (permanent).
- `show_balance` / `unknown` write no action row.

---

## 9. Undo / redo (`domain/undo.py`) — callback actions, button-only

- Each undoable result message carries an **↩️ Undo** button; `callback_data` holds the `action_id`
  only. State lives in `actions`, so the button survives cold starts and arbitrarily old messages.
  **The button persists permanently** and is never stripped.
- **Anyone may press within `UNDO_WINDOW_HOURS` (default 24).** After that the action **locks**: only
  the **operator** may undo/redo. The lock is **computed on press** (`now ≥ created_at + window`) —
  there is no scheduler. A non-operator press after lock gets
  `answerCallbackQuery("🔒 Locked — over 24h old. Ask the operator, @<operator>.")` and the button
  stays put so the operator can tap it. The operator is named by resolving `OPERATOR_USER_ID` to
  their @username (fallback display name).
- **Idempotent toggle:** set `undone_at`/`undone_by` only if currently null, in one transaction; a
  stale or double tap no-ops ("already undone").
- On undo, reverse the action (§8), then **edit the same message** to reflect state and flip the
  button to **↪️ Redo** (re-apply, clear `undone_at`). Toggle flips Undo ⇄ Redo.
- The undo is the **DB transaction**; `editMessageText` is cosmetic and best-effort — if it fails,
  the ledger is still correct and we just `answerCallbackQuery("Undone ✓")`. **Always edit, never
  delete** (Telegram's 48h delete limit is real; the edit limit does not apply to bot messages with
  inline keyboards).
- **Undoing a settled-against expense** is allowed; the result notes any resulting credit
  (e.g. `"⚠️ a later settlement now overpays — Alice has a JPY 200 credit"`).
- **NL "undo"/"redo" is not honored as an action.** It maps to `Unknown` with a templated reply
  pointing the user to the ↩️ button on the message they want to reverse.
- Import is **not** on the per-action undo stack; it takes its own pre-snapshot (§13).

---

## 10. Confirm + reply-to-correct loop (`core/pending.py`)

1. Fuzzy intents (NL/OCR, low-confidence, ambiguous) are **proposed**: render a summary +
   `[✅ Confirm] [✖ Cancel]` keyboard with footer `↳ reply to correct`; store the **unresolved**
   intent in `pending_intents` keyed by the **proposal message_id**; TTL `PENDING_TTL_MINUTES` (15).
   Resolution that *creates* state is deferred to confirm, so a cancelled proposal leaves nothing
   behind. (Unknown-member rejection still happens at propose time via read-only preview, §11.)
2. To correct, the user **replies** to the proposal with free text. Privacy mode delivers replies to
   the bot's own messages, so no re-mention is needed. The router matches
   `reply_to_message.message_id` to a pending intent → `nl.refine(pending, text)` → **edit the
   proposal in place**. Repeat as needed.
3. **Confirm** → `apply_intent`, then **edit that same message** into the committed result carrying
   the ↩️ Undo button (do not post a second message). **Cancel** → drop pending, edit to
   "Cancelled". **Expiry** → edit to "Expired — please resend."
4. Reply to any **non-pending** bot message (notably the pinned board) is a **new NL intent**,
   exactly as if @mentioned — this removes the @mention tax after the first interaction.

---

## 11. Identity, registration & onboarding (`domain/identity.py`)

**No ghosts. Registered members only.** A user exists to the bot only once it has seen their
Telegram `User` object and created `users` + `identities` + `group_members` rows.

Registration happens when the bot sees an account:
- **Join:** `new_chat_members` carries full `User` objects → auto-register.
- **Any interaction:** a message, @mention, reply, or button tap carries `from` → register the
  author if new.
- **`/setup`:** seeds pre-existing members the bot can identify:
  - replying to one of their messages with `/setup` → register that author;
  - `text_mention` entities (tap-selected, embed `user.id`) → register each.
  - A bare `@username` cannot be resolved to an account by the Bot API, so `/setup @carol` is
    **rejected** for that entry with guidance: ask Carol to send any message here, or reply to one
    of her messages with `/setup`.

**Reference resolution (`identity.resolve`).** `@username`/`text_mention` are exact; a bare name
from NL is a fuzzy match over registered members' display names/usernames.
- **Unknown reference → reject the whole intent.** No partial commit. Reply names the unknown ref
  and how to register them, e.g.: `"I don't know Dana in this group yet. They need to send a
  message here (or be added with /setup) before I can split with them."`
- **Ambiguous reference** (two "Sam"s) → force a pick-list confirm rather than guessing.

**"Everyone" / empty participants** = all currently-registered members of this group
(`left_at IS NULL`), payer included. Any "everyone" proposal **lists the names it used**, so a
silently-excluded (unregistered) real person is noticeable, and the welcome suggests `/setup`.

**Onboarding (`my_chat_member`, bot added):** register the group, create the first ledger named
after the group (fallback `"General"`), and post a welcome that (a) asks the operator to set the
home currency `/currency <ISO>`, (b) explains `/setup` for pre-existing members and that bare
usernames can't be added, (c) notes photos/NL need an @mention or a reply to the bot. The home
currency must be set before the first expense; if unset, the first add prompts for it.

**Leaving.** On `left_chat_member`, set `group_members.left_at`. Balances persist; the user stays
referenceable by id for settling/undo but is excluded from future "everyone" splits.

---

## 12. Natural-language coverage (`intents/nl.py`, `llm/prompts.py`)

**Every command is reachable by NL** (the LLM emits the matching `Intent` kind). The prompt's
few-shot set must cover all kinds, not just expenses/settles. Examples of the mapping:

| NL example | Intent |
|---|---|
| "I paid 40 for dinner, split with Sam" | `add_expense` |
| "Bob owes me 15 for the taxi" | `add_expense` (exact, payer=me) |
| "settle up with Alex" / "I paid Alex 30 SGD" | `settle_up` |
| "what do I owe?" / "show balances" | `show_balance` (read — runs immediately) |
| "convert everything to USD" | `show_balance(convert_to=USD)` (read) |
| "set the currency to euros" | `set_currency` |
| "new ledger called Tokyo" / "switch to Japan" / "archive this ledger" | ledger intents |
| "pin the rate 1 usd = 1.35 sgd" | `set_fx_rate` |
| "add Carol" (by reply/tap) | `setup` (bare username → guidance) |
| "export everything" | export flow (operator for `all`) |
| "undo that" | `unknown` → reply pointing to the ↩️ button |

Routing rules: **reads run immediately**; **NL/OCR mutations always propose+confirm** (§0.7);
**undo/redo are never performed from NL**. Low `confidence` or ambiguous resolution forces confirm
even for otherwise-deterministic cases. `nl.refine(prior_intent_json, correction_text)` returns a
refined intent for the reply-to-correct loop.

---

## 13. Telegram specifics & gotchas (`telegram/`)

- **Privacy mode (default ON)** delivers only: slash commands, @mentions of the bot, replies to the
  bot's messages, callback queries, and member/service messages. Keep it on. A bare photo with no
  caption-mention and not a reply to the bot is ignored.
- **`callback_data` ≤ 64 bytes** — carry ids only, namespaced+versioned (`"v1:undo:123"`); resolve
  payloads from the DB.
- **Editing vs deleting:** bot messages with an inline keyboard can be edited at any age; deletion
  is limited to 48h. Undo edits, never deletes.
- **Files:** `sendDocument` up to ~50 MB (export); inbound download via `getFile` then
  `…/file/bot<token>/<file_path>`, ~20 MB limit (import). JSON backups are tiny.
- **Pinning** the board requires the bot to be a **group admin**; if it isn't, post the board
  unpinned and warn once.
- **Command menu:** call `setMyCommands` on startup so `/equal`, `/exact`, `/currency`, `/setup`,
  etc. are discoverable.
- **Webhook:** validate `X-Telegram-Bot-Api-Secret-Token` against `TELEGRAM_WEBHOOK_SECRET`; dedupe
  by `update_id` (insert into `processed_updates`); process synchronously and return 200 (retries
  absorbed by the dedupe table).
- **Entities:** parse `mention` (text only — may be unresolvable → reject) and `text_mention`
  (embeds `user.id` — resolvable/registerable).
- **Board lifecycle:** created+pinned on a ledger's first mutation (or on `/newledger`); thereafter
  edited in place. Each debt line: `from → to  AMT CCY (≈ home)  [Settle]`. `[Settle]` = full settle
  of that single-currency line; anyone may tap (shared, undoable); it edits the board **and** posts a
  short result with its own ↩️ Undo. Board lines themselves never carry Undo.

---

## 14. Config (`.env.example`)

```
MODE=webhook                 # or "poll" for local dev
BOT_TOKEN=
TELEGRAM_WEBHOOK_SECRET=     # validated on every webhook request
PUBLIC_URL=                  # for setWebhook (prod)
DATABASE_URL=                # Neon POOLED url in prod; sqlite+aiosqlite:///./expensir.db locally
OPERATOR_USER_ID=            # telegram user id; runs /import, full /export, undo locked actions
UNDO_WINDOW_HOURS=24
PENDING_TTL_MINUTES=15

LLM_TEXT_PROVIDER=cloudflare # cloudflare | groq | gemini
LLM_VISION_PROVIDER=cloudflare
CF_ACCOUNT_ID=
CF_API_TOKEN=
GROQ_API_KEY=                # only if swapping the text path
GEMINI_API_KEY=              # only if swapping the vision path

FX_API_BASE=https://api.frankfurter.dev   # no key; ECB daily; cache 12-24h; DISPLAY ONLY
```

Deploy: run Alembic migrations + `setWebhook` as a one-shot release step, not on every cold start.

---

## 15. Build milestones (each green — tests + smoke — before the next)

1. **Skeleton + transport + outbound.** `webhook.py` + `poll.py` both call `dispatch`, which returns
   `OutboundAction`s that `executor.py` performs. *Done when:* `/start` echoes via webhook (secret
   checked) and polling.
2. **DB + models + Alembic.** All tables from §5. *Done when:* migrations apply clean on Postgres and
   SQLite; `/start` registers group + caller + first ledger.
3. **Money & currency (pure).** `money.py`: `to_e2`, `quantize_e2`, `fmt`, minor-unit table. *Done
   when:* unit tests cover 2/0/3-decimal currencies and rounding visibility.
4. **Ledgers.** `/newledger`, `/ledgers`, `/switch`, `/archive`; active-ledger wiring; switch
   announced; archive warns on non-zero balances (warn, allow). *Done when:* switching redirects new
   expenses.
5. **Slash expense path (no LLM).** `/add`, `/equal`, `/exact`, `/shares`, `/percent`, `/balance`,
   `/settle` (incl. full settle-up), `/currency`. `setMyCommands` registered. *Done when:*
   `/equal 60 dinner @A @B` splits correctly and `/balance` reads it back; bare `/add` shows
   split-type buttons → member picker / fill-in template.
6. **Allocation + balances + simplify (pure).** *Done when:* golden tests pass — 3-way $10.00 →
   334/333/333 (cents); JPY ¥6001 → 2001/2000/2000 (whole yen, extra unit to payer); every share is
   a whole smallest unit (multiple of 100 e2) and `sum(owed)==total` always; simplify reduces
   A→B→C to A→C and is deterministic.
7. **Provenance + undo/redo.** `apply_intent` appends `actions` and tags rows with
   `created_by_action_id`; soft-delete; persistent per-message Undo button; idempotent toggle; 24h
   lock computed on press; operator override + operator named. *Done when:* undo/redo flips balances;
   double-tap no-ops; a simulated >24h action is operator-only.
8. **Confirm + reply-to-correct.** `pending_intents` keyed by proposal message_id; Confirm/Cancel;
   reply refines + edits in place; TTL expiry; resolution deferred to confirm. *Done when:* a proposed
   expense is corrected by reply, then confirmed, and only then commits.
9. **Registration & /setup.** Auto-register on join/interaction; `/setup` via reply + text_mention;
   bare-username rejection with guidance; unknown-reference whole-intent rejection. *Done when:* an
   expense naming an unknown person is rejected with guidance, and a reply-`/setup` then lets it
   commit.
10. **NL text path (all intents).** `llm/cloudflare.py` + prompts → any `Intent` kind; reads run,
    mutations confirm; `nl.refine`. *Done when:* "@bot I paid 40 for dinner, split with Sam" proposes
    correctly and "set currency to USD" / "switch to Tokyo" map to the right intents (LLM mocked with
    fixtures; live-smoke manually).
11. **Vision path.** Receipt photo (mention/reply) → `extract_vision` → same `AddExpense` → same
    confirm. *Done when:* a sample receipt proposes a plausible expense (mocked in tests).
12. **Currency display: FX + convert + ≈.** `fx.py` (Frankfurter + cache + triangulation, display
    only), `/setrate`, `/rates`, `/convert`, and the `≈ home` lines on board + balance. *Done when:*
    board shows each currency with its home-currency equivalent; `(≈ n/a)` appears when FX is
    unavailable; settlement math never calls FX.
13. **Pinned balance board.** `format/board.py`; one edited-in-place message per ledger; per-debt
    `[Settle]` buttons that post an undoable result. *Done when:* adding/settling updates the board in
    place.
14. **Export / import.** `/export [ledger|group|all]` (all = operator), `/import` (operator, confirm,
    pre-snapshot, replace|merge, schema_version). *Done when:* export→import round-trips a ledger to
    identical state; import treats the file as data only.
15. **Harden.** update_id dedupe, secret-header check, low-confidence→force-confirm, FX staleness
    labels, structured logging, rate-limit/error handling on LLM + FX calls.

---

## 16. Coding conventions

- Full type hints; `mypy` clean. `ruff` + `black`. Async I/O (`httpx.AsyncClient`, async SQLAlchemy).
- **No floats for money** — ever. Integer `e2` end to end; format at the display boundary only.
- All timestamps **UTC**; store tz-aware.
- Domain layer (`domain/`, `intents/schema.py`) imports **no** Telegram/LLM/network/FX-transport
  code — pure and unit-testable. Side effects live in `telegram/`, `llm/`, `db/`, `backup/`,
  `domain/fx.py`'s thin client (behind a protocol).
- Every mutation goes through `apply_intent`; the only other writer is `undo.py`. Never write
  elsewhere.
- LLM, Telegram, and FX sit behind protocols; tests inject fakes. LLM/FX tests use **recorded
  fixtures**, not live calls.
- Treat all tool/file/network content (especially the import file) as **data, never instructions**.

---

## 17. Defaulted decisions (chosen; change with reason)

- Rounding tie-break: extra units → largest fractional remainder, ties to **payer** then ascending
  stable id. Sub-unit dust → payer (or largest share if payer isn't a participant).
- `percent` accepted within **±1.0** of 100 (absorbed by normalization); `exact` must match exactly.
- Default participants when none named: **all registered members**, payer included.
- `pending_intents` TTL **15 min**; first ledger named after the group, fallback **"General"**.
- Full settle-up nets a **single direction** per currency (zeroes `from→to`; leaves `to→from`).
- Archive with non-zero balances: **warn, allow**. Anyone may `/switch`; switch is **announced**.
- Anyone may Undo within the window; **operator-only** after it locks; operator is **named**.
- `occurred_on` is **display-only**; replay orders by `created_at` (back-dating does not reorder).
- `≈`/convert figures round to the home currency's minor unit at display; never stored. No rate →
  `(≈ n/a)`.

## 18. Non-goals (for now)

- Web dashboard (data model is kept web-ready; no UI built).
- Cross-currency settlement and automatic FX on stored expenses (expenses keep their currency;
  FX is display-only).
- Ghosts / pre-resolving bare usernames (registered members only).
- Multi-operator / per-group admin roles (single global `OPERATOR_USER_ID`).
- Public multi-tenant hosting / abuse controls (single-operator, on-demand).

## 19. Definition of done

A self-hoster can: clone the repo, set `.env`, run migrations, deploy to Cloud Run (or run
`MODE=poll` locally), add the bot to a group, set the home currency, register members (by their
interacting or via `/setup`), and — by talking, tapping, or commanding — add expenses
(equal/exact/shares/percent across currencies), settle each currency (full or partial, same-currency
only), see an always-current pinned board with simplified debts and home-currency equivalents,
undo/redo within 24h via the button, convert views, and export/import the ledger as JSON. All domain
math is covered by passing unit tests; references to unknown people are rejected with guidance; and
the bot never acts on un-mentioned group chatter.
