"""Просмотр и установка месячных лимитов по категориям (общий код для бота и веб-панели).

Категории — общий справочник, а лимиты персональные (таблица category_limits): у каждого свой бюджет.
Лимиты имеют смысл только для расходных категорий, кроме переводов между своими счетами.
"""
import re
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Category, CategoryLimit, Transaction
from app.finance.services.budget import local_today, month_start, next_month
from app.finance.services.categorizer import TRANSFER_CATEGORY_NAMES

MAX_LIMIT = Decimal("1000000000")  # влезает в Numeric(14, 2) с запасом


class LimitInputError(ValueError):
    """Некорректный ввод лимита; текст безопасно показывать пользователю."""


def parse_limit_input(raw: str) -> Optional[Decimal]:
    """«15 000», «15000,50», «15000 ₽» -> Decimal. «0», «нет», «-», «снять» -> None (лимит снимается)."""
    text = (raw or "").strip().lower()
    if text in ("0", "нет", "-", "снять", "убрать", "off", "none"):
        return None
    cleaned = re.sub(r"(?i)(руб(л\w*)?\.?|р\.?|₽)\s*$", "", text).strip()
    cleaned = re.sub(r"[\s ]", "", cleaned).replace(",", ".")
    if not re.fullmatch(r"\d+(\.\d+)?", cleaned):
        raise LimitInputError("Не получилось разобрать сумму. Пример: 15000 (или «нет», чтобы снять лимит)")
    try:
        value = Decimal(cleaned).quantize(Decimal("0.01"))
    except InvalidOperation:
        raise LimitInputError("Не получилось разобрать сумму. Пример: 15000")
    if value == 0:
        return None
    if value > MAX_LIMIT:
        raise LimitInputError("Слишком большая сумма для месячного лимита.")
    return value


async def list_limitable_categories(session: AsyncSession) -> List[Category]:
    stmt = (
        select(Category)
        .where(Category.type == "expense", Category.name.not_in(list(TRANSFER_CATEGORY_NAMES)))
        .order_by(Category.name)
    )
    return list((await session.execute(stmt)).scalars().all())


async def get_user_limits(session: AsyncSession, user_id: int) -> Dict[int, Decimal]:
    """{category_id: месячный лимит} пользователя."""
    rows = (
        await session.execute(
            select(CategoryLimit.category_id, CategoryLimit.monthly_limit).where(CategoryLimit.user_id == user_id)
        )
    ).all()
    return {cid: Decimal(str(limit)) for cid, limit in rows}


async def get_limits_overview(session: AsyncSession, user_id: int) -> List[Dict[str, Any]]:
    """Категории с лимитом и расходом пользователя за текущий месяц."""
    today = local_today()
    start = month_start(today.year, today.month)
    end = month_start(*next_month(today.year, today.month))

    spent_rows = (
        await session.execute(
            select(Transaction.category_id, func.sum(-Transaction.amount))
            .where(
                Transaction.user_id == user_id,
                Transaction.amount < 0,
                Transaction.category_id.is_not(None),
                Transaction.date >= start,
                Transaction.date < end,
            )
            .group_by(Transaction.category_id)
        )
    ).all()
    spent = {cid: Decimal(str(total)) for cid, total in spent_rows}
    limits = await get_user_limits(session, user_id)

    result = []
    for cat in await list_limitable_categories(session):
        limit = limits.get(cat.id)
        cat_spent = spent.get(cat.id, Decimal("0"))
        percent = int(cat_spent / limit * 100) if limit else None
        result.append(
            {
                "id": cat.id,
                "name": cat.name,
                "is_mandatory": cat.is_mandatory,
                "monthly_limit": limit,
                "spent": cat_spent,
                "percent": percent,
            }
        )
    return result


async def set_category_limit(
    session: AsyncSession, user_id: int, category_id: int, limit: Optional[Decimal]
) -> Optional[Category]:
    """Ставит (или снимает при None) лимит пользователя. Возвращает категорию или None, если она не подходит."""
    cat = await session.get(Category, category_id)
    if cat is None or cat.type != "expense" or cat.name in TRANSFER_CATEGORY_NAMES:
        return None

    existing = (
        await session.execute(
            select(CategoryLimit).where(CategoryLimit.user_id == user_id, CategoryLimit.category_id == category_id)
        )
    ).scalar_one_or_none()
    if limit is None:
        if existing is not None:
            await session.delete(existing)
    elif existing is not None:
        existing.monthly_limit = limit
    else:
        session.add(CategoryLimit(user_id=user_id, category_id=category_id, monthly_limit=limit))
    await session.commit()
    return cat
