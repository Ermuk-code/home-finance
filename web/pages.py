from datetime import datetime, time, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import APIRouter, Cookie, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from db.base import get_async_session
from db.models import Asset, Category, Deposit, Reminder, Transaction, User
from services.analytics import analyze_portfolio
from services.broker_parser import BrokerReportParser
from web.auth import get_current_admin

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

router = APIRouter(tags=["Web Pages & Dashboard API"])


class DepositCreateSchema(BaseModel):
    bank_name: str
    title: str
    amount: Decimal
    interest_rate: Decimal
    end_date: Optional[str] = None


class AssetCreateSchema(BaseModel):
    ticker: str
    asset_type: Optional[str] = None
    amount: Decimal
    average_buy_price: Decimal


async def _get_primary_user(session: AsyncSession) -> User:
    user = (await session.execute(select(User).order_by(User.id.asc()))).scalars().first()
    if user:
        return user
    tg_id = settings.ADMIN_TELEGRAM_ID or 1
    user = User(telegram_id=tg_id)
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return user


async def verify_admin_cookie(request: Request, access_token: Optional[str] = Cookie(default=None)) -> Optional[str]:
    try:
        return await get_current_admin(request=request, access_token=access_token)
    except Exception:
        return None


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, access_token: Optional[str] = Cookie(default=None)):
    if await verify_admin_cookie(request, access_token):
        return RedirectResponse(url="/", status_code=303)
    return templates.TemplateResponse(request=request, name="login.html")


@router.get("/", response_class=HTMLResponse)
async def dashboard_page(request: Request, access_token: Optional[str] = Cookie(default=None)):
    if not await verify_admin_cookie(request, access_token):
        return RedirectResponse(url="/login", status_code=303)
    return templates.TemplateResponse(request=request, name="index.html", context={"active_page": "dashboard"})


@router.get("/transactions", response_class=HTMLResponse)
async def transactions_page(request: Request, access_token: Optional[str] = Cookie(default=None)):
    if not await verify_admin_cookie(request, access_token):
        return RedirectResponse(url="/login", status_code=303)
    return templates.TemplateResponse(request=request, name="transactions.html", context={"active_page": "transactions"})


@router.get("/upload", response_class=HTMLResponse)
async def upload_page(request: Request, access_token: Optional[str] = Cookie(default=None)):
    if not await verify_admin_cookie(request, access_token):
        return RedirectResponse(url="/login", status_code=303)
    return templates.TemplateResponse(request=request, name="upload.html", context={"active_page": "upload"})


@router.get("/reminders", response_class=HTMLResponse)
async def reminders_page(request: Request, access_token: Optional[str] = Cookie(default=None)):
    if not await verify_admin_cookie(request, access_token):
        return RedirectResponse(url="/login", status_code=303)
    return templates.TemplateResponse(request=request, name="reminders.html", context={"active_page": "reminders"})


@router.get("/users", response_class=HTMLResponse)
async def users_page(request: Request, access_token: Optional[str] = Cookie(default=None)):
    if not await verify_admin_cookie(request, access_token):
        return RedirectResponse(url="/login", status_code=303)
    return templates.TemplateResponse(request=request, name="users.html", context={"active_page": "users"})


# =========================================================================
# УПРАВЛЕНИЕ ВКЛАДАМИ И АКТИВАМИ ПРЯМО С ДАШБОРДА
# =========================================================================
@router.post("/api/deposits", dependencies=[Depends(get_current_admin)])
async def add_deposit(
    payload: DepositCreateSchema,
    session: AsyncSession = Depends(get_async_session),
) -> Dict[str, Any]:
    user = await _get_primary_user(session)
    parsed_end = None
    if payload.end_date:
        try:
            parsed_end = datetime.fromisoformat(payload.end_date).replace(tzinfo=timezone.utc)
        except ValueError:
            parsed_end = None

    dep = Deposit(
        user_id=user.id,
        bank_name=payload.bank_name.strip(),
        title=payload.title.strip(),
        amount=payload.amount,
        interest_rate=payload.interest_rate,
        end_date=parsed_end,
    )
    session.add(dep)
    await session.commit()
    return {"status": "ok", "id": dep.id}


@router.delete("/api/deposits/{deposit_id}", dependencies=[Depends(get_current_admin)])
async def delete_deposit(
    deposit_id: int,
    session: AsyncSession = Depends(get_async_session),
) -> Dict[str, Any]:
    dep = await session.get(Deposit, deposit_id)
    if not dep:
        raise HTTPException(status_code=404, detail="Вклад не найден")
    await session.delete(dep)
    await session.commit()
    return {"status": "ok"}


@router.post("/api/assets", dependencies=[Depends(get_current_admin)])
async def add_or_update_asset(
    payload: AssetCreateSchema,
    session: AsyncSession = Depends(get_async_session),
) -> Dict[str, Any]:
    user = await _get_primary_user(session)
    ticker = payload.ticker.strip().upper()
    detected_type = payload.asset_type or BrokerReportParser.detect_asset_type(ticker)

    existing = (
        await session.execute(
            select(Asset).where(Asset.user_id == user.id, Asset.ticker == ticker)
        )
    ).scalar_one_or_none()

    if existing:
        existing.amount = payload.amount
        existing.average_buy_price = payload.average_buy_price
        existing.asset_type = detected_type
    else:
        session.add(
            Asset(
                user_id=user.id,
                ticker=ticker,
                asset_type=detected_type,
                amount=payload.amount,
                average_buy_price=payload.average_buy_price,
            )
        )
    await session.commit()
    return {"status": "ok", "ticker": ticker}


@router.delete("/api/assets/{ticker}", dependencies=[Depends(get_current_admin)])
async def delete_asset(
    ticker: str,
    session: AsyncSession = Depends(get_async_session),
) -> Dict[str, Any]:
    stmt = select(Asset).where(Asset.ticker == ticker.strip().upper())
    asset = (await session.execute(stmt)).scalars().first()
    if not asset:
        raise HTTPException(status_code=404, detail="Актив не найден")
    await session.delete(asset)
    await session.commit()
    return {"status": "ok"}


# =========================================================================
# ЕДИНЫЙ ЭНДПОИНТ СТАТИСТИКИ ДАШБОРДА (ГИБРИДНЫЙ ПЕРИОД 1С + ВКЛАДЫ + MOEX)
# =========================================================================
@router.get("/api/dashboard-stats", dependencies=[Depends(get_current_admin)])
async def get_dashboard_stats(
    start_date: Optional[str] = Query(default=None, description="Дата начала YYYY-MM-DD"),
    end_date: Optional[str] = Query(default=None, description="Дата окончания YYYY-MM-DD"),
    session: AsyncSession = Depends(get_async_session),
) -> Dict[str, Any]:
    stmt = select(Category.name, Transaction.amount, Transaction.bank_name, Transaction.date).outerjoin(
        Category, Transaction.category_id == Category.id
    )

    if start_date:
        try:
            dt_start = datetime.combine(
                datetime.strptime(start_date, "%Y-%m-%d").date(), time.min
            ).replace(tzinfo=timezone.utc)
            stmt = stmt.where(Transaction.date >= dt_start)
        except ValueError:
            pass

    if end_date:
        try:
            dt_end = datetime.combine(
                datetime.strptime(end_date, "%Y-%m-%d").date(), time.max
            ).replace(tzinfo=timezone.utc)
            stmt = stmt.where(Transaction.date <= dt_end)
        except ValueError:
            pass

    rows = (await session.execute(stmt)).all()

    total_income = Decimal("0.00")
    total_expense = Decimal("0.00")
    by_category: Dict[str, Decimal] = {}
    by_bank: Dict[str, Decimal] = {}

    for cat_name, amount, bank_name, _ in rows:
        if amount > 0:
            total_income += amount
        else:
            abs_amt = abs(amount)
            total_expense += abs_amt
            label = cat_name or "Неразобранное"
            by_category[label] = by_category.get(label, Decimal("0.00")) + abs_amt
            by_bank[bank_name] = by_bank.get(bank_name, Decimal("0.00")) + abs_amt

    uncategorized_count = (
        await session.execute(select(func.count(Transaction.id)).where(Transaction.category_id.is_(None)))
    ).scalar() or 0

    active_reminders_count = (
        await session.execute(select(func.count(Reminder.id)).where(Reminder.is_notified.is_(False)))
    ).scalar() or 0

    sorted_cats = sorted(by_category.items(), key=lambda x: x[1], reverse=True)

    # 1. Получаем список вкладов и накопительных счетов
    deposits_rows = (await session.execute(select(Deposit).order_by(Deposit.amount.desc()))).scalars().all()
    deposits_list = []
    total_deposits_rub = Decimal("0.00")
    for d in deposits_rows:
        total_deposits_rub += d.amount
        expected_yearly = (d.amount * (d.interest_rate / Decimal("100"))).quantize(Decimal("0.01"))
        deposits_list.append(
            {
                "id": d.id,
                "bank_name": d.bank_name,
                "title": d.title,
                "amount": float(d.amount),
                "interest_rate": float(d.interest_rate),
                "expected_yearly_income": float(expected_yearly),
                "end_date": d.end_date.strftime("%d.%m.%Y") if d.end_date else "Бессрочный",
            }
        )

    # 2. Получаем инвестиционный портфель (Акции и Облигации с котировками MOEX)
    user = await _get_primary_user(session)
    portfolio_raw = await analyze_portfolio(session, user.id)
    portfolio_positions = [
        {
            "ticker": p["ticker"],
            "asset_type": p["asset_type"],
            "amount": float(p["amount"]),
            "avg_buy_price": float(p["avg_buy_price"]),
            "current_price": float(p["current_price"]),
            "invested_value": float(p["invested_value"]),
            "current_value": float(p["current_value"]),
            "pnl": float(p["pnl"]),
            "pnl_pct": float(p["pnl_pct"]),
            "share_pct": float(p["share_pct"]),
        }
        for p in portfolio_raw["positions"]
    ]

    return {
        "total_income": float(total_income),
        "total_expense": float(total_expense),
        "net_balance": float(total_income - total_expense),
        "uncategorized_count": uncategorized_count,
        "active_reminders_count": active_reminders_count,
        "categories_labels": [k for k, _ in sorted_cats],
        "categories_values": [float(v) for _, v in sorted_cats],
        "banks_labels": list(by_bank.keys()),
        "banks_values": [float(v) for v in by_bank.values()],
        "deposits": deposits_list,
        "total_deposits": float(total_deposits_rub),
        "portfolio": {
            "total_invested": float(portfolio_raw["total_invested"]),
            "total_current": float(portfolio_raw["total_current"]),
            "total_pnl": float(portfolio_raw["total_pnl"]),
            "total_pnl_pct": float(portfolio_raw["total_pnl_pct"]),
            "stocks_share_pct": float(portfolio_raw["stocks_share_pct"]),
            "bonds_share_pct": float(portfolio_raw["bonds_share_pct"]),
            "positions": portfolio_positions,
            "rebalance_advice": portfolio_raw["rebalance_advice"],
        },
    }