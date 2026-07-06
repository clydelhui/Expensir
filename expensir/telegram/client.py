"""Thin httpx wrapper over the Telegram Bot API, behind a protocol so tests fake it (§0.8)."""

from typing import Any, Protocol, cast

import httpx

JsonDict = dict[str, Any]


class TelegramClient(Protocol):
    async def send_message(
        self, chat_id: int, text: str, reply_markup: JsonDict | None = None
    ) -> JsonDict: ...

    async def edit_message_text(
        self, chat_id: int, message_id: int, text: str, reply_markup: JsonDict | None = None
    ) -> JsonDict: ...

    async def edit_message_reply_markup(
        self, chat_id: int, message_id: int, reply_markup: JsonDict
    ) -> JsonDict: ...

    async def answer_callback_query(
        self, callback_query_id: str, text: str | None = None
    ) -> JsonDict: ...

    async def pin_chat_message(self, chat_id: int, message_id: int) -> Any: ...


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

    async def send_message(
        self, chat_id: int, text: str, reply_markup: JsonDict | None = None
    ) -> JsonDict:
        payload: JsonDict = {"chat_id": chat_id, "text": text}
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        return cast(JsonDict, await self._call("sendMessage", payload))

    async def edit_message_text(
        self, chat_id: int, message_id: int, text: str, reply_markup: JsonDict | None = None
    ) -> JsonDict:
        payload: JsonDict = {"chat_id": chat_id, "message_id": message_id, "text": text}
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        return cast(JsonDict, await self._call("editMessageText", payload))

    async def edit_message_reply_markup(
        self, chat_id: int, message_id: int, reply_markup: JsonDict
    ) -> JsonDict:
        payload: JsonDict = {
            "chat_id": chat_id,
            "message_id": message_id,
            "reply_markup": reply_markup,
        }
        return cast(JsonDict, await self._call("editMessageReplyMarkup", payload))

    async def answer_callback_query(
        self, callback_query_id: str, text: str | None = None
    ) -> JsonDict:
        payload: JsonDict = {"callback_query_id": callback_query_id}
        if text is not None:
            payload["text"] = text
        return cast(JsonDict, await self._call("answerCallbackQuery", payload))

    async def pin_chat_message(self, chat_id: int, message_id: int) -> Any:
        # the pin service message is noise next to the board itself: keep it silent
        payload: JsonDict = {
            "chat_id": chat_id,
            "message_id": message_id,
            "disable_notification": True,
        }
        return await self._call("pinChatMessage", payload)

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
