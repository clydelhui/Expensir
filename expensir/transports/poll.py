"""getUpdates polling transport for local dev; feeds the same dispatch seam as webhook (§0.5)."""

import logging

from expensir.core.handler import Deps, dispatch
from expensir.telegram.client import PollingTelegramClient
from expensir.transports.executor import execute

logger = logging.getLogger(__name__)


async def poll_once(deps: Deps, client: PollingTelegramClient, offset: int) -> int:
    updates = await client.get_updates(offset=offset, timeout=30)
    for update in updates:
        try:
            actions = await dispatch(update, deps)
            await execute(actions, client, session_factory=deps.session_factory)
        except Exception:
            # one bad update must not kill the whole loop; log the traceback and
            # still advance the offset so Telegram doesn't redeliver it forever
            logger.exception("dropping update %s after a handler error", update.get("update_id"))
        offset = max(offset, update["update_id"] + 1)
    return offset


async def run_poll(deps: Deps, client: PollingTelegramClient) -> None:
    offset = 0
    while True:
        offset = await poll_once(deps, client, offset)
