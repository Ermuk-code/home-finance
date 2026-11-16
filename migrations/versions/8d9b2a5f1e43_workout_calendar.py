"""Workout calendar and exercises."""
from alembic import op
import sqlalchemy as sa

revision: str = '8d9b2a5f1e43'
down_revision: str = '6c8a1f4e0d32'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('workouts',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
        sa.Column('date', sa.Date(), nullable=False),
        sa.Column('title', sa.String(120), nullable=False),
        sa.Column('is_completed', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('morning_notified_on', sa.Date(), nullable=True),
    )
    op.create_index('ix_workouts_user_date', 'workouts', ['user_id', 'date'])
    op.create_index('ix_workouts_date', 'workouts', ['date'])
    op.create_table('workout_exercises',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('workout_id', sa.Integer(), sa.ForeignKey('workouts.id', ondelete='CASCADE'), nullable=False),
        sa.Column('name', sa.String(100), nullable=False),
        sa.Column('sets', sa.Integer(), nullable=False),
        sa.Column('reps', sa.String(40), nullable=False),
        sa.Column('weight_kg', sa.Numeric(5, 2), nullable=True),
        sa.CheckConstraint('sets BETWEEN 1 AND 100', name='ck_workout_exercise_sets'),
        sa.CheckConstraint('weight_kg BETWEEN 0 AND 999.99', name='ck_workout_exercise_weight'),
    )
    op.create_index('ix_workout_exercises_workout_id', 'workout_exercises', ['workout_id'])


def downgrade():
    op.drop_table('workout_exercises')
    op.drop_table('workouts')
