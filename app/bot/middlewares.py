from typing import Any, Awaitable, Callable, Dict
from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import settings
from app.core.security import get_telegram_user
from app.models import ROLE_ADMIN, User


class DbSessionAndAuthMiddleware(BaseMiddleware):
    """
    Открывает сессию БД и идентифицирует пользователя по message.from_user.id (таблица users).

    Пускает только зарегистрированных: владельца из ADMIN_TELEGRAM_ID (role='admin', создаётся автоматически)
    и аккаунты, заведённые сид-скриптом или через веб-панель. Остальные получают отказ.
    В хендлеры передаётся ``current_user`` (с полем role).
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
                db_user = await get_telegram_user(session, tg_id)
                is_owner = bool(settings.ADMIN_TELEGRAM_ID and tg_id == settings.ADMIN_TELEGRAM_ID)

                # Владелец из .env: создаём запись, если её ещё нет, и гарантируем роль admin
                if is_owner:
                    if db_user is None:
                        db_user = User(
                            telegram_id=tg_id,
                            name=tg_user.full_name or settings.ADMIN_NAME,
                            role=ROLE_ADMIN,
                        )
                        session.add(db_user)
                        await session.commit()
                        await session.refresh(db_user)
                    elif db_user.role != ROLE_ADMIN:
                        db_user.role = ROLE_ADMIN
                        await session.commit()

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