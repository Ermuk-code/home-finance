"""Link reminders to deposits (reminders.deposit_id)

Revision ID: d4e8b2a61f30
Revises: c3a91f7d2e44
Create Date: 2026-11-05 23:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd4e8b2a61f30'
down_revision: Union[str, Sequence[str], None] = 'c3a91f7d2e44'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('reminders', sa.Column('deposit_id', sa.Integer(), nullable=True))
    op.create_index(op.f('ix_reminders_deposit_id'), 'reminders', ['deposit_id'], unique=False)
    op.create_foreign_key(
        'fk_reminders_deposit_id_deposits', 'reminders', 'deposits', ['deposit_id'], ['id'], ondelete='CASCADE'
    )
    # Привязываем уже созданные автонапоминания «Закрытие вклада: <название>» к их вкладам
    op.execute(
        """
        UPDATE reminders AS r
        SET deposit_id = d.id
        FROM deposits AS d
        WHERE r.deposit_id IS NULL
          AND r.user_id = d.user_id
          AND r.title = 'Закрытие вклада: ' || d.title
        """
    )


def downgrade() -> None:
    op.drop_constraint('fk_reminders_deposit_id_deposits', 'reminders', type_='foreignkey')
    op.drop_index(op.f('ix_reminders_deposit_id'), table_name='reminders')
    op.drop_column('reminders', 'deposit_id')
