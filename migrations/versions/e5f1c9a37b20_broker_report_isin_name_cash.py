"""Broker report: asset isin/name, nullable average_buy_price, users.broker_cash_rub

Revision ID: e5f1c9a37b20
Revises: d4e8b2a61f30
Create Date: 2026-11-07 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e5f1c9a37b20'
down_revision: Union[str, Sequence[str], None] = 'd4e8b2a61f30'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('assets', sa.Column('isin', sa.String(length=12), nullable=True))
    op.add_column('assets', sa.Column('name', sa.String(length=150), nullable=True))
    # Брокерский отчёт ТБанка не содержит цену покупки — она может быть неизвестна
    op.alter_column('assets', 'average_buy_price', existing_type=sa.Numeric(14, 4), nullable=True)
    op.add_column('users', sa.Column('broker_cash_rub', sa.Numeric(14, 2), nullable=True))


def downgrade() -> None:
    op.drop_column('users', 'broker_cash_rub')
    # Строки без цены покупки нельзя вернуть в NOT NULL — ставим 0, чтобы миграция не падала
    op.execute("UPDATE assets SET average_buy_price = 0 WHERE average_buy_price IS NULL")
    op.alter_column('assets', 'average_buy_price', existing_type=sa.Numeric(14, 4), nullable=False)
    op.drop_column('assets', 'name')
    op.drop_column('assets', 'isin')
