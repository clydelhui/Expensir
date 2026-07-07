"""The shared Intent contract (§4): every input modality produces the same shapes."""

from typing import Annotated, Literal

from pydantic import BaseModel, Field


class SplitMember(BaseModel):
    user_ref: str  # "@alice", a display name as seen in chat, or "me" (the author)
    weight: float | None = None  # split_type="shares"
    exact_minor: int | None = None  # split_type="exact"
    percent: float | None = None  # split_type="percent"


class AddExpense(BaseModel):
    kind: Literal["add_expense"] = "add_expense"
    payer_ref: str
    amount_minor: int
    currency: str | None = None  # None -> resolution order (§3); concrete on the slash path
    description: str
    occurred_on: str | None = None  # ISO date; DISPLAY ONLY (§7.2)
    split_type: Literal["equal", "exact", "shares", "percent"] = "equal"
    participants: list[SplitMember] = []  # empty -> all REGISTERED members, payer included
    confidence: float | None = None  # LLM self-report; COSMETIC ONLY (§0.7)


class SettleUp(BaseModel):
    """A settlement: one currency, one direction, one row, one action (ADR-0007).

    With an amount this is the CUSTOM path: fully ungated — any direction,
    overpayment allowed (ADR-0002). Without one it is the settle sheet, a read
    (ADR-0007, slice 10).
    """

    kind: Literal["settle_up"] = "settle_up"
    from_ref: str  # without an amount the pair is UNORDERED (ADR-0007)
    to_ref: str
    amount_minor: int | None = None  # None -> settle sheet (a READ; slice 10)
    currency: str | None = None  # required when amount given


class DeleteExpense(BaseModel):
    """Soft-delete: flips deleted_at, undoable — 'fixing history' (§4, §8)."""

    kind: Literal["delete_expense"] = "delete_expense"
    expense_id: int  # resolved via reply-to-target / #id (§11)


class EditExpense(BaseModel):
    """Non-financial fields ONLY (§4): display never reorders balances (§0.4)."""

    kind: Literal["edit_expense"] = "edit_expense"
    expense_id: int
    description: str | None = None
    occurred_on: str | None = None  # ISO date; DISPLAY ONLY (§7.2)


class SetHomeCurrency(BaseModel):
    kind: Literal["set_home_currency"] = "set_home_currency"
    currency: str


class SetLoggingCurrency(BaseModel):  # active ledger's new-expense default (ADR-0001)
    kind: Literal["set_logging_currency"] = "set_logging_currency"
    currency: str


class NewLedger(BaseModel):
    kind: Literal["new_ledger"] = "new_ledger"
    name: str
    logging_currency: str | None = None  # optional trailing ISO on /newledger


class SwitchLedger(BaseModel):
    kind: Literal["switch_ledger"] = "switch_ledger"
    name_or_id: str


class ArchiveLedger(BaseModel):
    kind: Literal["archive_ledger"] = "archive_ledger"
    name_or_id: str | None = None  # None -> the active ledger


class UnarchiveLedger(BaseModel):  # reopens; does NOT switch (orthogonal verbs, §17)
    kind: Literal["unarchive_ledger"] = "unarchive_ledger"
    name_or_id: str


class SetupTarget(BaseModel):
    """One person a /setup names, resolved by the router from the reply target or a
    text_mention entity — the only Telegram shapes that embed the user id (§11)."""

    platform_user_id: int
    display_name: str
    username: str | None = None


class Setup(BaseModel):
    """Register pre-existing members (§11). Permanent: no Undo affordance (§8)."""

    kind: Literal["setup"] = "setup"
    targets: list[SetupTarget] = []


class ShowBalance(BaseModel):
    """A read: never confirms, writes no action row (§0.7, §8). Active ledger only."""

    kind: Literal["show_balance"] = "show_balance"
    scope: Literal["me", "group"] = "group"
    convert_to: str | None = None  # /convert <TARGET>; arrives with the FX slice


class Unknown(BaseModel):
    """The LLM couldn't map it (§4): a no-op — ask to rephrase, write nothing."""

    kind: Literal["unknown"] = "unknown"
    reason: str = ""


# grows into the full §4 discriminated union as slices add kinds
Intent = Annotated[
    AddExpense
    | SettleUp
    | DeleteExpense
    | EditExpense
    | SetHomeCurrency
    | SetLoggingCurrency
    | NewLedger
    | SwitchLedger
    | ArchiveLedger
    | UnarchiveLedger
    | Setup
    | ShowBalance
    | Unknown,
    Field(discriminator="kind"),
]
