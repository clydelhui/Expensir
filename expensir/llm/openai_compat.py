"""One LLM client for every OpenAI-compatible provider (ADR-0010).

Cloudflare Workers AI, OpenRouter, DigitalOcean, and Groq all speak the same
chat-completions dialect: only LLM_BASE_URL / LLM_API_KEY / LLM_MODEL differ.
Vision (issue #15) rides the same dialect against a separate LLM_VISION_MODEL
id; the photo travels as a base64 data URL, never a Telegram file URL.
"""

import base64
import json
import re
from typing import Any

import httpx
from pydantic import TypeAdapter, ValidationError

from expensir.llm.base import LLMUnavailable
from expensir.llm.prompts import (
    extraction_messages,
    refine_messages,
    retry_messages,
    vision_extraction_messages,
)
from expensir.llm.wire import WireResult, WireUnknown

_WIRE: TypeAdapter[WireResult] = TypeAdapter(WireResult)


class OpenAICompatLLM:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        vision_model: str | None = None,
        timeout: float = 30.0,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model
        self._vision_model = vision_model
        self._http = http if http is not None else httpx.AsyncClient(timeout=timeout)

    @property
    def supports_vision(self) -> bool:
        return self._vision_model is not None

    async def extract_text(self, text: str) -> WireResult:
        return await self._complete_wire(extraction_messages(text), model=self._model)

    async def extract_vision(self, image: bytes, caption: str) -> WireResult:
        assert self._vision_model is not None  # the handler gates on supports_vision
        return await self._complete_wire(
            vision_extraction_messages(_data_url(image), caption), model=self._vision_model
        )

    async def refine(
        self,
        prior_intent: dict[str, Any],
        correction: str,
        candidates: list[str] | None = None,
        image: bytes | None = None,
    ) -> WireResult:
        if image is not None:
            # a photo correction (issue #15) needs eyes: the vision model
            assert self._vision_model is not None
            messages = refine_messages(
                prior_intent, correction, candidates, image_data_url=_data_url(image)
            )
            return await self._complete_wire(messages, model=self._vision_model)
        return await self._complete_wire(
            refine_messages(prior_intent, correction, candidates), model=self._model
        )

    async def _complete_wire(self, messages: list[dict[str, Any]], *, model: str) -> WireResult:
        """One parse attempt + one retry showing the validation error (issue #13
        grill); still invalid -> wire unknown. Transport trouble -> LLMUnavailable."""
        content = await self._chat(messages, model)
        try:
            return _parse(content)
        except (json.JSONDecodeError, ValidationError) as exc:
            messages = retry_messages(messages, bad_reply=content, error=str(exc))
        content = await self._chat(messages, model)
        try:
            return _parse(content)
        except (json.JSONDecodeError, ValidationError):
            return WireUnknown(reason="the model's output failed validation twice")

    async def _chat(self, messages: list[dict[str, Any]], model: str) -> str:
        try:
            response = await self._http.post(
                f"{self._base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self._api_key}"},
                json={"model": model, "messages": messages, "temperature": 0},
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            # includes non-2xx statuses: the sentence never reached a model, so
            # the caller must not answer with "rephrase that" (§12)
            raise LLMUnavailable(str(exc)) from exc
        return str(response.json()["choices"][0]["message"]["content"])


def _data_url(image: bytes) -> str:
    # Telegram photos are always JPEG re-encodes (§13)
    return f"data:image/jpeg;base64,{base64.b64encode(image).decode()}"


def _parse(content: str) -> WireResult:
    return _WIRE.validate_python(json.loads(_strip_fences(content)))


def _strip_fences(content: str) -> str:
    """Models fence JSON in markdown no matter how firmly told not to."""
    text = content.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    return text
