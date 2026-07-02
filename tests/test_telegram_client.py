import httpx
import pytest

from expensir.telegram.client import HttpxTelegramClient


async def test_send_message_posts_to_bot_api_and_returns_result():
    seen: dict = {}

    def record(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["json"] = request.read()
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 7}})

    http = httpx.AsyncClient(transport=httpx.MockTransport(record))
    client = HttpxTelegramClient("TOKEN123", http=http)

    result = await client.send_message(chat_id=-42, text="hello")

    assert result == {"message_id": 7}
    assert seen["url"] == "https://api.telegram.org/botTOKEN123/sendMessage"
    assert b'"chat_id":-42' in seen["json"].replace(b" ", b"")


async def test_api_base_is_overridable_for_stub_and_test_environments():
    seen: dict = {}

    def record(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"ok": True, "result": {}})

    http = httpx.AsyncClient(transport=httpx.MockTransport(record))
    client = HttpxTelegramClient("TOKEN123", http=http, api_base="http://localhost:9999")

    await client.send_message(chat_id=-42, text="hello")

    assert seen["url"] == "http://localhost:9999/botTOKEN123/sendMessage"


async def test_send_message_raises_when_telegram_says_not_ok():
    def refuse(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": False, "description": "Bad Request"})

    http = httpx.AsyncClient(transport=httpx.MockTransport(refuse))
    client = HttpxTelegramClient("TOKEN123", http=http)

    with pytest.raises(RuntimeError):
        await client.send_message(chat_id=-42, text="hello")
