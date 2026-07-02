import httpx
import pytest

from expensir.transports.webhook import create_app
from tests.factories import bot_added_update
from tests.test_executor import FakeTelegramClient

SECRET_HEADER = "X-Telegram-Bot-Api-Secret-Token"


@pytest.fixture
def telegram():
    return FakeTelegramClient()


@pytest.fixture
async def http(deps, telegram):
    app = create_app(deps=deps, telegram=telegram, webhook_secret="s3cret")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


async def test_webhook_rejects_missing_or_wrong_secret(http, telegram):
    update = bot_added_update(chat_id=-42)

    missing = await http.post("/webhook", json=update)
    wrong = await http.post("/webhook", json=update, headers={SECRET_HEADER: "nope"})

    assert missing.status_code == 403
    assert wrong.status_code == 403
    assert telegram.sent == []


async def test_webhook_dispatches_update_and_executes_outbound_actions(http, telegram):
    response = await http.post(
        "/webhook", json=bot_added_update(chat_id=-42), headers={SECRET_HEADER: "s3cret"}
    )

    assert response.status_code == 200
    [(chat_id, text)] = telegram.sent
    assert chat_id == -42
    assert "/homecurrency" in text


async def test_failed_processing_releases_the_dedupe_claim_for_telegrams_retry(deps):
    """A transient failure must not convert at-least-once delivery into at-most-once."""

    class FlakyOnceClient(FakeTelegramClient):
        def __init__(self):
            super().__init__()
            self.failures_left = 1

        async def send_message(self, chat_id: int, text: str) -> dict:
            if self.failures_left:
                self.failures_left -= 1
                raise RuntimeError("telegram is down")
            return await super().send_message(chat_id, text)

    telegram = FlakyOnceClient()
    app = create_app(deps=deps, telegram=telegram, webhook_secret="s3cret")
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        update = bot_added_update(update_id=88, chat_id=-42)

        first = await http.post("/webhook", json=update, headers={SECRET_HEADER: "s3cret"})
        retry = await http.post("/webhook", json=update, headers={SECRET_HEADER: "s3cret"})

    assert first.status_code == 500
    assert retry.status_code == 200
    [(chat_id, _)] = telegram.sent
    assert chat_id == -42


async def test_webhook_ignores_duplicate_update_ids(http, telegram):
    update = bot_added_update(update_id=77, chat_id=-42)

    first = await http.post("/webhook", json=update, headers={SECRET_HEADER: "s3cret"})
    second = await http.post("/webhook", json=update, headers={SECRET_HEADER: "s3cret"})

    assert first.status_code == 200
    assert second.status_code == 200
    assert len(telegram.sent) == 1
