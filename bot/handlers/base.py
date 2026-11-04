from datetime import datetime, timedelta
from decimal import Decimal
from aiogram import Router
from aiogram.filters import Command, CommandStart
from aiogram.types import Message
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import Category, Transaction, User
from services.categorizer import seed_default_categories

router = Router(name="base_router")


async def get_or_create_user(session: AsyncSession, telegram_id: int) -> User:
    """Проверяет наличие пользователя в БД по telegram_id и создает его при отсутствии."""
    result = await session.execute(select(User).where(User.telegram_id == telegram_id))
    user = result.scalar_one_or_none()
    if not user:
        user = User(telegram_id=telegram_id)
        session.add(user)
        await session.commit()
        await session.refresh(user)
    return user


@router.message(CommandStart())
@router.message(Command("help"))
async def cmd_start_help(message: Message, session: AsyncSession) -> None:
    if not message.from_user:
        return

    await get_or_create_user(session, message.from_user.id)
    await seed_default_categories(session)

    text = (
        "👋 <b>Добро пожаловать в Домашний сервис контроля денежных средств!</b>\n\n"
        "📎 <b>Как загрузить данные:</b>\n"
        "Просто отправьте мне в чат файл выписки (<code>.pdf</code>, <code>.csv</code>) "
        "от <b>СберБанка, Альфа-Банка или Т-Банка</b> (включая накопительные счета), "
        "либо файл брокерского отчета (<code>.csv</code>, <code>.xlsx</code>, <code>.pdf</code>).\n\n"
        "📋 <b>Доступные команды:</b>\n"
        "• /report — Сводка доходов и расходов за 30 дней\n"
        "• /uncategorized — Обучить парсер на неразобранных операциях\n"
        "• /portfolio — Инвестиционная аналитика (MOEX) и ребалансировка\n"
        "• /export_portfolio — Скачать PDF-сводку портфеля для нейросетей\n"
        "• /help — Показать эту справку"
    )
    await message.answer(text, parse_mode="HTML")


@router.message(Command("report"))
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

    total_income = Decimal("0.00")
    total_expense = Decimal("0.00")
    by_category: dict[str, Decimal] = {}

    for cat_name, amount in rows:
        label = cat_name or "❓ Неразобранное"
        if amount > 0:
            total_income += amount
        else:
            total_expense += abs(amount)
            by_category[label] = by_category.get(label, Decimal("0.00")) + abs(amount)

    sorted_expenses = sorted(by_category.items(), key=lambda x: x[1], reverse=True)

    lines = [
        "📊 <b>Финансовая сводка за последние 30 дней:</b>\n",
        f"🟢 Доходы / Пополнения: <b>+{total_income} ₽</b>",
        f"🔴 Расходы / Списания: <b>-{total_expense} ₽</b>\n",
        "<b>Структура расходов по категориям:</b>",
    ]
    for cat_label, total in sorted_expenses:
        share = (total / total_expense * Decimal("100")).quantize(Decimal("0.1")) if total_expense > 0 else Decimal("0")
        lines.append(f"▫️ {cat_label}: <b>{total} ₽</b> ({share}%)")

    await message.answer("\n".join(lines), parse_mode="HTML")