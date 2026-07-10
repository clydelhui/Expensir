"""The OpenAI-compatible client (ADR-0010) against recorded fixtures (§16).

No live calls: each fixture is a raw chat-completion body exactly as a provider
returns it, replayed through the real HTTP + parse path via httpx.MockTransport.
Re-record against a live endpoint with scripts/record_llm_fixtures.py.
"""

import base64
import json
from pathlib import Path

import httpx
import pytest

from expensir.llm.base import LLMUnavailable
from expensir.llm.openai_compat import OpenAICompatLLM
from expensir.llm.wire import WireUnknown

FIXTURES = json.loads((Path(__file__).parent / "fixtures" / "llm" / "extractions.json").read_text())
VISION_FIXTURES = json.loads(
    (Path(__file__).parent / "fixtures" / "llm" / "vision.json").read_text()
)


def client_returning(
    *bodies: dict | Exception | httpx.Response,
    vision_model: str | None = "test-vision-model",
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
        base_url="https://provider.example/v1",
        api_key="k",
        model="test-model",
        vision_model=vision_model,
        http=http,
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


RECEIPT_JSON = (
    '{"kind":"add_expense","payer_ref":"me","amount":"34.50","currency":"JPY",'
    '"description":"Ichiran Ramen","occurred_on":null,"split_type":"equal",'
    '"participants":[],"confidence":0.8}'
)


@pytest.mark.parametrize("case", VISION_FIXTURES, ids=[c["name"] for c in VISION_FIXTURES])
async def test_every_vision_case_parses_from_a_recorded_response(case):
    """Issue #15 acceptance: recorded fixtures behind the protocol, no live
    calls — re-record with scripts/record_llm_fixtures.py against a real photo."""
    llm, requests = client_returning(case["response"])

    wire = await llm.extract_vision(b"fixture-image-bytes", case["caption"])

    dumped = wire.model_dump()
    for key, value in case["expected"].items():
        assert dumped[key] == value, f"{case['name']}: {key}"
    assert requests[0]["model"] == "test-vision-model"


async def test_extract_vision_sends_the_image_to_the_vision_model():
    """Issue #15: the SAME client (ADR-0010) hits the separate vision model id
    with the photo as a base64 data URL — never Telegram's tokenized file URL."""
    llm, requests = client_returning(completion_with(RECEIPT_JSON))

    wire = await llm.extract_vision(b"jpeg-bytes", "dinner, split with Sam")

    assert wire.kind == "add_expense" and wire.amount == "34.50"
    assert requests[0]["model"] == "test-vision-model"
    parts = requests[0]["messages"][-1]["content"]
    (image_part,) = [p for p in parts if p["type"] == "image_url"]
    expected_b64 = base64.b64encode(b"jpeg-bytes").decode()
    assert image_part["image_url"]["url"] == f"data:image/jpeg;base64,{expected_b64}"
    (text_part,) = [p for p in parts if p["type"] == "text"]
    assert "dinner, split with Sam" in text_part["text"]  # the caption steers


async def test_the_vision_prompt_narrows_kinds_and_forbids_currency_guessing():
    """Issue #15 grill: photos only ever mean money moving, and a currency is
    emitted only when the receipt is unambiguous — bare $ resolves app-side."""
    llm, requests = client_returning(completion_with(RECEIPT_JSON))

    await llm.extract_vision(b"jpeg-bytes", "")

    system = requests[0]["messages"][0]
    assert system["role"] == "system"
    for phrase in ("add_expense", "settle_up", "unknown"):
        assert phrase in system["content"]
    assert "ONLY" in system["content"]  # the kind restriction is stated
    assert "$" in system["content"]  # the ambiguous-symbol rule is taught


async def test_supports_vision_follows_the_configured_model():
    with_vision, _ = client_returning()
    without, _ = client_returning(vision_model=None)

    assert with_vision.supports_vision is True
    assert without.supports_vision is False


async def test_a_vision_refine_carries_the_image_and_the_prior_intent():
    """A photo correction (issue #15 grill): merge semantics — the model sees
    the parked intent AND the receipt in one request."""
    llm, requests = client_returning(completion_with(RECEIPT_JSON))

    await llm.refine({"kind": "add_expense", "amount_minor": 3000}, "", image=b"jpeg-bytes")

    parts = requests[0]["messages"][-1]["content"]
    (image_part,) = [p for p in parts if p["type"] == "image_url"]
    assert image_part["image_url"]["url"].startswith("data:image/jpeg;base64,")
    (text_part,) = [p for p in parts if p["type"] == "text"]
    assert '"amount_minor": 3000' in text_part["text"] or '"amount_minor":3000' in text_part["text"]
    assert requests[0]["model"] == "test-vision-model"  # an image needs the vision model


async def test_a_text_refine_still_uses_the_text_model():
    llm, requests = client_returning(completion_with('{"kind": "show_balance", "scope": "me"}'))

    await llm.refine({"kind": "add_expense"}, "actually 50")

    assert requests[0]["model"] == "test-model"
    assert isinstance(requests[0]["messages"][-1]["content"], str)  # no parts, plain text


async def test_the_prompt_teaches_the_descriptive_match_field():
    """Issue #14 scope addition: "delete the dinner one" must extract a match
    query for the app to resolve CPU-side — never come back unknown."""
    llm, requests = client_returning(completion_with('{"kind": "undo_redo"}'))

    await llm.extract_text("undo that")

    system = requests[0]["messages"][0]["content"]
    assert '"match"' in system  # the field, in both mutation schemas
    assert "delete the dinner one" in system  # a few-shot showing the extraction
