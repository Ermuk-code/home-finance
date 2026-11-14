"""Пошаговое добавление вклада или накопительного счёта через Telegram (/add_deposit)."""
import logging
from datetime import date, datetime, time, timezone
from decimal import Decimal
from typing import Optional

from aiogram import F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.handlers.base import get_or_create_user
from app.bot.keyboards import (
    build_deposit_capitalization_kb,
    build_deposit_kind_kb,
    build_deposit_today_kb,
)
from app.models import Deposit
from app.finance.services.deposits import (
    KIND_DEPOSIT,
    KIND_SAVINGS,
    DepositInputError,
    describe_deposit,
    parse_date_input,
    parse_decimal_input,
    parse_term_input,
    validate_deposit_input,
)
from app.finance.services.reports import format_deposit_summary
from app.finance.services.scheduler import create_deposit_end_reminder

logger = logging.getLogger(__name__)
router = Router(name="deposits_router")

CANCEL_HINT = "\n\n<i>Отменить: /cancel</i>"


class DepositForm(StatesGroup):
    kind = State()
    bank = State()
    title = State()
    amount = State()
    rate = State()
    opened_at = State()
    term = State()
    capitalization = State()


# ---------------------------------------------------------------------------
# Старт и отмена
# ---------------------------------------------------------------------------
@router.message(Command("add_deposit"))
async def cmd_add_deposit(message: Message, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(DepositForm.kind)
    await message.answer(
        "🏦 <b>Новый вклад или накопительный счёт</b>\n\nЧто добавляем?",
        parse_mode="HTML",
        reply_markup=build_deposit_kind_kb(),
    )


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    if await state.get_state() is None:
        await message.answer("Сейчас нечего отменять.")
        return
    await state.clear()
    await message.answer("❌ Добавление отменено.")


@router.callback_query(F.data == "dep_cancel")
async def cb_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.answer("Отменено")
    if callback.message:
        await callback.message.edit_text("❌ Добавление отменено.")


# ---------------------------------------------------------------------------
# Шаги диалога
# ---------------------------------------------------------------------------
@router.callback_query(StateFilter(DepositForm.kind), F.data.startswith("dep_kind:"))
async def cb_kind(callback: CallbackQuery, state: FSMContext) -> None:
    kind = (callback.data or "").split(":", 1)[1]
    if kind not in (KIND_DEPOSIT, KIND_SAVINGS):
        await callback.answer("Неизвестный тип", show_alert=True)
        return
    await state.update_data(kind=kind)
    await state.set_state(DepositForm.bank)
    await callback.answer()
    if callback.message:
        label = "Вклад" if kind == KIND_DEPOSIT else "Накопительный счёт"
        await callback.message.edit_text(f"Выбрано: <b>{label}</b>", parse_mode="HTML")
        await callback.message.answer(
            "В каком банке? Например: <code>Сбер</code>, <code>Т-Банк</code>, <code>Альфа</code>" + CANCEL_HINT,
            parse_mode="HTML",
        )


async def _text_or_hint(message: Message) -> Optional[str]:
    """Возвращает текст шага или None (если пришла неизвестная команда / не текст)."""
    text = (message.text or "").strip()
    if not text or text.startswith("/"):
        await message.answer("Введите значение текстом или отмените: /cancel")
        return None
    return text


@router.message(StateFilter(DepositForm.bank), F.text)
async def step_bank(message: Message, state: FSMContext) -> None:
    text = await _text_or_hint(message)
    if text is None:
        return
    if len(text) > 80:
        await message.answer("Слишком длинное название банка (максимум 80 символов).")
        return
    await state.update_data(bank_name=text)
    await state.set_state(DepositForm.title)
    await message.answer(
        "Как назвать продукт? Например: <code>Весенний</code> или <code>Накопительный Максимум</code>" + CANCEL_HINT,
        parse_mode="HTML",
    )


@router.message(StateFilter(DepositForm.title), F.text)
async def step_title(message: Message, state: FSMContext) -> None:
    text = await _text_or_hint(message)
    if text is None:
        return
    if len(text) > 120:
        await message.answer("Слишком длинное название (максимум 120 символов).")
        return
    await state.update_data(title=text)
    data = await state.get_data()
    await state.set_state(DepositForm.amount)
    prompt = "Сумма вклада, ₽?" if data.get("kind") == KIND_DEPOSIT else "Текущий остаток на счёте, ₽?"
    await message.answer(prompt + " Например: <code>300000</code>" + CANCEL_HINT, parse_mode="HTML")


@router.message(StateFilter(DepositForm.amount), F.text)
async def step_amount(message: Message, state: FSMContext) -> None:
    text = await _text_or_hint(message)
    if text is None:
        return
    try:
        amount = parse_decimal_input(text)
        if amount <= 0:
            raise DepositInputError("Сумма должна быть больше нуля.")
    except DepositInputError as exc:
        await message.answer(f"⚠️ {exc}")
        return
    await state.update_data(amount=str(amount))
    await state.set_state(DepositForm.rate)
    await message.answer(
        "Ставка, % годовых? Например: <code>16,5</code>" + CANCEL_HINT, parse_mode="HTML"
    )


@router.message(StateFilter(DepositForm.rate), F.text)
async def step_rate(message: Message, state: FSMContext, session: AsyncSession) -> None:
    text = await _text_or_hint(message)
    if text is None:
        return
    try:
        rate = parse_decimal_input(text)
        if rate > 100:
            raise DepositInputError("Ставка должна быть от 0 до 100% годовых.")
    except DepositInputError as exc:
        await message.answer(f"⚠️ {exc}")
        return
    await state.update_data(interest_rate=str(rate))
    data = await state.get_data()

    if data.get("kind") == KIND_SAVINGS:
        # Для накопительного счёта дата и срок не нужны
        await _finish(message, state, session, message.from_user.id if message.from_user else 0)
        return

    await state.set_state(DepositForm.opened_at)
    await message.answer(
        "Дата открытия вклада? Формат <code>ДД.ММ.ГГГГ</code> или нажмите кнопку." + CANCEL_HINT,
        parse_mode="HTML",
        reply_markup=build_deposit_today_kb(),
    )


async def _ask_term(target: Message, state: FSMContext, opened: date) -> None:
    await state.update_data(opened_at=opened.isoformat())
    await state.set_state(DepositForm.term)
    await target.answer(
        f"Дата открытия: <b>{opened.strftime('%d.%m.%Y')}</b>\n\n"
        "На сколько месяцев вклад? Например: <code>6</code> или <code>12</code>" + CANCEL_HINT,
        parse_mode="HTML",
    )


@router.callback_query(StateFilter(DepositForm.opened_at), F.data == "dep_today")
async def cb_today(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    if callback.message:
        await _ask_term(callback.message, state, date.today())


@router.message(StateFilter(DepositForm.opened_at), F.text)
async def step_opened_at(message: Message, state: FSMContext) -> None:
    text = await _text_or_hint(message)
    if text is None:
        return
    try:
        opened = parse_date_input(text)
    except DepositInputError as exc:
        await message.answer(f"⚠️ {exc}")
        return
    await _ask_term(message, state, opened)


@router.message(StateFilter(DepositForm.term), F.text)
async def step_term(message: Message, state: FSMContext) -> None:
    text = await _text_or_hint(message)
    if text is None:
        return
    try:
        term = parse_term_input(text)
    except DepositInputError as exc:
        await message.answer(f"⚠️ {exc}")
        return
    await state.update_data(term_months=term)
    await state.set_state(DepositForm.capitalization)
    await message.answer(
        "Проценты прибавляются к сумме каждый месяц (капитализация)?",
        reply_markup=build_deposit_capitalization_kb(),
    )


@router.callback_query(StateFilter(DepositForm.capitalization), F.data.startswith("dep_cap:"))
async def cb_capitalization(callback: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    capitalization = (callback.data or "").endswith(":1")
    await state.update_data(capitalization=capitalization)
    await callback.answer()
    if callback.message:
        await callback.message.edit_text(
            "Капитализация: <b>да</b>" if capitalization else "Капитализация: <b>нет</b>", parse_mode="HTML"
        )
        await _finish(callback.message, state, session, callback.from_user.id)


@router.message(StateFilter(DepositForm.kind, DepositForm.capitalization), F.text)
async def step_expect_button(message: Message) -> None:
    await message.answer("Выберите вариант кнопкой выше или отмените: /cancel")


# ---------------------------------------------------------------------------
# Сохранение
# ---------------------------------------------------------------------------
async def _finish(target: Message, state: FSMContext, session: AsyncSession, telegram_id: int) -> None:
    data = await state.get_data()
    try:
        opened_at = date.fromisoformat(data["opened_at"]) if data.get("opened_at") else None
        amount = Decimal(data["amount"])
        rate = Decimal(data["interest_rate"])
        fields = validate_deposit_input(
            kind=data.get("kind", KIND_DEPOSIT),
            amount=amount,
            interest_rate=rate,
            opened_at=opened_at,
            term_months=data.get("term_months"),
            capitalization=bool(data.get("capitalization")),
        )
    except (DepositInputError, KeyError, ValueError) as exc:
        logger.warning("Не удалось собрать вклад из диалога: %s", exc)
        await state.clear()
        await target.answer("⚠️ Что-то пошло не так, данные не сохранены. Начните заново: /add_deposit")
        return

    user = await get_or_create_user(session, telegram_id)

    end_dt = None
    if fields["end_date"]:
        end_dt = datetime.combine(fields["end_date"], time.min, tzinfo=timezone.utc)

    dep = Deposit(
        user_id=user.id,
        bank_name=data["bank_name"],
        title=data["title"],
        kind=fields["kind"],
        amount=amount,
        interest_rate=rate,
        opened_at=fields["opened_at"],
        term_months=fields["term_months"],
        capitalization=fields["capitalization"],
        end_date=end_dt,
    )
    session.add(dep)
    await session.flush()
    await create_deposit_end_reminder(session, dep)  # напоминание за день до окончания срока
    await session.commit()
    await session.refresh(dep)
    await state.clear()

    await target.answer(format_deposit_summary(describe_deposit(dep)), parse_mode="HTML")


# ---------------------------------------------------------------------------
# Устаревшие кнопки (бот перезапускался или диалог уже завершён)
# ---------------------------------------------------------------------------
@router.callback_query(F.data.startswith("dep_"))
async def cb_stale(callback: CallbackQuery) -> None:
    await callback.answer("Эта кнопка устарела. Начните заново: /add_deposit", show_alert=True)
