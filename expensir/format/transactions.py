"""The shared transaction render (ADR-0012): one two-line formatter, so the
/transactions listing and the feed (ADR-0013) can never drift apart."""

from expensir.domain.money import fmt
from expensir.domain.transactions import ExpenseRow, TransactionPage, TransactionRow


def transaction_line(tx: TransactionRow) -> str:
    """Two lines: description/direction + native amount (no ≈ equivalents);
    then date — occurred_on when set, else created_at — and the split summary."""
    if isinstance(tx, ExpenseRow):
        first = f"#{tx.id} {tx.description} — {fmt(tx.amount_minor, tx.currency)}"
        date = tx.occurred_on or tx.created_at.date().isoformat()
        split = f", split {tx.participant_count} ways" if tx.participant_count > 1 else ""
        edited = " · ✏️ edited" if tx.edited else ""
        return f"{first}\n{date} · paid by {tx.payer_name}{split}{edited}"
    first = f"🤝 {tx.from_name} paid {tx.to_name} — {fmt(tx.amount_minor, tx.currency)}"
    return f"{first}\n{tx.created_at.date().isoformat()}"


def transactions_reply(*, ledger_name: str, page: TransactionPage) -> str:
    """The /transactions page (ADR-0012): ledger + total count header, newest
    first — no page numbers (keyset has none); a friendly nudge when empty."""
    if page.total == 0:
        return (
            f"📒 {ledger_name} • No transactions yet — log an expense with /equal, "
            "or record a payment with /settle."
        )
    unit = "transaction" if page.total == 1 else "transactions"
    header = f"📒 {ledger_name} • {page.total} {unit}, newest first"
    return "\n\n".join([header, *(transaction_line(tx) for tx in page.rows)])
