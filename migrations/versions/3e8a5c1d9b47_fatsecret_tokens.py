"""Life OS, этап 3: токены FatSecret (OAuth 1.0a) в users

Revision ID: 3e8a5c1d9b47
Revises: 7c1d2e9f4a60
Create Date: 2026-11-07 18:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '3e8a5c1d9b47'
down_revision: Union[str, Sequence[str], None] = '7c1d2e9f4a60'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('users', sa.Column('fs_access_token', sa.String(length=255), nullable=True))
    op.add_column('users', sa.Column('fs_access_secret', sa.String(length=255), nullable=True))


def downgrade() -> None:
    op.drop_column('users', 'fs_access_secret')
    op.drop_column('users', 'fs_access_token')
