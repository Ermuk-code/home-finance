from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from db.base import get_async_session
from db.models import Reminder, User
from web.auth import get_current_admin

router = APIRouter(
    prefix="/api/reminders",
    tags=["Reminders"],
    dependencies=[Depends(get_current_admin)],
)


class ReminderCreateSchema(BaseModel):
    title: str = Field(..., min_length=1, max_length=150)
    description: Optional[str] = None
    trigger_date: datetime
    user_id: Optional[int] = None


async def _get_default_user(session: AsyncSession, user_id: Optional[int] = None) -> User:
    if user_id is not None:
        u = await session.get(User, user_id)
        if not u:
            raise HTTPException(status_code=404, detail="Пользователь не найден.")
        return u

    first_user = (await session.execute(select(User).order_by(User.id.asc()))).scalars().first()
    if first_user:
        return first_user

    tg_id = settings.ADMIN_TELEGRAM_ID or 1
    new_user = User(telegram_id=tg_id)
    session.add(new_user)
    await session.commit()
    await session.refresh(new_user)
    return new_user


@router.get("")
async def list_reminders(
    include_notified: bool = Query(default=True, description="Включать ли уже отправленные"),
    session: AsyncSession = Depends(get_async_session),
) -> List[Dict[str, Any]]:
    """Возвращает список календарных напоминаний."""
    stmt = select(Reminder, User.telegram_id).join(User, Reminder.user_id == User.id)
    if not include_notified:
        stmt = stmt.where(Reminder.is_notified.is_(False))

    stmt = stmt.order_by(Reminder.is_notified.asc(), Reminder.trigger_date.asc())
    rows = (await session.execute(stmt)).all()

    return [
        {
            "id": rem.id,
            "user_id": rem.user_id,
            "telegram_id": tg_id,
            "title": rem.title,
            "description": rem.description,
            "trigger_date": rem.trigger_date.isoformat(),
            "is_notified": rem.is_notified,
        }
        for rem, tg_id in rows
    ]


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_reminder(
    payload: ReminderCreateSchema,
    session: AsyncSession = Depends(get_async_session),
) -> Dict[str, Any]:
    """Создает новое напоминание для отправки в Telegram."""
    target_user = await _get_default_user(session, payload.user_id)

    rem = Reminder(
        user_id=target_user.id,
        title=payload.title.strip(),
        description=payload.description.strip() if payload.description else None,
        trigger_date=payload.trigger_date,
        is_notified=False,
    )
    session.add(rem)
    await session.commit()
    await session.refresh(rem)

    return {
        "status": "ok",
        "id": rem.id,
        "title": rem.title,
        "trigger_date": rem.trigger_date.isoformat(),
        "is_notified": rem.is_notified,
    }


@router.delete("/{reminder_id}")
async def delete_reminder(
    reminder_id: int,
    session: AsyncSession = Depends(get_async_session),
) -> Dict[str, Any]:
    """Удаляет напоминание по ID."""
    rem = await session.get(Reminder, reminder_id)
    if not rem:
        raise HTTPException(status_code=404, detail="Напоминание не найдено.")

    await session.delete(rem)
    await session.commit()
    return {"status": "ok", "deleted_id": reminder_id}