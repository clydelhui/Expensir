"""The pinned board text (§13): a stateless projection of the simplified balances.

Each line is one suggested transfer with a WYSIWYG [Settle] button (ADR-0006).
`≈ home` equivalents arrive with the FX slice.
"""

from dataclasses import dataclass

from expensir.domain.money import fmt


@dataclass(frozen=True)
class BoardLine:
    """One suggested transfer, in simplify's stable order (§7.4)."""

    from_id: int
    to_id: int
    from_name: str
    to_name: str
    amount_minor: int
    currency: str


def board_text(*, ledger_name: str, transfers: list[BoardLine]) -> str:
    header = f"📒 {ledger_name} • Board"
    if not transfers:
        return f"{header}\nAll settled up."
    lines = (f"{t.from_name} → {t.to_name} {fmt(t.amount_minor, t.currency)}" for t in transfers)
    return "\n".join([header, *lines])
