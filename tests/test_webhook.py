import httpx
import pytest
from sqlalchemy import select

from expensir.db.models import Action, Expense
from expensir.transports.webhook import create_app
from tests.factories import bot_added_update, message_update
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


class FlakyOnceClient(FakeTelegramClient):
    def __init__(self, failures_left: int = 1):
        super().__init__()
        self.failures_left = failures_left

    async def send_message(self, chat_id: int, text: str) -> dict:
        if self.failures_left:
            self.failures_left -= 1
            raise RuntimeError("telegram is down")
        return await super().send_message(chat_id, text)


async def test_failed_dispatch_releases_the_dedupe_claim_for_telegrams_retry(deps, monkeypatch):
    """A failure BEFORE anything commits must not turn at-least-once into at-most-once."""
    from expensir.transports import webhook as webhook_module

    real_dispatch = webhook_module.dispatch
    failures = {"left": 1}

    async def flaky_dispatch(update, flaky_deps):
        if failures["left"]:
            failures["left"] -= 1
            raise RuntimeError("db hiccup")
        return await real_dispatch(update, flaky_deps)

    monkeypatch.setattr("expensir.transports.webhook.dispatch", flaky_dispatch)
    telegram = FakeTelegramClient()
    app = create_app(deps=deps, telegram=telegram, webhook_secret="s3cret")
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        update = bot_added_update(update_id=88, chat_id=-42)

        first = await http.post("/webhook", json=update, headers={SECRET_HEADER: "s3cret"})
        retry = await http.post("/webhook", json=update, headers={SECRET_HEADER: "s3cret"})

    assert first.status_code == 500
    assert retry.status_code == 200
    [(chat_id, _)] = telegram.sent  # the retry actually processed
    assert chat_id == -42


async def test_send_failure_after_commit_never_double_applies_the_mutation(deps):
    """Telegram's retry of a lost reply must not record the money twice (§0.2).

    The reply is sacrificed; the dedupe claim stays so the retry no-ops.
    """
    telegram = FlakyOnceClient(failures_left=0)
    app = create_app(deps=deps, telegram=telegram, webhook_secret="s3cret")
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        headers = {SECRET_HEADER: "s3cret"}
        await http.post(
            "/webhook", json=bot_added_update(update_id=1, chat_id=-42), headers=headers
        )
        await http.post(
            "/webhook",
            json=message_update(update_id=2, chat_id=-42, text="/homecurrency EUR"),
            headers=headers,
        )

        telegram.failures_left = 1  # the /equal reply send will fail AFTER the commit
        equal = message_update(update_id=3, chat_id=-42, text="/equal 60 dinner", message_id=13)
        first = await http.post("/webhook", json=equal, headers=headers)
        retry = await http.post("/webhook", json=equal, headers=headers)

    assert first.status_code == 500
    assert retry.status_code == 200
    async with deps.session_factory() as session:
        expenses = (await session.execute(select(Expense))).scalars().all()
        actions = (
            (await session.execute(select(Action).where(Action.kind == "add_expense")))
            .scalars()
            .all()
        )
    assert len(expenses) == 1  # committed exactly once despite the retry
    assert len(actions) == 1


async def test_webhook_ignores_duplicate_update_ids(http, telegram):
    update = bot_added_update(update_id=77, chat_id=-42)

    first = await http.post("/webhook", json=update, headers={SECRET_HEADER: "s3cret"})
    second = await http.post("/webhook", json=update, headers={SECRET_HEADER: "s3cret"})

    assert first.status_code == 200
    assert second.status_code == 200
    assert len(telegram.sent) == 1
