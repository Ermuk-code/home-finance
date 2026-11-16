from decimal import Decimal
from typing import Dict, List
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.models import Category


# Тексты кнопок главного меню (по ним же срабатывают хендлеры)
BTN_REPORT = "📊 Отчёт"
BTN_LIMITS = "🎯 Лимиты"
BTN_WEIGHT = "⚖️ Вес"
BTN_ACTIVITY = "🚶 Активность"
BTN_HEALTH_STATS = "📈 Отчет здоровья за неделю"
BTN_ABS_FORECAST = "💪 Прогноз пресса"
BTN_WORKOUT = '🏋️ Тренировка на сегодня'
BTN_WEB = "🌐 Веб-панель"
BTN_ADMIN_STATS = "👥 Аналитика пользователей"


def build_main_menu(role: str) -> ReplyKeyboardMarkup:
    """Контекстное меню: пользователь видит свои финансы и здоровье, у admin добавляется общая аналитика."""
    from app.bot.keyboards.reply import get_main_menu
    return get_main_menu()


def build_categories_keyboard(
    transaction_id: int, categories: List[Category]
) -> InlineKeyboardMarkup:
    """
    Генерирует инлайн-кнопки со списком категорий для ручного обучения парсера.
    Формат callback_data: learn:<tx_id>:<cat_id>
    """
    builder = InlineKeyboardBuilder()
    for cat in categories:
        icon = "🟢" if cat.type == "income" else "🔴"
        builder.button(
            text=f"{icon} {cat.name}",
            callback_data=f"learn:{transaction_id}:{cat.id}",
        )
    # Пропустить операцию, если не хочется размечать сейчас
    builder.button(
        text="⏭ Пропустить / Следующая",
        callback_data=f"skip_learn:{transaction_id}",
    )
    builder.adjust(1)
    return builder.as_markup()


def build_uncategorized_prompt_kb(uncategorized_count: int) -> InlineKeyboardMarkup:
    """Кнопка быстрого перехода к разбору неопознанных операций."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"🧠 Разобрать неизвестные ({uncategorized_count} шт.)",
                    callback_data="next_uncategorized",
                )
            ]
        ]
    )


def build_deposit_kind_kb() -> InlineKeyboardMarkup:
    """Выбор типа при добавлении вклада."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📅 Вклад (срок, без пополнения и снятия)", callback_data="dep_kind:deposit")],
            [InlineKeyboardButton(text="💳 Накопительный счёт (можно пополнять и снимать)", callback_data="dep_kind:savings")],
            [InlineKeyboardButton(text="✖️ Отмена", callback_data="dep_cancel")],
        ]
    )


def build_deposit_today_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="📆 Сегодня", callback_data="dep_today")]]
    )


def build_deposit_capitalization_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Нет, проценты в конце срока", callback_data="dep_cap:0")],
            [InlineKeyboardButton(text="Да, капитализация ежемесячно", callback_data="dep_cap:1")],
        ]
    )


def build_limit_categories_kb(categories: List[Category], limits: Dict[int, Decimal]) -> InlineKeyboardMarkup:
    """Кнопки выбора категории для /set_limit. ``limits`` — личные лимиты пользователя {category_id: сумма}.

    callback_data: lim_cat:<cat_id>.
    """
    builder = InlineKeyboardBuilder()
    for cat in categories:
        limit = limits.get(cat.id)
        mark = f" — {limit:,.0f} ₽".replace(",", " ") if limit is not None else ""
        builder.button(text=f"{cat.name}{mark}", callback_data=f"lim_cat:{cat.id}")
    builder.button(text="❌ Отмена", callback_data="lim_cancel")
    builder.adjust(1)
    return builder.as_markup()


def build_manual_kind_kb() -> InlineKeyboardMarkup:
    """Тип операции для /add. callback_data: man_kind:<kind>."""
    builder = InlineKeyboardBuilder()
    builder.button(text="🔻 Расход", callback_data="man_kind:expense")
    builder.button(text="🔺 Доход", callback_data="man_kind:income")
    builder.button(text="🏧 Снял наличные (с карты)", callback_data="man_kind:withdraw")
    builder.button(text="💳 Внёс наличные на карту", callback_data="man_kind:topup")
    builder.button(text="❌ Отмена", callback_data="man_cancel")
    builder.adjust(2, 1, 1, 1)
    return builder.as_markup()


def build_manual_source_kb(card_banks: List[str]) -> InlineKeyboardMarkup:
    """Источник операции: наличные, известные банки или другая карта. callback_data: man_src:<cash|i|other>."""
    builder = InlineKeyboardBuilder()
    builder.button(text="💵 Наличные", callback_data="man_src:cash")
    for i, bank in enumerate(card_banks):
        builder.button(text=f"💳 {bank}", callback_data=f"man_src:{i}")
    builder.button(text="➕ Другая карта / банк", callback_data="man_src:other")
    builder.button(text="❌ Отмена", callback_data="man_cancel")
    builder.adjust(1)
    return builder.as_markup()


def build_manual_today_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="📅 Сегодня", callback_data="man_today"),
                InlineKeyboardButton(text="Вчера", callback_data="man_yesterday"),
            ],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="man_cancel")],
        ]
    )


def build_manual_categories_kb(categories: List[Category]) -> InlineKeyboardMarkup:
    """Выбор категории, когда по описанию определить не удалось. callback_data: man_cat:<id|0>."""
    builder = InlineKeyboardBuilder()
    for cat in categories:
        builder.button(text=cat.name, callback_data=f"man_cat:{cat.id}")
    builder.button(text="⏭ Без категории", callback_data="man_cat:0")
    builder.adjust(1)
    return builder.as_markup()
