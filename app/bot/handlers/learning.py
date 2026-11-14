import logging
from typing import Optional
from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.handlers.base import get_or_create_user
from app.bot.keyboards import build_categories_keyboard
from app.models import Category, Transaction
from app.finance.services.categorizer import extract_smart_keyword, learn_from_correction, seed_default_categories

logger = logging.getLogger(__name__)
router = Router(name="learning_router")


async def _fetch_next_uncategorized(
    session: AsyncSession, user_id: int, exclude_tx_id: Optional[int] = None
) -> Optional[Transaction]:
    """Ищет одну неразобранную транзакцию пользователя."""
    stmt = (
        select(Transaction)
        .where(
            Transaction.user_id == user_id,
            Transaction.category_id.is_(None),
        )
        .order_by(Transaction.date.desc())
        .limit(1)
    )
    if exclude_tx_id is not None:
        stmt = stmt.where(Transaction.id != exclude_tx_id)

    res = await session.execute(stmt)
    tx = res.scalar_one_or_none()
    if tx:
        return tx

    # Если операций с category_id=None больше нет, проверяем is_parsed_automatically=False без категории
    stmt_fallback = (
        select(Transaction)
        .where(
            Transaction.user_id == user_id,
            Transaction.is_parsed_automatically.is_(False),
            Transaction.category_id.is_(None),
        )
        .order_by(Transaction.date.desc())
        .limit(1)
    )
    return (await session.execute(stmt_fallback)).scalar_one_or_none()


def _format_tx_for_learning(tx: Transaction) -> str:
    sign = "+" if tx.amount > 0 else ""
    suggested_kw = extract_smart_keyword(tx.description)
    return (
        "🧐 <b>Нераспознанная операция:</b>\n\n"
        f"📅 Дата: <b>{tx.date.strftime('%d.%m.%Y %H:%M')}</b>\n"
        f"🏦 Банк: <b>{tx.bank_name}</b>\n"
        f"💰 Сумма: <b>{sign}{tx.amount} ₽</b>\n"
        f"📝 Описание: <code>{tx.description}</code>\n"
        f"🔑 Ключевое слово для запоминания: <code>{suggested_kw}</code>\n\n"
        "👇 <b>Выберите подходящую категорию</b> (парсер запомнит её для всех будущих выписок):"
    )


@router.message(Command("uncategorized"))
async def cmd_uncategorized(message: Message, session: AsyncSession) -> None:
    if not message.from_user:
        return

    user = await get_or_create_user(session, message.from_user.id)
    await seed_default_categories(session)

    tx = await _fetch_next_uncategorized(session, user.id)
    if not tx:
        await message.answer("🎉 Все ваши транзакции уже распределены по категориям!")
        return

    cats = list((await session.execute(select(Category).order_by(Category.type, Category.name))).scalars().all())
    await message.answer(
        _format_tx_for_learning(tx),
        parse_mode="HTML",
        reply_markup=build_categories_keyboard(tx.id, cats),
    )


@router.callback_query(F.data == "next_uncategorized")
async def cb_next_uncategorized(callback: CallbackQuery, session: AsyncSession) -> None:
    if not callback.from_user or not callback.message:
        return

    user = await get_or_create_user(session, callback.from_user.id)
    tx = await _fetch_next_uncategorized(session, user.id)
    if not tx:
        await callback.message.edit_text("🎉 Все транзакции успешно распределены по категориям!")
        await callback.answer()
        return

    cats = list((await session.execute(select(Category).order_by(Category.type, Category.name))).scalars().all())
    await callback.message.edit_text(
        _format_tx_for_learning(tx),
        parse_mode="HTML",
        reply_markup=build_categories_keyboard(tx.id, cats),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("skip_learn:"))
async def cb_skip_learn(callback: CallbackQuery, session: AsyncSession) -> None:
    if not callback.from_user or not callback.message or not callback.data:
        return

    skipped_id = int(callback.data.split(":")[1])
    user = await get_or_create_user(session, callback.from_user.id)
    tx = await _fetch_next_uncategorized(session, user.id, exclude_tx_id=skipped_id)

    if not tx:
        await callback.message.edit_text("✅ Больше неразобранных операций нет.")
        await callback.answer("Пропущено")
        return

    cats = list((await session.execute(select(Category).order_by(Category.type, Category.name))).scalars().all())
    await callback.message.edit_text(
        _format_tx_for_learning(tx),
        parse_mode="HTML",
        reply_markup=build_categories_keyboard(tx.id, cats),
    )
    await callback.answer("Показана следующая операция")


@router.callback_query(F.data.startswith("learn:"))
async def cb_learn_category(callback: CallbackQuery, session: AsyncSession) -> None:
    """Обработчик выбора категории: обучает парсер и переходит к следующей операции."""
    if not callback.from_user or not callback.message or not callback.data:
        return

    try:
        _, tx_id_str, cat_id_str = callback.data.split(":")
        tx_id = int(tx_id_str)
        cat_id = int(cat_id_str)
    except ValueError:
        await callback.answer("Некорректные данные кнопки", show_alert=True)
        return

    user = await get_or_create_user(session, callback.from_user.id)
    try:
        learn_res = await learn_from_correction(
            session=session,
            transaction_id=tx_id,
            new_category_id=cat_id,
            user_id=user.id,
        )
    except Exception as e:
        logger.exception("Ошибка при обучении категоризатора: %s", e)
        await callback.answer(f"Ошибка сохранения: {e}", show_alert=True)
        return

    summary_header = (
        f"✅ Категория изменена на <b>«{learn_res['new_category_name']}»</b>.\n"
        f"🧠 Запомнено слово: <code>{learn_res['learned_keyword']}</code>\n"
        f"🔄 Автоматически обновлено похожих операций: <b>{learn_res['similar_transactions_updated']}</b>\n"
        "────────────────────\n"
    )

    next_tx = await _fetch_next_uncategorized(session, user.id)

    if next_tx:
        cats = list((await session.execute(select(Category).order_by(Category.type, Category.name))).scalars().all())
        await callback.message.edit_text(
            summary_header + _format_tx_for_learning(next_tx),
            parse_mode="HTML",
            reply_markup=build_categories_keyboard(next_tx.id, cats),
        )
    else:
        await callback.message.edit_text(
            summary_header + "🎉 <b>Все неразобранные операции успешно размечены!</b>",
            parse_mode="HTML",
        )

    await callback.answer("Парсер обучен!")