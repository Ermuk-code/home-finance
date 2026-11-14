"""Reminder recurrence interval (every N days/weeks/months/years)

Revision ID: a8c3e5d7f102
Revises: f6a2d8b41c93
Create Date: 2026-11-07 16:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a8c3e5d7f102'
down_revision: Union[str, Sequence[str], None] = 'f6a2d8b41c93'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'reminders',
        sa.Column('recurrence_every', sa.Integer(), nullable=False, server_default='1'),
    )


def downgrade() -> None:
    op.drop_column('reminders', 'recurrence_every')
