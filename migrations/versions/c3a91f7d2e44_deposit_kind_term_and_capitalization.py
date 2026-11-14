"""Deposit kind (deposit/savings), opened_at, term_months, capitalization

Revision ID: c3a91f7d2e44
Revises: 130750653161
Create Date: 2026-11-05 20:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c3a91f7d2e44'
down_revision: Union[str, Sequence[str], None] = '130750653161'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('deposits', sa.Column('kind', sa.String(length=16), server_default='deposit', nullable=False))
    op.add_column('deposits', sa.Column('opened_at', sa.Date(), nullable=True))
    op.add_column('deposits', sa.Column('term_months', sa.Integer(), nullable=True))
    op.add_column('deposits', sa.Column('capitalization', sa.Boolean(), server_default='false', nullable=False))
    # Ранее созданные записи с «накоп» в названии считаем накопительными счетами.
    op.execute("UPDATE deposits SET kind = 'savings' WHERE lower(title) LIKE '%накоп%'")


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('deposits', 'capitalization')
    op.drop_column('deposits', 'term_months')
    op.drop_column('deposits', 'opened_at')
    op.drop_column('deposits', 'kind')
