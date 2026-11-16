"""Health measurements and nutrition goals."""
from alembic import op
import sqlalchemy as sa

revision: str = '5b7f0e3d9c21'
down_revision: str = '4a6e9d2c8b10'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('users', sa.Column('health_goal', sa.String(8), nullable=False, server_default='maintain'))
    op.add_column('users', sa.Column('deficit_percent', sa.Integer(), nullable=False, server_default='15'))
    op.add_column('users', sa.Column('goal_weight_kg', sa.Numeric(5, 1), nullable=True))
    op.add_column('users', sa.Column('activity_factor', sa.Numeric(3, 2), nullable=False, server_default='1.2'))
    for name, expression in (
        ('ck_users_health_goal', "health_goal IN ('maintain', 'lose')"),
        ('ck_users_deficit', 'deficit_percent BETWEEN 0 AND 20'),
        ('ck_users_goal_weight', 'goal_weight_kg BETWEEN 30 AND 300'),
        ('ck_users_activity_factor', 'activity_factor BETWEEN 1.2 AND 2.4'),
    ):
        op.create_check_constraint(name, 'users', expression)
    op.create_table('body_measurements',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
        sa.Column('date', sa.Date(), nullable=False),
        sa.Column('height_cm', sa.Numeric(5, 1), nullable=True),
        sa.Column('waist_cm', sa.Numeric(5, 1), nullable=True),
        sa.CheckConstraint('height_cm BETWEEN 50 AND 260', name='ck_body_height'),
        sa.CheckConstraint('waist_cm BETWEEN 30 AND 250', name='ck_body_waist'),
    )
    op.create_index('uq_body_measurements_user_date', 'body_measurements', ['user_id', 'date'], unique=True)


def downgrade():
    op.drop_table('body_measurements')
    for name in ('ck_users_health_goal', 'ck_users_deficit', 'ck_users_goal_weight', 'ck_users_activity_factor'):
        op.drop_constraint(name, 'users', type_='check')
    for name in ('activity_factor', 'goal_weight_kg', 'deficit_percent', 'health_goal'):
        op.drop_column('users', name)
