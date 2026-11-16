"""Здоровье в боте: вес и активность (только свои данные)."""
from html import escape
from datetime import timedelta
import httpx

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.handlers.base import get_or_create_user
from app.bot.keyboards import BTN_ABS_FORECAST, BTN_ACTIVITY, BTN_HEALTH_STATS, BTN_WEIGHT
from app.core.security import create_access_token
from app.health.abs_forecast import AbsForecastResponse, format_abs_forecast
from app.health.analytics import HealthAnalytics, format_weekly_summary
from app.health.activity import local_today
from app.models import ActivityLog, WeightLog

router = Router(name="health_router")


@router.message(Command('abs_forecast'))
@router.message(F.text == BTN_ABS_FORECAST)
async def cmd_abs_forecast(message: Message, session: AsyncSession) -> None:
    if not message.from_user:
        return
    user = await get_or_create_user(session, message.from_user.id)
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get('http://127.0.0.1:8000/api/v1/health/forecast-abs',
                                        headers={'Authorization': 'Bearer ' + create_access_token(user.id)})
            response.raise_for_status()
            result = AbsForecastResponse.model_validate(response.json())
    except (httpx.HTTPError, ValueError):
        await message.answer('Не удалось получить прогноз. Попробуйте позже или откройте страницу «Здоровье».')
        return
    await message.answer(format_abs_forecast(result), parse_mode='HTML')


@router.message(Command("stats"))
@router.message(F.text == BTN_HEALTH_STATS)
async def cmd_stats(message: Message, session: AsyncSession) -> None:
    if not message.from_user:
        return
    user = await get_or_create_user(session, message.from_user.id)
    end = local_today() - timedelta(days=1)
    summary = await HealthAnalytics(session).get_summary(user.id, end - timedelta(days=6), end)
    await message.answer(format_weekly_summary(summary), parse_mode="HTML")


@router.message(Command("activity"))
@router.message(F.text == BTN_ACTIVITY)
async def cmd_activity(message: Message, session: AsyncSession) -> None:
    if not message.from_user:
        return
    user = await get_or_create_user(session, message.from_user.id)
    log = (await session.execute(
        select(ActivityLog).where(ActivityLog.user_id == user.id, ActivityLog.date == local_today())
    )).scalar_one_or_none()
    if log is None:
        await message.answer("🚶 За сегодня данных об активности пока нет. Ожидаем синхронизацию Home Assistant.")
        return
    await message.answer(
        f"🚶 <b>Активность за сегодня</b>\nШаги: <b>{log.steps}</b>\n"
        f"Активные калории: <b>{log.active_calories} ккал</b>\n"
        "По последней синхронизации Home Assistant; 0 ккал может означать отсутствие данных.",
        parse_mode="HTML",
    )


@router.message(Command("weight"))
@router.message(F.text == BTN_WEIGHT)
async def cmd_weight(message: Message, session: AsyncSession) -> None:
    if not message.from_user:
        return
    user = await get_or_create_user(session, message.from_user.id)
    rows = (
        await session.execute(
            select(WeightLog)
            .where(WeightLog.user_id == user.id)
            .order_by(WeightLog.timestamp.desc(), WeightLog.id.desc())
            .limit(5)
        )
    ).scalars().all()

    if not rows:
        await message.answer("⚖️ Замеров веса пока нет. Они появятся, когда весы пришлют данные через Home Assistant.")
        return

    lines = [f"⚖️ <b>Вес — {escape(user.name)}</b>\n"]
    for w in rows:
        bmi = f" · ИМТ {w.bmi}" if w.bmi is not None else ""
        lines.append(f"• {w.timestamp.strftime('%d.%m.%Y')}: <b>{w.weight_kg:.1f} кг</b>{bmi}")
    await message.answer("\n".join(lines), parse_mode="HTML")
