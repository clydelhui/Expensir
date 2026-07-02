"""SQLAlchemy models (§5). All money is integer minor units (ADR-0008)."""

from datetime import UTC, datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    display_name: Mapped[str] = mapped_column(String)


class Identity(Base):
    """A member EXISTS only once we have their identity row — no ghosts (§5, §11)."""

    __tablename__ = "identities"
    __table_args__ = (
        Index("ix_identities_platform_username", "platform", "username"),
        Index("ix_identities_platform_user", "platform", "platform_user_id", unique=True),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    platform: Mapped[str] = mapped_column(String, default="telegram")
    platform_user_id: Mapped[int] = mapped_column(BigInteger)
    username: Mapped[str | None] = mapped_column(String)


class GroupMember(Base):
    __tablename__ = "group_members"
    __table_args__ = (Index("ix_group_members_group_user", "group_id", "user_id", unique=True),)

    id: Mapped[int] = mapped_column(primary_key=True)
    group_id: Mapped[int] = mapped_column(ForeignKey("groups.id"))
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    joined_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    # set on leave; cleared on re-join/interaction (reactivation, §11)
    left_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ProcessedUpdate(Base):
    """Webhook idempotency: an update_id is processed at most once (§5)."""

    __tablename__ = "processed_updates"

    update_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Group(Base):
    __tablename__ = "groups"

    id: Mapped[int] = mapped_column(primary_key=True)
    platform_chat_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    name: Mapped[str] = mapped_column(String)
    home_currency: Mapped[str | None] = mapped_column(String(3))  # nullable until set (ADR-0001)
    # App-maintained invariant, not a DB FK: must always point at an OPEN ledger of this
    # group (ADR-0004); a DB-level FK would be circular with ledgers.group_id.
    active_ledger_id: Mapped[int | None] = mapped_column(BigInteger)


class Ledger(Base):
    __tablename__ = "ledgers"
    # create-board-once guard (ADR-0003). Composite because Telegram message ids are
    # only unique per chat — a global UNIQUE(board_message_id) as written in §5 would
    # collide across groups (spec deviation filed as a follow-up issue).
    __table_args__ = (
        Index("ix_ledgers_board_chat_message", "board_chat_id", "board_message_id", unique=True),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    group_id: Mapped[int] = mapped_column(ForeignKey("groups.id"), index=True)
    name: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, default="open")  # 'open' | 'archived'
    logging_currency: Mapped[str | None] = mapped_column(String(3))  # null -> home (ADR-0001)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    board_message_id: Mapped[int | None] = mapped_column(BigInteger)
    board_chat_id: Mapped[int | None] = mapped_column(BigInteger)
