import io
from datetime import date
from decimal import Decimal as D

import pdfplumber

from app.finance.services.moex_data import BondInfo, Coupon, DividendPayment, InstrumentData
from app.finance.services.portfolio_insights import build_findings, build_income_forecast, build_statistics
from app.finance.services.portfolio_pdf import build_ai_prompt, generate_portfolio_pdf

TODAY = date(2026, 11, 7)


def _position(ticker, name, kind, amount, price, **kw):
    value = D(amount) * D(price)
    p = {
        "ticker": ticker, "name": name, "isin": None, "asset_type": kind, "amount": D(amount),
        "avg_buy_price": None, "cost_known": False, "current_price": D(price), "price_known": True,
        "price_from_moex": True, "invested_value": None, "current_value": value, "pnl": None, "pnl_pct": None,
        "share_pct": D("0"), "is_corporate": kind == "bond" and not ticker.startswith("SU"),
        "mat_date": None, "next_coupon_date": None, "coupon_value": None, "yield_pct": None, "annual_income": None,
    }
    p.update(kw)
    return p


def make_report():
    """Синтетический отчёт: цены и купоны выдуманы, структура — как у analyze_portfolio."""
    positions = [
        _position("SU29020RMFS3", "ОФЗ 29020", "bond", 2, "980", mat_date=date(2030, 2, 6), yield_pct=D("14.9"),
                  next_coupon_date=date(2026, 12, 2), coupon_value=D("40")),
        _position("RU000A10F9D3", "ИКС 5 ФИНАНС обб3П21", "bond", 1, "1010", mat_date=date(2027, 1, 20),
                  yield_pct=D("17.5")),
        _position("TPAY", "TPAY ETF", "fund", 7, "120"),
    ]
    total = sum((p["current_value"] for p in positions), D("0"))
    for p in positions:
        p["share_pct"] = (p["current_value"] / total * 100).quantize(D("0.01"))
    instruments = {
        "SU29020RMFS3": InstrumentData(bond=BondInfo(coupons=[Coupon(date(2026, 12, 2), D("40")), Coupon(date(2027, 6, 2), D("40"))])),
        "RU000A10F9D3": InstrumentData(bond=BondInfo(coupons=[Coupon(date(2027, 1, 20), D("45"))])),
        "TPAY": InstrumentData(dividends=[DividendPayment(date(2026, 8, 15), D("3"))]),
    }
    fc = build_income_forecast(positions, instruments, TODAY)
    for p in positions:
        info = fc["by_ticker"].get(p["ticker"])
        p["annual_income"] = info["annual"] if info else None
    return {
        "as_of": TODAY, "positions": positions, "total_invested": D("0"), "total_current": total,
        "total_pnl": None, "total_pnl_pct": None, "cost_known_count": 0,
        "stocks_value": D("0"), "bonds_value": D("0"), "funds_value": D("0"),
        "stocks_share_pct": D("0"), "bonds_share_pct": D("0"), "funds_share_pct": D("0"),
        "cash_rub": D("25.83"), "target_stocks_pct": D("70"), "target_bonds_pct": D("30"),
        "income_forecast": fc, "statistics": build_statistics(positions, total, fc, TODAY),
        "findings": build_findings(positions, total, D("25.83"), fc, TODAY),
        "rebalance_advice": ["Текущая структура: условный пример."],
    }


def test_prompt_contains_data_profile_and_rules():
    text = build_ai_prompt(make_report())
    assert "ОФЗ 29020" in text and "TPAY" in text
    assert "[__ лет]" in text                      # профиль для заполнения
    assert "Не обещай доходность" in text
    assert "Ожидаемый доход" in text


def test_pdf_is_text_not_images_and_has_all_sections():
    pdf = generate_portfolio_pdf(make_report())
    assert pdf.startswith(b"%PDF")
    with pdfplumber.open(io.BytesIO(pdf)) as doc:
        text = "\n".join(p.extract_text() or "" for p in doc.pages)
    for title in ("1. Сводка", "2. Позиции", "3. Облигации", "4. Акции и фонды",
                  "5. Ожидаемый пассивный доход", "6. Статистика", "7. Наблюдения", "8. Промпт для нейросети"):
        assert title in text, title
    assert "ИКС 5 ФИНАНС" in text and "Не обещай доходность" in text


def test_pdf_has_no_personal_data():
    pdf = generate_portfolio_pdf(make_report())
    with pdfplumber.open(io.BytesIO(pdf)) as doc:
        text = "\n".join(p.extract_text() or "" for p in doc.pages).upper()
    assert "ЕРМАКОВ" not in text and "ДОГОВОР" not in text.replace("ДОГОВОРА", "")
