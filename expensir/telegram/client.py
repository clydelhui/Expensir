"""Thin httpx wrapper over the Telegram Bot API, behind a protocol so tests fake it (§0.8)."""

from typing import Any, Protocol, cast

import httpx

JsonDict = dict[str, Any]


class TelegramClient(Protocol):
    async def send_message(self, chat_id: int, text: str) -> JsonDict: ...


class PollingTelegramClient(TelegramClient, Protocol):
    async def get_updates(self, offset: int, timeout: int) -> list[JsonDict]: ...


class HttpxTelegramClient:
    def __init__(
        self,
        bot_token: str,
        http: httpx.AsyncClient | None = None,
        api_base: str = "https://api.telegram.org",
    ):
        self._base = f"{api_base}/bot{bot_token}"
        self._http = http or httpx.AsyncClient(timeout=30)

    async def send_message(self, chat_id: int, text: str) -> JsonDict:
        return cast(JsonDict, await self._call("sendMessage", {"chat_id": chat_id, "text": text}))

    async def get_me(self) -> JsonDict:
        return cast(JsonDict, await self._call("getMe", {}))

    async def get_updates(self, offset: int, timeout: int) -> list[JsonDict]:
        # the HTTP read timeout must outlast Telegram's long-poll hold
        result = await self._call(
            "getUpdates", {"offset": offset, "timeout": timeout}, http_timeout=timeout + 10
        )
        return cast(list[JsonDict], result)

    async def _call(self, method: str, payload: JsonDict, http_timeout: float | None = None) -> Any:
        response = await self._http.post(
            f"{self._base}/{method}",
            json=payload,
            timeout=http_timeout if http_timeout is not None else httpx.USE_CLIENT_DEFAULT,
        )
        response.raise_for_status()
        body = response.json()
        if not body.get("ok"):
            raise RuntimeError(f"Telegram {method} failed: {body}")
        return body["result"]
