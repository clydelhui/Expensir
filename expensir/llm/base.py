"""LLMClient protocol (§2): the seam tests fake and providers implement (ADR-0010)."""

from typing import Any, Protocol

from expensir.llm.wire import WireResult


class LLMUnavailable(Exception):
    """The model endpoint could not be reached — distinct from 'couldn't parse'
    so the user isn't told to rephrase a sentence that was never read."""


class LLMClient(Protocol):
    @property
    def supports_vision(self) -> bool:
        """Whether the receipt-photo door is open (issue #15): False leaves
        photos unanswered, exactly like text mentions with no LLM at all."""
        ...

    async def extract_text(self, text: str) -> WireResult:
        """Parse one bot-addressed message into a wire result.

        Raises LLMUnavailable on transport failure; unmappable text comes back
        as the wire 'unknown' kind, never an exception."""
        ...

    async def extract_vision(self, image: bytes, caption: str) -> WireResult:
        """Parse one receipt photo (+ mention-stripped caption) into a wire
        result — only ever add_expense, settle_up, or unknown (issue #15 grill).

        Same failure contract as extract_text."""
        ...

    async def refine(
        self,
        prior_intent: dict[str, Any],
        correction: str,
        candidates: list[str] | None = None,
        image: bytes | None = None,
    ) -> WireResult:
        """Apply one correction reply to a proposed intent (§10.2, issue #14).

        prior_intent is the parked intent's JSON; candidates are the open
        pick-list slot's choices, when the proposal is awaiting one; image is
        a receipt photo sent as the correction (issue #15) — it merges into
        the prior intent, never restarts it. Same failure contract as
        extract_text."""
        ...
