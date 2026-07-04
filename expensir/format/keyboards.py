"""Inline keyboards (§9, §13). callback_data ≤ 64 bytes: namespace+version+id only."""

from typing import Any

InlineKeyboard = dict[str, Any]


def undo_keyboard(action_id: int) -> InlineKeyboard:
    return {"inline_keyboard": [[{"text": "↩️ Undo", "callback_data": f"v1:undo:{action_id}"}]]}


def redo_keyboard(action_id: int) -> InlineKeyboard:
    return {"inline_keyboard": [[{"text": "↪️ Redo", "callback_data": f"v1:redo:{action_id}"}]]}
