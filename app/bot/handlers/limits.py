"""Месячные лимиты по категориям: /limits (просмотр) и /set_limit (установка)."""
import logging
from html import escape

from aiogram import F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.handlers.base import get_or_create_user
from app.bot.keyboards import BTN_LIMITS, build_limit_categories_kb
from app.models import Category
from app.finance.services.categorizer import seed_default_categories
from app.finance.services.limits import (
    LimitInputError,
    get_limits_overview,
    get_user_limits,
    list_limitable_categories,
    parse_limit_input,
    set_category_limit,
)
from app.finance.services.reports import fmt_money

logger = logging.getLogger(__name__)
router = Router(name="limits_router")

CANCEL_HINT = "\n\n<i>Отменить: /cancel</i>"


class LimitForm(StatesGroup):
    amount = State()


@router.message(Command("limits"))
@router.message(F.text == BTN_LIMITS)
async def cmd_limits(message: Message, session: AsyncSession) -> None:
    if not message.from_user:
        return
    await seed_default_categories(session)
    user = await get_or_create_user(session, message.from_user.id)
    rows = await get_limits_overview(session, user.id)

    with_limit = [r for r in rows if r["monthly_limit"] is not None]
    if not with_limit:
        await message.answer(
            "🎯 Лимиты пока не заданы.\nУстановить: /set_limit — бот предупредит, когда траты дойдут до 80% лимита."
        )
        return

    lines = ["🎯 <b>Месячные лимиты</b> (траты за текущий месяц):\n"]
    for r in sorted(with_limit, key=lambda x: x["percent"] or 0, reverse=True):
        icon = "🔴" if r["percent"] >= 100 else ("🟠" if r["percent"] >= 80 else "🟢")
        lines.append(
            f"{icon} {escape(r['name'])}: <b>{fmt_money(r['spent'])}</b> из {fmt_money(r['monthly_limit'])} ₽ "
            f"({r['percent']}%)"
        )
    lines.append("\nИзменить или снять лимит: /set_limit")
    await message.answer("\n".join(lines), parse_mode="HTML")


@router.message(Command("set_limit"))
async def cmd_set_limit(message: Message, state: FSMContext, session: AsyncSession) -> None:
    await state.clear()
    if not message.from_user:
        return
    await seed_default_categories(session)
    user = await get_or_create_user(session, message.from_user.id)
    categories = await list_limitable_categories(session)
    await message.answer(
        "🎯 <b>Месячный лимит</b>\n\nДля какой категории расходов?",
        parse_mode="HTML",
        reply_markup=build_limit_categories_kb(categories, await get_user_limits(session, user.id)),
    )


@router.callback_query(F.data == "lim_cancel")
async def cb_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.answer("Отменено")
    if callback.message:
        await callback.message.edit_text("❌ Установка лимита отменена.")


@router.callback_query(F.data.startswith("lim_cat:"))
async def cb_pick_category(callback: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    try:
        category_id = int((callback.data or "").split(":", 1)[1])
    except ValueError:
        await callback.answer("Некорректная кнопка", show_alert=True)
        return
    cat = await session.get(Category, category_id)
    if cat is None:
        await callback.answer("Категория не найдена", show_alert=True)
        return
    user = await get_or_create_user(session, callback.from_user.id)
    current_limit = (await get_user_limits(session, user.id)).get(category_id)

    await state.set_state(LimitForm.amount)
    await state.update_data(category_id=category_id)
    await callback.answer()
    if callback.message:
        current = (
            f"Сейчас: <b>{fmt_money(current_limit)} ₽</b>\n" if current_limit is not None else "Сейчас лимит не задан.\n"
        )
        await callback.message.edit_text(
            f"Категория: <b>{escape(cat.name)}</b>\n{current}\n"
            "Введите месячный лимит в рублях, например <code>15000</code>.\n"
            "Чтобы снять лимит — напишите <code>нет</code>." + CANCEL_HINT,
            parse_mode="HTML",
        )


@router.message(StateFilter(LimitForm.amount), F.text)
async def step_amount(message: Message, state: FSMContext, session: AsyncSession) -> None:
    text = (message.text or "").strip()
    if text.startswith("/"):
        await message.answer("Введите сумму текстом или отмените: /cancel")
        return
    try:
        limit = parse_limit_input(text)
    except LimitInputError as exc:
        await message.answer(f"⚠️ {exc}")
        return

    if not message.from_user:
        return
    user = await get_or_create_user(session, message.from_user.id)
    data = await state.get_data()
    cat = await set_category_limit(session, user.id, data["category_id"], limit)
    await state.clear()
    if cat is None:
        await message.answer("⚠️ Для этой категории нельзя задать лимит.")
        return
    if limit is None:
        await message.answer(f"✅ Лимит на «{escape(cat.name)}» снят.", parse_mode="HTML")
    else:
        await message.answer(
            f"✅ Лимит на «{escape(cat.name)}»: <b>{fmt_money(limit)} ₽</b> в месяц.\n"
            "Предупрежу, когда траты дойдут до 80%.",
            parse_mode="HTML",
        )


@router.callback_query(F.data.startswith("lim_"))
async def cb_stale(callback: CallbackQuery) -> None:
    await callback.answer("Эта кнопка устарела. Начните заново: /set_limit", show_alert=True)
