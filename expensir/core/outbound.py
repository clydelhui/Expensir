"""Side effects the core returns as data; a transport-edge executor performs them (§0.12)."""

from typing import Any, Literal

from pydantic import BaseModel


class SendMessage(BaseModel):
    kind: Literal["send_message"] = "send_message"
    chat_id: int
    text: str
    reply_markup: dict[str, Any] | None = None
    # when set, the executor stores the sent message's chat/message id back onto
    # this actions row so undo can edit it and reply-to-target can resolve it (§8)
    records_result_for_action_id: int | None = None
    # when set, the executor stores the sent message's id onto this pending_intents
    # row — the proposal message keys the confirm/reply-to-correct loop (§10)
    records_message_for_pending_id: int | None = None


class EditMessage(BaseModel):
    """Cosmetic and best-effort (§9): the committed transaction is the truth, the
    edit only syncs the message; the executor swallows its failures."""

    kind: Literal["edit_message"] = "edit_message"
    chat_id: int
    message_id: int
    text: str
    reply_markup: dict[str, Any] | None = None


class EditMessageReplyMarkup(BaseModel):
    """Markup-only edit for InaccessibleMessage callbacks (§13): the original text
    is unavailable, but the button must still flip or redo becomes unreachable.
    Cosmetic and best-effort, like EditMessage."""

    kind: Literal["edit_message_reply_markup"] = "edit_message_reply_markup"
    chat_id: int
    message_id: int
    reply_markup: dict[str, Any]


class AnswerCallbackQuery(BaseModel):
    kind: Literal["answer_callback_query"] = "answer_callback_query"
    callback_query_id: str
    text: str | None = None


class PinChatMessage(BaseModel):
    """Pin the just-created board (§13). Best-effort: pinning needs the bot to be a
    group admin — on refusal the board stays unpinned and warn_text is sent, which
    happens once because only board creation emits this action."""

    kind: Literal["pin_chat_message"] = "pin_chat_message"
    chat_id: int
    message_id: int
    warn_text: str | None = None


OutboundAction = (
    SendMessage | EditMessage | EditMessageReplyMarkup | AnswerCallbackQuery | PinChatMessage
)
