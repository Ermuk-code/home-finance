from datetime import date, datetime, time, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_async_session
from app.core.security import DataScope, get_optional_user, get_scope
from app.finance.services.manual import get_cash_balance
from app.finance.services.reports import summarize_dashboard
from app.finance.services.scheduler import create_deposit_end_reminder
from app.finance.services.analytics import analyze_portfolio, build_prompt_text, generate_portfolio_pdf_async
from app.finance.services.broker_parser import BrokerReportParser
from app.finance.services.deposits import (
    KIND_DEPOSIT,
    KIND_SAVINGS,
    DepositInputError,
    describe_deposit,
    validate_deposit_input,
)
from app.finance.services.categorizer import seed_default_categories
from app.models import ROLE_ADMIN, Asset, Category, Deposit, Reminder, Transaction, User

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

router = APIRouter(tags=["Web Pages & Dashboard API"])


class DepositCreateSchema(BaseModel):
    bank_name: str
    title: str
    kind: str = KIND_DEPOSIT  # 'deposit' — срочный вклад, 'savings' — накопительный счёт
    amount: Decimal
    interest_rate: Decimal
    opened_at: Optional[str] = None  # YYYY-MM-DD, обязательно для вклада
    term_months: Optional[int] = None  # срок в месяцах, обязательно для вклада
    capitalization: bool = False  # ежемесячная капитализация (только вклад)


class DepositBalanceSchema(BaseModel):
    amount: Decimal


class AssetCreateSchema(BaseModel):
    ticker: str
    asset_type: Optional[str] = None
    amount: Decimal
    # Необязательна: в брокерском отчёте ТБанка цены покупки нет, её можно указать позже
    average_buy_price: Optional[Decimal] = None


def _f(value: Any) -> Optional[float]:
    """Decimal -> float с сохранением None (для JSON дашборда)."""
    return float(value) if value is not None else None


def _render(request: Request, user: User, template: str, active_page: str):
    return templates.TemplateResponse(
        request=request, name=template, context={"active_page": active_page, "current_user": user}
    )


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, user: Optional[User] = Depends(get_optional_user)):
    if user:
        return RedirectResponse(url="/", status_code=303)
    return templates.TemplateResponse(request=request, name="login.html")


def _page(path: str, template: str, active_page: str, admin_only: bool = False) -> None:
    """Регистрирует HTML-страницу: без входа — редирект на /login, для admin_only — на главную."""

    async def handler(request: Request, user: Optional[User] = Depends(get_optional_user)):
        if not user:
            return RedirectResponse(url="/login", status_code=303)
        if admin_only and user.role != ROLE_ADMIN:
            return RedirectResponse(url="/", status_code=303)
        return _render(request, user, template, active_page)

    handler.__name__ = f"{active_page}_page"
    router.add_api_route(path, handler, methods=["GET"], response_class=HTMLResponse)


_page("/", "index.html", "dashboard")
_page("/health-dashboard", "health.html", "health")
_page('/workouts', 'workouts.html', 'workouts')
_page("/transactions", "transactions.html", "transactions")
_page("/upload", "upload.html", "upload")
_page("/income", "income.html", "income")
_page("/reminders", "reminders.html", "reminders")
_page("/limits", "limits.html", "limits")
_page("/users", "users.html", "users", admin_only=True)


# =========================================================================
# УПРАВЛЕНИЕ ВКЛАДАМИ И АКТИВАМИ ПРЯМО С ДАШБОРДА
# =========================================================================
@router.post("/api/deposits")
async def add_deposit(
    payload: DepositCreateSchema,
    scope: DataScope = Depends(get_scope),
    session: AsyncSession = Depends(get_async_session),
) -> Dict[str, Any]:
    opened_at: Optional[date] = None
    if payload.opened_at:
        try:
            opened_at = date.fromisoformat(payload.opened_at[:10])
        except ValueError:
            raise HTTPException(status_code=422, detail="Дата открытия указана неверно.")

    try:
        fields = validate_deposit_input(
            kind=payload.kind,
            amount=payload.amount,
            interest_rate=payload.interest_rate,
            opened_at=opened_at,
            term_months=payload.term_months,
            capitalization=payload.capitalization,
        )
    except DepositInputError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    end_dt: Optional[datetime] = None
    if fields["end_date"]:
        end_dt = datetime.combine(fields["end_date"], time.min, tzinfo=timezone.utc)

    dep = Deposit(
        user_id=scope.owner_id,
        bank_name=payload.bank_name.strip(),
        title=payload.title.strip(),
        kind=fields["kind"],
        amount=payload.amount,
        interest_rate=payload.interest_rate,
        opened_at=fields["opened_at"],
        term_months=fields["term_months"],
        capitalization=fields["capitalization"],
        end_date=end_dt,
    )
    session.add(dep)
    await session.flush()  # нужен dep.id и user_id для напоминания

    # Срочный вклад: напоминание в календарь за день до окончания срока
    reminder = await create_deposit_end_reminder(session, dep)
    await session.commit()
    return {"status": "ok", "id": dep.id, "reminder_created": reminder is not None}


@router.patch("/api/deposits/{deposit_id}/balance")
async def update_savings_balance(
    deposit_id: int,
    payload: DepositBalanceSchema,
    scope: DataScope = Depends(get_scope),
    session: AsyncSession = Depends(get_async_session),
) -> Dict[str, Any]:
    """Обновляет остаток накопительного счёта после пополнения или снятия."""
    dep = (
        await session.execute(scope.apply(select(Deposit).where(Deposit.id == deposit_id), Deposit))
    ).scalar_one_or_none()
    if not dep:
        raise HTTPException(status_code=404, detail="Вклад не найден")
    if dep.kind != KIND_SAVINGS:
        raise HTTPException(
            status_code=400, detail="Менять сумму можно только у накопительного счёта: вклад фиксирован."
        )
    if payload.amount < 0:
        raise HTTPException(status_code=422, detail="Остаток не может быть отрицательным.")
    dep.amount = payload.amount
    await session.commit()
    return {"status": "ok"}


@router.delete("/api/deposits/{deposit_id}")
async def delete_deposit(
    deposit_id: int,
    scope: DataScope = Depends(get_scope),
    session: AsyncSession = Depends(get_async_session),
) -> Dict[str, Any]:
    dep = (
        await session.execute(scope.apply(select(Deposit).where(Deposit.id == deposit_id), Deposit))
    ).scalar_one_or_none()
    if not dep:
        raise HTTPException(status_code=404, detail="Вклад не найден")
    await session.delete(dep)
    await session.commit()
    return {"status": "ok"}


@router.post("/api/assets")
async def add_or_update_asset(
    payload: AssetCreateSchema,
    scope: DataScope = Depends(get_scope),
    session: AsyncSession = Depends(get_async_session),
) -> Dict[str, Any]:
    ticker = payload.ticker.strip().upper()
    detected_type = payload.asset_type or BrokerReportParser.detect_asset_type(ticker)

    existing = (
        await session.execute(
            select(Asset).where(Asset.user_id == scope.owner_id, Asset.ticker == ticker)
        )
    ).scalar_one_or_none()

    if existing:
        existing.amount = payload.amount
        if payload.average_buy_price is not None:
            existing.average_buy_price = payload.average_buy_price
        existing.asset_type = detected_type
    else:
        session.add(
            Asset(
                user_id=scope.owner_id,
                ticker=ticker,
                asset_type=detected_type,
                amount=payload.amount,
                average_buy_price=payload.average_buy_price,
            )
        )
    await session.commit()
    return {"status": "ok", "ticker": ticker}


@router.delete("/api/assets/{ticker}")
async def delete_asset(
    ticker: str,
    scope: DataScope = Depends(get_scope),
    session: AsyncSession = Depends(get_async_session),
) -> Dict[str, Any]:
    stmt = scope.apply(select(Asset).where(Asset.ticker == ticker.strip().upper()), Asset)
    asset = (await session.execute(stmt)).scalars().first()
    if not asset:
        raise HTTPException(status_code=404, detail="Актив не найден")
    await session.delete(asset)
    await session.commit()
    return {"status": "ok"}


@router.get("/api/portfolio/report.pdf")
async def download_portfolio_pdf(
    scope: DataScope = Depends(get_scope),
    session: AsyncSession = Depends(get_async_session),
) -> Response:
    """PDF-досье портфеля с готовым промптом для ИИ (без персональных данных)."""
    report = await analyze_portfolio(session, scope.owner_id)
    if not report["positions"]:
        raise HTTPException(status_code=404, detail="Портфель пуст: сначала загрузите брокерский отчет")
    pdf_bytes = await generate_portfolio_pdf_async(report)
    filename = f"portfolio_{date.today().strftime('%Y_%m_%d')}.pdf"
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/api/portfolio/prompt.txt")
async def download_portfolio_prompt(
    scope: DataScope = Depends(get_scope),
    session: AsyncSession = Depends(get_async_session),
) -> Response:
    """Тот же промпт, что в PDF, — обычным текстом для копирования."""
    report = await analyze_portfolio(session, scope.owner_id)
    if not report["positions"]:
        raise HTTPException(status_code=404, detail="Портфель пуст: сначала загрузите брокерский отчет")
    return Response(content=build_prompt_text(report), media_type="text/plain; charset=utf-8")


# =========================================================================
# ЕДИНЫЙ ЭНДПОИНТ СТАТИСТИКИ ДАШБОРДА (ГИБРИДНЫЙ ПЕРИОД 1С + ВКЛАДЫ + MOEX)
# =========================================================================
@router.get("/api/dashboard-stats")
async def get_dashboard_stats(
    start_date: Optional[str] = Query(default=None, description="Дата начала YYYY-MM-DD"),
    end_date: Optional[str] = Query(default=None, description="Дата окончания YYYY-MM-DD"),
    include_transfers: bool = Query(default=False, description="Включать ли категории 12 и 13 в графики"),
    scope: DataScope = Depends(get_scope),
    session: AsyncSession = Depends(get_async_session),
) -> Dict[str, Any]:
    """Сводка дашборда. Обычный пользователь видит только свои данные; admin — свои, чужие или все.

    Инвестпортфель (MOEX) всегда считается по владельцу профиля (scope.owner_id).
    """
    await seed_default_categories(session)

    stmt = select(
        Category.name,
        Category.type,
        Category.is_mandatory,
        Transaction.amount,
        Transaction.bank_name,
        Transaction.date,
    ).outerjoin(Category, Transaction.category_id == Category.id)
    stmt = scope.apply(stmt, Transaction)

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

    summary = summarize_dashboard(
        [(name, ctype, mandatory, amount, bank) for name, ctype, mandatory, amount, bank, _ in rows],
        include_transfers=include_transfers,
    )
    clean_income = summary["clean_income"]
    clean_expense = summary["clean_expense"]
    total_income_all = summary["total_income_all"]
    total_expense_all = summary["total_expense_all"]
    mandatory_expense = summary["mandatory_expense"]
    by_category = summary["by_category"]
    by_bank = summary["by_bank"]
    # Свободный денежный поток = доход − ВСЕ расходы (включая необязательные: такси, кафе и т.д.)
    cash_flow = summary["cash_flow"]

    uncategorized_count = (
        await session.execute(
            scope.apply(select(func.count(Transaction.id)).where(Transaction.category_id.is_(None)), Transaction)
        )
    ).scalar() or 0

    active_reminders_count = (
        await session.execute(
            scope.apply(select(func.count(Reminder.id)).where(Reminder.is_notified.is_(False)), Reminder)
        )
    ).scalar() or 0

    cash_balance = await get_cash_balance(session, scope.filter_id)

    sorted_cats = sorted(by_category.items(), key=lambda x: x[1], reverse=True)
    sorted_banks = sorted(by_bank.items(), key=lambda x: x[1], reverse=True)

    # Вклады
    deposits_rows = (
        await session.execute(scope.apply(select(Deposit), Deposit).order_by(Deposit.amount.desc()))
    ).scalars().all()
    deposits_list = []
    total_deposits_rub = Decimal("0.00")
    for d in deposits_rows:
        total_deposits_rub += d.amount
        deposits_list.append(describe_deposit(d))

    # Портфель MOEX
    portfolio_raw = await analyze_portfolio(session, scope.owner_id)
    portfolio_positions = [
        {
            "ticker": p["ticker"],
            "name": p["name"],
            "asset_type": p["asset_type"],
            "amount": float(p["amount"]),
            "avg_buy_price": _f(p["avg_buy_price"]),
            "cost_known": p["cost_known"],
            "current_price": _f(p["current_price"]),
            "price_known": p["price_known"],
            "invested_value": _f(p["invested_value"]),
            "current_value": float(p["current_value"]),
            "pnl": _f(p["pnl"]),
            "pnl_pct": _f(p["pnl_pct"]),
            "share_pct": float(p["share_pct"]),
            "annual_income": _f(p["annual_income"]),
            "mat_date": p["mat_date"].isoformat() if p.get("mat_date") else None,
            "yield_pct": _f(p.get("yield_pct")),
        }
        for p in portfolio_raw["positions"]
    ]
    forecast = portfolio_raw["income_forecast"]

    return {
        "clean_income": float(clean_income),
        "clean_expense": float(clean_expense),
        "total_income_all": float(total_income_all),
        "total_expense_all": float(total_expense_all),
        "mandatory_expense": float(mandatory_expense),
        "cash_flow": float(cash_flow),
        "uncategorized_count": uncategorized_count,
        "cash_balance": float(cash_balance),
        "active_reminders_count": active_reminders_count,
        "categories_labels": [k for k, _ in sorted_cats],
        "categories_values": [float(v) for _, v in sorted_cats],
        "banks_labels": [k for k, _ in sorted_banks],
        "banks_values": [float(v) for _, v in sorted_banks],
        "deposits": deposits_list,
        "total_deposits": float(total_deposits_rub),
        "portfolio": {
            "total_invested": float(portfolio_raw["total_invested"]),
            "total_current": float(portfolio_raw["total_current"]),
            "total_pnl": _f(portfolio_raw["total_pnl"]),
            "total_pnl_pct": _f(portfolio_raw["total_pnl_pct"]),
            "cost_known_count": portfolio_raw["cost_known_count"],
            "stocks_share_pct": float(portfolio_raw["stocks_share_pct"]),
            "bonds_share_pct": float(portfolio_raw["bonds_share_pct"]),
            "funds_share_pct": float(portfolio_raw["funds_share_pct"]),
            "cash_rub": _f(portfolio_raw["cash_rub"]),
            "positions": portfolio_positions,
            "income_months": [{"label": m["label"], "amount": float(m["amount"])} for m in forecast["months"]],
            "income_total": float(forecast["total"]),
            "income_monthly_avg": float(forecast["monthly_avg"]),
            "findings": portfolio_raw["findings"],
            "rebalance_advice": portfolio_raw["rebalance_advice"],
        },
    }
