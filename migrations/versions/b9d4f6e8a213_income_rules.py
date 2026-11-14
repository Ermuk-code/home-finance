"""Income rules (parents / transfers / ... by keyword in description)

Revision ID: b9d4f6e8a213
Revises: a8c3e5d7f102
Create Date: 2026-11-07 17:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b9d4f6e8a213'
down_revision: Union[str, Sequence[str], None] = 'a8c3e5d7f102'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'income_rules',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('keyword', sa.String(length=120), nullable=False),
        sa.Column('source', sa.String(length=20), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_income_rules_keyword'), 'income_rules', ['keyword'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_income_rules_keyword'), table_name='income_rules')
    op.drop_table('income_rules')
