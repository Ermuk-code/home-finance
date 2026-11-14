from datetime import date
from decimal import Decimal as D
from types import SimpleNamespace

from app.finance.services.deposits import (
    accrue,
    add_months,
    deposit_end_date,
    deposit_interest,
    describe_deposit,
    money,
    savings_credit_dates,
    savings_projection,
)


def test_full_year_equals_rate_in_normal_and_leap_year():
    assert deposit_interest(D(100000), D(16), date(2026, 1, 1), 12, False) == D("16000.00")
    assert deposit_interest(D(100000), D(16), date(2028, 1, 1), 12, False) == D("16000.00")


def test_term_crossing_year_boundary_uses_actual_days():
    # 05.11.2026 -> 05.05.2027 = 181 день
    expected = money(D(100000) * D(16) / 100 * 181 / 365)
    assert deposit_interest(D(100000), D(16), date(2026, 11, 5), 6, False) == expected


def test_end_date_clamps_to_month_end():
    assert deposit_end_date(date(2026, 8, 31), 6) == date(2027, 2, 28)
    assert deposit_end_date(date(2026, 11, 5), 12) == date(2027, 11, 5)
    assert add_months(date(2026, 1, 31), 1) == date(2026, 2, 28)


def test_capitalization_gives_more_than_simple_interest():
    simple = deposit_interest(D(100000), D(16), date(2026, 1, 1), 12, False)
    cap = deposit_interest(D(100000), D(16), date(2026, 1, 1), 12, True)
    assert cap > simple


def test_interest_to_date_is_capped_by_end_date():
    full = deposit_interest(D(100000), D(12), date(2026, 1, 10), 6, False)
    later = deposit_interest(D(100000), D(12), date(2026, 1, 10), 6, False, upto=date(2030, 1, 1))
    assert full == later


def test_savings_credit_dates():
    assert savings_credit_dates(date(2026, 11, 5)) == (date(2026, 10, 12), date(2026, 11, 12))
    assert savings_credit_dates(date(2026, 11, 12)) == (date(2026, 11, 12), date(2026, 12, 12))
    assert savings_credit_dates(date(2026, 1, 3)) == (date(2025, 12, 12), date(2026, 1, 12))


def test_savings_next_credit_amount_and_daily():
    p = savings_projection(D(100000), D(15), date(2026, 11, 5))
    assert p["next_credit_amount"] == money(accrue(D(100000), D(15), date(2026, 10, 12), date(2026, 11, 12)))
    assert p["daily_income"] == D("41.10")
    # проценты зачисляются и приносят доход -> чуть больше простых 15%
    assert p["yearly_income"] > D(15000)


def test_describe_legacy_deposit_without_term():
    dep = SimpleNamespace(
        id=1, bank_name="X", title="Y", kind="deposit", amount=D(50000), interest_rate=D(10),
        capitalization=False, opened_at=None, term_months=None, end_date=None,
    )
    info = describe_deposit(dep, date(2026, 11, 5))
    assert info["has_schedule"] is False
    assert info["expected_yearly_income"] == 5000.0


# --- разбор ввода и валидация (общие для API и Telegram-бота) ---
import pytest  # noqa: E402

from app.finance.services.deposits import (  # noqa: E402
    DepositInputError,
    parse_date_input,
    parse_decimal_input,
    parse_term_input,
    validate_deposit_input,
)


@pytest.mark.parametrize("raw,expected", [("300000", "300000"), ("300 000,50", "300000.50"), ("16,5%", "16.5"), ("1 000 ₽", "1000")])
def test_parse_decimal_input(raw, expected):
    assert parse_decimal_input(raw) == D(expected)


@pytest.mark.parametrize("raw", ["", "abc", "-5", "1,2,3"])
def test_parse_decimal_input_rejects_garbage(raw):
    with pytest.raises(DepositInputError):
        parse_decimal_input(raw)


def test_parse_date_input():
    assert parse_date_input("05.11.2026") == date(2026, 11, 5)
    assert parse_date_input("2026-11-05") == date(2026, 11, 5)
    assert parse_date_input("Сегодня", today=date(2026, 11, 5)) == date(2026, 11, 5)
    for bad in ("32.01.2026", "вчера", "05.11.1900"):
        with pytest.raises(DepositInputError):
            parse_date_input(bad)


def test_parse_term_input():
    assert parse_term_input("12") == 12
    assert parse_term_input("6 мес.") == 6
    for bad in ("0", "121", "полгода", "1.5"):
        with pytest.raises(DepositInputError):
            parse_term_input(bad)


def test_validate_deposit_requires_date_and_term():
    with pytest.raises(DepositInputError):
        validate_deposit_input("deposit", D(1000), D(10), None, 12)
    with pytest.raises(DepositInputError):
        validate_deposit_input("deposit", D(1000), D(10), date(2026, 11, 5), None)
    with pytest.raises(DepositInputError):
        validate_deposit_input("deposit", D(0), D(10), date(2026, 11, 5), 12)
    with pytest.raises(DepositInputError):
        validate_deposit_input("deposit", D(1000), D(101), date(2026, 11, 5), 12)
    with pytest.raises(DepositInputError):
        validate_deposit_input("bond", D(1000), D(10))


def test_validate_savings_ignores_term_fields():
    f = validate_deposit_input("savings", D(1000), D(10), date(2026, 11, 5), 12, True)
    assert f["opened_at"] is None and f["term_months"] is None and f["end_date"] is None
    assert f["capitalization"] is False


def test_validate_deposit_computes_end_date():
    f = validate_deposit_input("deposit", D(1000), D(10), date(2026, 8, 31), 6)
    assert f["end_date"] == date(2027, 2, 28)
