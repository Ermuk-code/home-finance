"""Прогноз пассивного дохода, статистика и правила-наблюдения по портфелю.

Всё — обычная арифметика без ИИ. Модуль не зависит от БД и сети: на вход — готовые словари позиций
и данные MOEX (moex_data.InstrumentData), на выход — словари для PDF / бота / дашборда.

Важно: «наблюдения» — это факты о портфеле («60% в одной бумаге»), а не призывы купить или продать.
Решения по ним принимает человек или внешний ИИ по промпту из PDF.
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Dict, List, Optional

from app.finance.services.moex_data import InstrumentData

_ZERO = Decimal("0")
_Q2 = Decimal("0.01")


def _rub(v: Decimal) -> str:
    """1234.5 -> '1 234,50' (рубли в русском формате)."""
    return f"{Decimal(v).quantize(_Q2):,.2f}".replace(",", " ").replace(".", ",")


def _add_months(d: date, n: int) -> date:
    y, m = divmod(d.month - 1 + n, 12)
    return date(d.year + y, m + 1, 1)


def month_labels(today: date, months: int = 12) -> List[str]:
    first = date(today.year, today.month, 1)
    return [_add_months(first, i).strftime("%Y-%m") for i in range(months)]


# ---------------------------------------------------------------- прогноз дохода

def build_income_forecast(
    positions: List[Dict[str, Any]],
    instruments: Dict[str, InstrumentData],
    today: date,
    months: int = 12,
) -> Dict[str, Any]:
    """Ожидаемые выплаты на ближайшие `months` месяцев (брутто, до налога).

    * облигации — по графику купонов MOEX; купон без суммы (плавающий) в прогноз не входит;
    * акции и фонды — по выплатам за последние 12 месяцев, сдвинутым на год вперёд (это оценка,
      а не обещание: дивиденды могут быть отменены или изменены).
    """
    labels = month_labels(today, months)
    by_month: Dict[str, Decimal] = {lb: _ZERO for lb in labels}
    window_end = _add_months(date(today.year, today.month, 1), months)  # не включительно

    by_ticker: Dict[str, Dict[str, Any]] = {}
    unknown_coupons: List[str] = []
    no_data: List[str] = []

    for pos in positions:
        ticker = pos["ticker"]
        qty = Decimal(str(pos["amount"]))
        data = instruments.get(ticker)
        annual = _ZERO

        if pos["asset_type"] == "bond":
            bond = data.bond if data else None
            if not bond or not bond.coupons:
                no_data.append(ticker)
                continue
            has_unknown = False
            for c in bond.coupons:
                if c.pay_date <= today or c.pay_date >= window_end:
                    continue
                if c.amount is None:
                    has_unknown = True
                    continue
                amount = (c.amount * qty).quantize(_Q2)
                by_month[c.pay_date.strftime("%Y-%m")] += amount
                annual += amount
            if has_unknown:
                unknown_coupons.append(ticker)
            by_ticker[ticker] = {"kind": "coupon", "annual": annual.quantize(_Q2)}
        else:
            divs = data.dividends if data else []
            ttm = [d for d in divs if today - timedelta(days=365) < d.record_date <= today]
            if not ttm:
                no_data.append(ticker)
                continue
            for d in ttm:
                projected = d.record_date + timedelta(days=365)
                if projected <= today or projected >= window_end:
                    continue
                amount = (d.amount * qty).quantize(_Q2)
                by_month[projected.strftime("%Y-%m")] += amount
                annual += amount
            by_ticker[ticker] = {"kind": "dividend", "annual": annual.quantize(_Q2)}

    total = sum(by_month.values(), _ZERO).quantize(_Q2)
    return {
        "months": [{"label": lb, "amount": by_month[lb].quantize(_Q2)} for lb in labels],
        "total": total,
        "monthly_avg": (total / Decimal(months)).quantize(_Q2),
        "by_ticker": by_ticker,
        "unknown_coupons": unknown_coupons,
        "no_data": no_data,
    }


# ---------------------------------------------------------------- статистика

def build_statistics(positions: List[Dict[str, Any]], total_current: Decimal,
                     forecast: Dict[str, Any], today: date) -> Dict[str, Any]:
    bonds = [p for p in positions if p["asset_type"] == "bond" and p["current_value"] > 0]

    wavg_yield = None
    with_yield = [p for p in bonds if p.get("yield_pct") is not None]
    if with_yield:
        w = sum((p["current_value"] for p in with_yield), _ZERO)
        wavg_yield = (sum((p["current_value"] * p["yield_pct"] for p in with_yield), _ZERO) / w).quantize(_Q2)

    wavg_years = None
    with_mat = [p for p in bonds if p.get("mat_date")]
    if with_mat:
        w = sum((p["current_value"] for p in with_mat), _ZERO)
        years = sum((p["current_value"] * Decimal((p["mat_date"] - today).days) / Decimal("365") for p in with_mat), _ZERO)
        wavg_years = (years / w).quantize(_Q2)

    income_yield = None
    if total_current > 0 and forecast["total"] > 0:
        income_yield = (forecast["total"] / total_current * Decimal("100")).quantize(_Q2)

    return {
        "positions_count": len(positions),
        "bonds_avg_yield_pct": wavg_yield,
        "bonds_avg_years": wavg_years,
        "income_yield_pct": income_yield,
        "top_position_share_pct": max((p["share_pct"] for p in positions), default=_ZERO),
    }


# ---------------------------------------------------------------- правила

def build_findings(
    positions: List[Dict[str, Any]],
    total_current: Decimal,
    cash_rub: Optional[Decimal],
    forecast: Dict[str, Any],
    today: date,
) -> List[Dict[str, str]]:
    """Список наблюдений: {"level": "warn"|"info", "text": ...}."""
    out: List[Dict[str, str]] = []

    def add(level: str, text: str) -> None:
        out.append({"level": level, "text": text})

    if not positions:
        return out

    missing = [p["ticker"] for p in positions if not p["price_known"]]
    if missing:
        add("warn", f"Нет цены MOEX для: {', '.join(missing)}. Стоимость и доли посчитаны без этих бумаг.")

    n = len(positions)
    if n < 5:
        add("info", f"В портфеле всего {n} бумаг(и): диверсификация низкая, один неудачный эмитент заметно влияет на результат.")

    for p in positions:
        if n > 1 and p["share_pct"] > Decimal("35"):
            add("warn", f"{p['name']} занимает {p['share_pct']}% портфеля — высокая концентрация в одной бумаге.")

    kinds = {p["asset_type"] for p in positions}
    if "stock" not in kinds:
        add("info", "В портфеле нет акций: доход состоит только из купонов и выплат фондов, а защиты от инфляции через рост бизнеса нет.")
    if kinds == {"stock"}:
        add("info", "В портфеле только акции: дивиденды не гарантированы и цена может заметно колебаться.")

    bonds = [p for p in positions if p["asset_type"] == "bond" and p["current_value"] > 0]
    bonds_value = sum((p["current_value"] for p in bonds), _ZERO)
    if bonds_value > 0:
        long_value = sum(
            (p["current_value"] for p in bonds if p.get("mat_date") and (p["mat_date"] - today).days > 5 * 365),
            _ZERO,
        )
        if long_value / bonds_value > Decimal("0.5"):
            add("info", "Больше половины облигаций погашается позже чем через 5 лет: их цена заметно падает, если ключевая ставка растёт.")
        corp = sum((p["current_value"] for p in bonds if p.get("is_corporate")), _ZERO)
        if total_current > 0 and corp / total_current > Decimal("0.3"):
            add("info", "Свыше 30% портфеля в корпоративных облигациях: доходность выше, чем у ОФЗ, но есть риск, что эмитент не заплатит.")

    for p in bonds:
        if p.get("mat_date") and 0 <= (p["mat_date"] - today).days <= 90:
            repay = (p["amount"] * Decimal("1000")).quantize(_Q2)
            add("info", f"{p['name']} погашается {p['mat_date'].strftime('%d.%m.%Y')}: вернётся номинал (около {_rub(repay)} ₽) — заранее решите, куда его вложить.")
        if p.get("next_coupon_date") and 0 <= (p["next_coupon_date"] - today).days <= 14:
            add("info", f"Ближайший купон по {p['name']} — {p['next_coupon_date'].strftime('%d.%m.%Y')}.")

    if forecast["unknown_coupons"]:
        add("info", "По части облигаций размер будущих купонов ещё не определён (плавающий купон), поэтому прогноз дохода по ним занижен: "
            + ", ".join(forecast["unknown_coupons"]) + ".")
    if forecast["no_data"]:
        add("info", "Для прогноза дохода нет данных по: " + ", ".join(forecast["no_data"])
            + " (у акций и фондов прогноз строится по выплатам за прошлый год).")

    if cash_rub is not None and total_current + cash_rub > 0:
        share = cash_rub / (total_current + cash_rub) * Decimal("100")
        if cash_rub >= Decimal("1000") and share > Decimal("5"):
            add("info", f"На брокерском счёте свободно {_rub(cash_rub)} ₽ ({share.quantize(Decimal('0.1'))}% от всех средств): деньги простаивают без дохода.")

    unknown_cost = [p["ticker"] for p in positions if not p["cost_known"]]
    if unknown_cost:
        add("info", "Цена покупки неизвестна (в отчёте её нет), поэтому прибыль/убыток не считается: " + ", ".join(unknown_cost) + ". Её можно указать вручную в дашборде.")

    return out
