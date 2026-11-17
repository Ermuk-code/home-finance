"""Keep device total energy separate from active energy to avoid counting BMR twice."""
from alembic import op
import sqlalchemy as sa

revision: str = 'a1f4c8d2e690'
down_revision: str = '9e0c3f6a2b71'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('activity_logs', sa.Column('total_calories', sa.Numeric(12, 2), nullable=True))


def downgrade():
    op.drop_column('activity_logs', 'total_calories')
