"""Ручное добавление операций (/add) и учёт наличных (/cash)."""
import logging
from datetime import date
from decimal import Decimal
from html import escape
from typing import Optional

from aiogram import F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.handlers.base import get_or_create_user
from app.bot.keyboards import (
    build_manual_categories_kb,
    build_manual_kind_kb,
    build_manual_source_kb,
    build_manual_today_kb,
)
from app.finance.services.budget import check_category_limits
from app.finance.services.manual import (
    CARD_BANKS,
    CASH_BANK_NAME,
    KIND_EXPENSE,
    KIND_LABELS,
    KIND_WITHDRAW,
    TRANSFER_KINDS,
    ManualTxError,
    add_manual_transaction,
    clean_description,
    get_cash_balance,
    get_recent_cash_transactions,
    guess_category,
    normalize_source,
    parse_amount,
    parse_tx_date,
)
from app.finance.services.reports import fmt_money

logger = logging.getLogger(__name__)
router = Router(name="manual_router")

CANCEL_HINT = "\n\n<i>Отменить: /cancel</i>"


class ManualForm(StatesGroup):
    kind = State()
    source = State()
    source_text = State()
    amount = State()
    description = State()
    date = State()
    category = State()


def _signed(amount: Decimal) -> str:
    return f"{'+' if amount > 0 else '−'}{fmt_money(abs(amount))} ₽"


# ---------------------------------------------------------------------------
# /cash — баланс наличных
# ---------------------------------------------------------------------------
@router.message(Command("cash"))
async def cmd_cash(message: Message, session: AsyncSession) -> None:
    if not message.from_user:
        return
    user = await get_or_create_user(session, message.from_user.id)
    balance = await get_cash_balance(session, user.id)
    recent = await get_recent_cash_transactions(session, user.id)

    lines = [f"💵 <b>Наличные:</b> {fmt_money(balance)} ₽"]
    if balance < 0:
        lines.append(
            "<i>Баланс отрицательный: не хватает внесённых наличных. Добавьте «Снял наличные» "
            "или начальный остаток через /add.</i>"
        )
    if recent:
        lines.append("\n<b>Последние операции:</b>")
        for tx in recent:
            lines.append(
                f"▫️ {tx.date.strftime('%d.%m')} · {escape(tx.description[:40])} · <b>{_signed(tx.amount)}</b>"
            )
    else:
        lines.append("\nОпераций с наличными пока нет. Добавить: /add")
    await message.answer("\n".join(lines), parse_mode="HTML")


# ---------------------------------------------------------------------------
# /add — диалог
# ---------------------------------------------------------------------------
@router.message(Command("add", "add_transaction"))
async def cmd_add(message: Message, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(ManualForm.kind)
    await message.answer(
        "➕ <b>Новая операция вручную</b>\n\nЧто добавляем?",
        parse_mode="HTML",
        reply_markup=build_manual_kind_kb(),
    )


@router.callback_query(F.data == "man_cancel")
async def cb_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.answer("Отменено")
    if callback.message:
        await callback.message.edit_text("❌ Добавление отменено.")


@router.callback_query(StateFilter(ManualForm.kind), F.data.startswith("man_kind:"))
async def cb_kind(callback: CallbackQuery, state: FSMContext) -> None:
    kind = (callback.data or "").split(":", 1)[1]
    if kind not in KIND_LABELS:
        await callback.answer("Неизвестный тип", show_alert=True)
        return
    await state.update_data(kind=kind)
    await callback.answer()
    if not callback.message:
        return
    await callback.message.edit_text(f"Тип: <b>{KIND_LABELS[kind]}</b>", parse_mode="HTML")

    if kind in TRANSFER_KINDS:
        # Перемещение между картой и кошельком — источник всегда «Наличные»
        await state.update_data(source=CASH_BANK_NAME)
        await _ask_amount(callback.message, state)
    else:
        await state.set_state(ManualForm.source)
        await callback.message.answer(
            "Откуда деньги? Выберите наличные или карту:",
            reply_markup=build_manual_source_kb(CARD_BANKS),
        )


@router.callback_query(StateFilter(ManualForm.source), F.data.startswith("man_src:"))
async def cb_source(callback: CallbackQuery, state: FSMContext) -> None:
    choice = (callback.data or "").split(":", 1)[1]
    await callback.answer()
    if not callback.message:
        return

    if choice == "other":
        await state.set_state(ManualForm.source_text)
        await callback.message.edit_text("Источник: <b>другая карта</b>", parse_mode="HTML")
        await callback.message.answer(
            "Как назвать карту или банк? Например: <code>Райффайзен</code>, <code>ВТБ</code>" + CANCEL_HINT,
            parse_mode="HTML",
        )
        return

    if choice == "cash":
        source = CASH_BANK_NAME
    else:
        try:
            source = CARD_BANKS[int(choice)]
        except (ValueError, IndexError):
            await callback.message.answer("Выберите вариант кнопкой или начните заново: /add")
            return
    await state.update_data(source=source)
    await callback.message.edit_text(f"Источник: <b>{escape(source)}</b>", parse_mode="HTML")
    await _ask_amount(callback.message, state)


@router.message(StateFilter(ManualForm.source_text), F.text)
async def step_source_text(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if text.startswith("/"):
        await message.answer("Введите название текстом или отмените: /cancel")
        return
    try:
        source = normalize_source(text)
    except ManualTxError as exc:
        await message.answer(f"⚠️ {exc}")
        return
    await state.update_data(source=source)
    await _ask_amount(message, state)


async def _ask_amount(target: Message, state: FSMContext) -> None:
    await state.set_state(ManualForm.amount)
    await target.answer("Сумма в рублях? Например: <code>350</code> или <code>1250,50</code>" + CANCEL_HINT, parse_mode="HTML")


@router.message(StateFilter(ManualForm.amount), F.text)
async def step_amount(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if text.startswith("/"):
        await message.answer("Введите сумму текстом или отмените: /cancel")
        return
    try:
        amount = parse_amount(text)
    except ManualTxError as exc:
        await message.answer(f"⚠️ {exc}")
        return
    await state.update_data(amount=str(amount))
    await state.set_state(ManualForm.description)

    data = await state.get_data()
    if data["kind"] in TRANSFER_KINDS:
        prompt = "Комментарий? Можно отправить «-», чтобы пропустить (например, для начального остатка напишите «Начальный остаток»)."
    else:
        prompt = "Что это за операция? Например: <code>Продукты на рынке</code>"
    await message.answer(prompt + CANCEL_HINT, parse_mode="HTML")


@router.message(StateFilter(ManualForm.description), F.text)
async def step_description(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if text.startswith("/"):
        await message.answer("Введите описание текстом или отмените: /cancel")
        return
    data = await state.get_data()
    try:
        # Для переводов описание необязательно; для проверки длины/пустоты используем общую функцию
        description = clean_description(text, default="" if data["kind"] not in TRANSFER_KINDS else "-")
    except ManualTxError as exc:
        await message.answer(f"⚠️ {exc}")
        return
    await state.update_data(description=description)
    await state.set_state(ManualForm.date)
    await message.answer(
        "Дата операции? Нажмите кнопку или введите, например, <code>05.11.2026</code>" + CANCEL_HINT,
        parse_mode="HTML",
        reply_markup=build_manual_today_kb(),
    )


@router.callback_query(StateFilter(ManualForm.date), F.data.in_({"man_today", "man_yesterday"}))
async def cb_date_quick(callback: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    await callback.answer()
    if not callback.message:
        return
    day = parse_tx_date("сегодня" if callback.data == "man_today" else "вчера")
    await _after_date(callback.message, state, session, callback.from_user.id, day)


@router.message(StateFilter(ManualForm.date), F.text)
async def step_date(message: Message, state: FSMContext, session: AsyncSession) -> None:
    text = (message.text or "").strip()
    if text.startswith("/"):
        await message.answer("Введите дату текстом или отмените: /cancel")
        return
    try:
        day = parse_tx_date(text)
    except ManualTxError as exc:
        await message.answer(f"⚠️ {exc}")
        return
    if not message.from_user:
        return
    await _after_date(message, state, session, message.from_user.id, day)


async def _after_date(
    target: Message, state: FSMContext, session: AsyncSession, telegram_id: int, day: date
) -> None:
    await state.update_data(date=day.isoformat())
    data = await state.get_data()
    kind = data["kind"]

    if kind in TRANSFER_KINDS:
        await _save(target, state, session, telegram_id, category_id=None)
        return

    matched, same_type = await guess_category(session, kind, data["description"])
    if matched is not None:
        await _save(target, state, session, telegram_id, category_id=matched.id)
        return

    await state.set_state(ManualForm.category)
    await target.answer(
        "Не смог определить категорию по описанию. Выберите:",
        reply_markup=build_manual_categories_kb(same_type),
    )


@router.callback_query(StateFilter(ManualForm.category), F.data.startswith("man_cat:"))
async def cb_category(callback: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    try:
        category_id = int((callback.data or "").split(":", 1)[1])
    except ValueError:
        await callback.answer("Некорректная кнопка", show_alert=True)
        return
    await callback.answer()
    if callback.message:
        await _save(
            callback.message, state, session, callback.from_user.id, category_id=category_id or None
        )


async def _save(
    target: Message,
    state: FSMContext,
    session: AsyncSession,
    telegram_id: int,
    category_id: Optional[int],
) -> None:
    data = await state.get_data()
    user = await get_or_create_user(session, telegram_id)
    kind = data["kind"]

    try:
        tx, category = await add_manual_transaction(
            session,
            user_id=user.id,
            kind=kind,
            amount=Decimal(data["amount"]),
            source=data["source"],
            description=data["description"],
            tx_date=date.fromisoformat(data["date"]),
            category_id=category_id,
        )
    except (ManualTxError, KeyError, ValueError) as exc:
        logger.warning("Не удалось сохранить ручную операцию: %s", exc)
        await state.clear()
        await target.answer("⚠️ Что-то пошло не так, операция не сохранена. Начните заново: /add")
        return
    await state.clear()

    lines = [
        "✅ <b>Операция добавлена</b>",
        f"{'💵' if tx.bank_name == CASH_BANK_NAME else '💳'} {escape(tx.bank_name)} · {KIND_LABELS[kind]} · "
        f"<b>{_signed(tx.amount)}</b>",
        f"📝 {escape(tx.description)}",
        f"📅 {tx.date.strftime('%d.%m.%Y')}",
    ]
    if kind not in TRANSFER_KINDS:
        if category:
            lines.append(f"🏷 {escape(category.name)}")
        else:
            lines.append("🏷 Без категории — разобрать позже: /uncategorized")

    if tx.bank_name == CASH_BANK_NAME:
        balance = await get_cash_balance(session, user.id)
        lines.append(f"\n💵 Баланс наличных: <b>{fmt_money(balance)} ₽</b>")

    if kind == KIND_WITHDRAW:
        lines.append("<i>Снятие с карты в выписке учтётся как перевод — расходом не посчитается.</i>")

    if kind == KIND_EXPENSE and category:
        try:
            lines.extend(["", *await check_category_limits(session, user.id)])
        except Exception as e:
            logger.warning("Не удалось проверить бюджетные лимиты: %s", e)

    await target.answer("\n".join(lines).rstrip(), parse_mode="HTML")


@router.callback_query(F.data.startswith("man_"))
async def cb_stale(callback: CallbackQuery) -> None:
    await callback.answer("Эта кнопка устарела. Начните заново: /add", show_alert=True)
