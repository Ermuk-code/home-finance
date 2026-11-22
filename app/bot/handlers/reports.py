import asyncio
import logging
from time import perf_counter

from aiogram import F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, CallbackQuery, Message
from aiogram.exceptions import TelegramAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.handlers.base import get_or_create_user
from app.bot.keyboards import BTN_REPORT
from app.bot.keyboards.reply import BTN_FINANCE, BTN_HEALTH, MAIN_MENU_TEXTS, get_main_menu
from app.bot.keyboards.inline import cancel_report_kb, get_period_kb
from app.services.reporting import build_report_data, format_period_summary, parse_custom_period, report_period
from app.services.screenshot import create_render_token, take_dashboard_screenshot

logger = logging.getLogger(__name__)
router = Router(name='period_reports_router')
_busy_users: set[int] = set()
REPORT_PHOTO_TIMEOUT_SEC = 15


class ReportState(StatesGroup):
    waiting_for_custom_dates = State()


@router.message(Command('report'))
@router.message(F.text.in_({BTN_FINANCE,BTN_REPORT}))
async def finance_menu(message: Message, state: FSMContext):
    await state.clear()
    await message.answer('📊 Финансы: выберите период отчета или действие.',reply_markup=get_period_kb('finance'))


@router.message(F.text == BTN_HEALTH)
@router.message(Command('health_report'))
async def health_menu(message: Message, state: FSMContext):
    await state.clear()
    await message.answer('⚖️ Отчет здоровья с графиками: выберите период.\n'
                         'Отдельные показатели и питание за сегодня доступны ниже.',reply_markup=get_period_kb('health'))


async def send_report(message: Message, user, session: AsyncSession, kind, start, end, period=None):
    if user.id in _busy_users:
        await message.answer('Предыдущий отчет еще готовится. Дождитесь результата.')
        return
    _busy_users.add(user.id)
    try:
        started = perf_counter()
        await message.answer('⏳ Собираю данные и генерирую графики…')
        try:
            data = await build_report_data(session,user,kind,start,end,period=period or ('day' if start==end else 'custom'))
        except Exception as exc:
            logger.warning('Report query failed user_id=%s error=%s',user.id,type(exc).__name__)
            await message.answer('Не удалось получить данные отчета. Попробуйте позже.')
            return
        text = format_period_summary(data)
        logger.info('Report data ready user_id=%s kind=%s elapsed_ms=%.0f', user.id, kind,
                    (perf_counter()-started)*1000)
        # Сводка доступна сразу: генерация и загрузка картинки не задерживают текст.
        await message.answer(text,parse_mode='HTML')
        photo = None
        render_started = perf_counter()
        try:
            token = create_render_token(user.id,start,end,kind)
            photo = await take_dashboard_screenshot(start,end,token,kind)
        except Exception as exc:  # Browser failures must not hide the text report.
            logger.warning('Report image failed user_id=%s error=%s',user.id,type(exc).__name__)
        logger.info('Report image ready user_id=%s kind=%s elapsed_ms=%.0f bytes=%s', user.id, kind,
                    (perf_counter()-render_started)*1000, len(photo) if photo else 0)
        if photo:
            header='\n'.join(text.splitlines()[:2])
            caption = header if len(header)<=900 else f'Отчет Life OS · {start} — {end}.'
            delivery_started = perf_counter()
            try:
                async with asyncio.timeout(REPORT_PHOTO_TIMEOUT_SEC):
                    await message.answer_photo(BufferedInputFile(photo,'dashboard.png'),caption=caption,parse_mode='HTML')
                logger.info('Report photo delivered user_id=%s kind=%s elapsed_ms=%.0f', user.id, kind,
                            (perf_counter()-delivery_started)*1000)
            except (TelegramAPIError, TimeoutError) as exc:
                logger.warning('Report photo delivery failed user_id=%s error=%s elapsed_ms=%.0f',
                               user.id,type(exc).__name__,(perf_counter()-delivery_started)*1000)
                await message.answer('Не удалось отправить график. Текстовая сводка уже отправлена выше.')
        if photo is None:
            await message.answer('График временно недоступен. Текстовая сводка выше содержит доступные данные.')
    finally:
        _busy_users.discard(user.id)


@router.callback_query(F.data == 'report:cancel')
async def cancel_report(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.answer('Ввод отменен')
    if isinstance(callback.message,Message):
        await callback.message.answer('Выберите раздел в меню.',reply_markup=get_main_menu())


@router.callback_query(F.data.startswith('report:'))
async def choose_period(callback: CallbackQuery, state: FSMContext, session: AsyncSession):
    if not isinstance(callback.message,Message):
        await callback.answer('Откройте меню в чате с ботом.',show_alert=True)
        return
    parts=(callback.data or '').split(':')
    if len(parts)!=3 or parts[1] not in ('finance','health') or parts[2] not in ('day','week','month','year','custom'):
        await callback.answer('Неизвестный период',show_alert=True)
        return
    _,kind,period=parts
    await callback.answer()
    await state.clear()
    if period=='custom':
        await state.set_state(ReportState.waiting_for_custom_dates)
        await state.update_data(report_type=kind)
        await callback.message.answer('Введите период ДД.ММ.ГГГГ-ДД.ММ.ГГГГ.\nНапример: 01.11.2026-16.11.2026.',reply_markup=cancel_report_kb())
        return
    user=await get_or_create_user(session,callback.from_user.id)
    start,end=report_period(period)
    await send_report(callback.message,user,session,kind,start,end,period=period)


@router.message(StateFilter(ReportState.waiting_for_custom_dates),F.text,
                ~F.text.in_(MAIN_MENU_TEXTS | {BTN_REPORT,'🏋️ Тренировка сегодня'}),~F.text.startswith('/'))
async def custom_dates(message: Message, state: FSMContext, session: AsyncSession):
    try:
        start,end=parse_custom_period(message.text)
    except ValueError as exc:
        await message.answer(str(exc),reply_markup=cancel_report_kb())
        return
    data=await state.get_data()
    kind=data.get('report_type')
    if kind not in ('finance','health'):
        await state.clear()
        await message.answer('Откройте раздел отчета заново кнопкой меню.')
        return
    user=await get_or_create_user(session,message.from_user.id)
    await state.clear()
    await send_report(message,user,session,kind,start,end,period='custom')


@router.callback_query(F.data.startswith('menu:'))
async def menu_action(callback: CallbackQuery, state: FSMContext, session: AsyncSession):
    if not isinstance(callback.message,Message):
        await callback.answer('Откройте меню в чате с ботом.',show_alert=True)
        return
    from app.bot.handlers import admin, base, deposits, health, invest, learning, limits, manual, nutrition
    actions={
        'web':base.cmd_web,'cash':manual.cmd_cash,'add':manual.cmd_add,
        'limits':limits.cmd_limits,'set_limit':limits.cmd_set_limit,'add_deposit':deposits.cmd_add_deposit,
        'portfolio':invest.cmd_portfolio,'export_portfolio':invest.cmd_export_portfolio,
        'uncategorized':learning.cmd_uncategorized,'weight':health.cmd_weight,'activity':health.cmd_activity,
        'stats':health.cmd_stats,'abs_forecast':health.cmd_abs_forecast,'nutrition':nutrition.cmd_nutrition,
        'link_fatsecret':nutrition.cmd_link_fatsecret,'unlink_fatsecret':nutrition.cmd_unlink_fatsecret,
        'admin_stats':admin.cmd_admin_stats,
    }
    action=(callback.data or '').removeprefix('menu:')
    handler=actions.get(action)
    if handler is None:
        await callback.answer('Неизвестное действие',show_alert=True)
        return
    await callback.answer()
    await state.clear()
    message=callback.message.model_copy(update={'from_user':callback.from_user})
    if action in ('add','add_deposit'):
        await handler(message,state)
    elif action == 'set_limit':
        await handler(message,state,session)
    else:
        await handler(message,session)
