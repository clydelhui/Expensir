"""slice 11 actions ledger_id nullable

Revision ID: 27325f1f9957
Revises: 5674c11d33cc
Create Date: 2026-07-07 16:28:33.166272

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '27325f1f9957'
down_revision: Union[str, None] = '5674c11d33cc'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # setup actions (§11) are group-scoped: registration is no ledger's activity
    with op.batch_alter_table('actions', schema=None) as batch_op:
        batch_op.alter_column('ledger_id', existing_type=sa.Integer(), nullable=True)


def downgrade() -> None:
    with op.batch_alter_table('actions', schema=None) as batch_op:
        batch_op.alter_column('ledger_id', existing_type=sa.Integer(), nullable=False)
