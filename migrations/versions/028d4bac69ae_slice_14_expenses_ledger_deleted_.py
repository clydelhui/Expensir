"""slice 14 expenses ledger_deleted_created index

Revision ID: 028d4bac69ae
Revises: 574bcef09bb9
Create Date: 2026-07-10 15:58:18.070470

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '028d4bac69ae'
down_revision: Union[str, None] = '574bcef09bb9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # the merged transaction listing (#24, ADR-0012) walks expenses by
    # (ledger, standing, recency) — the shape settlements already index
    with op.batch_alter_table('expenses', schema=None) as batch_op:
        batch_op.create_index(
            'ix_expenses_ledger_deleted_created',
            ['ledger_id', 'deleted_at', 'created_at'],
            unique=False,
        )


def downgrade() -> None:
    with op.batch_alter_table('expenses', schema=None) as batch_op:
        batch_op.drop_index('ix_expenses_ledger_deleted_created')
