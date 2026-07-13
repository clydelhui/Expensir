"""FastAPI webhook transport: secret-header check, update_id dedupe, then dispatch (§6)."""

import logging
import time

from fastapi import FastAPI, Request, Response
from sqlalchemy import delete
from sqlalchemy.exc import IntegrityError

from expensir.core.handler import Deps, dispatch
from expensir.db.models import ProcessedUpdate
from expensir.logsetup import current_update_id, update_log_fields
from expensir.telegram.client import TelegramClient
from expensir.transports.executor import execute

logger = logging.getLogger(__name__)

SECRET_HEADER = "X-Telegram-Bot-Api-Secret-Token"


def create_app(deps: Deps, telegram: TelegramClient, webhook_secret: str) -> FastAPI:
    app = FastAPI()

    @app.post("/webhook")
    async def webhook(request: Request) -> Response:
        if request.headers.get(SECRET_HEADER) != webhook_secret:
            # a forged or misconfigured caller: with access_log off, this line is
            # the only trace of it
            logger.warning("rejected: bad or missing secret header")
            return Response(status_code=403)
        update = await request.json()
        update_id = update.get("update_id")
        if not await _first_time_seeing(deps, update_id):
            # visible on purpose: a burst of these is a retry storm
            logger.info("duplicate update %s ignored", update_id)
            return Response(status_code=200)
        token = current_update_id.set(update_id)
        started = time.monotonic()
        try:
            logger.info("received %s", update_log_fields(update))
            logger.debug("update payload: %r", update)
            try:
                actions = await dispatch(update, deps)
            except Exception:
                # traceback logged here, while the update tag is still set —
                # letting the exception escape would log it untagged (uvicorn
                # prints it after the finally resets the contextvar)
                logger.exception(
                    "done outcome=error %dms — 500 to Telegram, retry expected",
                    (time.monotonic() - started) * 1000,
                )
                # nothing committed (dispatch's transaction rolled back): release the
                # dedupe claim so Telegram's retry is processed, not swallowed (§13)
                try:
                    await _release_claim(deps, update_id)
                except Exception:
                    logger.exception(
                        "claim release failed — the retry will be deduped, update lost"
                    )
                return Response(status_code=500)
            # dispatch committed; a send failure must NOT release the claim or the
            # retry would run the mutation again and double-record money — the retry
            # no-ops against the kept claim and only the reply is lost
            try:
                await execute(actions, telegram, session_factory=deps.session_factory)
            except Exception:
                logger.exception(
                    "done outcome=error %dms — effects failed after commit; "
                    "claim kept, the retry no-ops and the reply is lost",
                    (time.monotonic() - started) * 1000,
                )
                return Response(status_code=500)
            logger.info("done outcome=ok %dms", (time.monotonic() - started) * 1000)
            return Response(status_code=200)
        finally:
            current_update_id.reset(token)

    return app


async def _first_time_seeing(deps: Deps, update_id: int | None) -> bool:
    if update_id is None:
        return True
    try:
        async with deps.session_factory() as session, session.begin():
            session.add(ProcessedUpdate(update_id=update_id))
    except IntegrityError:
        return False
    return True


async def _release_claim(deps: Deps, update_id: int | None) -> None:
    if update_id is None:
        return
    async with deps.session_factory() as session, session.begin():
        await session.execute(delete(ProcessedUpdate).where(ProcessedUpdate.update_id == update_id))
