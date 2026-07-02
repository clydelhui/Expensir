"""Side effects the core returns as data; a transport-edge executor performs them (§0.12)."""

from typing import Literal

from pydantic import BaseModel


class SendMessage(BaseModel):
    kind: Literal["send_message"] = "send_message"
    chat_id: int
    text: str


OutboundAction = SendMessage
