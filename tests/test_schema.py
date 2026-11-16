"""Схема БД: все пользовательские данные привязаны к users.id, нужные уникальные индексы на месте."""
import pytest

from app.core.database import Base
import app.models  # noqa: F401

USER_OWNED_TABLES = [
    "transactions", "assets", "reminders", "deposits", "vacations", "income_rules", "category_limits",
    "weight_logs", "daily_nutrition", "activity_logs", "body_measurements", "workouts",
]


@pytest.mark.parametrize("table_name", USER_OWNED_TABLES)
def test_table_is_bound_to_users(table_name):
    table = Base.metadata.tables[table_name]
    col = table.c.user_id
    assert not col.nullable
    assert any(fk.column.table.name == "users" and fk.column.name == "id" for fk in col.foreign_keys)


def test_users_table_columns():
    users = Base.metadata.tables["users"]
    assert {"id", "telegram_id", "name", "role"} <= set(users.c.keys())
    assert users.c.telegram_id.unique or any(
        i.unique and [c.name for c in i.columns] == ["telegram_id"] for i in users.indexes
    )
    assert users.c.role.server_default.arg == "user"


@pytest.mark.parametrize("table_name", ["daily_nutrition", "activity_logs"])
def test_unique_user_date_index(table_name):
    table = Base.metadata.tables[table_name]
    assert any(i.unique and [c.name for c in i.columns] == ["user_id", "date"] for i in table.indexes)


def test_users_have_fatsecret_token_columns():
    users = Base.metadata.tables["users"]
    for name in ("fs_access_token", "fs_access_secret"):
        assert name in users.c and users.c[name].nullable
