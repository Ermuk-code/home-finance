"""Синхронизация питания: FatSecret -> daily_nutrition (UPSERT) для всех привязанных пользователей."""
import logging
from datetime import date, datetime
from decimal import Decimal
from html import escape
from typing import Optional
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import async_session_maker
from app.health.fatsecret_client import DayTotals, FatSecretError, fetch_day_totals, is_configured
from app.health.models import DailyNutrition
from app.notifications.sender import send_telegram_message
from app.users.models import User

logger = logging.getLogger(__name__)

_ONE_DECIMAL = Decimal("0.1")


def local_today() -> date:
    return datetime.now(ZoneInfo(settings.FATSECRET_TZ)).date()


async def upsert_nutrition(session: AsyncSession, user_id: int, day: date, totals: DayTotals) -> None:
    """Одна строка на (user_id, date): повторный запуск обновляет цифры, а не плодит дубли."""
    values = {
        "calories": totals.calories,
        "protein": totals.protein.quantize(_ONE_DECIMAL),
        "fat": totals.fat.quantize(_ONE_DECIMAL),
        "carbs": totals.carbs.quantize(_ONE_DECIMAL),
    }
    stmt = pg_insert(DailyNutrition).values(user_id=user_id, date=day, **values)
    stmt = stmt.on_conflict_do_update(index_elements=["user_id", "date"], set_=values)
    await session.execute(stmt)
    await session.commit()


async def sync_user(session: AsyncSession, user: User, day: Optional[date] = None) -> DayTotals:
    """Скачивает КБЖУ пользователя за день и сохраняет. Бросает FatSecretError при сбое."""
    if not user.fs_access_token or not user.fs_access_secret:
        raise FatSecretError("Аккаунт FatSecret не привязан.")
    day = day or local_today()
    totals = await fetch_day_totals(user.fs_access_token, user.fs_access_secret, day)
    await upsert_nutrition(session, user.id, day, totals)
    return totals


def format_day_summary(name: str, totals: DayTotals) -> str:
    return (
        f"🍎 Итоги питания за день ({escape(name)}): {totals.calories} ккал. "
        f"Белки: {totals.protein:.0f} г, Жиры: {totals.fat:.0f} г, Углеводы: {totals.carbs:.0f} г"
    )


async def sync_all_users(notify_summary: bool = False) -> None:
    """Задача планировщика: обходит всех пользователей с токеном FatSecret. Сбой одного не мешает остальным."""
    if not is_configured():
        logger.debug("FatSecret не настроен — синхронизация питания пропущена.")
        return
    async with async_session_maker() as session:
        users = (await session.execute(select(User).where(User.fs_access_token.is_not(None)))).scalars().all()
        for user in users:
            try:
                totals = await sync_user(session, user)
                logger.info(
                    "FatSecret: user_id=%s %s ккал (записей: %s)", user.id, totals.calories, totals.entries
                )
                if notify_summary and totals.calories > 0:
                    await send_telegram_message(user.telegram_id, format_day_summary(user.name, totals))
            except Exception as e:  # noqa: BLE001
                await session.rollback()
                logger.warning("FatSecret: не удалось обновить питание user_id=%s: %s", user.id, e)
