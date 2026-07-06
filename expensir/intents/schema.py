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


class ShowBalance(BaseModel):
    """A read: never confirms, writes no action row (§0.7, §8). Active ledger only."""

    kind: Literal["show_balance"] = "show_balance"
    scope: Literal["me", "group"] = "group"
    convert_to: str | None = None  # /convert <TARGET>; arrives with the FX slice


# grows into the full §4 discriminated union as slices add kinds
Intent = Annotated[
    AddExpense
    | DeleteExpense
    | EditExpense
    | SetHomeCurrency
    | SetLoggingCurrency
    | NewLedger
    | SwitchLedger
    | ArchiveLedger
    | UnarchiveLedger
    | ShowBalance,
    Field(discriminator="kind"),
]
