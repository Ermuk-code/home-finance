"""Reminder recurrence (daily/weekly/monthly/yearly)

Revision ID: f6a2d8b41c93
Revises: e5f1c9a37b20
Create Date: 2026-11-07 15:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f6a2d8b41c93'
down_revision: Union[str, Sequence[str], None] = 'e5f1c9a37b20'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('reminders', sa.Column('recurrence', sa.String(length=16), nullable=True))
    op.add_column('reminders', sa.Column('recurrence_day', sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column('reminders', 'recurrence_day')
    op.drop_column('reminders', 'recurrence')
