from aiogram.types import KeyboardButton, ReplyKeyboardMarkup

BTN_FINANCE = '📊 Финансы (Отчет)'
BTN_HEALTH = '⚖️ Здоровье и Питание'
BTN_TRAINING = '🏋️ Тренировка на сегодня'
BTN_SETTINGS = '⚙️ Команды и Настройки'
MAIN_MENU_TEXTS = {BTN_FINANCE, BTN_HEALTH, BTN_TRAINING, BTN_SETTINGS}


def get_main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text=BTN_FINANCE), KeyboardButton(text=BTN_HEALTH)],
        [KeyboardButton(text=BTN_TRAINING)],
        [KeyboardButton(text=BTN_SETTINGS)],
    ], resize_keyboard=True, is_persistent=True)
