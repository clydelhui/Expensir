import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from expensir.core.handler import Deps
from expensir.db.models import Base


@pytest.fixture
async def deps():
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield Deps(
        session_factory=async_sessionmaker(engine, expire_on_commit=False),
        bot_username="expensir_bot",
    )
    await engine.dispose()
