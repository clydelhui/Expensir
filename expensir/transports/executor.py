"""Performs the OutboundActions the core returns as data (§0.12)."""

from expensir.core.outbound import OutboundAction
from expensir.telegram.client import TelegramClient


async def execute(actions: list[OutboundAction], client: TelegramClient) -> None:
    for action in actions:
        if action.kind == "send_message":
            await client.send_message(chat_id=action.chat_id, text=action.text)
