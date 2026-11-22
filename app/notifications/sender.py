"""Адресные push-уведомления в Telegram (используются из FastAPI, планировщика и т.д.)."""
import asyncio
import logging
from typing import Optional

from aiogram.types import InlineKeyboardMarkup

logger = logging.getLogger(__name__)

SEND_TIMEOUT_SEC = 10


async def send_telegram_message(telegram_id: Optional[int], text: str, parse_mode: Optional[str] = "HTML",
                                *, reply_markup: InlineKeyboardMarkup | None = None) -> bool:
    """Отправляет text в личный чат telegram_id. Никогда не бросает исключений.

    Возвращает True, если Telegram принял сообщение. Ошибка доставки (бот заблокирован, нет сети,
    таймаут) не должна ломать вызывающий код, например вебхук, у которого данные уже сохранены.
    """
    if not telegram_id:
        return False
    # Импорт здесь: модуль бота подтягивает все хендлеры и создаёт Bot(token=...)
    from app.bot.instance import bot

    try:
        await asyncio.wait_for(
            bot.send_message(telegram_id, text, parse_mode=parse_mode, reply_markup=reply_markup),
            timeout=SEND_TIMEOUT_SEC,
        )
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning("Не удалось отправить сообщение в Telegram (chat_id=%s): %s", telegram_id, e)
        return False
