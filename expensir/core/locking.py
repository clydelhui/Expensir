"""Per-group write serialization (§0.11, ADR-0003)."""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


async def per_group_lock(session: AsyncSession, group_id: int) -> None:
    """Serialize mutating transactions per group; auto-released at transaction end.

    Postgres advisory lock only — SQLite serializes writes globally already.
    """
    if session.get_bind().dialect.name != "postgresql":
        return
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
        {"key": f"group:{group_id}"},
    )
