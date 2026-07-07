"""Inline keyboards (§9, §13). callback_data ≤ 64 bytes: ids only — except the
board [Settle] button, which carries its whole tuple + amount inline (ADR-0006)."""

from typing import Any

from expensir.domain.money import fmt
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


def redo_keyboard(action_id: int) -> InlineKeyboard:
    return {"inline_keyboard": [[{"text": "↪️ Redo", "callback_data": f"v1:redo:{action_id}"}]]}
