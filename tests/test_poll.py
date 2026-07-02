from expensir.transports.poll import poll_once
from tests.factories import bot_added_update, message_update
from tests.test_executor import FakeTelegramClient


class FakePollClient(FakeTelegramClient):
    def __init__(self, batches: list[list[dict]]):
        super().__init__()
        self.batches = batches
        self.requested_offsets: list[int] = []

    async def get_updates(self, offset: int, timeout: int) -> list[dict]:
        self.requested_offsets.append(offset)
        return self.batches.pop(0) if self.batches else []


async def test_poll_once_dispatches_each_update_and_advances_offset(deps):
    client = FakePollClient(
        [
            [
                bot_added_update(update_id=5, chat_id=-42),
                message_update(update_id=6, chat_id=-42, text="/start"),
            ]
        ]
    )

    new_offset = await poll_once(deps, client, offset=0)

    assert new_offset == 7
    assert client.requested_offsets == [0]
    assert len(client.sent) == 2  # welcome + /start reply, via the same dispatch seam


async def test_poll_once_with_no_updates_keeps_offset(deps):
    client = FakePollClient([])

    assert await poll_once(deps, client, offset=12) == 12
    assert client.sent == []
