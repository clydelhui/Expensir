"""FastAPI webhook transport: secret-header check, update_id dedupe, then dispatch (§6)."""

from fastapi import FastAPI, Request, Response
from sqlalchemy import delete
from sqlalchemy.exc import IntegrityError

from expensir.core.handler import Deps, dispatch
from expensir.db.models import ProcessedUpdate
from expensir.telegram.client import TelegramClient
from expensir.transports.executor import execute

SECRET_HEADER = "X-Telegram-Bot-Api-Secret-Token"


def create_app(deps: Deps, telegram: TelegramClient, webhook_secret: str) -> FastAPI:
    app = FastAPI()

    @app.post("/webhook")
    async def webhook(request: Request) -> Response:
        if request.headers.get(SECRET_HEADER) != webhook_secret:
            return Response(status_code=403)
        update = await request.json()
        update_id = update.get("update_id")
        if not await _first_time_seeing(deps, update_id):
            return Response(status_code=200)
        try:
            actions = await dispatch(update, deps)
        except Exception:
            # nothing committed (dispatch's transaction rolled back): release the
            # dedupe claim so Telegram's retry is processed, not swallowed (§13)
            await _release_claim(deps, update_id)
            raise
        # dispatch committed; a send failure must NOT release the claim or the
        # retry would run the mutation again and double-record money — the retry
        # no-ops against the kept claim and only the reply is lost
        await execute(actions, telegram, session_factory=deps.session_factory)
        return Response(status_code=200)

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
