"""Здоровье в боте: последние замеры веса (только свои)."""
from html import escape

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.handlers.base import get_or_create_user
from app.bot.keyboards import BTN_WEIGHT
from app.models import WeightLog

router = Router(name="health_router")


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
