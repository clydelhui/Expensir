"""Deterministic slash-command parsers — CPU only, no LLM (§2).

Amounts stay strings here: converting to minor units needs the resolved currency,
which the core looks up (ledger logging -> group home, §3) before building the Intent.
"""

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Literal

EQUAL_USAGE = "Usage: /equal <amount> [ISO] <description> [@name ...]"
EXACT_USAGE = "Usage: /exact <amount> [ISO] <description> @name=<amount> ..."
SHARES_USAGE = "Usage: /shares <amount> [ISO] <description> @name=<weight> ... (bare @name = 1)"
PERCENT_USAGE = "Usage: /percent <amount> [ISO] <description> @name=<percent> ..."
HOMECURRENCY_USAGE = "Usage: /homecurrency <ISO>, e.g. /homecurrency USD"
BALANCE_USAGE = "Usage: /balance — everyone's position, or /balance me for yours"
MEMBERS_USAGE = "Usage: /members — lists everyone registered in this group. It takes no arguments."
TRANSACTIONS_USAGE = (
    "Usage: /transactions — the ledger's history, newest first. It takes no arguments."
)
DELETE_USAGE = "Usage: reply to the expense with /delete, or /delete <id> (the #id on its line)"
EDIT_USAGE = (
    "Usage: reply to the expense with /edit, or /edit <id> — then [YYYY-MM-DD] "
    "[new description]. Amounts and participants can't be edited; delete and re-add instead."
)
NEWLEDGER_USAGE = "Usage: /newledger <name> [ISO], e.g. /newledger Tokyo JPY"
SWITCH_USAGE = "Usage: /switch <ledger>, e.g. /switch Tokyo — /ledgers to see them"
UNARCHIVE_USAGE = "Usage: /unarchive <ledger>, e.g. /unarchive Tokyo — /ledgers to see them"
CURRENCY_USAGE = "Usage: /currency <ISO>, e.g. /currency JPY"
SETTLE_USAGE = (
    "Usage: /settle [@from] @to <amount> <ISO>, e.g. /settle @alice 30 EUR — "
    "records that @from (you, if omitted) paid @to. Bare /settle @name shows "
    "what's left to settle between you two."
)

_EXPENSE_ID = re.compile(r"^#?(\d+)$")
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

_AMOUNT = re.compile(r"^\d+(\.\d+)?$")
_UPPER_ISO = re.compile(r"^[A-Z]{3}$")
_ANY_ISO = re.compile(r"^[A-Za-z]{3}$")


SplitType = Literal["equal", "exact", "shares", "percent"]


@dataclass
class ParsedExpense:
    amount: str
    currency: str | None
    description: str
    participant_refs: list[str] = field(default_factory=list)
    # parallel to participant_refs; the raw string after '=' (amount/weight/percent)
    participant_values: list[str] = field(default_factory=list)
    split_type: SplitType = "equal"


def parse_equal(text: str) -> ParsedExpense:
    tokens = text.split()[1:]  # drop the /equal itself
    if not tokens or not _AMOUNT.match(tokens[0]):
        raise ValueError(EQUAL_USAGE)
    amount, rest = tokens[0], tokens[1:]

    currency = None
    # an UPPERCASE 3-letter token right after the amount is an ISO override (§3);
    # lowercase 3-letter words stay part of the description ("fun", "the", ...)
    if rest and _UPPER_ISO.match(rest[0]):
        currency, rest = rest[0], rest[1:]

    participant_refs = [t for t in rest if t.startswith("@")]
    description = " ".join(t for t in rest if not t.startswith("@"))
    if not description:
        raise ValueError(EQUAL_USAGE)

    return ParsedExpense(
        amount=amount,
        currency=currency,
        description=description,
        participant_refs=participant_refs,
    )


def parse_exact(text: str) -> ParsedExpense:
    return _parse_valued_split(text, split_type="exact", usage=EXACT_USAGE)


def parse_shares(text: str) -> ParsedExpense:
    return _parse_valued_split(text, split_type="shares", usage=SHARES_USAGE, default_value="1")


def parse_percent(text: str) -> ParsedExpense:
    return _parse_valued_split(text, split_type="percent", usage=PERCENT_USAGE)


def _parse_valued_split(
    text: str, *, split_type: SplitType, usage: str, default_value: str | None = None
) -> ParsedExpense:
    """Shared shape of /exact, /shares, /percent: participants carry '@name=value'."""
    tokens = text.split()[1:]  # drop the command itself
    if not tokens or not _AMOUNT.match(tokens[0]):
        raise ValueError(usage)
    amount, rest = tokens[0], tokens[1:]

    currency = None
    if rest and _UPPER_ISO.match(rest[0]):
        currency, rest = rest[0], rest[1:]

    refs: list[str] = []
    values: list[str] = []
    description_tokens: list[str] = []
    for token in rest:
        if not token.startswith("@"):
            description_tokens.append(token)
            continue
        ref, sep, value = token.partition("=")
        if not sep:
            if default_value is None:
                raise ValueError(usage)
            value = default_value
        elif not _AMOUNT.match(value):
            raise ValueError(usage)
        if ref.lower() in (r.lower() for r in refs):
            # a person's value must be stated once; two values is a guess we won't make
            raise ValueError(f"{ref} appears more than once — name each person once.")
        refs.append(ref)
        values.append(value)

    description = " ".join(description_tokens)
    if not description or not refs:
        raise ValueError(usage)

    return ParsedExpense(
        amount=amount,
        currency=currency,
        description=description,
        participant_refs=refs,
        participant_values=values,
        split_type=split_type,
    )


@dataclass
class ParsedSettle:
    """The custom settle (§7.3): a directed pair, an amount, an explicit currency —
    or the settle sheet when amount is None (ADR-0007: the pair is then unordered)."""

    from_ref: str  # "me" when the speaker left themselves implicit
    to_ref: str
    amount: str | None
    currency: str | None


def parse_settle(text: str) -> ParsedSettle:
    """'/settle [@from] @to <amount> <ISO>' — the ISO is required: a settlement
    with an amount never falls back to ledger/home resolution (§4).
    No amount at all ('/settle @x') is the settle sheet, a read (ADR-0007)."""
    tokens = text.split()[1:]
    mentions = [t for t in tokens if t.startswith("@")]
    rest = [t for t in tokens if not t.startswith("@")]
    if len(mentions) == 1:
        from_ref, to_ref = "me", mentions[0]
    elif len(mentions) == 2:
        from_ref, to_ref = mentions
    else:
        raise ValueError(SETTLE_USAGE)
    if not rest:
        return ParsedSettle(from_ref=from_ref, to_ref=to_ref, amount=None, currency=None)
    if len(rest) != 2 or not _AMOUNT.match(rest[0]) or not _ANY_ISO.match(rest[1]):
        raise ValueError(SETTLE_USAGE)
    return ParsedSettle(from_ref=from_ref, to_ref=to_ref, amount=rest[0], currency=rest[1].upper())


def parse_balance(text: str) -> Literal["me", "group"]:
    tokens = text.split()[1:]
    if not tokens:
        return "group"
    if tokens == ["me"]:
        return "me"
    raise ValueError(BALANCE_USAGE)


def parse_members(text: str) -> None:
    """/members takes no arguments (#22): any trailing token is a usage error."""
    if text.split()[1:]:
        raise ValueError(MEMBERS_USAGE)


def parse_transactions(text: str) -> None:
    """/transactions is parameterless (ADR-0012): any trailing token is a usage error."""
    if text.split()[1:]:
        raise ValueError(TRANSACTIONS_USAGE)


def parse_delete(text: str) -> int | None:
    """The explicit '#id' if given (§11); None means 'resolve from the reply target'."""
    tokens = text.split()[1:]
    if not tokens:
        return None
    match = _EXPENSE_ID.match(tokens[0]) if len(tokens) == 1 else None
    if match is None:
        raise ValueError(DELETE_USAGE)
    return int(match.group(1))


@dataclass
class ParsedEdit:
    """Non-financial fields only (§4): a #id (optional when replying), a date, a description."""

    expense_id: int | None
    description: str | None
    occurred_on: str | None  # ISO date; DISPLAY ONLY (§7.2)


def parse_edit(text: str) -> ParsedEdit:
    tokens = text.split()[1:]

    expense_id = None
    if tokens:
        match = _EXPENSE_ID.match(tokens[0])
        if match is not None:
            expense_id = int(match.group(1))
            tokens = tokens[1:]

    occurred_on = None
    # a date is recognized only as the FIRST token after the id, so descriptions
    # that merely mention a date ("dinner on 2026-07-01") stay intact
    if tokens and _ISO_DATE.match(tokens[0]):
        try:
            date.fromisoformat(tokens[0])
        except ValueError:
            raise ValueError(f"{tokens[0]} isn't a real date — use YYYY-MM-DD.") from None
        occurred_on = tokens[0]
        tokens = tokens[1:]

    description = " ".join(tokens) or None
    if description is None and occurred_on is None:
        raise ValueError(EDIT_USAGE)
    return ParsedEdit(expense_id=expense_id, description=description, occurred_on=occurred_on)


def parse_newledger(text: str) -> tuple[str, str | None]:
    """'/newledger Tokyo JPY' -> ('Tokyo', 'JPY'); the ISO is a trailing UPPERCASE token (§3)."""
    tokens = text.split()[1:]
    if not tokens:
        raise ValueError(NEWLEDGER_USAGE)
    currency = None
    # a lone ISO-looking token is the NAME ('/newledger JPY' names a ledger, sets nothing)
    if len(tokens) > 1 and _UPPER_ISO.match(tokens[-1]):
        currency, tokens = tokens[-1], tokens[:-1]
    return " ".join(tokens), currency


def parse_switch(text: str) -> str:
    name = " ".join(text.split()[1:])
    if not name:
        raise ValueError(SWITCH_USAGE)
    return name


def parse_archive(text: str) -> str | None:
    """'/archive' -> None (the active ledger); '/archive Tokyo' -> 'Tokyo'."""
    name = " ".join(text.split()[1:])
    return name or None


def parse_unarchive(text: str) -> str:
    name = " ".join(text.split()[1:])
    if not name:
        raise ValueError(UNARCHIVE_USAGE)
    return name


def parse_homecurrency(text: str) -> str:
    return _parse_iso(text, HOMECURRENCY_USAGE)


def parse_currency(text: str) -> str:
    return _parse_iso(text, CURRENCY_USAGE)


def _parse_iso(text: str, usage: str) -> str:
    tokens = text.split()[1:]
    if len(tokens) != 1 or not _ANY_ISO.match(tokens[0]):
        raise ValueError(usage)
    return tokens[0].upper()
