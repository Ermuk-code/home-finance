from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_async_session
from app.core.security import DataScope, get_current_user, get_scope
from app.models import Reminder, User
from app.finance.services.recurrence import RecurrenceError, anchor_day_of, describe, validate_every, validate_recurrence

router = APIRouter(
    prefix="/api/reminders",
    tags=["Reminders"],
    dependencies=[Depends(get_current_user)],
)


class ReminderCreateSchema(BaseModel):
    title: str = Field(..., min_length=1, max_length=150)
    description: Optional[str] = None
    trigger_date: datetime
    # None / "" — разовое; "daily" | "weekly" | "monthly" | "yearly"
    recurrence: Optional[str] = None
    # «каждые N …»: 1 — каждый день/неделю/месяц/год, 2 — раз в два и т.д.
    recurrence_every: int = 1


@router.get("")
async def list_reminders(
    include_notified: bool = Query(default=True, description="Включать ли уже отправленные"),
    scope: DataScope = Depends(get_scope),
    session: AsyncSession = Depends(get_async_session),
) -> List[Dict[str, Any]]:
    """Возвращает список календарных напоминаний (только свои; admin — target_user_id / all_users)."""
    stmt = scope.apply(select(Reminder, User.telegram_id).join(User, Reminder.user_id == User.id), Reminder)
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
            "recurrence": rem.recurrence,
            "recurrence_every": rem.recurrence_every,
            "recurrence_label": describe(rem.recurrence, rem.trigger_date, rem.recurrence_every) if rem.recurrence else "",
        }
        for rem, tg_id in rows
    ]


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_reminder(
    payload: ReminderCreateSchema,
    scope: DataScope = Depends(get_scope),
    session: AsyncSession = Depends(get_async_session),
) -> Dict[str, Any]:
    """Создает напоминание для отправки в Telegram владельцу (себе; admin — любому через target_user_id)."""
    try:
        recurrence = validate_recurrence(payload.recurrence)
        every = validate_every(payload.recurrence_every)
    except RecurrenceError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e

    rem = Reminder(
        user_id=scope.owner_id,
        title=payload.title.strip(),
        description=payload.description.strip() if payload.description else None,
        trigger_date=payload.trigger_date,
        is_notified=False,
        recurrence=recurrence,
        recurrence_every=every if recurrence else 1,
        recurrence_day=anchor_day_of(payload.trigger_date) if recurrence else None,
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
        "recurrence": rem.recurrence,
        "recurrence_label": describe(rem.recurrence, rem.trigger_date, rem.recurrence_every) if rem.recurrence else "",
    }


@router.delete("/{reminder_id}")
async def delete_reminder(
    reminder_id: int,
    scope: DataScope = Depends(get_scope),
    session: AsyncSession = Depends(get_async_session),
) -> Dict[str, Any]:
    """Удаляет напоминание по ID (только из доступной области данных)."""
    rem = (
        await session.execute(scope.apply(select(Reminder).where(Reminder.id == reminder_id), Reminder))
    ).scalar_one_or_none()
    if not rem:
        raise HTTPException(status_code=404, detail="Напоминание не найдено.")

    await session.delete(rem)
    await session.commit()
    return {"status": "ok", "deleted_id": reminder_id}