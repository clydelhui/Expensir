"""ADR-0003: mutations serialize per group on Postgres; SQLite needs no lock.

Real advisory-lock contention needs Postgres and is env-gated with the migration
tests; here we pin the dialect branch and that the mutation paths acquire the lock.
"""

from typing import Any

from sqlalchemy import select

from expensir.core.locking import per_group_lock
from expensir.db.models import Group
from tests.factories import bot_added_update, message_update


async def test_lock_is_a_noop_off_postgres(deps):
    async with deps.session_factory() as session, session.begin():
        # SQLite has no pg_advisory_xact_lock; emitting it would raise here
        await per_group_lock(session, 42)


async def test_lock_takes_a_pg_advisory_xact_lock_on_postgres():
    executed: list[tuple[str, Any]] = []

    class FakeDialect:
        name = "postgresql"

    class FakeBind:
        dialect = FakeDialect()

    class FakeSession:
        def get_bind(self) -> FakeBind:
            return FakeBind()

        async def execute(self, statement: Any, params: Any = None) -> None:
            executed.append((str(statement), params))

    await per_group_lock(FakeSession(), 42)  # type: ignore[arg-type]

    # the contract: an xact-scoped advisory lock, keyed per group
    [(sql, params)] = executed
    assert "pg_advisory_xact_lock" in sql
    assert "42" in str(params)


async def test_mutating_commands_acquire_the_group_lock(deps, monkeypatch):
    """Deleting the per_group_lock call sites must not go unnoticed (§0.11, AC4)."""
    from expensir.core.handler import dispatch

    locked: list[int] = []

    async def spy(session: Any, group_id: int) -> None:
        locked.append(group_id)
        await per_group_lock(session, group_id)

    monkeypatch.setattr("expensir.core.handler.per_group_lock", spy)
    monkeypatch.setattr("expensir.domain.apply.per_group_lock", spy)

    await dispatch(bot_added_update(chat_id=-42), deps)
    await dispatch(message_update(update_id=3, chat_id=-42, text="/homecurrency EUR"), deps)
    await dispatch(message_update(update_id=4, chat_id=-42, text="/equal 60 dinner"), deps)

    async with deps.session_factory() as session:
        group = (await session.execute(select(Group))).scalar_one()
    assert len(locked) >= 2  # both mutating commands took the lock
    assert set(locked) == {group.id}

    # reads must NOT serialize against writes (§0.11): /balance takes no lock
    locked.clear()
    await dispatch(message_update(update_id=5, chat_id=-42, text="/balance"), deps)
    assert locked == []
