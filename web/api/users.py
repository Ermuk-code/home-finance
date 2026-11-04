from typing import Any, Dict, List

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from db.base import get_async_session
from db.models import Transaction, User
from web.auth import get_current_admin

router = APIRouter(
    prefix="/api/users",
    tags=["Telegram Whitelist Users"],
    dependencies=[Depends(get_current_admin)],
)


class UserCreateSchema(BaseModel):
    telegram_id: int = Field(..., gt=0, description="Числовой Telegram ID пользователя")


@router.get("")
async def list_allowed_users(
    session: AsyncSession = Depends(get_async_session),
) -> List[Dict[str, Any]]:
    """Возвращает список всех разрешенных пользователей Telegram и количество их транзакций."""
    stmt = (
        select(
            User,
            func.count(Transaction.id).label("tx_count"),
        )
        .outerjoin(Transaction, Transaction.user_id == User.id)
        .group_by(User.id)
        .order_by(User.created_at.asc())
    )
    rows = (await session.execute(stmt)).all()

    return [
        {
            "id": user.id,
            "telegram_id": user.telegram_id,
            "is_owner": bool(settings.ADMIN_TELEGRAM_ID and user.telegram_id == settings.ADMIN_TELEGRAM_ID),
            "transactions_count": tx_count,
            "created_at": user.created_at.isoformat() if user.created_at else None,
        }
        for user, tx_count in rows
    ]


@router.post("", status_code=status.HTTP_201_CREATED)
async def add_allowed_user(
    payload: UserCreateSchema,
    session: AsyncSession = Depends(get_async_session),
) -> Dict[str, Any]:
    """Добавляет новый Telegram ID в белый список (таблицу users)."""
    existing = (
        await session.execute(select(User).where(User.telegram_id == payload.telegram_id))
    ).scalar_one_or_none()

    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Пользователь с Telegram ID {payload.telegram_id} уже есть в списке разрешенных.",
        )

    new_user = User(telegram_id=payload.telegram_id)
    session.add(new_user)
    await session.commit()
    await session.refresh(new_user)

    return {
        "status": "ok",
        "id": new_user.id,
        "telegram_id": new_user.telegram_id,
        "created_at": new_user.created_at.isoformat() if new_user.created_at else None,
    }


@router.delete("/{user_id}")
async def remove_allowed_user(
    user_id: int,
    session: AsyncSession = Depends(get_async_session),
) -> Dict[str, Any]:
    """Удаляет пользователя из белого списка (кроме главного владельца из .env)."""
    user = await session.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="Пользователь не найден.")

    if settings.ADMIN_TELEGRAM_ID and user.telegram_id == settings.ADMIN_TELEGRAM_ID:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Нельзя удалить главного администратора (ADMIN_TELEGRAM_ID).",
        )

    await session.delete(user)
    await session.commit()
    return {"status": "ok", "deleted_user_id": user_id, "deleted_telegram_id": user.telegram_id}