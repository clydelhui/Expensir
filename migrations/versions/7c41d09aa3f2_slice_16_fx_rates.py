"""slice 16 fx_rates display rates

Revision ID: 7c41d09aa3f2
Revises: 028d4bac69ae
Create Date: 2026-07-11 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '7c41d09aa3f2'
down_revision: Union[str, None] = '028d4bac69ae'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # display rates (#16, §7.5): one row per (group, pair, source), upserted in
    # place — manual pins are group-scoped, API rows deployment-global (group_id NULL)
    op.create_table(
        'fx_rates',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('group_id', sa.Integer(), nullable=True),
        sa.Column('base_currency', sa.String(length=3), nullable=False),
        sa.Column('quote_currency', sa.String(length=3), nullable=False),
        sa.Column('rate', sa.Float(), nullable=False),
        sa.Column('source', sa.String(), nullable=False),
        sa.Column('fetched_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('set_by', sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(['group_id'], ['groups.id']),
        sa.ForeignKeyConstraint(['set_by'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    # one row per (pair, source): PARTIAL unique indexes, because a plain UNIQUE
    # never collides on NULL group_ids (PG and SQLite both treat NULLs as
    # distinct) — and the global API rows are exactly the group_id-NULL ones
    # that concurrent unlocked reads race to insert
    with op.batch_alter_table('fx_rates', schema=None) as batch_op:
        batch_op.create_index(
            'ux_fx_rates_global_pair',
            ['base_currency', 'quote_currency', 'source'],
            unique=True,
            sqlite_where=sa.text('group_id IS NULL'),
            postgresql_where=sa.text('group_id IS NULL'),
        )
        batch_op.create_index(
            'ux_fx_rates_group_pair',
            ['group_id', 'base_currency', 'quote_currency', 'source'],
            unique=True,
            sqlite_where=sa.text('group_id IS NOT NULL'),
            postgresql_where=sa.text('group_id IS NOT NULL'),
        )


def downgrade() -> None:
    with op.batch_alter_table('fx_rates', schema=None) as batch_op:
        batch_op.drop_index('ux_fx_rates_group_pair')
        batch_op.drop_index('ux_fx_rates_global_pair')
    op.drop_table('fx_rates')
