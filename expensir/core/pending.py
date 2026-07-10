"""Park / fetch / consume unconfirmed intents (§10). DB-backed: Cloud Run is stateless."""

from datetime import UTC, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from expensir.db.models import PendingIntent, User, utcnow
from expensir.intents.schema import Intent


def is_expired(pending: PendingIntent) -> bool:
    """Expiry is computed on read (§10): no sweeper, the next tap/reply decides."""
    expires = pending.expires_at
    if expires.tzinfo is None:  # SQLite returns naive datetimes; storage is UTC (§16)
        expires = expires.replace(tzinfo=UTC)
    return expires <= utcnow()


async def by_message(session: AsyncSession, chat_id: int, message_id: int) -> PendingIntent | None:
    """The pending row a reply targets (§10.2), live or not — the caller decides
    what expiry means for its interaction."""
    return (
        await session.execute(
            select(PendingIntent).where(
                PendingIntent.chat_id == chat_id, PendingIntent.message_id == message_id
            )
        )
    ).scalar_one_or_none()


def refresh(pending: PendingIntent, *, ttl_minutes: int) -> None:
    """Any successful write to the proposal keeps it live (§10.2, issue #14 grill):
    the TTL restarts on refine and pick alike."""
    pending.expires_at = utcnow() + timedelta(minutes=ttl_minutes)


async def park(
    session: AsyncSession,
    *,
    chat_id: int,
    ledger_id: int,
    proposer: User,
    seed: int,
    intent: Intent,
    ttl_minutes: int,
) -> PendingIntent:
    """Store the UNRESOLVED intent pinned to the ledger active at propose time (§10).

    message_id stays NULL until the executor sends the proposal and reports the
    id back — the row is keyed by the proposal message, which doesn't exist yet."""
    now = utcnow()
    pending = PendingIntent(
        chat_id=chat_id,
        ledger_id=ledger_id,
        proposer_user_id=proposer.id,
        seed=seed,
        intent_json=intent.model_dump(mode="json"),
        created_at=now,
        expires_at=now + timedelta(minutes=ttl_minutes),
    )
    session.add(pending)
    await session.flush()
    return pending
