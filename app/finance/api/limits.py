from decimal import Decimal
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_async_session
from app.core.security import DataScope, get_current_user, get_scope
from app.finance.services.categorizer import seed_default_categories
from app.finance.services.limits import get_limits_overview, set_category_limit

router = APIRouter(
    prefix="/api/limits",
    tags=["Limits"],
    dependencies=[Depends(get_current_user)],
)


class LimitSchema(BaseModel):
    # null или 0 — снять лимит
    monthly_limit: Optional[Decimal] = Field(default=None, ge=0, le=Decimal("1000000000"))


@router.get("")
async def list_limits(
    scope: DataScope = Depends(get_scope),
    session: AsyncSession = Depends(get_async_session),
) -> List[Dict[str, Any]]:
    """Расходные категории с личным лимитом и тратами текущего месяца."""
    await seed_default_categories(session)
    rows = await get_limits_overview(session, scope.owner_id)
    return [
        {
            **r,
            "monthly_limit": float(r["monthly_limit"]) if r["monthly_limit"] is not None else None,
            "spent": float(r["spent"]),
        }
        for r in rows
    ]


@router.put("/{category_id}")
async def update_limit(
    category_id: int,
    payload: LimitSchema,
    scope: DataScope = Depends(get_scope),
    session: AsyncSession = Depends(get_async_session),
) -> Dict[str, Any]:
    limit = payload.monthly_limit if payload.monthly_limit else None
    cat = await set_category_limit(session, scope.owner_id, category_id, limit)
    if cat is None:
        raise HTTPException(status_code=404, detail="Категория не найдена или для неё нельзя задать лимит.")
    return {"status": "ok", "id": cat.id, "monthly_limit": float(limit) if limit else None}
