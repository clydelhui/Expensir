# Expensir

A self-hostable Telegram bot for Splitwise-style expense splitting inside group chats. This glossary fixes the domain language; implementation lives in ARCHITECTURE-v2.md.

## Language

### Containers

**Group**:
A Telegram group chat the bot tracks. Owns its members and a single home currency, and contains one or more ledgers.
_Avoid_: chat, room, team

**Ledger**:
A self-contained book of expenses and settlements within a group. Balances and settlements never cross ledger boundaries. Typically one per trip or shared context; a group always has exactly one active ledger.
_Avoid_: book, account, tab

**Archived ledger**:
A ledger closed to new activity. Its balances are preserved and readable, but it cannot become active while archived — switching to it is refused until it is explicitly unarchived. Unarchiving reopens it without making it active; reopening and switching are separate, deliberate steps.
_Avoid_: deleted ledger, closed ledger

**Member**:
A person the bot has registered in a group (it has seen their Telegram account). Only current members may appear in a new transaction — there are no ghosts.
_Avoid_: user (reserved for the platform-agnostic identity), participant (reserved for the people on a single expense)

**Departed member**:
A member the bot has seen leave the group. Their balances and history persist untouched, but to new activity they are unknown: excluded from "everyone" and not nameable in new expenses or settlements until reactivated. Departure narrows the language, not the books.
_Avoid_: removed member, deleted member, ex-member

**Identity refresh**:
Keeping a member's stored username and display name current so references never resolve from stale data. Trusts only live sources — data the person themself just carried into the chat (their own message, tap, or join). Snapshots embedded in old messages (a reply target, a tapped mention) may seed a brand-new registration but never overwrite what is already stored. A lifecycle event — writes no action, not undoable.

**Reactivation**:
A departed member becoming a current member again, with the same identity and balances. Strictly self-triggered: the bot seeing them re-join, or any interaction of their own (a message, a tap, running a command — including `/setup`). No third party can reactivate them — a `/setup` naming a departed member does not bring them back. A lifecycle event like the original join — writes no action, not undoable.

### Currency

**Home currency**:
The single currency, set per group, that every balance and board figure is additionally shown in as an approximate `≈` equivalent. The "what is this worth back home" frame. Display only — never used in ledger math.
_Avoid_: base currency, default currency, group currency

**Logging currency**:
The default currency for *new* expenses in a given ledger. Set per ledger; overridable per expense; resolves to the home currency when unset. Distinct from the home currency: a ledger may log in JPY while the group's home currency is USD. It is only a default-picker — once an expense is written it freezes its own currency, so later changes to the logging or home currency never re-denominate existing transactions.
_Avoid_: default currency, ledger currency

**Equivalent** (`≈`):
The home-currency rendering of an amount that is natively in another currency, computed at today's rate and labelled approximate. Pure display; never stored; shows `(≈ n/a)` when no rate is available.

**Recognized currency**:
A circulating ISO 4217 code — the only kind of currency any input may introduce, whatever the door (slash command, natural language, receipt photo). Non-circulating codes (gold, SDRs, fund codes, "no currency") are not recognized. Recognition is checked at the input edge only: stored transactions and imported backups keep whatever code they were written with, even one since retired from the standard. Distinct from FX support — a recognized currency may still have no rate and render `(≈ n/a)`.
_Avoid_: valid currency, supported currency (reserved for FX rate availability)

### Transactions & balances

**Expense**:
A payment one member made on behalf of a set of participants, split among them. Belongs to one ledger and one currency.

**Participant**:
A member who shares in the cost of a single expense. Distinct from a Member (group-wide) — participation is per-expense.
_Avoid_: split member, sharer

**Settlement**:
A recorded payment from one member to another in a single currency. Treated as an immutable, stated fact: the bot records what a member says was paid and does not police it against the pool — any direction and overpayment are allowed, and any member may record a settlement, including between two other members.
_Avoid_: payment (ambiguous), transfer (reserved for a suggested transfer)

**Pool**:
The shared model balances are computed against. Every member holds a net position against the pool per currency (positive = owes the pool); there are no stored pairwise debts. Balances are derived by replaying expenses and settlements, never stored.
_Avoid_: kitty, group balance

**Suggested transfer**:
A single debtor→creditor payment in the minimum-cash-flow simplification of the pool, shown on the board. Solver-dependent: one of possibly several minimal solutions. Tapping `[Settle]` records that suggested transfer as a settlement.
_Avoid_: simplified debt, owed amount

**Settle sheet**:
The bot's reply to "settle up with X": the suggested transfers between two members, both directions, one line per currency, each with its own `[Settle]` button. A pure read, like the board — nothing commits until a line is tapped, and each tap records exactly that one line as one settlement.
_Avoid_: full settle-up (retired — there is no bulk multi-currency settle action, ADR-0007)

### Roles & artifacts

**Operator**:
The single person (one global `OPERATOR_USER_ID` per deployment) who may run imports and full exports and may undo actions after they lock. Not a per-group role — one operator across every group the bot serves.
_Avoid_: admin (reserved for Telegram's group-admin status), owner

**Backup**:
A JSON export of a ledger, group, or the whole deployment, carrying a declared scope and a `schema_version`. Treated as data, never instructions, on import. Only a `replace` import round-trips to identical state; `merge` produces equivalent balances under fresh ids.
_Avoid_: dump, snapshot (reserved for the pre-import safety snapshot)

### Intents & auditing

**Intent**:
The single shared contract every input (slash command, natural language, receipt photo) is parsed into before anything downstream runs. The parser is never the source of truth for math or persistence.

**Proposal**:
A rendered summary of a fuzzy intent (from NL/OCR, or an ambiguous reference) shown with Confirm/Cancel, awaiting a tap. Its intent is stored unresolved and re-resolved at confirm time, but it is pinned to the ledger it was proposed against — confirming commits there even if the group's active ledger changed meanwhile. A reply to a live proposal refines it in place; a reply to a dead one starts a fresh proposal.
_Avoid_: draft, preview

**Action**:
One audited, reversible unit of change. Every mutation appends exactly one action row, and every data row it writes carries that action's id, so undo is "soft-delete everything this action created." Reads and lifecycle events (join, leave, reactivation, identity refresh) write no action; registration via `/setup` appends one — permanent, with no undo.
_Avoid_: event, transaction (reserved for the DB transaction), operation

**Confidence**:
An LLM self-report on a parsed intent. Cosmetic only — it never changes a branch. NL/OCR intents always propose + confirm regardless; confidence may at most decorate a proposal with an "I'm not fully sure I read this right" cue.
