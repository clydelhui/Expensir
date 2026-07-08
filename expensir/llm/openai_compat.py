"""One LLM client for every OpenAI-compatible provider (ADR-0010).

Cloudflare Workers AI, OpenRouter, DigitalOcean, and Groq all speak the same
chat-completions dialect: only LLM_BASE_URL / LLM_API_KEY / LLM_MODEL differ.
"""

import json
import re

import httpx
from pydantic import TypeAdapter, ValidationError

from expensir.llm.base import LLMUnavailable
from expensir.llm.prompts import extraction_messages, retry_messages
from expensir.llm.wire import WireResult, WireUnknown

_WIRE: TypeAdapter[WireResult] = TypeAdapter(WireResult)


class OpenAICompatLLM:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        timeout: float = 30.0,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model
        self._http = http if http is not None else httpx.AsyncClient(timeout=timeout)

    async def extract_text(self, text: str) -> WireResult:
        """One parse attempt + one retry showing the validation error (issue #13
        grill); still invalid -> wire unknown. Transport trouble -> LLMUnavailable."""
        messages = extraction_messages(text)
        content = await self._chat(messages)
        try:
            return _parse(content)
        except (json.JSONDecodeError, ValidationError) as exc:
            messages = retry_messages(messages, bad_reply=content, error=str(exc))
        content = await self._chat(messages)
        try:
            return _parse(content)
        except (json.JSONDecodeError, ValidationError):
            return WireUnknown(reason="the model's output failed validation twice")

    async def _chat(self, messages: list[dict[str, str]]) -> str:
        try:
            response = await self._http.post(
                f"{self._base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self._api_key}"},
                json={"model": self._model, "messages": messages, "temperature": 0},
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            # includes non-2xx statuses: the sentence never reached a model, so
            # the caller must not answer with "rephrase that" (§12)
            raise LLMUnavailable(str(exc)) from exc
        return str(response.json()["choices"][0]["message"]["content"])


def _parse(content: str) -> WireResult:
    return _WIRE.validate_python(json.loads(_strip_fences(content)))


def _strip_fences(content: str) -> str:
    """Models fence JSON in markdown no matter how firmly told not to."""
    text = content.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    return text
