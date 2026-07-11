import os
import sqlite3

import pytest
from alembic import command
from alembic.config import Config

SLICE_1_TABLES = {
    "users",
    "identities",
    "groups",
    "group_members",
    "ledgers",
    "processed_updates",
}
SLICE_2_TABLES = {"expenses", "expense_splits", "actions"}
SLICE_9_TABLES = {"settlements"}


def upgrade_config(url: str) -> Config:
    cfg = Config("alembic.ini")
    # programmatic channel; beats DATABASE_URL and alembic.ini in env.py
    cfg.attributes["sqlalchemy_url"] = url
    return cfg


def test_migrations_apply_cleanly_on_sqlite(tmp_path):
    db_path = tmp_path / "migrated.db"

    command.upgrade(upgrade_config(f"sqlite+aiosqlite:///{db_path}"), "head")

    with sqlite3.connect(db_path) as conn:
        tables = {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    assert tables >= SLICE_1_TABLES | SLICE_2_TABLES | SLICE_9_TABLES


def test_expense_gains_the_ledger_deleted_created_index_to_match_settlements(tmp_path):
    """Slice 14 (#24, ADR-0012): the merged listing walks expenses the same way
    settlements are already indexed."""
    db_path = tmp_path / "migrated.db"

    command.upgrade(upgrade_config(f"sqlite+aiosqlite:///{db_path}"), "head")

    with sqlite3.connect(db_path) as conn:
        indexes = {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")
        }
    assert "ix_expenses_ledger_deleted_created" in indexes


def test_fx_rates_table_arrives_with_the_fx_slice(tmp_path):
    """Slice 16 (#16, §7.5): display rates — group-scoped pins, global API cache."""
    db_path = tmp_path / "migrated.db"

    command.upgrade(upgrade_config(f"sqlite+aiosqlite:///{db_path}"), "head")

    with sqlite3.connect(db_path) as conn:
        tables = {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        indexes = {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")
        }
    assert "fx_rates" in tables
    # the one-row-per-(pair,source) backstop (§5): partial, because plain UNIQUE
    # never collides on the global rows' NULL group_ids
    assert "ux_fx_rates_global_pair" in indexes
    assert "ux_fx_rates_group_pair" in indexes


def test_exported_database_url_never_hijacks_a_programmatic_url(tmp_path, monkeypatch):
    """A test/tooling run must migrate the URL it was given, not the deploy DB from the env."""
    deploy_db = tmp_path / "pretend-prod.db"
    target_db = tmp_path / "target.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{deploy_db}")

    command.upgrade(upgrade_config(f"sqlite+aiosqlite:///{target_db}"), "head")

    assert target_db.exists()
    assert not deploy_db.exists()


@pytest.mark.skipif(
    "EXPENSIR_TEST_POSTGRES_URL" not in os.environ,
    reason="set EXPENSIR_TEST_POSTGRES_URL to run migrations against Postgres",
)
def test_migrations_apply_cleanly_on_postgres():
    cfg = upgrade_config(os.environ["EXPENSIR_TEST_POSTGRES_URL"])

    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")
