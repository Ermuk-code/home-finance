from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.exceptions import TelegramBadRequest
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.handlers.base import get_or_create_user
from app.bot.keyboards import BTN_WORKOUT
from app.core.security import scope_for_user
from app.health.workouts import complete_workout, format_workout, today_workouts

router = Router(name='workouts_router')


@router.message(Command('workout'))
@router.message(F.text == BTN_WORKOUT)
@router.message(F.text == '🏋️ Тренировка сегодня')
async def cmd_workout(message: Message, session: AsyncSession, state: FSMContext = None):
    if not message.from_user:
        return
    if state is not None:
        await state.clear()
    user = await get_or_create_user(session, message.from_user.id)
    workouts = await today_workouts(session, user.id)
    if not workouts:
        await message.answer('На сегодня тренировок не запланировано. Отдыхаем! 🛋')
        return
    for workout in workouts:
        keyboard = None if workout.is_completed else InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text='✅ Завершить', callback_data=f'workout:done:{workout.id}')]])
        await message.answer(format_workout(workout), reply_markup=keyboard, parse_mode='HTML')


@router.callback_query(F.data.startswith('workout:done:'))
async def done_callback(callback: CallbackQuery, session: AsyncSession):
    try:
        workout_id = int((callback.data or '').removeprefix('workout:done:'))
        if not 1 <= workout_id <= 2147483647:
            raise ValueError
    except ValueError:
        await callback.answer('Некорректный номер тренировки', show_alert=True)
        return
    user = await get_or_create_user(session, callback.from_user.id)
    workout, changed = await complete_workout(session, workout_id, scope_for_user(user))
    if workout is None:
        await callback.answer('Тренировка не найдена', show_alert=True)
        return
    await callback.answer('✅ Тренировка выполнена!' if changed else 'Уже отмечено выполненным')
    if isinstance(callback.message, Message):
        try:
            await callback.message.edit_text(format_workout(workout), reply_markup=None, parse_mode='HTML')
        except TelegramBadRequest:
            pass  # Данные уже сохранены; старое сообщение может быть недоступно для редактирования.
