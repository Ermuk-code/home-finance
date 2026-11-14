"""Life OS, этап 1: роли пользователей, таблицы здоровья, персональные лимиты, user_id у income_rules

* users: name, role ('admin' | 'user'); владелец (ADMIN_TELEGRAM_ID, иначе самый старый пользователь) -> admin
* health: weight_logs, daily_nutrition, activity_logs (все с user_id -> users.id, ON DELETE CASCADE)
* income_rules: user_id (существующие правила переходят администратору)
* category_limits: персональные лимиты; значения categories.monthly_limit переносятся администратору,
  после чего колонка categories.monthly_limit удаляется

Revision ID: 7c1d2e9f4a60
Revises: b9d4f6e8a213
Create Date: 2026-11-07 16:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '7c1d2e9f4a60'
down_revision: Union[str, Sequence[str], None] = 'b9d4f6e8a213'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _admin_id(conn):
    return conn.execute(sa.text("SELECT id FROM users WHERE role = 'admin' ORDER BY id LIMIT 1")).scalar()


def upgrade() -> None:
    from app.core.config import settings

    conn = op.get_bind()

    # --- users: name + role ------------------------------------------------
    op.add_column('users', sa.Column('name', sa.String(length=100), nullable=True))
    op.add_column('users', sa.Column('role', sa.String(length=16), server_default='user', nullable=False))

    if settings.ADMIN_TELEGRAM_ID:
        conn.execute(
            sa.text("UPDATE users SET role = 'admin' WHERE telegram_id = :tg"),
            {"tg": settings.ADMIN_TELEGRAM_ID},
        )
    # Нет владельца из .env среди пользователей — админом становится самый старый (он и был единственным владельцем)
    conn.execute(
        sa.text(
            "UPDATE users SET role = 'admin' "
            "WHERE id = (SELECT MIN(id) FROM users) "
            "AND NOT EXISTS (SELECT 1 FROM users WHERE role = 'admin')"
        )
    )
    conn.execute(sa.text("UPDATE users SET name = 'User ' || CAST(id AS TEXT)"))
    conn.execute(sa.text("UPDATE users SET name = :n WHERE role = 'admin'"), {"n": settings.ADMIN_NAME})

    op.alter_column('users', 'name', existing_type=sa.String(length=100), nullable=False, server_default='User')
    op.create_check_constraint('ck_users_role', 'users', "role IN ('admin', 'user')")

    admin_id = _admin_id(conn)

    # --- income_rules: user_id --------------------------------------------
    op.add_column('income_rules', sa.Column('user_id', sa.Integer(), nullable=True))
    if admin_id is not None:
        conn.execute(sa.text("UPDATE income_rules SET user_id = :uid"), {"uid": admin_id})
    conn.execute(sa.text("DELETE FROM income_rules WHERE user_id IS NULL"))  # правила без владельца (БД без пользователей)
    op.alter_column('income_rules', 'user_id', existing_type=sa.Integer(), nullable=False)
    op.create_foreign_key(
        'fk_income_rules_user_id_users', 'income_rules', 'users', ['user_id'], ['id'], ondelete='CASCADE'
    )
    op.create_index(op.f('ix_income_rules_user_id'), 'income_rules', ['user_id'], unique=False)

    # --- category_limits (персональные лимиты) ----------------------------
    op.create_table(
        'category_limits',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('category_id', sa.Integer(), nullable=False),
        sa.Column('monthly_limit', sa.Numeric(precision=14, scale=2), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['category_id'], ['categories.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('user_id', 'category_id', name='uq_category_limits_user_category'),
    )
    op.create_index(op.f('ix_category_limits_user_id'), 'category_limits', ['user_id'], unique=False)
    op.create_index(op.f('ix_category_limits_category_id'), 'category_limits', ['category_id'], unique=False)
    if admin_id is not None:
        conn.execute(
            sa.text(
                "INSERT INTO category_limits (user_id, category_id, monthly_limit) "
                "SELECT :uid, id, monthly_limit FROM categories WHERE monthly_limit IS NOT NULL"
            ),
            {"uid": admin_id},
        )
    op.drop_column('categories', 'monthly_limit')

    # --- health -----------------------------------------------------------
    op.create_table(
        'weight_logs',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('weight_kg', sa.Numeric(precision=5, scale=2), nullable=False),
        sa.Column('bmi', sa.Numeric(precision=4, scale=1), nullable=True),
        sa.Column('timestamp', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_weight_logs_user_id'), 'weight_logs', ['user_id'], unique=False)
    op.create_index(op.f('ix_weight_logs_timestamp'), 'weight_logs', ['timestamp'], unique=False)

    op.create_table(
        'daily_nutrition',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('date', sa.Date(), nullable=False),
        sa.Column('calories', sa.Integer(), server_default='0', nullable=False),
        sa.Column('protein', sa.Numeric(precision=6, scale=1), server_default='0', nullable=False),
        sa.Column('fat', sa.Numeric(precision=6, scale=1), server_default='0', nullable=False),
        sa.Column('carbs', sa.Numeric(precision=6, scale=1), server_default='0', nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('uq_daily_nutrition_user_date', 'daily_nutrition', ['user_id', 'date'], unique=True)

    op.create_table(
        'activity_logs',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('date', sa.Date(), nullable=False),
        sa.Column('steps', sa.Integer(), server_default='0', nullable=False),
        sa.Column('active_calories', sa.Integer(), server_default='0', nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('uq_activity_logs_user_date', 'activity_logs', ['user_id', 'date'], unique=True)


def downgrade() -> None:
    conn = op.get_bind()

    op.drop_index('uq_activity_logs_user_date', table_name='activity_logs')
    op.drop_table('activity_logs')
    op.drop_index('uq_daily_nutrition_user_date', table_name='daily_nutrition')
    op.drop_table('daily_nutrition')
    op.drop_index(op.f('ix_weight_logs_timestamp'), table_name='weight_logs')
    op.drop_index(op.f('ix_weight_logs_user_id'), table_name='weight_logs')
    op.drop_table('weight_logs')

    # Общий лимит на категорию: берём наибольший из персональных
    op.add_column('categories', sa.Column('monthly_limit', sa.Numeric(precision=14, scale=2), nullable=True))
    conn.execute(
        sa.text(
            "UPDATE categories SET monthly_limit = "
            "(SELECT MAX(l.monthly_limit) FROM category_limits l WHERE l.category_id = categories.id)"
        )
    )
    op.drop_index(op.f('ix_category_limits_category_id'), table_name='category_limits')
    op.drop_index(op.f('ix_category_limits_user_id'), table_name='category_limits')
    op.drop_table('category_limits')

    op.drop_index(op.f('ix_income_rules_user_id'), table_name='income_rules')
    op.drop_constraint('fk_income_rules_user_id_users', 'income_rules', type_='foreignkey')
    op.drop_column('income_rules', 'user_id')

    op.drop_constraint('ck_users_role', 'users', type_='check')
    op.drop_column('users', 'role')
    op.drop_column('users', 'name')
