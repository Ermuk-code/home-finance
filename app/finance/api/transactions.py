from decimal import Decimal
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.database import get_async_session
from app.core.security import DataScope, get_current_user, get_scope
from app.models import Category, Transaction
from app.finance.services.categorizer import learn_from_correction, seed_default_categories
from app.finance.services.manual import (
    CARD_BANKS,
    CASH_BANK_NAME,
    ManualTxError,
    add_manual_transaction,
    get_cash_balance,
    parse_tx_date,
)

# Доступ: любой авторизованный пользователь, данные ограничены его user_id (см. DataScope)
router = APIRouter(
    prefix="/api/transactions",
    tags=["Transactions"],
    dependencies=[Depends(get_current_user)],
)


class TransactionUpdateSchema(BaseModel):
    category_id: int
    custom_keyword: Optional[str] = None
    learn: bool = True


class ManualTransactionSchema(BaseModel):
    kind: str = "expense"  # expense | income | withdraw (снял наличные) | topup (внёс наличные на карту)
    amount: Decimal = Field(..., gt=0)
    source: str = CASH_BANK_NAME  # «Наличные» или название карты/банка; для withdraw/topup игнорируется
    description: Optional[str] = None
    date: Optional[str] = None  # YYYY-MM-DD, по умолчанию сегодня
    category_id: Optional[int] = None  # None — определить по описанию


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


async def _get_scoped_transaction(session: AsyncSession, scope: DataScope, tx_id: int) -> Optional[Transaction]:
    stmt = scope.apply(select(Transaction).where(Transaction.id == tx_id), Transaction)
    return (await session.execute(stmt)).scalar_one_or_none()


@router.get("/sources")
async def list_sources(
    scope: DataScope = Depends(get_scope),
    session: AsyncSession = Depends(get_async_session),
) -> Dict[str, Any]:
    """Источники для формы ручного ввода и текущий остаток наличных."""
    used = (await session.execute(scope.apply(select(Transaction.bank_name).distinct(), Transaction))).scalars().all()
    extra = sorted(b for b in used if b not in CARD_BANKS and b != CASH_BANK_NAME)
    return {
        "cash": CASH_BANK_NAME,
        "cards": CARD_BANKS + extra,
        "cash_balance": float(await get_cash_balance(session, scope.filter_id)),
    }


@router.post("/manual", status_code=status.HTTP_201_CREATED)
async def create_manual_transaction(
    payload: ManualTransactionSchema,
    scope: DataScope = Depends(get_scope),
    session: AsyncSession = Depends(get_async_session),
) -> Dict[str, Any]:
    """Ручное добавление операции: наличные или карта (запись принадлежит scope.owner_id)."""
    try:
        tx_date = parse_tx_date(payload.date) if payload.date else None
        tx, category = await add_manual_transaction(
            session,
            user_id=scope.owner_id,
            kind=payload.kind,
            amount=payload.amount,
            source=payload.source,
            description=payload.description,
            tx_date=tx_date,
            category_id=payload.category_id,
        )
    except ManualTxError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    return {
        "status": "ok",
        "id": tx.id,
        "amount": float(tx.amount),
        "bank_name": tx.bank_name,
        "category_name": category.name if category else None,
        "cash_balance": float(await get_cash_balance(session, scope.owner_id)),
    }


@router.get("")
async def get_transactions(
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    is_parsed_automatically: Optional[bool] = Query(default=None),
    unassigned_only: bool = Query(default=False, description="Только без категории (category_id IS NULL)"),
    category_id: Optional[int] = Query(default=None),
    bank_name: Optional[str] = Query(default=None),
    search: Optional[str] = Query(default=None),
    import_id: Optional[int] = Query(default=None, ge=1),
    scope: DataScope = Depends(get_scope),
    session: AsyncSession = Depends(get_async_session),
) -> Dict[str, Any]:
    """Возвращает список транзакций с фильтрацией и пагинацией (только свои; admin — см. target_user_id / all_users)."""
    stmt = scope.apply(select(Transaction).options(selectinload(Transaction.category)), Transaction)
    count_stmt = scope.apply(select(func.count(Transaction.id)), Transaction)

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

    if import_id is not None:
        stmt = stmt.where(Transaction.import_id == import_id)
        count_stmt = count_stmt.where(Transaction.import_id == import_id)

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
            "import_id": tx.import_id,
            "account_key": tx.account_key,
            "external_id": tx.external_id,
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
    scope: DataScope = Depends(get_scope),
    session: AsyncSession = Depends(get_async_session),
) -> Dict[str, Any]:
    """
    Меняет категорию у транзакции и (при learn=True) обучает категоризатор
    на основе назначения платежа с помощью learn_from_correction.
    """
    if await _get_scoped_transaction(session, scope, tx_id) is None:
        # Чужая или несуществующая транзакция — одинаковый ответ, чтобы не раскрывать чужие id
        raise HTTPException(status_code=404, detail=f"Транзакция {tx_id} не найдена.")

    if payload.learn:
        try:
            learn_result = await learn_from_correction(
                session=session,
                transaction_id=tx_id,
                new_category_id=payload.category_id,
                custom_keyword=payload.custom_keyword,
                user_id=scope.filter_id,
            )
            return {
                "status": "ok",
                "learned": True,
                "details": learn_result,
            }
        except ValueError as e:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e)) from e

    # Если обучение отключено (learn=False), просто меняем категорию у одной записи
    tx = await _get_scoped_transaction(session, scope, tx_id)
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
    scope: DataScope = Depends(get_scope),
    session: AsyncSession = Depends(get_async_session),
) -> Dict[str, Any]:
    """Удаляет транзакцию по ID (только из доступной области данных)."""
    tx = await _get_scoped_transaction(session, scope, tx_id)
    if not tx:
        raise HTTPException(status_code=404, detail="Транзакция не найдена.")

    await session.delete(tx)
    await session.commit()
    return {"status": "ok", "deleted_id": tx_id}
