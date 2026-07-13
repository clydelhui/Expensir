"""Frankfurter FX client (§7.5): the thin transport behind FxProvider (§0.8).

DISPLAY ONLY (ADR-0001) — called from read-path TTL refreshes and /setrate's
pre-lock fetch-and-pin, never from apply_intent or settlement math. Free, no
key, ECB daily, EUR-based; non-EUR pairs triangulate app-side (domain/fx.py).
"""

import logging
import time

import httpx

logger = logging.getLogger(__name__)


class FrankfurterClient:
    """The one FxProvider implementation: GET /v1/latest?base=EUR&symbols=..."""

    def __init__(
        self,
        base_url: str = "https://api.frankfurter.dev",
        http: httpx.AsyncClient | None = None,
        timeout: float = 10.0,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._http = http or httpx.AsyncClient(timeout=timeout)

    async def eur_rates(self, symbols: set[str]) -> dict[str, float] | None:
        """Today's EUR-based rates for `symbols` (§7.5). Unsupported symbols are
        simply absent from the answer; None = the API couldn't be reached."""
        started = time.monotonic()
        try:
            response = await self._http.get(
                f"{self._base}/v1/latest",
                params={"base": "EUR", "symbols": ",".join(sorted(symbols))},
            )
            if response.is_client_error:
                # one unrecognized symbol 404s the whole request: refetch the full
                # list and intersect, so the good symbols still resolve
                response = await self._http.get(f"{self._base}/v1/latest", params={"base": "EUR"})
            response.raise_for_status()
            rates = response.json().get("rates", {})
            resolved = {s: float(rates[s]) for s in symbols if s in rates}
            logger.info(
                "fx rates fetched %d/%d symbols %dms",
                len(resolved),
                len(symbols),
                (time.monotonic() - started) * 1000,
            )
            return resolved
        except (httpx.HTTPError, ValueError, TypeError, AttributeError):
            logger.warning("frankfurter fetch failed; cached rates stand", exc_info=True)
            return None
