"""Test doubles for the protocol seams (§16): no network, no live calls."""

from typing import Any

from expensir.llm.base import LLMUnavailable
from expensir.llm.wire import WireResult


class UnavailableLLM:
    """LLMClient double for a transport outage: every call raises."""

    async def extract_text(self, text: str) -> WireResult:
        raise LLMUnavailable("connect timeout")

    async def refine(
        self,
        prior_intent: dict[str, Any],
        correction: str,
        candidates: list[str] | None = None,
    ) -> WireResult:
        raise LLMUnavailable("connect timeout")


class FakeLLM:
    """LLMClient double: canned wire results, records what it was asked."""

    def __init__(
        self, results: list[WireResult], refinements: list[WireResult] | None = None
    ) -> None:
        self.results = list(results)
        self.refinements = list(refinements or [])
        self.seen: list[str] = []
        self.refined: list[tuple[dict[str, Any], str, list[str] | None]] = []

    async def extract_text(self, text: str) -> WireResult:
        self.seen.append(text)
        return self.results.pop(0)

    async def refine(
        self,
        prior_intent: dict[str, Any],
        correction: str,
        candidates: list[str] | None = None,
    ) -> WireResult:
        self.refined.append((prior_intent, correction, candidates))
        return self.refinements.pop(0)
