"""Re-record tests/fixtures/llm/extractions.json against a live endpoint (§16).

The checked-in fixtures are hand-authored in the exact chat-completion shape
(slice 12 grill); this script replaces each case's "response" with what the
REAL model returns, so the prompt is proven against the actual provider.

Usage:
    LLM_BASE_URL=... LLM_API_KEY=... LLM_MODEL=... uv run python scripts/record_llm_fixtures.py

Writes extractions.recorded.json next to the original — review the diff (does
every "expected" still hold when the recorded file replaces the original?),
run pytest against it, then move it into place.
"""

import asyncio
import json
import os
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).parent.parent))

from expensir.llm.prompts import extraction_messages  # noqa: E402

FIXTURES = Path(__file__).parent.parent / "tests" / "fixtures" / "llm" / "extractions.json"


async def main() -> None:
    base_url = os.environ.get("LLM_BASE_URL", "").rstrip("/")
    api_key = os.environ.get("LLM_API_KEY", "")
    model = os.environ.get("LLM_MODEL", "")
    if not (base_url and api_key and model):
        sys.exit("Set LLM_BASE_URL, LLM_API_KEY and LLM_MODEL (ADR-0010).")

    cases = json.loads(FIXTURES.read_text())
    async with httpx.AsyncClient(timeout=60.0) as http:
        for case in cases:
            response = await http.post(
                f"{base_url}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                json={
                    "model": model,
                    "messages": extraction_messages(case["utterance"]),
                    "temperature": 0,
                },
            )
            response.raise_for_status()
            case["response"] = response.json()
            content = case["response"]["choices"][0]["message"]["content"]
            print(f"{case['name']}: {content[:100]}")

    out = FIXTURES.with_suffix(".recorded.json")
    out.write_text(json.dumps(cases, indent=2, ensure_ascii=False) + "\n")
    print(f"\nWrote {out} — review the diff against {FIXTURES.name}, then move into place.")


if __name__ == "__main__":
    asyncio.run(main())
