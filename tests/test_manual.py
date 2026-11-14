from datetime import date
from decimal import Decimal as D

import pytest

from app.finance.services.manual import (
    CASH_BANK_NAME,
    KIND_EXPENSE,
    KIND_INCOME,
    KIND_TOPUP,
    KIND_WITHDRAW,
    ManualTxError,
    clean_description,
    normalize_source,
    parse_amount,
    parse_tx_date,
    signed_amount,
    tx_datetime,
)

TODAY = date(2026, 11, 7)


@pytest.mark.parametrize(
    "raw, expected",
    [("350", D("350.00")), ("1 250,50", D("1250.50")), ("200 ₽", D("200.00")), ("99 руб.", D("99.00"))],
)
def test_parse_amount_ok(raw, expected):
    assert parse_amount(raw) == expected


@pytest.mark.parametrize("raw", ["", "abc", "0", "0,00", "-5", "99999999999"])
def test_parse_amount_rejected(raw):
    with pytest.raises(ManualTxError):
        parse_amount(raw)


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("нал", CASH_BANK_NAME),
        ("Наличка", CASH_BANK_NAME),
        ("сбер", "СберБанк"),
        ("Тинькофф", "Т-Банк"),
        ("альфа банк", "Альфа-Банк"),
        ("  ВТБ  ", "ВТБ"),
    ],
)
def test_normalize_source(raw, expected):
    assert normalize_source(raw) == expected


def test_normalize_source_rejects_empty_and_too_long():
    with pytest.raises(ManualTxError):
        normalize_source("  ")
    with pytest.raises(ManualTxError):
        normalize_source("x" * 51)


def test_parse_date_variants():
    assert parse_tx_date("сегодня", TODAY) == TODAY
    assert parse_tx_date("вчера", TODAY) == date(2026, 11, 6)
    assert parse_tx_date("05.11.2026", TODAY) == date(2026, 11, 5)
    assert parse_tx_date("05.11", TODAY) == date(2026, 11, 5)
    assert parse_tx_date("2026-11-01", TODAY) == date(2026, 11, 1)


@pytest.mark.parametrize("raw", ["08.11.2026", "завтра", "32.01.2026", "01.01.1999", ""])
def test_parse_date_rejected(raw):
    with pytest.raises(ManualTxError):
        parse_tx_date(raw, TODAY)


def test_signed_amounts():
    assert signed_amount(KIND_EXPENSE, D("100")) == D("-100")
    assert signed_amount(KIND_TOPUP, D("100")) == D("-100")
    assert signed_amount(KIND_INCOME, D("100")) == D("100")
    assert signed_amount(KIND_WITHDRAW, D("100")) == D("100")


def test_description_rules():
    assert clean_description("  Продукты   на рынке ") == "Продукты на рынке"
    assert clean_description("-", default="Снятие наличных") == "Снятие наличных"
    with pytest.raises(ManualTxError):
        clean_description("")
    with pytest.raises(ManualTxError):
        clean_description("x" * 301)


def test_tx_datetime_is_noon_moscow():
    dt = tx_datetime(date(2026, 11, 1))
    assert dt.hour == 12 and dt.utcoffset().total_seconds() == 3 * 3600
    assert dt.astimezone().date() is not None
    # в UTC это 09:00 того же дня — даты не «уезжают» ни в одну сторону
    assert dt.utctimetuple()[:4] == (2026, 11, 1, 9)
