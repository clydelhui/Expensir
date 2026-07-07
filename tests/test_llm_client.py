"""The OpenAI-compatible client (ADR-0010) against recorded fixtures (§16).

No live calls: each fixture is a raw chat-completion body exactly as a provider
returns it, replayed through the real HTTP + parse path via httpx.MockTransport.
Re-record against a live endpoint with scripts/record_llm_fixtures.py.
"""

import json
from pathlib import Path

import httpx
import pytest

from expensir.llm.base import LLMUnavailable
from expensir.llm.openai_compat import OpenAICompatLLM
from expensir.llm.wire import WireUnknown

FIXTURES = json.loads((Path(__file__).parent / "fixtures" / "llm" / "extractions.json").read_text())


def client_returning(
    *bodies: dict | Exception | httpx.Response,
) -> tuple[OpenAICompatLLM, list[dict]]:
    """An OpenAICompatLLM whose HTTP layer replays canned bodies, oldest first;
    also returns the captured request payloads for prompt assertions."""
    queue = list(bodies)
    requests: list[dict] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        body = queue.pop(0)
        if isinstance(body, Exception):
            raise body
        if isinstance(body, httpx.Response):
            return body
        return httpx.Response(200, json=body)

    http = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    llm = OpenAICompatLLM(
        base_url="https://provider.example/v1", api_key="k", model="test-model", http=http
    )
    return llm, requests


def completion_with(content: str) -> dict:
    return {"choices": [{"message": {"role": "assistant", "content": content}}]}


@pytest.mark.parametrize("case", FIXTURES, ids=[c["name"] for c in FIXTURES])
async def test_every_intent_kind_parses_from_a_recorded_response(case):
    llm, requests = client_returning(case["response"])

    wire = await llm.extract_text(case["utterance"])

    dumped = wire.model_dump()
    for key, value in case["expected"].items():
        assert dumped[key] == value, f"{case['name']}: {key}"
    # the utterance reached the model as the user message
    assert requests[0]["messages"][-1]["content"] == case["utterance"]
    assert requests[0]["model"] == "test-model"


async def test_invalid_output_is_retried_once_with_the_error_then_unknown():
    llm, requests = client_returning(
        completion_with("I think you paid for dinner?"),  # not JSON
        completion_with('{"kind": "add_expense"}'),  # JSON but missing required fields
    )

    wire = await llm.extract_text("I paid 40 for dinner")

    assert isinstance(wire, WireUnknown)
    assert len(requests) == 2
    # the retry shows the model its own reply and what was wrong with it
    retry_messages = requests[1]["messages"]
    assert retry_messages[-2]["role"] == "assistant"
    assert "I think you paid for dinner?" in retry_messages[-2]["content"]
    assert retry_messages[-1]["role"] == "user"


async def test_a_retry_that_heals_returns_the_intent():
    llm, requests = client_returning(
        completion_with("sorry, here you go:"),
        completion_with('{"kind": "show_balance", "scope": "me"}'),
    )

    wire = await llm.extract_text("what do I owe?")

    assert wire.kind == "show_balance"
    assert len(requests) == 2


async def test_a_transport_error_raises_llm_unavailable():
    llm, _ = client_returning(httpx.ConnectTimeout("boom"))

    with pytest.raises(LLMUnavailable):
        await llm.extract_text("I paid 40 for dinner")


async def test_an_http_error_status_raises_llm_unavailable():
    llm, _ = client_returning(httpx.Response(503, json={"error": "overloaded"}))

    with pytest.raises(LLMUnavailable):
        await llm.extract_text("hi")


async def test_the_system_prompt_defines_the_wire_contract():
    llm, requests = client_returning(completion_with('{"kind": "undo_redo"}'))

    await llm.extract_text("undo that")

    system = requests[0]["messages"][0]
    assert system["role"] == "system"
    for kind in (
        "add_expense",
        "settle_up",
        "show_balance",
        "delete_expense",
        "edit_expense",
        "new_ledger",
        "switch_ledger",
        "archive_ledger",
        "unarchive_ledger",
        "set_home_currency",
        "set_logging_currency",
        "setup",
        "unknown",
        "undo_redo",
    ):
        assert kind in system["content"], f"prompt must cover {kind} (§12)"
