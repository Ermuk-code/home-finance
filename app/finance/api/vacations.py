from datetime import datetime, time, timezone
from typing import Any, Dict, List

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.database import get_async_session
from app.core.security import DataScope, get_current_user, get_scope
from app.models import Category, Transaction, Vacation
from app.finance.services.categorizer import TRANSFER_CATEGORY_NAMES, seed_default_categories

router = APIRouter(
    prefix="/api/vacations",
    tags=["Vacations"],
    dependencies=[Depends(get_current_user)],
)


class VacationCreateSchema(BaseModel):
    name: str = Field(..., min_length=1, max_length=150)
    start_date: str  # Формат YYYY-MM-DD
    end_date: str    # Формат YYYY-MM-DD


@router.get("")
async def list_vacations(
    scope: DataScope = Depends(get_scope),
    session: AsyncSession = Depends(get_async_session),
) -> List[Dict[str, Any]]:
    stmt = scope.apply(select(Vacation), Vacation).order_by(Vacation.start_date.desc())
    rows = (await session.execute(stmt)).scalars().all()
    return [
        {
            "id": v.id,
            "name": v.name,
            "start_date": v.start_date.strftime("%Y-%m-%d"),
            "end_date": v.end_date.strftime("%Y-%m-%d"),
            "display_period": f"{v.start_date.strftime('%d.%m.%Y')} — {v.end_date.strftime('%d.%m.%Y')}",
        }
        for v in rows
    ]


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_vacation(
    payload: VacationCreateSchema,
    scope: DataScope = Depends(get_scope),
    session: AsyncSession = Depends(get_async_session),
) -> Dict[str, Any]:
    """
    Создает период отпуска и автоматически переводит уже загруженные
    расходы за эти даты в категорию «Путешествия» (кроме обязательных платежей и переводов).
    """
    try:
        dt_start = datetime.combine(
            datetime.strptime(payload.start_date, "%Y-%m-%d").date(), time.min
        ).replace(tzinfo=timezone.utc)
        dt_end = datetime.combine(
            datetime.strptime(payload.end_date, "%Y-%m-%d").date(), time.max
        ).replace(tzinfo=timezone.utc)
    except ValueError as e:
        raise HTTPException(status_code=400, detail="Неверный формат даты. Ожидается YYYY-MM-DD.") from e

    if dt_end < dt_start:
        raise HTTPException(status_code=400, detail="Дата окончания не может быть раньше даты начала.")

    await seed_default_categories(session)

    vac = Vacation(
        user_id=scope.owner_id,
        name=payload.name.strip(),
        start_date=dt_start,
        end_date=dt_end,
    )
    session.add(vac)

    # Находим категорию «Путешествия» и переносим в нее существующие траты за этот период
    travel_cat = (
        await session.execute(select(Category).where(Category.name == "Путешествия"))
    ).scalar_one_or_none()

    reassigned_count = 0
    if travel_cat:
        tx_stmt = (
            select(Transaction)
            .options(selectinload(Transaction.category))
            .where(
                Transaction.user_id == scope.owner_id,
                Transaction.date >= dt_start,
                Transaction.date <= dt_end,
                Transaction.amount < 0,
            )
        )
        tx_rows = (await session.execute(tx_stmt)).scalars().all()
        for tx in tx_rows:
            if tx.category and (tx.category.is_mandatory or tx.category.name in TRANSFER_CATEGORY_NAMES):
                continue
            if tx.category_id != travel_cat.id:
                tx.category_id = travel_cat.id
                reassigned_count += 1

    await session.commit()
    await session.refresh(vac)

    return {
        "status": "ok",
        "id": vac.id,
        "name": vac.name,
        "reassigned_transactions": reassigned_count,
    }


@router.delete("/{vacation_id}")
async def delete_vacation(
    vacation_id: int,
    scope: DataScope = Depends(get_scope),
    session: AsyncSession = Depends(get_async_session),
) -> Dict[str, Any]:
    vac = (
        await session.execute(scope.apply(select(Vacation).where(Vacation.id == vacation_id), Vacation))
    ).scalar_one_or_none()
    if not vac:
        raise HTTPException(status_code=404, detail="Период отпуска не найден.")
    await session.delete(vac)
    await session.commit()
    return {"status": "ok", "deleted_id": vacation_id}