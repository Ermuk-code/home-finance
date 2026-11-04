from typing import Any, Awaitable, Callable, Dict
from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from config import settings
from db.models import User


class DbSessionAndAuthMiddleware(BaseMiddleware):
    """
    Открывает сессию БД и проверяет доступ:
    пускает ADMIN_TELEGRAM_ID из .env и любые аккаунты, добавленные в таблицу users через веб-панель.
    """

    def __init__(self, session_pool: async_sessionmaker[AsyncSession]):
        super().__init__()
        self.session_pool = session_pool

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        tg_user = data.get("event_from_user")
        if tg_user is None and isinstance(event, (Message, CallbackQuery)):
            tg_user = event.from_user

        async with self.session_pool() as session:
            data["session"] = session

            if tg_user is not None:
                tg_id = tg_user.id
                result = await session.execute(select(User).where(User.telegram_id == tg_id))
                db_user = result.scalar_one_or_none()

                # Автоматически создаем запись для владельца из .env
                if db_user is None and settings.ADMIN_TELEGRAM_ID and tg_id == settings.ADMIN_TELEGRAM_ID:
                    db_user = User(telegram_id=tg_id)
                    session.add(db_user)
                    await session.commit()
                    await session.refresh(db_user)

                if db_user is None:
                    if isinstance(event, Message):
                        await event.answer(
                            f"⛔ <b>Доступ запрещен.</b>\nВаш Telegram ID: <code>{tg_id}</code> не в белом списке.",
                            parse_mode="HTML",
                        )
                    elif isinstance(event, CallbackQuery):
                        await event.answer("⛔ Доступ запрещен.", show_alert=True)
                    return None

                data["current_user"] = db_user

            return await handler(event, data)