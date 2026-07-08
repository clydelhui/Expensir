"""getUpdates polling transport for local dev; feeds the same dispatch seam as webhook (§0.5)."""

import logging

from expensir.core.handler import Deps, dispatch
from expensir.telegram.client import PollingTelegramClient
from expensir.transports.executor import execute

logger = logging.getLogger(__name__)


async def poll_once(deps: Deps, client: PollingTelegramClient, offset: int) -> int:
    updates = await client.get_updates(offset=offset, timeout=30)
    for update in updates:
        update_id = update.get("update_id")
        if update_id is None:
            # every getUpdates result carries one; guard so a malformed update can't
            # KeyError past the error boundary and take the loop down
            logger.warning("skipping update with no update_id: %r", update)
            continue
        try:
            actions = await dispatch(update, deps)
            await execute(actions, client, session_factory=deps.session_factory)
        except Exception:
            # advancing the offset on ANY failure is deliberate: poll has no
            # update_id dedupe (that lives in the webhook transport), so NOT
            # advancing would let a redelivery re-run a committed dispatch and
            # double-record money. On failure we sacrifice the reply and move on,
            # never reprocess — the poll mirror of the webhook's kept-claim
            # tradeoff (§0.2). A deterministic handler bug is likewise dropped
            # here rather than blocking every later update behind it.
            logger.exception("dropping update %s after an error", update_id)
        offset = max(offset, update_id + 1)
    return offset


async def run_poll(deps: Deps, client: PollingTelegramClient) -> None:
    offset = 0
    while True:
        offset = await poll_once(deps, client, offset)
