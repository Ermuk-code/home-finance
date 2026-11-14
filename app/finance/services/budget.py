"""Бюджетные лимиты по категориям расходов.

Для каждой категории с заданным пользователем лимитом (``category_limits.monthly_limit``) считается сумма расходов пользователя за текущий
календарный месяц. Предупреждения:

* жёлтая зона — потрачено от 80% лимита (но меньше 100%);
* красная зона — потрачено 100% лимита и больше.

Переводы между своими счетами расходом не считаются (как и в отчётах бота).
"""
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from html import escape
from typing import Dict, List, Optional, Tuple

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Category, CategoryLimit, Transaction
from app.finance.services.categorizer import TRANSFER_CATEGORY_NAMES
from app.finance.services.reports import fmt_money

# Часовой пояс пользователей (МСК, без перехода на летнее время). Границы месяцев считаются в нём.
LOCAL_TZ = timezone(timedelta(hours=3))

YELLOW_THRESHOLD = Decimal("0.80")
RED_THRESHOLD = Decimal("1.00")

_ZERO = Decimal("0")


def local_today() -> date:
    return datetime.now(LOCAL_TZ).date()


def month_start(year: int, month: int) -> datetime:
    """Начало месяца (00:00 по местному времени) как aware-datetime для сравнения с Transaction.date."""
    return datetime(year, month, 1, tzinfo=LOCAL_TZ)


def next_month(year: int, month: int) -> Tuple[int, int]:
    return (year + 1, 1) if month == 12 else (year, month + 1)


def build_limit_warning(category_name: str, spent: Decimal, limit: Decimal) -> Optional[str]:
    """Текст предупреждения (HTML) или None, если лимит в «зелёной» зоне."""
    if limit <= 0:
        return None
    ratio = spent / limit
    if ratio < YELLOW_THRESHOLD:
        return None

    # Проценты округляем вниз: 99,9% не должно выглядеть как «100%».
    percent = int(ratio * 100)
    name = escape(category_name)
    details = f"({fmt_money(spent)} из {fmt_money(limit)} ₽)"
    if ratio >= RED_THRESHOLD:
        return (
            f"🔴 Внимание: Вы исчерпали {percent}% лимита на категорию {name} "
            f"в этом месяце {details}"
        )
    return (
        f"⚠️ Внимание: Вы исчерпали {percent}% лимита на категорию {name} "
        f"в этом месяце {details}"
    )


async def check_category_limits(
    session: AsyncSession,
    user_id: int,
    today: Optional[date] = None,
) -> List[str]:
    """Возвращает список предупреждений по категориям, где расходы текущего месяца дошли до 80% лимита.

    Красные предупреждения (лимит исчерпан) идут первыми, дальше — по убыванию процента.
    Пустой список — все лимиты в норме или лимиты не заданы.
    """
    today = today or local_today()
    start = month_start(today.year, today.month)
    end = month_start(*next_month(today.year, today.month))

    stmt = (
        select(
            Category.name,
            CategoryLimit.monthly_limit,
            func.coalesce(func.sum(-Transaction.amount), 0).label("spent"),
        )
        .join(Transaction, Transaction.category_id == Category.id)
        .join(
            CategoryLimit,
            (CategoryLimit.category_id == Category.id) & (CategoryLimit.user_id == user_id),
        )
        .where(
            Transaction.user_id == user_id,
            Transaction.amount < 0,
            Transaction.date >= start,
            Transaction.date < end,
            CategoryLimit.monthly_limit > 0,
            Category.name.not_in(list(TRANSFER_CATEGORY_NAMES)),
        )
        .group_by(Category.id, Category.name, CategoryLimit.monthly_limit)
    )
    rows = (await session.execute(stmt)).all()

    scored: Dict[str, Tuple[Decimal, str]] = {}
    for name, limit, spent in rows:
        limit_d = Decimal(str(limit))
        spent_d = Decimal(str(spent))
        text = build_limit_warning(name, spent_d, limit_d)
        if text:
            scored[name] = (spent_d / limit_d, text)

    ordered = sorted(scored.values(), key=lambda item: item[0], reverse=True)
    return [text for _, text in ordered]
