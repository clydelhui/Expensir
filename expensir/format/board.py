"""The pinned board text (§13): a stateless projection of the simplified balances.

Each line is one suggested transfer. `≈ home` equivalents arrive with the FX
slice; `[Settle]` buttons with the settling slice (issue #9).
"""

from expensir.domain.money import fmt

# (from_name, to_name, amount_minor, currency), already in simplify's stable order
BoardLine = tuple[str, str, int, str]


def board_text(*, ledger_name: str, transfers: list[BoardLine]) -> str:
    header = f"📒 {ledger_name} • Board"
    if not transfers:
        return f"{header}\nAll settled up."
    lines = (f"{frm} → {to} {fmt(minor, ccy)}" for frm, to, minor, ccy in transfers)
    return "\n".join([header, *lines])
