"""Add physical params to users.

Revision ID: 4a6e9d2c8b10
Revises: 3e8a5c1d9b47
"""
from alembic import op
import sqlalchemy as sa

revision: str = '4a6e9d2c8b10'
down_revision: str = '3e8a5c1d9b47'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('users', sa.Column('gender', sa.String(6), nullable=True))
    op.add_column('users', sa.Column('height_cm', sa.Integer(), nullable=True))
    op.add_column('users', sa.Column('birth_date', sa.Date(), nullable=True))
    op.create_check_constraint('ck_users_gender', 'users', "gender IN ('male', 'female')")
    op.create_check_constraint('ck_users_height', 'users', 'height_cm BETWEEN 50 AND 260')
    from app.core.config import settings
    if settings.ADMIN_TELEGRAM_ID:
        op.get_bind().execute(sa.text(
            'UPDATE users SET height_cm = 184 WHERE telegram_id = :tg AND height_cm IS NULL'
        ), {'tg': settings.ADMIN_TELEGRAM_ID})


def downgrade() -> None:
    op.drop_constraint('ck_users_height', 'users', type_='check')
    op.drop_constraint('ck_users_gender', 'users', type_='check')
    op.drop_column('users', 'birth_date')
    op.drop_column('users', 'height_cm')
    op.drop_column('users', 'gender')
