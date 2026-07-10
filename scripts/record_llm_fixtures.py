"""Re-record the LLM fixtures under tests/fixtures/llm/ against a live endpoint (§16).

The checked-in fixtures are hand-authored in the exact chat-completion shape
(slice 12 grill); this script replaces each case's "response" with what the
REAL model returns, so the prompt is proven against the actual provider.
Covers extractions.json (text) and, when LLM_VISION_MODEL is set, vision.json —
each vision case names its image file, resolved relative to the fixture dir.
The checked-in sample-receipt.jpg is a placeholder: drop in real receipt photos
before recording, or the vision responses will be meaningless.

Usage:
    LLM_BASE_URL=... LLM_API_KEY=... LLM_MODEL=... [LLM_VISION_MODEL=...] \\
        uv run python scripts/record_llm_fixtures.py

Writes <name>.recorded.json next to each original — review the diff (does
every "expected" still hold when the recorded file replaces the original?),
run pytest against it, then move it into place.
"""

import asyncio
import json
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).parent.parent))

from expensir.llm.openai_compat import _data_url  # noqa: E402
from expensir.llm.prompts import extraction_messages, vision_extraction_messages  # noqa: E402

FIXTURE_DIR = Path(__file__).parent.parent / "tests" / "fixtures" / "llm"


async def record(
    path: Path,
    model: str,
    http: httpx.AsyncClient,
    messages_of: Callable[[dict[str, Any]], list[dict[str, Any]]],
) -> None:
    cases = json.loads(path.read_text())
    base_url = os.environ["LLM_BASE_URL"].rstrip("/")
    for case in cases:
        response = await http.post(
            f"{base_url}/chat/completions",
            headers={"Authorization": f"Bearer {os.environ['LLM_API_KEY']}"},
            json={"model": model, "messages": messages_of(case), "temperature": 0},
        )
        response.raise_for_status()
        case["response"] = response.json()
        content = case["response"]["choices"][0]["message"]["content"]
        print(f"{case['name']}: {content[:100]}")
    out = path.with_suffix(".recorded.json")
    out.write_text(json.dumps(cases, indent=2, ensure_ascii=False) + "\n")
    print(f"\nWrote {out} — review the diff against {path.name}, then move into place.\n")


def vision_messages(case: dict[str, Any]) -> list[dict[str, Any]]:
    # the client's own encoder, so recordings can't drift from real requests
    image = (FIXTURE_DIR / case["image"]).read_bytes()
    return vision_extraction_messages(_data_url(image), case["caption"])


async def main() -> None:
    if not all(os.environ.get(k) for k in ("LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL")):
        sys.exit("Set LLM_BASE_URL, LLM_API_KEY and LLM_MODEL (ADR-0010).")

    async with httpx.AsyncClient(timeout=60.0) as http:
        await record(
            FIXTURE_DIR / "extractions.json",
            os.environ["LLM_MODEL"],
            http,
            lambda case: extraction_messages(case["utterance"]),
        )
        vision_model = os.environ.get("LLM_VISION_MODEL")
        if vision_model:
            await record(FIXTURE_DIR / "vision.json", vision_model, http, vision_messages)
        else:
            print("LLM_VISION_MODEL unset — skipped vision.json (issue #15).")


if __name__ == "__main__":
    asyncio.run(main())
