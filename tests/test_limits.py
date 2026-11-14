from decimal import Decimal as D

import pytest

from app.finance.services.limits import LimitInputError, parse_limit_input


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("15000", D("15000.00")),
        ("15 000", D("15000.00")),
        ("15000,50", D("15000.50")),
        ("15000 ₽", D("15000.00")),
        ("15000 руб.", D("15000.00")),
        ("  7500р ", D("7500.00")),
    ],
)
def test_parse_valid_amounts(raw, expected):
    assert parse_limit_input(raw) == expected


@pytest.mark.parametrize("raw", ["0", "нет", "Нет", "-", "снять", "0,00"])
def test_parse_removes_limit(raw):
    assert parse_limit_input(raw) is None


@pytest.mark.parametrize("raw", ["", "abc", "-500", "12k", "99999999999"])
def test_parse_rejects_garbage(raw):
    with pytest.raises(LimitInputError):
        parse_limit_input(raw)
