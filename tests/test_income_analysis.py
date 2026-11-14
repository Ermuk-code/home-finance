from datetime import date, datetime, timedelta, timezone
from decimal import Decimal as D

import pytest

from app.finance.services.income_analysis import (
    IncomeTx, build_analysis, build_source_detail, classify_income, normalize_rule,
)

MSK = timezone(timedelta(hours=3))
SAL, INT, TIN = "Зарплата и Стипендия", "Проценты по вкладам и Кэшбэк", "Переводы и Пополнения (Входящие)"


def tx(i, d, amount, desc, cat=None, ctype="income"):
    return IncomeTx(id=i, date=d, amount=D(str(amount)), description=desc, category=cat, category_type=ctype, bank="Т-Банк")


def dd(y, m, d):
    return datetime(y, m, d, 12, 0, tzinfo=MSK)


PARENTS = [normalize_rule("Иванов Пётр", "parents")]


def test_classify_builtin_sources():
    assert classify_income("Стипендия учащимся за октябрь", SAL) == "scholarship"
    assert classify_income("Заработная плата за октябрь", SAL) == "salary"
    assert classify_income("Аванс по заработной плате", SAL) == "salary"
    assert classify_income("Кэшбэк за покупки", INT) == "interest"
    assert classify_income("Перевод от Сидоров А.", TIN) == "transfers"
    assert classify_income("Перевод себе", TIN) == "own"
    assert classify_income("Что-то странное", None) == "other"


def test_scholarship_is_not_mixed_into_salary_even_in_same_category():
    # В базе стипендия и зарплата лежат в одной категории — разделяем по тексту
    assert classify_income("стипендия", SAL) != classify_income("зарплата", SAL)


def test_user_rule_beats_builtin_and_matches_case_insensitively():
    assert classify_income("Перевод от ИВАНОВ  пётр А.", TIN, PARENTS) == "parents"
    assert classify_income("Перевод от Сидоров А.", TIN, PARENTS) == "transfers"


def test_rule_validation():
    assert normalize_rule("  Мама  Анна ", "parents") == ("мама анна", "parents")
    with pytest.raises(ValueError):
        normalize_rule("ab", "parents")
    with pytest.raises(ValueError):
        normalize_rule("мама", "wallet")


def test_refund_in_expense_category_is_not_income():
    t = [tx(1, dd(2026, 10, 5), 300, "Возврат", "Продукты", "expense"), tx(2, dd(2026, 10, 6), 100, "Зарплата", SAL)]
    r = build_analysis(t)
    assert r["total"] == 100.0


def test_analysis_totals_months_and_shares():
    t = [
        tx(1, dd(2026, 9, 10), 50000, "Заработная плата", SAL),
        tx(2, dd(2026, 10, 10), 50000, "Заработная плата", SAL),
        tx(3, dd(2026, 10, 15), 5000, "Стипендия", SAL),
        tx(4, dd(2026, 10, 20), 10000, "Перевод от Иванов Пётр", TIN),
        tx(5, dd(2026, 11, 2), 2000, "Перевод от Сидоров А.", TIN),
    ]
    r = build_analysis(t, PARENTS)
    assert r["months"] == ["2026-09", "2026-10", "2026-11"]
    by = {s["key"]: s for s in r["sources"]}
    assert by["salary"]["total"] == 100000.0 and by["salary"]["series"] == [50000.0, 50000.0, 0.0]
    assert by["scholarship"]["total"] == 5000.0
    assert by["parents"]["series"] == [0.0, 10000.0, 0.0]
    assert by["transfers"]["total"] == 2000.0
    assert r["total"] == 117000.0 and r["monthly_avg"] == 39000.0
    assert round(sum(s["share_pct"] for s in r["sources"]), 1) == 100.0


def test_own_transfers_excluded_by_default_but_reported():
    t = [tx(1, dd(2026, 10, 1), 1000, "Перевод себе", TIN), tx(2, dd(2026, 10, 2), 500, "Зарплата", SAL)]
    r = build_analysis(t)
    assert r["total"] == 500.0 and r["own_total"] == 1000.0
    assert "own" not in {s["key"] for s in r["sources"]}
    r2 = build_analysis(t, include_own=True)
    assert r2["total"] == 1500.0


def test_period_filter_and_empty_months_are_zero_filled():
    t = [tx(1, dd(2026, 1, 10), 100, "Зарплата", SAL), tx(2, dd(2026, 4, 10), 100, "Зарплата", SAL)]
    r = build_analysis(t, start=date(2026, 1, 1), end=date(2026, 5, 31))
    assert r["months"] == ["2026-01", "2026-02", "2026-03", "2026-04", "2026-05"]
    assert r["sources"][0]["series"] == [100.0, 0.0, 0.0, 100.0, 0.0]
    assert build_analysis(t, start=date(2026, 3, 1), end=date(2026, 3, 31))["total"] == 0.0


def test_month_bucket_uses_moscow_time():
    # 22:00 UTC 31 окт = 01:00 МСК 1 ноября -> ноябрь
    late = IncomeTx(1, datetime(2026, 10, 31, 22, 0, tzinfo=timezone.utc), D("100"), "Зарплата", SAL, "income")
    r = build_analysis([late])
    assert r["months"] == ["2026-11"]


def test_source_detail_lists_transactions_newest_first_with_stats():
    t = [
        tx(1, dd(2026, 9, 10), 10000, "Перевод от Иванов Пётр", TIN),
        tx(2, dd(2026, 10, 10), 20000, "Перевод от Иванов Пётр", TIN),
        tx(3, dd(2026, 10, 12), 700, "Перевод от Сидоров А.", TIN),
    ]
    d = build_source_detail(t, "parents", PARENTS)
    assert d["label"] == "От родителей и родных"
    assert d["months"] == ["2026-09", "2026-10"] and d["series"] == [10000.0, 20000.0]
    assert d["total"] == 30000.0 and d["count"] == 2 and d["avg_payment"] == 15000.0 and d["max_payment"] == 20000.0
    assert [x["id"] for x in d["transactions"]] == [2, 1]
    assert d["transactions"][0]["date"] == "10.10.2026"


def test_source_detail_unknown_source_raises():
    with pytest.raises(ValueError):
        build_source_detail([], "wallet")


def test_detail_limit_flags_truncation():
    t = [tx(i, dd(2026, 10, 1) + timedelta(hours=i), 10, "Зарплата", SAL) for i in range(5)]
    d = build_source_detail(t, "salary", limit=3)
    assert len(d["transactions"]) == 3 and d["truncated"] and d["count"] == 5
