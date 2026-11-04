from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from db.base import get_async_session
from db.models import Category, Transaction
from services.categorizer import learn_from_correction, seed_default_categories
from web.auth import get_current_admin

router = APIRouter(
    prefix="/api/transactions",
    tags=["Transactions"],
    dependencies=[Depends(get_current_admin)],
)


class TransactionUpdateSchema(BaseModel):
    category_id: int
    custom_keyword: Optional[str] = None
    learn: bool = True


class CategoryCreateSchema(BaseModel):
    name: str
    type: str  # 'income' или 'expense'
    keywords: List[str] = []


@router.get("/categories")
async def list_categories(
    session: AsyncSession = Depends(get_async_session),
) -> List[Dict[str, Any]]:
    """Возвращает список всех категорий и их ключевых слов."""
    await seed_default_categories(session)
    stmt = select(Category).order_by(Category.type, Category.name)
    categories = (await session.execute(stmt)).scalars().all()
    return [
        {
            "id": c.id,
            "name": c.name,
            "type": c.type,
            "keywords": c.keywords or [],
        }
        for c in categories
    ]


@router.post("/categories", status_code=status.HTTP_201_CREATED)
async def create_category(
    payload: CategoryCreateSchema,
    session: AsyncSession = Depends(get_async_session),
) -> Dict[str, Any]:
    """Создает новую пользовательскую категорию."""
    if payload.type not in ("income", "expense"):
        raise HTTPException(status_code=400, detail="Поле type должно быть 'income' или 'expense'.")

    existing = (
        await session.execute(select(Category).where(Category.name == payload.name.strip()))
    ).scalar_one_or_none()
    if existing:
        raise HTTPException(status_code=409, detail="Категория с таким названием уже существует.")

    new_cat = Category(
        name=payload.name.strip(),
        type=payload.type,
        keywords=[k.strip().lower() for k in payload.keywords if k.strip()],
    )
    session.add(new_cat)
    await session.commit()
    await session.refresh(new_cat)
    return {"id": new_cat.id, "name": new_cat.name, "type": new_cat.type, "keywords": new_cat.keywords}


@router.get("")
async def get_transactions(
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    is_parsed_automatically: Optional[bool] = Query(default=None),
    unassigned_only: bool = Query(default=False, description="Только без категории (category_id IS NULL)"),
    category_id: Optional[int] = Query(default=None),
    bank_name: Optional[str] = Query(default=None),
    search: Optional[str] = Query(default=None),
    session: AsyncSession = Depends(get_async_session),
) -> Dict[str, Any]:
    """Возвращает список транзакций с фильтрацией и пагинацией."""
    stmt = select(Transaction).options(selectinload(Transaction.category))
    count_stmt = select(func.count(Transaction.id))

    if is_parsed_automatically is not None:
        stmt = stmt.where(Transaction.is_parsed_automatically == is_parsed_automatically)
        count_stmt = count_stmt.where(Transaction.is_parsed_automatically == is_parsed_automatically)

    if unassigned_only:
        stmt = stmt.where(Transaction.category_id.is_(None))
        count_stmt = count_stmt.where(Transaction.category_id.is_(None))

    if category_id is not None:
        stmt = stmt.where(Transaction.category_id == category_id)
        count_stmt = count_stmt.where(Transaction.category_id == category_id)

    if bank_name:
        stmt = stmt.where(Transaction.bank_name.ilike(f"%{bank_name}%"))
        count_stmt = count_stmt.where(Transaction.bank_name.ilike(f"%{bank_name}%"))

    if search:
        stmt = stmt.where(Transaction.description.ilike(f"%{search}%"))
        count_stmt = count_stmt.where(Transaction.description.ilike(f"%{search}%"))

    total = (await session.execute(count_stmt)).scalar() or 0
    stmt = stmt.order_by(Transaction.date.desc(), Transaction.id.desc()).limit(limit).offset(offset)
    rows = (await session.execute(stmt)).scalars().all()

    items = [
        {
            "id": tx.id,
            "user_id": tx.user_id,
            "date": tx.date.isoformat(),
            "amount": float(tx.amount),
            "description": tx.description,
            "bank_name": tx.bank_name,
            "category_id": tx.category_id,
            "category_name": tx.category.name if tx.category else None,
            "is_parsed_automatically": tx.is_parsed_automatically,
        }
        for tx in rows
    ]

    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "items": items,
    }


@router.patch("/{tx_id}")
async def update_transaction_category(
    tx_id: int,
    payload: TransactionUpdateSchema,
    session: AsyncSession = Depends(get_async_session),
) -> Dict[str, Any]:
    """
    Меняет категорию у транзакции и (при learn=True) обучает категоризатор
    на основе назначения платежа с помощью learn_from_correction.
    """
    if payload.learn:
        try:
            learn_result = await learn_from_correction(
                session=session,
                transaction_id=tx_id,
                new_category_id=payload.category_id,
                custom_keyword=payload.custom_keyword,
            )
            return {
                "status": "ok",
                "learned": True,
                "details": learn_result,
            }
        except ValueError as e:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e)) from e

    # Если обучение отключено (learn=False), просто меняем категорию у одной записи
    tx = await session.get(Transaction, tx_id)
    if not tx:
        raise HTTPException(status_code=404, detail=f"Транзакция {tx_id} не найдена.")

    cat = await session.get(Category, payload.category_id)
    if not cat:
        raise HTTPException(status_code=404, detail=f"Категория {payload.category_id} не найдена.")

    tx.category_id = payload.category_id
    tx.is_parsed_automatically = False
    await session.commit()

    return {
        "status": "ok",
        "learned": False,
        "details": {
            "transaction_id": tx.id,
            "new_category_id": cat.id,
            "new_category_name": cat.name,
        },
    }


@router.delete("/{tx_id}")
async def delete_transaction(
    tx_id: int,
    session: AsyncSession = Depends(get_async_session),
) -> Dict[str, Any]:
    """Удаляет транзакцию по ID."""
    tx = await session.get(Transaction, tx_id)
    if not tx:
        raise HTTPException(status_code=404, detail="Транзакция не найдена.")

    await session.delete(tx)
    await session.commit()
    return {"status": "ok", "deleted_id": tx_id}