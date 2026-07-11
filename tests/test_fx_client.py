"""FrankfurterClient (§7.5): canned bodies through the real HTTP + parse path.

No live calls — like the LLM client tests, responses replay via MockTransport.
"""

import httpx

from expensir.fx.frankfurter import FrankfurterClient


def client_returning(
    *bodies: dict | Exception | httpx.Response,
) -> tuple[FrankfurterClient, list[httpx.Request]]:
    queue = list(bodies)
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        body = queue.pop(0)
        if isinstance(body, Exception):
            raise body
        if isinstance(body, httpx.Response):
            return body
        return httpx.Response(200, json=body)

    http = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    return FrankfurterClient(base_url="https://fx.example", http=http), requests


async def test_fetches_eur_based_rates_for_the_requested_symbols():
    client, requests = client_returning(
        {"base": "EUR", "date": "2026-07-10", "rates": {"USD": 1.08, "SGD": 1.46}}
    )

    rates = await client.eur_rates({"USD", "SGD"})

    assert rates == {"USD": 1.08, "SGD": 1.46}
    assert requests[0].url.params["base"] == "EUR"
    assert requests[0].url.params["symbols"] == "SGD,USD"  # sorted, deterministic


async def test_an_unsupported_symbol_falls_back_to_the_full_list_and_intersects():
    # Frankfurter 404s the WHOLE request over one bad symbol: retry unfiltered so
    # the good symbols still resolve — the bad one is absent, not an outage (§7.5)
    client, requests = client_returning(
        httpx.Response(404, json={"message": "not found"}),
        {"base": "EUR", "date": "2026-07-10", "rates": {"USD": 1.08, "SGD": 1.46}},
    )

    rates = await client.eur_rates({"USD", "XXY"})

    assert rates == {"USD": 1.08}
    assert "symbols" not in requests[1].url.params


async def test_transport_failure_returns_none():
    client, _ = client_returning(httpx.ConnectError("boom"))

    assert await client.eur_rates({"USD"}) is None


async def test_a_garbage_body_returns_none():
    client, _ = client_returning(httpx.Response(200, text="<html>surprise</html>"))

    assert await client.eur_rates({"USD"}) is None
