from datetime import date
from decimal import Decimal as D

from app.finance.services.moex_data import (
    BondInfo, Coupon, DividendPayment, InstrumentData, parse_bond_info, parse_coupons, parse_dividends,
)
from app.finance.services.portfolio_insights import build_findings, build_income_forecast, build_statistics, month_labels

TODAY = date(2026, 11, 7)


def pos(ticker, kind="bond", amount=1, value="1000", share="50", **kw):
    base = {
        "ticker": ticker, "name": ticker, "asset_type": kind, "amount": D(amount),
        "current_value": D(value), "share_pct": D(share), "price_known": True, "cost_known": False,
        "is_corporate": False, "mat_date": None, "next_coupon_date": None, "yield_pct": None,
    }
    base.update(kw)
    return base


def test_month_labels_cross_year():
    assert month_labels(date(2026, 11, 7), 3) == ["2026-11", "2026-12", "2027-01"]


def test_forecast_bond_coupons_in_window_only():
    bond = BondInfo(coupons=[
        Coupon(date(2026, 10, 1), D("40")),    # прошёл — не считаем
        Coupon(date(2026, 12, 10), D("40")),
        Coupon(date(2027, 6, 10), D("41")),
        Coupon(date(2027, 12, 10), D("42")),   # вне окна 12 месяцев
    ])
    fc = build_income_forecast([pos("B1", amount=2)], {"B1": InstrumentData(bond=bond)}, TODAY)
    months = {m["label"]: m["amount"] for m in fc["months"]}
    assert months["2026-12"] == D("80.00")
    assert months["2027-06"] == D("82.00")
    assert fc["total"] == D("162.00")
    assert fc["monthly_avg"] == D("13.50")
    assert fc["by_ticker"]["B1"]["annual"] == D("162.00")


def test_forecast_floating_coupon_is_flagged_not_counted():
    bond = BondInfo(coupons=[Coupon(date(2026, 12, 10), D("40")), Coupon(date(2027, 3, 10), None)])
    fc = build_income_forecast([pos("FL")], {"FL": InstrumentData(bond=bond)}, TODAY)
    assert fc["total"] == D("40.00")
    assert fc["unknown_coupons"] == ["FL"]


def test_forecast_dividends_repeat_last_year_payments():
    divs = [DividendPayment(date(2026, 7, 15), D("5")), DividendPayment(date(2025, 7, 15), D("4"))]
    fc = build_income_forecast([pos("DIV", kind="fund", amount=10)], {"DIV": InstrumentData(dividends=divs)}, TODAY)
    months = {m["label"]: m["amount"] for m in fc["months"]}
    assert months["2027-07"] == D("50.00")      # только выплата за последние 12 мес.
    assert fc["by_ticker"]["DIV"]["kind"] == "dividend"


def test_forecast_no_data_is_listed():
    fc = build_income_forecast([pos("X", kind="stock")], {}, TODAY)
    assert fc["total"] == D("0.00") and fc["no_data"] == ["X"]


def test_statistics_weighted_yield_and_years():
    p1 = pos("A", value="3000", yield_pct=D("14"), mat_date=date(2027, 11, 7))
    p2 = pos("B", value="1000", yield_pct=D("18"), mat_date=date(2029, 11, 7))
    st = build_statistics([p1, p2], D("4000"), {"total": D("0")}, TODAY)
    assert st["bonds_avg_yield_pct"] == D("15.00")
    assert st["bonds_avg_years"] == D("1.50")


def texts(findings):
    return " | ".join(f["text"] for f in findings)


def test_findings_concentration_and_few_positions():
    fc = build_income_forecast([], {}, TODAY)
    out = build_findings([pos("BIG", share="60"), pos("SMALL", share="40")], D("2000"), None, fc, TODAY)
    assert "концентрация" in texts(out) and "диверсификация низкая" in texts(out)


def test_findings_maturity_and_idle_cash():
    fc = build_income_forecast([], {}, TODAY)
    soon = pos("SOON", amount=3, mat_date=date(2026, 12, 20))
    out = build_findings([soon], D("3000"), D("5000"), fc, TODAY)
    t = texts(out)
    assert "погашается 20.12.2026" in t and "3 000,00" in t
    assert "простаивают" in t


def test_findings_small_cash_is_ignored_and_unknown_cost_noted():
    fc = build_income_forecast([], {}, TODAY)
    out = build_findings([pos("A")], D("1000"), D("25.83"), fc, TODAY)
    t = texts(out)
    assert "простаивают" not in t
    assert "Цена покупки неизвестна" in t


def test_findings_are_facts_not_buy_sell_orders():
    fc = build_income_forecast([], {}, TODAY)
    out = build_findings([pos("A", share="90"), pos("B", share="10")], D("1000"), D("9000"), fc, TODAY)
    t = texts(out).lower()
    assert "купите" not in t and "продайте" not in t


# --- разбор JSON MOEX на фикстурах той же структуры, что отдаёт ISS
def test_parse_coupons_and_floating():
    data = {"coupons": {"columns": ["coupondate", "value", "value_rub"], "data": [
        ["2027-01-15", 40.5, 40.5], ["2027-07-15", None, None], ["0000-00-00", 1, 1]]}}
    c = parse_coupons(data)
    assert [(x.pay_date, x.amount) for x in c] == [(date(2027, 1, 15), D("40.5")), (date(2027, 7, 15), None)]


def test_parse_bond_info_prefers_government_board_and_yield():
    data = {
        "securities": {"columns": ["BOARDID", "MATDATE", "NEXTCOUPON", "COUPONVALUE", "COUPONPERIOD", "FACEVALUE"],
                       "data": [["TQOB", "2030-01-02", "2026-12-01", 35.4, 182, 1000]]},
        "marketdata_yields": {"columns": ["BOARDID", "EFFECTIVEYIELD"], "data": [["TQOB", 15.2]]},
    }
    b = parse_bond_info(data)
    assert b.mat_date == date(2030, 1, 2) and b.coupon_value == D("35.4") and b.yield_pct == D("15.2")


def test_parse_dividends_skips_foreign_currency():
    data = {"dividends": {"columns": ["registryclosedate", "value", "currencyid"],
                          "data": [["2026-07-15", 10, "SUR"], ["2026-07-16", 2, "USD"]]}}
    assert [(d.record_date, d.amount) for d in parse_dividends(data)] == [(date(2026, 7, 15), D("10"))]
