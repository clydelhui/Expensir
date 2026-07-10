"""SQLAlchemy models (§5). All money is integer minor units (ADR-0008)."""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, BigInteger, DateTime, ForeignKey, Index, String
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


class Action(Base):
    """The audit log (§5, §8): every mutation appends exactly one row.

    Row-creating ops are reversed via created_by_action_id on their rows;
    field/pointer flips store a minimal before_image and restore it on undo.
    """

    __tablename__ = "actions"
    __table_args__ = (Index("ix_actions_ledger_undone", "ledger_id", "undone_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    # None for group-scoped actions (setup, §11): registration is no ledger's activity
    ledger_id: Mapped[int | None] = mapped_column(ForeignKey("ledgers.id"))
    actor_user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    kind: Mapped[str] = mapped_column(String)
    intent_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    before_image: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    result_chat_id: Mapped[int | None] = mapped_column(BigInteger)
    result_message_id: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    undone_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    undone_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))


class Expense(Base):
    __tablename__ = "expenses"
    __table_args__ = (
        Index("ix_expenses_ledger_deleted", "ledger_id", "deleted_at"),
        # the merged transaction listing's walk (ADR-0012), matching Settlement's
        Index("ix_expenses_ledger_deleted_created", "ledger_id", "deleted_at", "created_at"),
        Index("ix_expenses_created_by_action", "created_by_action_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    ledger_id: Mapped[int] = mapped_column(ForeignKey("ledgers.id"))
    payer_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    amount_minor: Mapped[int] = mapped_column(BigInteger)
    currency: Mapped[str] = mapped_column(String(3))  # FROZEN at creation (§3)
    description: Mapped[str] = mapped_column(String)
    occurred_on: Mapped[str | None] = mapped_column(String)  # ISO date; DISPLAY ONLY (§7.2)
    split_type: Mapped[str] = mapped_column(String)  # 'equal' | 'exact' | 'shares' | 'percent'
    source: Mapped[str] = mapped_column(String)  # 'command' | 'nl' | 'ocr'
    created_by_user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_by_action_id: Mapped[int] = mapped_column(ForeignKey("actions.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    edited_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ExpenseSplit(Base):
    """Exact minor-unit share per participant; excluded from reads via the expense's deleted_at."""

    __tablename__ = "expense_splits"
    __table_args__ = (
        Index("ix_expense_splits_expense_user", "expense_id", "user_id", unique=True),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    expense_id: Mapped[int] = mapped_column(ForeignKey("expenses.id"))
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    owed_minor: Mapped[int] = mapped_column(BigInteger)
    created_by_action_id: Mapped[int] = mapped_column(ForeignKey("actions.id"))


class Settlement(Base):
    """A recorded stated payment (ADR-0002): always one currency, one direction,
    one row, one action (ADR-0007). Never policed against the pool."""

    __tablename__ = "settlements"
    __table_args__ = (
        Index("ix_settlements_ledger_deleted_created", "ledger_id", "deleted_at", "created_at"),
        Index("ix_settlements_created_by_action", "created_by_action_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    ledger_id: Mapped[int] = mapped_column(ForeignKey("ledgers.id"))
    from_user: Mapped[int] = mapped_column(ForeignKey("users.id"))
    to_user: Mapped[int] = mapped_column(ForeignKey("users.id"))
    amount_minor: Mapped[int] = mapped_column(BigInteger)
    currency: Mapped[str] = mapped_column(String(3))
    created_by_action_id: Mapped[int] = mapped_column(ForeignKey("actions.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PendingIntent(Base):
    """An unresolved proposed intent awaiting Confirm (§10), pinned to the ledger
    active at propose time — confirm commits THERE, not to the current active
    ledger (WYSIWYG). Expiry is computed on read; consumed on confirm/cancel."""

    __tablename__ = "pending_intents"
    __table_args__ = (Index("ix_pending_intents_chat_message", "chat_id", "message_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    chat_id: Mapped[int] = mapped_column(BigInteger)
    # the proposal message's id (§10 reply routing); NULL until the executor's
    # send reports it back, like actions.result_message_id
    message_id: Mapped[int | None] = mapped_column(BigInteger)
    ledger_id: Mapped[int] = mapped_column(ForeignKey("ledgers.id"))
    # "me" in the stored intent means this member, whoever presses Confirm
    proposer_user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    seed: Mapped[int] = mapped_column(BigInteger)  # frozen: proposed shares == committed (§7.1)
    intent_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Ledger(Base):
    __tablename__ = "ledgers"
    # create-board-once guard (ADR-0003, §5). Composite because Telegram message ids are
    # only unique per chat — a global UNIQUE(board_message_id) would collide across groups.
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
