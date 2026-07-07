"""Test doubles for the protocol seams (§16): no network, no live calls."""

from expensir.llm.wire import WireResult


class FakeLLM:
    """LLMClient double: canned wire results, records what it was asked."""

    def __init__(self, results: list[WireResult]) -> None:
        self.results = list(results)
        self.seen: list[str] = []

    async def extract_text(self, text: str) -> WireResult:
        self.seen.append(text)
        return self.results.pop(0)
