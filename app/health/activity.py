"""Суточная активность из HA и персональные вечерние напоминания."""
from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import async_session_maker
from app.health.models import ActivityLog
from app.notifications.sender import send_telegram_message
from app.users.models import User


def local_today() -> date:
    return datetime.now(ZoneInfo(settings.ACTIVITY_TZ)).date()


async def upsert_activity(
    session: AsyncSession, user_id: int, day: date, steps: int, active_calories: int,
    total_calories: Decimal | None = None,
) -> ActivityLog:
    """Атомарный UPSERT: повторы и запоздалые меньшие показания не уменьшают итог."""
    stmt = pg_insert(ActivityLog).values(
        user_id=user_id, date=day, steps=steps, active_calories=active_calories, total_calories=total_calories
    )
    updates = {
        "steps": func.greatest(ActivityLog.steps, stmt.excluded.steps),
        "active_calories": func.greatest(ActivityLog.active_calories, stmt.excluded.active_calories),
    }
    if total_calories is not None:
        updates["total_calories"] = func.greatest(func.coalesce(ActivityLog.total_calories, 0), stmt.excluded.total_calories)
    stmt = stmt.on_conflict_do_update(
        index_elements=["user_id", "date"],
        set_=updates,
    ).returning(ActivityLog)
    log = (await session.execute(stmt, execution_options={"populate_existing": True})).scalar_one()
    await session.commit()
    return log


async def send_activity_nudges() -> None:
    """Без сегодняшней телеметрии активность неизвестна — прогулку не предлагаем."""
    if not settings.ACTIVITY_NUDGE_ENABLED:
        return
    async with async_session_maker() as session:
        rows = (await session.execute(
            select(User.telegram_id, ActivityLog.steps)
            .join(ActivityLog, ActivityLog.user_id == User.id)
            .where(ActivityLog.date == local_today(), ActivityLog.steps < settings.ACTIVITY_NUDGE_STEPS)
        )).all()
    for telegram_id, steps in rows:
        await send_telegram_message(
            telegram_id,
            f"🚶 Твоя активность сегодня ниже цели ({steps} шагов). "
            "Отличный повод для вечерней прогулки!",
        )
