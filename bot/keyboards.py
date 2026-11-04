from typing import List
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from db.models import Category


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