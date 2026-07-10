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


async def test_download_file_resolves_getfile_then_fetches_the_bytes():
    """The vision door's photo fetch (issue #15): getFile gives a file_path,
    the bytes live under /file/bot<token>/ (§13)."""
    calls: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if "getFile" in str(request.url):
            return httpx.Response(
                200, json={"ok": True, "result": {"file_id": "abc", "file_path": "photos/f_1.jpg"}}
            )
        return httpx.Response(200, content=b"jpeg-bytes")

    http = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    client = HttpxTelegramClient("TOKEN123", http=http)

    data = await client.download_file("abc")

    assert data == b"jpeg-bytes"
    assert calls == [
        "https://api.telegram.org/botTOKEN123/getFile",
        "https://api.telegram.org/file/botTOKEN123/photos/f_1.jpg",
    ]


async def test_download_file_returns_none_when_telegram_refuses():
    """None, not an exception (FileSource contract): the handler answers with
    a transient 'try again', never a crash mid-update."""

    def refuse(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": False, "description": "file too big"})

    http = httpx.AsyncClient(transport=httpx.MockTransport(refuse))
    client = HttpxTelegramClient("TOKEN123", http=http)

    assert await client.download_file("abc") is None


async def test_download_file_returns_none_on_transport_trouble():
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("boom")

    http = httpx.AsyncClient(transport=httpx.MockTransport(boom))
    client = HttpxTelegramClient("TOKEN123", http=http)

    assert await client.download_file("abc") is None


async def test_download_file_returns_none_on_a_non_json_getfile_body():
    """Review fix: a 200 whose body isn't JSON (proxy, truncation) raises
    json.JSONDecodeError — a ValueError, which must keep the None contract."""

    def garbage(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html>captive portal</html>")

    http = httpx.AsyncClient(transport=httpx.MockTransport(garbage))
    client = HttpxTelegramClient("TOKEN123", http=http)

    assert await client.download_file("abc") is None
