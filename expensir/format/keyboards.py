"""Inline keyboards (§9, §13). callback_data ≤ 64 bytes: ids only — except the
board [Settle] button, which carries its whole tuple + amount inline (ADR-0006)."""

from typing import Any

from expensir.domain.money import fmt
from expensir.domain.transactions import (
    TransactionCursor,
    TransactionPage,
    TransactionRow,
    encode_cursor,
)
from expensir.format.board import BoardLine

InlineKeyboard = dict[str, Any]


def undo_keyboard(action_id: int) -> InlineKeyboard:
    return {"inline_keyboard": [[{"text": "↩️ Undo", "callback_data": f"v1:undo:{action_id}"}]]}


def board_keyboard(transfers: list[BoardLine]) -> InlineKeyboard | None:
    """One WYSIWYG [Settle] button per suggested transfer (ADR-0006): the shown
    amount rides along as the optimistic-concurrency token."""
    if not transfers:
        return None
    return {
        "inline_keyboard": [
            [
                {
                    "text": f"🤝 Settle {t.from_name} → {t.to_name} "
                    f"{fmt(t.amount_minor, t.currency)}",
                    "callback_data": f"v1:st:{t.from_id}:{t.to_id}:{t.currency}:{t.amount_minor}",
                }
            ]
            for t in transfers
        ]
    }


def sheet_keyboard(ledger_id: int, transfers: list[BoardLine]) -> InlineKeyboard | None:
    """Per-line [Settle] buttons for the settle sheet (ADR-0007): same WYSIWYG
    amount token as the board, plus the ledger id — a sheet message is not the
    pinned board, so the tap can't resolve its ledger from the message itself."""
    if not transfers:
        return None
    return {
        "inline_keyboard": [
            [
                {
                    "text": f"🤝 Settle {t.from_name} → {t.to_name} "
                    f"{fmt(t.amount_minor, t.currency)}",
                    "callback_data": f"v1:sh:{ledger_id}:{t.from_id}:{t.to_id}"
                    f":{t.currency}:{t.amount_minor}",
                }
            ]
            for t in transfers
        ]
    }


def transactions_pager_keyboard(ledger_id: int, page: TransactionPage) -> InlineKeyboard | None:
    """The /transactions pager (ADR-0012): keyset cursors anchored on the page's
    edge rows, the ledger pinned at render time like the settle sheet's."""
    if not page.rows:
        # a cursor page can resolve to zero rows while has_newer/has_older stay
        # True (concurrent delete of the last row on this side): nothing to anchor
        return None
    buttons: list[dict[str, str]] = []
    if page.has_newer:
        buttons.append(
            {"text": "◀ Newer", "callback_data": _tx_cursor(ledger_id, "p", page.rows[0])}
        )
    if page.has_older:
        buttons.append(
            {"text": "▶ Older", "callback_data": _tx_cursor(ledger_id, "n", page.rows[-1])}
        )
    if not buttons:
        return None
    return {"inline_keyboard": [buttons]}


def _tx_cursor(ledger_id: int, verb: str, anchor: TransactionRow) -> str:
    """v1:tx:<ledger_id>:<n|p>:<epoch_us>:<kind>:<row_id> — ~40 bytes, within
    Telegram's 64-byte callback_data budget (ADR-0012)."""
    cursor = TransactionCursor(created_at=anchor.created_at, kind=anchor.kind, id=anchor.id)
    return f"v1:tx:{ledger_id}:{verb}:{encode_cursor(cursor)}"


def confirm_keyboard(pending_id: int) -> InlineKeyboard:
    """Confirm/Cancel on a proposal (§10), keyed by the pending row's id."""
    return {
        "inline_keyboard": [
            [
                {"text": "✅ Confirm", "callback_data": f"v1:confirm:{pending_id}"},
                {"text": "✖ Cancel", "callback_data": f"v1:cancel:{pending_id}"},
            ]
        ]
    }


def pick_keyboard(pending_id: int, choices: list[tuple[int, str]]) -> InlineKeyboard:
    """One ambiguous slot at a time (§10): a button per candidate, plus Cancel.

    Data stays ids-only within the 64-byte cap: the tap handler re-derives the
    open slot, so the ref itself never rides in the callback."""
    return {
        "inline_keyboard": [
            *(
                [{"text": label, "callback_data": f"v1:pick:{pending_id}:{user_id}"}]
                for user_id, label in choices
            ),
            [{"text": "✖ Cancel", "callback_data": f"v1:cancel:{pending_id}"}],
        ]
    }


def expense_pick_keyboard(pending_id: int, choices: list[tuple[int, str]]) -> InlineKeyboard:
    """The expense flavour of the pick stage (§11 tertiary, §13): a button per
    candidate expense, plus Cancel. Same ids-only discipline as v1:pick."""
    return {
        "inline_keyboard": [
            *(
                [{"text": label, "callback_data": f"v1:pickx:{pending_id}:{expense_id}"}]
                for expense_id, label in choices
            ),
            [{"text": "✖ Cancel", "callback_data": f"v1:cancel:{pending_id}"}],
        ]
    }


def redo_keyboard(action_id: int) -> InlineKeyboard:
    return {"inline_keyboard": [[{"text": "↪️ Redo", "callback_data": f"v1:redo:{action_id}"}]]}
