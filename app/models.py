"""Реестр всех моделей.

Импорт этого модуля регистрирует все таблицы в Base.metadata (нужно Alembic) и разрешает строковые
связи между доменами (User -> Transaction и т.д.). Вне доменных пакетов модели импортируются отсюда.
"""
from app.users.models import ROLE_ADMIN, ROLE_USER, ROLES, User
from app.finance.models import (
    Asset,
    Category,
    CategoryLimit,
    Deposit,
    IncomeRule,
    Reminder,
    Transaction,
    Vacation,
)
from app.health.models import ActivityLog, DailyNutrition, WeightLog

__all__ = [
    "ROLE_ADMIN", "ROLE_USER", "ROLES", "User",
    "Asset", "Category", "CategoryLimit", "Deposit", "IncomeRule", "Reminder", "Transaction", "Vacation",
    "ActivityLog", "DailyNutrition", "WeightLog",
]
