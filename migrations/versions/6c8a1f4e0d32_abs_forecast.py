"""Body fat measurements and personal forecast parameters."""
from alembic import op
import sqlalchemy as sa

revision: str = '6c8a1f4e0d32'
down_revision: str = '5b7f0e3d9c21'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('users', sa.Column('target_body_fat', sa.Numeric(4, 1), nullable=False, server_default='12'))
    op.add_column('users', sa.Column('avg_daily_deficit', sa.Integer(), nullable=False, server_default='500'))
    op.add_column('weight_logs', sa.Column('body_fat', sa.Numeric(4, 1), nullable=True))
    op.create_check_constraint('ck_users_target_body_fat', 'users', 'target_body_fat > 0 AND target_body_fat < 100')
    op.create_check_constraint('ck_users_avg_daily_deficit', 'users', 'avg_daily_deficit BETWEEN 0 AND 2000')
    op.create_check_constraint('ck_weight_body_fat', 'weight_logs', 'body_fat > 0 AND body_fat < 100')


def downgrade():
    op.drop_constraint('ck_weight_body_fat', 'weight_logs', type_='check')
    op.drop_constraint('ck_users_avg_daily_deficit', 'users', type_='check')
    op.drop_constraint('ck_users_target_body_fat', 'users', type_='check')
    op.drop_column('weight_logs', 'body_fat')
    op.drop_column('users', 'avg_daily_deficit')
    op.drop_column('users', 'target_body_fat')
