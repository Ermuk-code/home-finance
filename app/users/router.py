"""Пользователи и права: профиль, белый список Telegram (управляет только admin)."""
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import get_async_session
from app.core.security import get_current_user, is_admin
from app.finance.services.reports import summarize_transactions
from app.models import ROLE_ADMIN, ROLE_USER, ROLES, Category, Transaction, User, WeightLog

router = APIRouter(prefix="/api/users", tags=["Users & Roles"])


def _validate_role(value: str) -> str:
    if value not in ROLES:
        raise ValueError(f"role должен быть одним из: {', '.join(ROLES)}")
    return value


class UserCreateSchema(BaseModel):
    telegram_id: int = Field(..., gt=0, description="Числовой Telegram ID пользователя")
    name: str = Field(..., min_length=1, max_length=100)
    role: str = ROLE_USER

    @field_validator("role")
    @classmethod
    def _check_role(cls, value: str) -> str:
        return _validate_role(value)


class UserUpdateSchema(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=100)
    role: Optional[str] = None

    @field_validator("role")
    @classmethod
    def _check_role(cls, value: Optional[str]) -> Optional[str]:
        return _validate_role(value) if value is not None else value


def _is_owner(user: User) -> bool:
    return bool(settings.ADMIN_TELEGRAM_ID and user.telegram_id == settings.ADMIN_TELEGRAM_ID)


def _serialize(user: User) -> Dict[str, Any]:
    return {
        "id": user.id,
        "telegram_id": user.telegram_id,
        "name": user.name,
        "role": user.role,
        "is_owner": _is_owner(user),
        "created_at": user.created_at.isoformat() if user.created_at else None,
    }


@router.get("/me")
async def get_me(current_user: User = Depends(get_current_user)) -> Dict[str, Any]:
    """Профиль текущего пользователя (любая роль)."""
    return _serialize(current_user)


@router.get("/overview", dependencies=[Depends(is_admin)])
async def users_overview(
    days: int = Query(default=30, ge=1, le=366),
    session: AsyncSession = Depends(get_async_session),
) -> List[Dict[str, Any]]:
    """Общая сводка по всем участникам: финансы за период и последний вес (только admin)."""
    since = datetime.now(timezone.utc) - timedelta(days=days)
    result = []
    for user in (await session.execute(select(User).order_by(User.id.asc()))).scalars().all():
        rows = (
            await session.execute(
                select(Category.name, Transaction.amount, Category.type)
                .outerjoin(Category, Transaction.category_id == Category.id)
                .where(Transaction.user_id == user.id, Transaction.date >= since)
            )
        ).all()
        summary = summarize_transactions(rows)
        weight = (
            await session.execute(
                select(WeightLog)
                .where(WeightLog.user_id == user.id)
                .order_by(WeightLog.timestamp.desc(), WeightLog.id.desc())
                .limit(1)
            )
        ).scalars().first()
        result.append(
            {
                **_serialize(user),
                "period_days": days,
                "clean_income": float(summary["clean_income"]),
                "clean_expense": float(summary["clean_expense"]),
                "net": float(summary["net"]),
                "last_weight_kg": float(weight.weight_kg) if weight else None,
                "last_weight_at": weight.timestamp.isoformat() if weight else None,
            }
        )
    return result


@router.get("", dependencies=[Depends(is_admin)])
async def list_allowed_users(
    session: AsyncSession = Depends(get_async_session),
) -> List[Dict[str, Any]]:
    """Список пользователей белого списка и количество их транзакций (только admin)."""
    stmt = (
        select(User, func.count(Transaction.id).label("tx_count"))
        .outerjoin(Transaction, Transaction.user_id == User.id)
        .group_by(User.id)
        .order_by(User.created_at.asc(), User.id.asc())
    )
    rows = (await session.execute(stmt)).all()
    return [{**_serialize(user), "transactions_count": tx_count} for user, tx_count in rows]


@router.post("", status_code=status.HTTP_201_CREATED, dependencies=[Depends(is_admin)])
async def add_allowed_user(
    payload: UserCreateSchema,
    session: AsyncSession = Depends(get_async_session),
) -> Dict[str, Any]:
    """Добавляет пользователя в белый список (только admin)."""
    existing = (
        await session.execute(select(User).where(User.telegram_id == payload.telegram_id))
    ).scalar_one_or_none()
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Пользователь с Telegram ID {payload.telegram_id} уже есть в списке разрешенных.",
        )

    new_user = User(telegram_id=payload.telegram_id, name=payload.name.strip(), role=payload.role)
    session.add(new_user)
    await session.commit()
    await session.refresh(new_user)
    return {"status": "ok", **_serialize(new_user)}


@router.patch("/{user_id}")
async def update_user(
    user_id: int,
    payload: UserUpdateSchema,
    admin: User = Depends(is_admin),
    session: AsyncSession = Depends(get_async_session),
) -> Dict[str, Any]:
    """Меняет имя и/или роль пользователя (только admin)."""
    user = await session.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="Пользователь не найден.")

    if payload.role is not None and payload.role != user.role:
        if user.role == ROLE_ADMIN:
            if _is_owner(user):
                raise HTTPException(status_code=400, detail="Нельзя снять роль admin у владельца (ADMIN_TELEGRAM_ID).")
            admins = (await session.execute(select(func.count(User.id)).where(User.role == ROLE_ADMIN))).scalar() or 0
            if admins <= 1:
                raise HTTPException(status_code=400, detail="Нельзя снять роль admin у единственного администратора.")
        user.role = payload.role
    if payload.name is not None:
        user.name = payload.name.strip()

    await session.commit()
    await session.refresh(user)
    return {"status": "ok", **_serialize(user)}


@router.delete("/{user_id}")
async def remove_allowed_user(
    user_id: int,
    admin: User = Depends(is_admin),
    session: AsyncSession = Depends(get_async_session),
) -> Dict[str, Any]:
    """Удаляет пользователя вместе с его данными (только admin; нельзя удалить владельца и себя)."""
    user = await session.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="Пользователь не найден.")

    if _is_owner(user):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Нельзя удалить главного администратора (ADMIN_TELEGRAM_ID).",
        )
    if user.id == admin.id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Нельзя удалить самого себя.")

    await session.delete(user)
    await session.commit()
    return {"status": "ok", "deleted_user_id": user_id, "deleted_telegram_id": user.telegram_id}
