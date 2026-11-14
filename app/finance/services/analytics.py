import asyncio
from datetime import date
from decimal import Decimal
from typing import Any, Dict, List

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Asset, User
from app.finance.services.invest_api import MoexClient
from app.finance.services.moex_data import InstrumentData, fetch_instruments
from app.finance.services.portfolio_insights import build_findings, build_income_forecast, build_statistics
from app.finance.services.portfolio_pdf import build_ai_prompt, generate_portfolio_pdf  # noqa: F401  (реэкспорт)


async def analyze_portfolio(
    session: AsyncSession,
    user_id: int,
    target_stocks_pct: Decimal = Decimal("70.0"),
    target_bonds_pct: Decimal = Decimal("30.0"),
) -> Dict[str, Any]:
    """
    Полная аналитика портфеля: стоимость и доли, P&L (там, где известна цена покупки),
    прогноз пассивного дохода (купоны MOEX и дивиденды), статистика и наблюдения по правилам.
    Всё считается арифметикой, без ИИ. Сбой MOEX не ломает отчёт: недостающие данные помечаются.
    """
    result = await session.execute(select(Asset).where(Asset.user_id == user_id))
    assets = list(result.scalars().all())
    user = await session.get(User, user_id)
    cash_rub = Decimal(str(user.broker_cash_rub)) if user is not None and user.broker_cash_rub is not None else None
    today = date.today()

    if not assets:
        empty_fc = build_income_forecast([], {}, today)
        return {
            "as_of": today,
            "positions": [],
            "total_invested": Decimal("0.00"),
            "total_current": Decimal("0.00"),
            "total_pnl": None,
            "total_pnl_pct": None,
            "cost_known_count": 0,
            "stocks_value": Decimal("0.00"),
            "bonds_value": Decimal("0.00"),
            "funds_value": Decimal("0.00"),
            "stocks_share_pct": Decimal("0.00"),
            "bonds_share_pct": Decimal("0.00"),
            "funds_share_pct": Decimal("0.00"),
            "cash_rub": cash_rub,
            "target_stocks_pct": target_stocks_pct,
            "target_bonds_pct": target_bonds_pct,
            "income_forecast": empty_fc,
            "statistics": build_statistics([], Decimal("0"), empty_fc, today),
            "findings": [],
            "rebalance_advice": ["Портфель пуст. Загрузите брокерский отчет для расчета аналитики."],
        }

    query_items = [{"ticker": a.ticker, "asset_type": a.asset_type} for a in assets]
    live_prices, instruments = await asyncio.gather(
        MoexClient.get_prices_for_tickers(query_items),
        _safe_fetch_instruments(query_items),
    )

    positions: List[Dict[str, Any]] = []
    for asset in assets:
        ticker = asset.ticker.upper()
        qty = Decimal(str(asset.amount))
        avg_buy = Decimal(str(asset.average_buy_price)) if asset.average_buy_price is not None else None
        market_price = live_prices.get(ticker)
        # Цена: биржа; если биржа недоступна — цена покупки (если известна); иначе цены нет.
        current_price = market_price if market_price is not None else avg_buy
        price_known = current_price is not None
        current_price = current_price if price_known else Decimal("0")

        current_val = (qty * current_price).quantize(Decimal("0.01"))
        cost_known = avg_buy is not None
        invested_val = (qty * avg_buy).quantize(Decimal("0.01")) if cost_known else None
        pnl_rub = (current_val - invested_val).quantize(Decimal("0.01")) if cost_known and price_known else None
        pnl_pct = (
            ((pnl_rub / invested_val) * Decimal("100")).quantize(Decimal("0.01"))
            if pnl_rub is not None and invested_val and invested_val > 0
            else None
        )

        inst = instruments.get(ticker)
        bond = inst.bond if inst else None
        positions.append(
            {
                "ticker": ticker,
                "name": asset.name or ticker,
                "isin": asset.isin,
                "asset_type": asset.asset_type,
                "amount": qty.quantize(Decimal("0.01")) if qty % 1 != 0 else Decimal(int(qty)),
                "avg_buy_price": avg_buy.quantize(Decimal("0.01")) if cost_known else None,
                "cost_known": cost_known,
                "current_price": current_price.quantize(Decimal("0.01")),
                "price_known": price_known,
                "price_from_moex": market_price is not None,
                "invested_value": invested_val,
                "current_value": current_val,
                "pnl": pnl_rub,
                "pnl_pct": pnl_pct,
                "share_pct": Decimal("0.00"),
                "is_corporate": asset.asset_type == "bond" and not ticker.startswith("SU"),
                "mat_date": bond.mat_date if bond else None,
                "next_coupon_date": bond.next_coupon_date if bond else None,
                "coupon_value": bond.coupon_value if bond else None,
                "yield_pct": bond.yield_pct if bond else None,
                "annual_income": None,
            }
        )

    total_current = sum((p["current_value"] for p in positions), Decimal("0.00"))
    for pos in positions:
        if total_current > 0:
            pos["share_pct"] = ((pos["current_value"] / total_current) * Decimal("100")).quantize(Decimal("0.01"))
    positions.sort(key=lambda x: x["current_value"], reverse=True)

    # P&L считаем только по бумагам, у которых известны и цена покупки, и текущая цена
    pnl_positions = [p for p in positions if p["pnl"] is not None]
    total_invested = sum((p["invested_value"] for p in pnl_positions), Decimal("0.00"))
    total_pnl = sum((p["pnl"] for p in pnl_positions), Decimal("0.00")) if pnl_positions else None
    total_pnl_pct = (
        ((total_pnl / total_invested) * Decimal("100")).quantize(Decimal("0.01"))
        if total_pnl is not None and total_invested > 0
        else None
    )

    def _value(kind: str) -> Decimal:
        return sum((p["current_value"] for p in positions if p["asset_type"] == kind), Decimal("0.00"))

    def _share(v: Decimal) -> Decimal:
        return ((v / total_current) * Decimal("100")).quantize(Decimal("0.01")) if total_current > 0 else Decimal("0.00")

    stocks_value, bonds_value, funds_value = _value("stock"), _value("bond"), _value("fund")
    stocks_share_pct, bonds_share_pct, funds_share_pct = _share(stocks_value), _share(bonds_value), _share(funds_value)

    forecast = build_income_forecast(positions, instruments, today)
    for pos in positions:
        info = forecast["by_ticker"].get(pos["ticker"])
        pos["annual_income"] = info["annual"] if info else None

    statistics = build_statistics(positions, total_current, forecast, today)
    findings = build_findings(positions, total_current, cash_rub, forecast, today)

    rebalance_advice = _build_rebalance_recommendations(
        positions=positions,
        total_current=total_current,
        stocks_value=stocks_value,
        bonds_value=bonds_value,
        stocks_share_pct=stocks_share_pct,
        bonds_share_pct=bonds_share_pct,
        target_stocks_pct=target_stocks_pct,
        target_bonds_pct=target_bonds_pct,
    )

    return {
        "as_of": today,
        "positions": positions,
        "total_invested": total_invested,
        "total_current": total_current,
        "total_pnl": total_pnl,
        "total_pnl_pct": total_pnl_pct,
        "cost_known_count": len(pnl_positions),
        "stocks_value": stocks_value,
        "bonds_value": bonds_value,
        "funds_value": funds_value,
        "stocks_share_pct": stocks_share_pct,
        "bonds_share_pct": bonds_share_pct,
        "funds_share_pct": funds_share_pct,
        "cash_rub": cash_rub,
        "target_stocks_pct": target_stocks_pct,
        "target_bonds_pct": target_bonds_pct,
        "income_forecast": forecast,
        "statistics": statistics,
        "findings": findings,
        "rebalance_advice": rebalance_advice,
    }


async def _safe_fetch_instruments(items: List[Dict[str, str]]) -> Dict[str, InstrumentData]:
    """Обогащение данными MOEX не должно ронять отчёт."""
    try:
        return await fetch_instruments(items)
    except Exception:
        return {}


def _build_rebalance_recommendations(
    positions: List[Dict[str, Any]],
    total_current: Decimal,
    stocks_value: Decimal,
    bonds_value: Decimal,
    stocks_share_pct: Decimal,
    bonds_share_pct: Decimal,
    target_stocks_pct: Decimal,
    target_bonds_pct: Decimal,
) -> List[str]:
    """
    Математический алгоритм расчета рекомендаций по ребалансировке портфеля.
    Возвращает конкретный список: что купить/продать и в каком количестве.
    """
    if total_current <= 0:
        return ["Недостаточно данных для расчета баланса."]

    advice: List[str] = []
    target_stocks_val = (total_current * (target_stocks_pct / Decimal("100"))).quantize(Decimal("0.01"))
    target_bonds_val = (total_current * (target_bonds_pct / Decimal("100"))).quantize(Decimal("0.01"))

    diff_stocks = (target_stocks_val - stocks_value).quantize(Decimal("0.01"))
    diff_bonds = (target_bonds_val - bonds_value).quantize(Decimal("0.01"))

    advice.append(
        f"Текущая структура: Акции {stocks_share_pct}% ({stocks_value} ₽) | "
        f"Облигации {bonds_share_pct}% ({bonds_value} ₽). "
        f"Целевой ориентир: {target_stocks_pct}% / {target_bonds_pct}%."
    )

    # Если отклонение меньше 2%, портфель сбалансирован
    if abs(stocks_share_pct - target_stocks_pct) <= Decimal("2.0"):
        advice.append("Баланс классов активов в норме (отклонение менее 2%). Ребалансировка не требуется.")
    else:
        stock_positions = [p for p in positions if p["asset_type"] == "stock" and p["current_price"] > 0]
        bond_positions = [p for p in positions if p["asset_type"] == "bond" and p["current_price"] > 0]

        if diff_stocks > 0:
            # Не хватает акций (перевес в облигациях)
            advice.append(
                f"Дефицит акций: необходимо увеличить долю акций на {diff_stocks} ₽ "
                f"(или сократить облигации на эту же сумму)."
            )
            if stock_positions:
                underweight_stock = min(stock_positions, key=lambda x: x["share_pct"])
                qty_to_buy = int(diff_stocks // underweight_stock["current_price"])
                if qty_to_buy > 0:
                    advice.append(
                        f"КУПИТЬ: {underweight_stock['ticker']} (наименьшая доля среди акций) — "
                        f"{qty_to_buy} шт. по ~{underweight_stock['current_price']} ₽ "
                        f"(на сумму {(qty_to_buy * underweight_stock['current_price']):.2f} ₽)."
                    )
            if bond_positions:
                overweight_bond = max(bond_positions, key=lambda x: x["share_pct"])
                qty_to_sell = int(diff_stocks // overweight_bond["current_price"])
                if qty_to_sell > 0:
                    advice.append(
                        f"Или ПРОДАТЬ (при ребалансировке без пополнения): {overweight_bond['ticker']} — "
                        f"{qty_to_sell} шт. по ~{overweight_bond['current_price']} ₽."
                    )
        elif diff_bonds > 0:
            # Не хватает облигаций (перевес в акциях)
            advice.append(
                f"Дефицит облигаций: необходимо увеличить защитную часть (облигации) на {diff_bonds} ₽."
            )
            if bond_positions:
                underweight_bond = min(bond_positions, key=lambda x: x["share_pct"])
                qty_to_buy = int(diff_bonds // underweight_bond["current_price"])
                if qty_to_buy > 0:
                    advice.append(
                        f"КУПИТЬ: {underweight_bond['ticker']} (наименьшая доля среди облигаций) — "
                        f"{qty_to_buy} шт. по ~{underweight_bond['current_price']} ₽ "
                        f"(на сумму {(qty_to_buy * underweight_bond['current_price']):.2f} ₽)."
                    )
            if stock_positions:
                overweight_stock = max(stock_positions, key=lambda x: x["share_pct"])
                qty_to_sell = int(diff_bonds // overweight_stock["current_price"])
                if qty_to_sell > 0:
                    advice.append(
                        f"Или ПРОДАТЬ (для фиксации перевеса в акциях): {overweight_stock['ticker']} — "
                        f"{qty_to_sell} шт. по ~{overweight_stock['current_price']} ₽."
                    )

    # Проверка риска концентрации на один тикер (если одна бумага > 35% портфеля)
    if len(positions) > 1:
        for pos in positions:
            if pos["share_pct"] > Decimal("35.0") and pos["current_price"] > 0:
                excess_val = (((pos["share_pct"] - Decimal("25.0")) / Decimal("100")) * total_current).quantize(Decimal("0.01"))
                qty_trim = int(excess_val // pos["current_price"])
                if qty_trim > 0:
                    advice.append(
                        f"Риск концентрации: бумага {pos['ticker']} занимает {pos['share_pct']}% портфеля. "
                        f"Для снижения доли до безопасных 25% рекомендуется сократить позицию на {qty_trim} шт. (~{excess_val} ₽)."
                    )

    return advice


async def generate_portfolio_pdf_async(report: Dict[str, Any]) -> bytes:
    """Асинхронная обертка генерации PDF-досье (см. services/portfolio_pdf.py)."""
    return await asyncio.to_thread(generate_portfolio_pdf, report)


def build_prompt_text(report: Dict[str, Any]) -> str:
    """Тот же промпт, что внутри PDF, — отдельным текстом для копирования."""
    return build_ai_prompt(report)
