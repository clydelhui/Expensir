"""LLMClient protocol (§2): the seam tests fake and providers implement (ADR-0010)."""

from typing import Protocol

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
