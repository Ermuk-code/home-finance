from datetime import datetime, timedelta
from html import escape
from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.bot.keyboards import BTN_REPORT, BTN_WEB, build_main_menu
from app.core.config import settings
from app.core.security import create_login_token, get_telegram_user, LOGIN_LINK_EXPIRE_MINUTES
from app.models import ROLE_ADMIN, Category, Transaction, User
from app.finance.services.categorizer import seed_default_categories
from app.finance.services.reports import format_report, summarize_transactions

router = Router(name="base_router")


async def get_or_create_user(session: AsyncSession, telegram_id: int) -> User:
    """Возвращает зарегистрированного пользователя по telegram_id.

    Имя сохранено ради совместимости: пользователей больше не создаём «на лету» — доступ только по
    белому списку (таблица users). Middleware уже отсекает чужих, поэтому ошибка здесь — защита «на всякий случай».
    """
    user = await get_telegram_user(session, telegram_id)
    if user is None:
        raise PermissionError(f"Telegram ID {telegram_id} не зарегистрирован в таблице users")
    return user


@router.message(CommandStart())
@router.message(Command("help"))
async def cmd_start_help(message: Message, session: AsyncSession) -> None:
    if not message.from_user:
        return

    user = await get_or_create_user(session, message.from_user.id)
    await seed_default_categories(session)

    text = (
        f"👋 <b>Привет, {escape(user.name)}! Это Life OS — семейные финансы и здоровье.</b>\n\n"
        "📎 <b>Как загрузить данные:</b>\n"
        "Просто отправьте мне в чат файл выписки (<code>.pdf</code>, <code>.csv</code>) "
        "от <b>СберБанка, Альфа-Банка или Т-Банка</b> (включая накопительные счета), "
        "либо файл брокерского отчета (<code>.csv</code>, <code>.xlsx</code>, <code>.pdf</code>).\n\n"
        "📋 <b>Доступные команды:</b>\n"
        "• /report — Чистый доход, расходы и их структура за 30 дней\n"
        "• /add — Добавить операцию вручную (наличные или любая карта)\n"
        "• /cash — Баланс наличных и последние операции\n"
        "• /add_deposit — Добавить вклад или накопительный счёт\n"
        "• /limits — Месячные лимиты по категориям и текущие траты\n"
        "• /set_limit — Задать или снять лимит на категорию\n"
        "• /uncategorized — Обучить парсер на неразобранных операциях\n"
        "• /portfolio — Инвестиционная аналитика (MOEX) и ребалансировка\n"
        "• /export_portfolio — Скачать PDF-сводку портфеля для нейросетей\n"
        "• /weight — Последние замеры веса\n"
        "• /link_fatsecret — Привязать FatSecret (питание)\n"
        "• /nutrition — КБЖУ за сегодня\n"
        "• /unlink_fatsecret — Отвязать FatSecret\n"
        "• /web — Ссылка для входа в веб-панель\n"
        "• /help — Показать эту справку"
    )
    if user.role == ROLE_ADMIN:
        text += "\n\n👑 <b>Администратор:</b>\n• /admin_stats — Аналитика пользователей (общая сводка)"
    await message.answer(text, parse_mode="HTML", reply_markup=build_main_menu(user.role))


@router.message(Command("report"))
@router.message(F.text == BTN_REPORT)
async def cmd_report(message: Message, session: AsyncSession) -> None:
    """Выводит сводку доходов и расходов по категориям за последние 30 дней."""
    if not message.from_user:
        return

    user = await get_or_create_user(session, message.from_user.id)
    since_date = datetime.now() - timedelta(days=30)

    stmt = (
        select(
            Category.name,
            Transaction.amount,
            Category.type,
        )
        .outerjoin(Category, Transaction.category_id == Category.id)
        .where(
            Transaction.user_id == user.id,
            Transaction.date >= since_date,
        )
    )
    rows = (await session.execute(stmt)).all()

    if not rows:
        await message.answer(
            "📭 За последние 30 дней транзакций не найдено.\n"
            "Отправьте мне PDF или CSV выписку из банка!"
        )
        return

    await message.answer(format_report(summarize_transactions(rows)), parse_mode="HTML")


@router.message(Command("web"))
@router.message(F.text == BTN_WEB)
async def cmd_web(message: Message, session: AsyncSession) -> None:
    """Ссылка для входа в веб-панель под своим профилем (живёт несколько минут)."""
    if not message.from_user:
        return
    user = await get_or_create_user(session, message.from_user.id)
    link = f"{settings.WEB_BASE_URL.rstrip('/')}/api/login/telegram?token={create_login_token(user.id)}"
    await message.answer(
        f"🌐 <b>Вход в веб-панель</b> как {escape(user.name)}:\n{link}\n\n"
        f"Ссылка действует {LOGIN_LINK_EXPIRE_MINUTES} минут, никому её не пересылайте.",
        parse_mode="HTML",
        disable_web_page_preview=True,
    )
