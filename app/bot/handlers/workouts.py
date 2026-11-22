from datetime import date, datetime, timedelta
from html import escape

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.handlers.base import get_or_create_user
from app.bot.keyboards import BTN_WORKOUT
from app.bot.keyboards.reply import MAIN_MENU_TEXTS
from app.bot.keyboards.workouts import reschedule_button, workout_actions
from app.core.security import scope_for_user
from app.health.activity import local_today
from app.health.workouts import (
    complete_workout, find_workout, format_workout, missed_workouts, reschedule_workout, today_workouts,
)

router = Router(name='workouts_router')
MISSED_PAGE_SIZE = 8


class WorkoutReschedule(StatesGroup):
    new_date = State()


def parse_workout_selection(selection: str) -> tuple[int, date | None]:
    raw_id, separator, raw_date = selection.partition(':')
    workout_id = int(raw_id)
    if not 1 <= workout_id <= 2147483647:
        raise ValueError
    return workout_id, date.fromisoformat(raw_date) if separator else None


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
    for workout in workouts:
        keyboard = None if workout.is_completed else workout_actions(workout.id, workout.date)
        await message.answer(format_workout(workout), reply_markup=keyboard, parse_mode='HTML')
    if not workouts or await missed_workouts(session, user.id, offset=0, limit=1):
        keyboard = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text='🗓 Пропущенные тренировки', callback_data='workout:missed:0'),
        ]])
        text = 'Тренировки за прошлые дни' if workouts else 'На сегодня тренировок не запланировано. Отдыхаем! 🛋'
        await message.answer(text, reply_markup=keyboard)


@router.callback_query(F.data.regexp(r'^workout:missed:[0-9]{1,9}$'))
async def missed_callback(callback: CallbackQuery, session: AsyncSession):
    if not isinstance(callback.message, Message):
        await callback.answer('Откройте список командой /workout.', show_alert=True)
        return
    page = int(callback.data.rsplit(':', 1)[1])
    user = await get_or_create_user(session, callback.from_user.id)
    rows = await missed_workouts(session, user.id, offset=page * MISSED_PAGE_SIZE, limit=MISSED_PAGE_SIZE + 1)
    buttons = [
        [reschedule_button(workout_id, day, f'{day:%d.%m.%Y} · {title}')]
        for workout_id, day, title in rows[:MISSED_PAGE_SIZE]
    ]
    navigation = []
    if page:
        navigation.append(InlineKeyboardButton(text='← Назад', callback_data=f'workout:missed:{page - 1}'))
    if len(rows) > MISSED_PAGE_SIZE:
        navigation.append(InlineKeyboardButton(text='Далее →', callback_data=f'workout:missed:{page + 1}'))
    if navigation:
        buttons.append(navigation)
    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons) if buttons else None
    await callback.answer()
    text = 'Какую тренировку перенести?' if rows else 'На этой странице нет пропущенных тренировок.'
    if not rows and not page:
        text = 'Пропущенных тренировок нет.'
    try:
        await callback.message.edit_text(text, reply_markup=keyboard)
    except TelegramBadRequest:
        await callback.message.answer(text, reply_markup=keyboard)


@router.callback_query(F.data.startswith('workout:move:'))
async def start_reschedule(callback: CallbackQuery, session: AsyncSession, state: FSMContext):
    selection = callback.data.removeprefix('workout:move:')
    try:
        workout_id, scheduled_date = parse_workout_selection(selection)
    except ValueError:
        await callback.answer('Некорректный номер или дата тренировки.', show_alert=True)
        return
    if not isinstance(callback.message, Message):
        await callback.answer('Откройте тренировку командой /workout.', show_alert=True)
        return
    user = await get_or_create_user(session, callback.from_user.id)
    workout = await find_workout(session, workout_id, scope_for_user(user))
    if workout is None or workout.is_completed or workout.date != scheduled_date:
        await callback.answer('Тренировка уже изменилась или недоступна. Откройте /workout.', show_alert=True)
        return

    today = local_today()
    tomorrow = today + timedelta(days=1)
    choices = [('Сегодня', today), ('Завтра', tomorrow)]
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=label, callback_data=f'workout:date:{selection}:{day}')
        for label, day in choices if day != scheduled_date
    ]])
    await state.set_data({'workout_selection': selection})
    await state.set_state(WorkoutReschedule.new_date)
    await callback.answer()
    await callback.message.answer(
        f'На какую дату перенести <b>{escape(workout.title)}</b> с {workout.date:%d.%m.%Y}?\n'
        'Выберите день или введите дату в формате ДД.ММ.ГГГГ. Для отмены отправьте /cancel.',
        reply_markup=keyboard, parse_mode='HTML',
    )


async def save_reschedule(message: Message, telegram_id: int, session: AsyncSession, state: FSMContext,
                          selection: str, new_date: date):
    workout_id, old_date = parse_workout_selection(selection)
    if new_date < local_today() or new_date == old_date:
        await message.answer('Выберите другую дату: сегодня или позже.')
        return

    user = await get_or_create_user(session, telegram_id)
    title = await reschedule_workout(session, workout_id, scope_for_user(user), old_date, new_date)
    await state.clear()
    if title is None:
        await message.answer('Тренировка уже изменилась или недоступна. Откройте /workout.')
        return
    await message.answer(
        f'📅 <b>{escape(title)}</b> — тренировка перенесена на {new_date:%d.%m.%Y}.', parse_mode='HTML',
    )


@router.callback_query(F.data.startswith('workout:date:'))
async def reschedule_date_callback(callback: CallbackQuery, session: AsyncSession, state: FSMContext):
    selection, _, raw_date = callback.data.removeprefix('workout:date:').rpartition(':')
    if (not isinstance(callback.message, Message) or await state.get_state() != WorkoutReschedule.new_date.state
            or selection != (await state.get_data()).get('workout_selection')):
        await callback.answer('Этот выбор уже недоступен. Откройте перенос заново.', show_alert=True)
        return
    try:
        new_date = date.fromisoformat(raw_date)
    except ValueError:
        await callback.answer('Некорректная дата.', show_alert=True)
        return
    await callback.answer()
    await save_reschedule(callback.message, callback.from_user.id, session, state, selection, new_date)


@router.message(StateFilter(WorkoutReschedule.new_date), Command('cancel'))
async def cancel_reschedule(message: Message, state: FSMContext):
    await state.clear()
    await message.answer('Перенос тренировки отменён.')


@router.message(StateFilter(WorkoutReschedule.new_date), F.text, ~F.text.startswith('/'), ~F.text.in_(MAIN_MENU_TEXTS))
async def reschedule_date_message(message: Message, session: AsyncSession, state: FSMContext):
    if not message.from_user:
        return
    try:
        new_date = datetime.strptime(message.text.strip(), '%d.%m.%Y').date()
    except ValueError:
        await message.answer('Введите дату в формате ДД.ММ.ГГГГ или отправьте /cancel.')
        return
    selection = (await state.get_data()).get('workout_selection')
    if selection is None:
        await state.clear()
        await message.answer('Откройте перенос заново через /workout.')
        return
    await save_reschedule(message, message.from_user.id, session, state, selection, new_date)


@router.callback_query(F.data.startswith('workout:done:'))
async def done_callback(callback: CallbackQuery, session: AsyncSession):
    try:
        workout_id, scheduled_date = parse_workout_selection(callback.data.removeprefix('workout:done:'))
    except ValueError:
        await callback.answer('Некорректный номер тренировки', show_alert=True)
        return
    user = await get_or_create_user(session, callback.from_user.id)
    workout, changed = await complete_workout(session, workout_id, scope_for_user(user), expected_date=scheduled_date)
    if workout is None:
        await callback.answer('Тренировка уже изменилась или недоступна. Откройте /workout.', show_alert=True)
        return
    await callback.answer('✅ Тренировка выполнена!' if changed else 'Уже отмечено выполненным')
    if isinstance(callback.message, Message):
        try:
            await callback.message.edit_text(format_workout(workout), reply_markup=None, parse_mode='HTML')
        except TelegramBadRequest:
            pass  # Данные уже сохранены; старое сообщение может быть недоступно для редактирования.
