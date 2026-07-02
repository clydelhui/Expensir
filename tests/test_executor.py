from expensir.core.outbound import SendMessage
from expensir.transports.executor import execute


class FakeTelegramClient:
    def __init__(self):
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, chat_id: int, text: str) -> dict:
        self.sent.append((chat_id, text))
        return {"message_id": len(self.sent)}


async def test_executor_performs_send_message_actions_in_order():
    client = FakeTelegramClient()

    await execute(
        [SendMessage(chat_id=-42, text="first"), SendMessage(chat_id=-42, text="second")],
        client,
    )

    assert client.sent == [(-42, "first"), (-42, "second")]
