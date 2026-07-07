"""Entry point: MODE=webhook serves FastAPI; MODE=poll runs the getUpdates loop (§14)."""

import asyncio
import os

import uvicorn

from expensir.config import Settings
from expensir.core.handler import Deps
from expensir.db.session import make_session_factory
from expensir.llm.base import LLMClient
from expensir.llm.openai_compat import OpenAICompatLLM
from expensir.telegram.client import HttpxTelegramClient
from expensir.transports.poll import run_poll
from expensir.transports.webhook import create_app


def _make_llm(settings: Settings) -> LLMClient | None:
    """One OpenAI-compatible client, provider chosen by base URL (ADR-0010).
    Unconfigured -> NL stays off; slash commands are unaffected."""
    if not (settings.llm_base_url and settings.llm_api_key and settings.llm_model):
        return None
    return OpenAICompatLLM(
        base_url=settings.llm_base_url, api_key=settings.llm_api_key, model=settings.llm_model
    )


def main() -> None:
    settings = Settings()  # type: ignore[call-arg]  # fields come from env/.env at runtime
    telegram = HttpxTelegramClient(settings.bot_token, api_base=settings.telegram_api_base)
    bot_username = asyncio.run(telegram.get_me()).get("username")
    deps = Deps(
        session_factory=make_session_factory(settings.database_url),
        bot_username=bot_username,
        operator_user_id=settings.operator_user_id,
        undo_window_hours=settings.undo_window_hours,
        client=telegram,  # board creation sends inside the locked transaction (ADR-0003)
        llm=_make_llm(settings),
        pending_ttl_minutes=settings.pending_ttl_minutes,
    )

    if settings.mode == "poll":
        asyncio.run(run_poll(deps, telegram))
    else:
        app = create_app(deps, telegram, settings.telegram_webhook_secret)
        uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))


if __name__ == "__main__":
    main()
