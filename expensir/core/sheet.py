"""The settle sheet (§7.3, ADR-0007): a stateless projection of the suggested
transfers between one UNORDERED pair, each line its own WYSIWYG [Settle] button.

Rendering is a read — no lock, no action row. The buttons carry the same
amount token as the board (ADR-0006), so a sheet is never trusted at tap time;
callback handling recomputes under the lock.
"""

from sqlalchemy.ext.asyncio import AsyncSession

from expensir.core.board import suggested_transfers
from expensir.db.models import Ledger, User
from expensir.format.keyboards import InlineKeyboard, sheet_keyboard
from expensir.format.render import settle_sheet_reply


async def sheet_view(
    session: AsyncSession,
    ledger: Ledger,
    a: User,
    b: User,
    net: dict[int, dict[str, int]] | None = None,
) -> tuple[str, InlineKeyboard | None]:
    """Render the pair's sheet: solver-suggested transfers only, both directions,
    one line per currency. No transfers -> "Nothing to settle" (ADR-0007: no
    reverse credit invented from pairwise history)."""
    pair = {a.id, b.id}
    transfers = [
        t
        for t in await suggested_transfers(session, ledger.id, net=net)
        if {t.from_id, t.to_id} == pair
    ]
    # stable pair order regardless of who asked: the sheet is identical both ways
    first, second = sorted((a, b), key=lambda u: u.id)
    text = settle_sheet_reply(
        ledger_name=ledger.name,
        pair_names=(first.display_name, second.display_name),
        transfers=transfers,
    )
    return text, sheet_keyboard(ledger.id, transfers)
