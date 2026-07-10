"""LLMClient protocol (§2): the seam tests fake and providers implement (ADR-0010)."""

from typing import Any, Protocol

from expensir.llm.wire import WireResult


class LLMUnavailable(Exception):
    """The model endpoint could not be reached — distinct from 'couldn't parse'
    so the user isn't told to rephrase a sentence that was never read."""


class LLMClient(Protocol):
    async def extract_text(self, text: str) -> WireResult:
        """Parse one bot-addressed message into a wire result.

        Raises LLMUnavailable on transport failure; unmappable text comes back
        as the wire 'unknown' kind, never an exception."""
        ...

    async def refine(
        self,
        prior_intent: dict[str, Any],
        correction: str,
        candidates: list[str] | None = None,
    ) -> WireResult:
        """Apply one correction reply to a proposed intent (§10.2, issue #14).

        prior_intent is the parked intent's JSON; candidates are the open
        pick-list slot's choices, when the proposal is awaiting one. Same
        failure contract as extract_text."""
        ...
