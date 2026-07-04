"""getUpdates polling transport for local dev; feeds the same dispatch seam as webhook (§0.5)."""

from expensir.core.handler import Deps, dispatch
from expensir.telegram.client import PollingTelegramClient
from expensir.transports.executor import execute


async def poll_once(deps: Deps, client: PollingTelegramClient, offset: int) -> int:
    updates = await client.get_updates(offset=offset, timeout=30)
    for update in updates:
        actions = await dispatch(update, deps)
        await execute(actions, client, session_factory=deps.session_factory)
        offset = max(offset, update["update_id"] + 1)
    return offset


async def run_poll(deps: Deps, client: PollingTelegramClient) -> None:
    offset = 0
    while True:
        offset = await poll_once(deps, client, offset)
