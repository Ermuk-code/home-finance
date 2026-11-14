"""Админ-раздел бота: «Аналитика пользователей» (общая сводка по всем участникам). Только role='admin'."""
from datetime import datetime, timedelta
from html import escape

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.handlers.base import get_or_create_user
from app.bot.keyboards import BTN_ADMIN_STATS
from app.finance.services.reports import fmt_money, summarize_transactions
from app.models import ROLE_ADMIN, Category, Transaction, User, WeightLog

router = Router(name="admin_router")

STATS_DAYS = 30


@router.message(Command("admin_stats"))
@router.message(F.text == BTN_ADMIN_STATS)
async def cmd_admin_stats(message: Message, session: AsyncSession) -> None:
    if not message.from_user:
        return
    caller = await get_or_create_user(session, message.from_user.id)
    if caller.role != ROLE_ADMIN:
        await message.answer("⛔ Раздел доступен только администратору.")
        return

    since = datetime.now() - timedelta(days=STATS_DAYS)
    users = (await session.execute(select(User).order_by(User.id.asc()))).scalars().all()

    blocks = [f"👥 <b>Аналитика пользователей</b> (финансы — за {STATS_DAYS} дн.)\n"]
    for user in users:
        rows = (
            await session.execute(
                select(Category.name, Transaction.amount, Category.type)
                .outerjoin(Category, Transaction.category_id == Category.id)
                .where(Transaction.user_id == user.id, Transaction.date >= since)
            )
        ).all()
        summary = summarize_transactions(rows)

        last_weight = (
            await session.execute(
                select(WeightLog)
                .where(WeightLog.user_id == user.id)
                .order_by(WeightLog.timestamp.desc(), WeightLog.id.desc())
                .limit(1)
            )
        ).scalars().first()
        weight_text = (
            f"{last_weight.weight_kg:.1f} кг ({last_weight.timestamp.strftime('%d.%m')})" if last_weight else "нет данных"
        )

        net = summary["net"]
        sign = "+" if net >= 0 else "−"
        blocks.append(
            f"<b>{escape(user.name)}</b> · {user.role}\n"
            f"  💰 Доход: {fmt_money(summary['clean_income'])} ₽\n"
            f"  🧾 Расход: {fmt_money(summary['clean_expense'])} ₽\n"
            f"  📈 Баланс: {sign}{fmt_money(abs(net))} ₽\n"
            f"  ⚖️ Вес: {weight_text}\n"
        )
    await message.answer("\n".join(blocks), parse_mode="HTML")
