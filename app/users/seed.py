"""Сид-скрипт: заводит двух участников семьи (admin и user) из .env. Идемпотентен.

Запуск:  python -m app.users.seed
Нужные переменные: ADMIN_TELEGRAM_ID (+ ADMIN_NAME) и PARTNER_TELEGRAM_ID (+ PARTNER_NAME).
"""
import asyncio
import logging
from typing import List, Tuple

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import async_session_maker, engine
from app.models import ROLE_ADMIN, ROLE_USER, User

logger = logging.getLogger(__name__)


async def seed_users(session: AsyncSession) -> List[Tuple[str, User]]:
    """Создаёт недостающих пользователей. Существующим синхронизирует имя и роль. Возвращает (действие, user)."""
    wanted = []
    if settings.ADMIN_TELEGRAM_ID:
        wanted.append((settings.ADMIN_TELEGRAM_ID, settings.ADMIN_NAME, ROLE_ADMIN))
    if settings.PARTNER_TELEGRAM_ID:
        wanted.append((settings.PARTNER_TELEGRAM_ID, settings.PARTNER_NAME, ROLE_USER))
    if not wanted:
        raise RuntimeError("Задайте ADMIN_TELEGRAM_ID и PARTNER_TELEGRAM_ID в .env")
    if len({tg for tg, _, _ in wanted}) != len(wanted):
        raise RuntimeError("ADMIN_TELEGRAM_ID и PARTNER_TELEGRAM_ID должны быть разными")

    result: List[Tuple[str, User]] = []
    for telegram_id, name, role in wanted:
        user = (await session.execute(select(User).where(User.telegram_id == telegram_id))).scalar_one_or_none()
        if user is None:
            user = User(telegram_id=telegram_id, name=name, role=role)
            session.add(user)
            action = "created"
        else:
            changed = user.name != name or user.role != role
            user.name, user.role = name, role
            action = "updated" if changed else "unchanged"
        if telegram_id == settings.ADMIN_TELEGRAM_ID and user.height_cm is None:
            user.height_cm = 184
        result.append((action, user))
    await session.commit()
    return result


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    async with async_session_maker() as session:
        for action, user in await seed_users(session):
            logger.info("%-9s id=%s telegram_id=%s name=%s role=%s", action, user.id, user.telegram_id, user.name, user.role)
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
