"""The wire schema: what the extractor LLM emits (§12, issue #13 grill).

Deliberately thinner than the Intent contract: money is a decimal string the app
converts via to_minor AFTER resolving the currency — the LLM is a parser only,
never the source of truth for math (§0). Wire-only kinds (undo_redo, unknown)
map to templated replies and never enter the app's Intent union.
"""

from typing import Annotated, Literal

from pydantic import BaseModel, Field


class WireSplitMember(BaseModel):
    user_ref: str  # "@alice", a bare name as heard, or "me" (the author)
    weight: float | None = None  # split_type="shares"
    exact: str | None = None  # decimal string; split_type="exact"
    percent: float | None = None  # split_type="percent"


class WireAddExpense(BaseModel):
    kind: Literal["add_expense"] = "add_expense"
    payer_ref: str = "me"
    amount: str  # decimal string, e.g. "40" or "12.50"
    currency: str | None = None  # ISO code if stated; None -> resolution order (§3)
    description: str
    occurred_on: str | None = None  # ISO date; DISPLAY ONLY (§7.2)
    split_type: Literal["equal", "exact", "shares", "percent"] = "equal"
    participants: list[WireSplitMember] = []
    confidence: float | None = None  # self-report; COSMETIC ONLY (§0.7)


class WireSettleUp(BaseModel):
    kind: Literal["settle_up"] = "settle_up"
    from_ref: str = "me"
    to_ref: str
    amount: str | None = None  # None -> the settle sheet, a read (ADR-0007)
    currency: str | None = None
    confidence: float | None = None


class WireShowBalance(BaseModel):
    kind: Literal["show_balance"] = "show_balance"
    scope: Literal["me", "group"] = "group"
    convert_to: str | None = None  # arrives with the FX slice; parsed, not served yet
    confidence: float | None = None


class WireShowTransactions(BaseModel):
    kind: Literal["show_transactions"] = "show_transactions"
    confidence: float | None = None


class WireDeleteExpense(BaseModel):
    kind: Literal["delete_expense"] = "delete_expense"
    expense_id: int | None = None  # a bare #id in the text; None -> the reply names it (§11)
    match: str | None = None  # descriptive words ("the dinner one" -> "dinner"), §11 tertiary
    confidence: float | None = None


class WireEditExpense(BaseModel):
    kind: Literal["edit_expense"] = "edit_expense"
    expense_id: int | None = None  # a bare #id in the text; None -> the reply names it (§11)
    match: str | None = None  # descriptive words naming the expense, §11 tertiary
    description: str | None = None
    occurred_on: str | None = None  # ISO date; DISPLAY ONLY (§7.2)
    confidence: float | None = None


class WireSwitchLedger(BaseModel):
    kind: Literal["switch_ledger"] = "switch_ledger"
    name_or_id: str
    confidence: float | None = None


class WireNewLedger(BaseModel):
    kind: Literal["new_ledger"] = "new_ledger"
    name: str
    logging_currency: str | None = None
    confidence: float | None = None


class WireArchiveLedger(BaseModel):
    kind: Literal["archive_ledger"] = "archive_ledger"
    name_or_id: str | None = None  # None -> the active ledger
    confidence: float | None = None


class WireUnarchiveLedger(BaseModel):
    kind: Literal["unarchive_ledger"] = "unarchive_ledger"
    name_or_id: str
    confidence: float | None = None


class WireSetHomeCurrency(BaseModel):
    kind: Literal["set_home_currency"] = "set_home_currency"
    currency: str
    confidence: float | None = None


class WireSetLoggingCurrency(BaseModel):
    kind: Literal["set_logging_currency"] = "set_logging_currency"
    currency: str
    confidence: float | None = None


class WireSetup(BaseModel):
    """The extractor only signals the KIND — targets come from the message's
    reply/text_mention entities, the only shapes carrying account ids (§11)."""

    kind: Literal["setup"] = "setup"
    confidence: float | None = None


class WireUnknown(BaseModel):
    """Text the model couldn't map -> the §4 Unknown intent (ask to rephrase)."""

    kind: Literal["unknown"] = "unknown"
    reason: str = ""


class WireUndoRedo(BaseModel):
    """WIRE-ONLY: NL undo/redo is detected, never honored (§9) — the app answers
    with the templated ↩️-button pointer and this never becomes an Intent."""

    kind: Literal["undo_redo"] = "undo_redo"


# grows into one variant per §4 intent kind, plus the wire-only ones
WireResult = Annotated[
    WireAddExpense
    | WireSettleUp
    | WireShowBalance
    | WireShowTransactions
    | WireDeleteExpense
    | WireEditExpense
    | WireNewLedger
    | WireSwitchLedger
    | WireArchiveLedger
    | WireUnarchiveLedger
    | WireSetHomeCurrency
    | WireSetLoggingCurrency
    | WireSetup
    | WireUnknown
    | WireUndoRedo,
    Field(discriminator="kind"),
]
