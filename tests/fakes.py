"""Test doubles for the protocol seams (§16): no network, no live calls."""

from typing import Any

from expensir.llm.base import LLMUnavailable
from expensir.llm.wire import WireResult


class UnavailableLLM:
    """LLMClient double for a transport outage: every call raises."""

    supports_vision = True

    async def extract_text(self, text: str) -> WireResult:
        raise LLMUnavailable("connect timeout")

    async def extract_vision(self, image: bytes, caption: str) -> WireResult:
        raise LLMUnavailable("connect timeout")

    async def refine(
        self,
        prior_intent: dict[str, Any],
        correction: str,
        candidates: list[str] | None = None,
        image: bytes | None = None,
    ) -> WireResult:
        raise LLMUnavailable("connect timeout")


class FakeLLM:
    """LLMClient double: canned wire results, records what it was asked."""

    supports_vision = True

    def __init__(
        self,
        results: list[WireResult],
        refinements: list[WireResult] | None = None,
        visions: list[WireResult] | None = None,
    ) -> None:
        self.results = list(results)
        self.refinements = list(refinements or [])
        self.visions = list(visions or [])
        self.seen: list[str] = []
        self.seen_images: list[tuple[bytes, str]] = []
        self.refined: list[tuple[dict[str, Any], str, list[str] | None]] = []
        self.refine_images: list[bytes | None] = []

    async def extract_text(self, text: str) -> WireResult:
        self.seen.append(text)
        return self.results.pop(0)

    async def extract_vision(self, image: bytes, caption: str) -> WireResult:
        self.seen_images.append((image, caption))
        return self.visions.pop(0)

    async def refine(
        self,
        prior_intent: dict[str, Any],
        correction: str,
        candidates: list[str] | None = None,
        image: bytes | None = None,
    ) -> WireResult:
        self.refined.append((prior_intent, correction, candidates))
        self.refine_images.append(image)
        return self.refinements.pop(0)


class FakeFiles:
    """FileSource double: canned bytes for any file_id, records what was fetched."""

    def __init__(self, content: bytes | None = b"jpeg-bytes") -> None:
        self.content = content
        self.requested: list[str] = []

    async def download_file(self, file_id: str) -> bytes | None:
        self.requested.append(file_id)
        return self.content
