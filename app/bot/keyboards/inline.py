from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def action_button(label: str, action: str):
    return InlineKeyboardButton(text=label,callback_data='menu:'+action)


def get_period_kb(report_type: str = 'finance') -> InlineKeyboardMarkup:
    if report_type not in ('finance','health'):
        raise ValueError('Неизвестный отчет')
    rows = [[InlineKeyboardButton(text=label,callback_data=f'report:{report_type}:{period}')]
            for label,period in [('За сегодня','day'),('За неделю','week'),('За месяц','month'),('За год','year'),('Выбрать период','custom')]]
    if report_type == 'finance':
        rows += [[action_button('💵 Наличные','cash'),action_button('＋ Операция','add')],
                 [action_button('🎯 Лимиты','limits'),action_button('✏️ Задать лимит','set_limit')],
                 [action_button('🏦 Добавить вклад','add_deposit'),action_button('📈 Портфель','portfolio')]]
    else:
        rows += [[action_button('⚖️ Вес','weight'),action_button('🚶 Активность','activity')],
                 [action_button('🍎 Питание сегодня','nutrition'),action_button('📈 Неделя здоровья','stats')],
                 [action_button('💪 Прогноз пресса','abs_forecast'),action_button('🔗 Связать FatSecret','link_fatsecret')]]
    rows.append([action_button('🌐 Открыть веб-панель','web')])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def get_settings_kb(role: str = 'user') -> InlineKeyboardMarkup:
    rows = [[action_button('🌐 Веб-панель','web'),action_button('📄 PDF портфеля','export_portfolio')],
            [action_button('🧩 Разобрать операции','uncategorized')],
            [action_button('🔗 Связать FatSecret','link_fatsecret'),action_button('🔌 Отвязать FatSecret','unlink_fatsecret')]]
    if role == 'admin':
        rows.append([action_button('👥 Аналитика пользователей','admin_stats')])
    rows.append([InlineKeyboardButton(text='Отменить текущий ввод',callback_data='report:cancel')])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def cancel_report_kb():
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='Отмена',callback_data='report:cancel')]])
