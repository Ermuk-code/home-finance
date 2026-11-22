from datetime import date

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def reschedule_button(workout_id: int, day: date, text: str = '📅 Перенести') -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=f'workout:move:{workout_id}:{day}')


def workout_actions(workout_id: int, day: date) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text='✅ Завершить', callback_data=f'workout:done:{workout_id}:{day}'),
        reschedule_button(workout_id, day),
    ]])
