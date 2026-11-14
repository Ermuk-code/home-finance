"""Анализ доходов: сводка по источникам, расшифровка источника, правила «кто есть кто»."""
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_async_session
from app.core.security import DataScope, get_current_user, get_scope
from app.models import Category, IncomeRule, Transaction
from app.finance.services.income_analysis import (
    SOURCES, IncomeTx, build_analysis, build_source_detail, normalize_rule,
)

router = APIRouter(
    prefix="/api/income",
    tags=["Income analysis"],
    dependencies=[Depends(get_current_user)],
)


class RuleCreateSchema(BaseModel):
    keyword: str = Field(..., min_length=1, max_length=120)
    source: str = "parents"


def _parse_date(value: Optional[str], name: str) -> Optional[date]:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as e:
        raise HTTPException(status_code=422, detail=f"{name}: ожидается формат YYYY-MM-DD.") from e


def _period(start_date: Optional[str], end_date: Optional[str]) -> tuple:
    start, end = _parse_date(start_date, "start_date"), _parse_date(end_date, "end_date")
    if start and end and start > end:
        raise HTTPException(status_code=422, detail="Дата начала позже даты окончания.")
    return start, end


async def _load_rules(session: AsyncSession, scope: DataScope) -> List[tuple]:
    rows = (await session.execute(scope.apply(select(IncomeRule), IncomeRule).order_by(IncomeRule.id))).scalars().all()
    return [(r.keyword, r.source) for r in rows]


async def _load_income(
    session: AsyncSession, scope: DataScope, start: Optional[date], end: Optional[date]
) -> List[IncomeTx]:
    """Положительные операции за период (с запасом по краям — точную границу по МСК режет сервис)."""
    stmt = scope.apply(
        select(
            Transaction.id, Transaction.date, Transaction.amount, Transaction.description,
            Transaction.bank_name, Category.name, Category.type,
        )
        .outerjoin(Category, Transaction.category_id == Category.id)
        .where(Transaction.amount > 0),
        Transaction,
    )
    if start:
        stmt = stmt.where(Transaction.date >= datetime.combine(start, datetime.min.time(), tzinfo=timezone.utc) - timedelta(days=1))
    if end:
        stmt = stmt.where(Transaction.date <= datetime.combine(end, datetime.min.time(), tzinfo=timezone.utc) + timedelta(days=2))
    rows = (await session.execute(stmt)).all()
    return [
        IncomeTx(id=r[0], date=r[1], amount=r[2], description=r[3] or "", bank=r[4] or "", category=r[5], category_type=r[6])
        for r in rows
    ]


@router.get("/analysis")
async def income_analysis(
    start_date: Optional[str] = Query(default=None, description="YYYY-MM-DD"),
    end_date: Optional[str] = Query(default=None, description="YYYY-MM-DD"),
    include_own: bool = Query(default=False, description="Учитывать переводы между своими счетами"),
    scope: DataScope = Depends(get_scope),
    session: AsyncSession = Depends(get_async_session),
) -> Dict[str, Any]:
    start, end = _period(start_date, end_date)
    txs = await _load_income(session, scope, start, end)
    return build_analysis(txs, await _load_rules(session, scope), start, end, include_own)


@router.get("/source/{source}")
async def income_source_detail(
    source: str,
    start_date: Optional[str] = Query(default=None),
    end_date: Optional[str] = Query(default=None),
    scope: DataScope = Depends(get_scope),
    session: AsyncSession = Depends(get_async_session),
) -> Dict[str, Any]:
    """Расшифровка источника: помесячный график + список операций."""
    start, end = _period(start_date, end_date)
    txs = await _load_income(session, scope, start, end)
    try:
        return build_source_detail(txs, source, await _load_rules(session, scope), start, end)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


@router.get("/sources")
async def income_sources() -> List[Dict[str, str]]:
    return SOURCES


@router.get("/rules")
async def list_rules(
    scope: DataScope = Depends(get_scope),
    session: AsyncSession = Depends(get_async_session),
) -> List[Dict[str, Any]]:
    rows = (await session.execute(scope.apply(select(IncomeRule), IncomeRule).order_by(IncomeRule.id))).scalars().all()
    return [{"id": r.id, "keyword": r.keyword, "source": r.source} for r in rows]


@router.post("/rules", status_code=status.HTTP_201_CREATED)
async def create_rule(
    payload: RuleCreateSchema,
    scope: DataScope = Depends(get_scope),
    session: AsyncSession = Depends(get_async_session),
) -> Dict[str, Any]:
    try:
        keyword, source = normalize_rule(payload.keyword, payload.source)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e

    existing = (
        await session.execute(
            select(IncomeRule).where(IncomeRule.user_id == scope.owner_id, IncomeRule.keyword == keyword)
        )
    ).scalars().first()
    if existing:
        existing.source = source          # то же имя — просто меняем источник
        rule = existing
    else:
        rule = IncomeRule(user_id=scope.owner_id, keyword=keyword, source=source)
        session.add(rule)
    await session.commit()
    await session.refresh(rule)
    return {"status": "ok", "id": rule.id, "keyword": rule.keyword, "source": rule.source}


@router.delete("/rules/{rule_id}")
async def delete_rule(
    rule_id: int,
    scope: DataScope = Depends(get_scope),
    session: AsyncSession = Depends(get_async_session),
) -> Dict[str, Any]:
    rule = (
        await session.execute(scope.apply(select(IncomeRule).where(IncomeRule.id == rule_id), IncomeRule))
    ).scalar_one_or_none()
    if not rule:
        raise HTTPException(status_code=404, detail="Правило не найдено.")
    await session.delete(rule)
    await session.commit()
    return {"status": "ok", "deleted_id": rule_id}
