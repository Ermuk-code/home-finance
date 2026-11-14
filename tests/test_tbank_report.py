from datetime import date
from decimal import Decimal as D

import pytest

from app.finance.services.tbank_report import TBankReportError, is_tbank_report, parse_tbank_text

# Текст из реального отчёта ТБанка (без ФИО инвестора и номера договора).
REPORT_TEXT = """Брокер: АО «ТБанк», ИНН/КПП 7710140679/771301001
Дата расчета: 07.11.2026
АКЦИОНЕРНОЕ ОБЩЕСТВО «ТБАНК»
Отчет о сделках и операциях за период 01.11.2026 - 02.11.2026
1.1 Информация о совершенных и исполненных сделках на конец отчетного периода
Номер сделки
Вид сделки
1.2 Информация о неисполненных сделках на конец отчетного периода
Номер сделки
2. Операции с денежными средствами
Валюта Входящий остаток Исходящий остаток Плановый исходящий остаток Задолженность клиента Непокрытая позиция
RUB 25.83 25.83 25.83 0.00 0.00 0.00
USD 0.00 0.00 0.00 0.00 0.00 0.00
3.1 Движение по ценным бумагам инвестора
ОФЗ 29020 SU29020RMFS3 RU000A102BV4 ТБанк 2 0 0 2 2
ОФЗ 26236 SU26236RMFS8 RU000A102BT8 ТБанк 1 0 0 1 1
TPAY ETF TPAY RU000A108WX3 ТБанк 7 0 0 7 7
ИКС 5 ФИНАНС обб3П21 RU000A10F9D3 RU000A10F9D3 ТБанк 1 0 0 1 1
Наименование актива Код актива ISIN Место хранения Входящий остаток Зачисление Списание Исходящий остаток
3.2 Движение по производным финансовым инструментам
4.1 Информация о ценных бумагах
TPAY ETF TPAY RU000A108WX3 6287 ПАИ ФОНДОВ-РЕЗОВ ОБЩЕСТВО С ОГРАНИЧЕННОЙ
ОТВЕТСТВЕННОСТЬЮ "Т-КАПИТАЛ"
ОФЗ 26236 SU26236RMFS8 RU000A102BT8 26236RMFS обл органов власти МИНИСТЕРСТВО ФИНАНСОВ
ОФЗ 29020 SU29020RMFS3 RU000A102BV4 29020RMFS обл органов власти МИНИСТЕРСТВО ФИНАНСОВ
ИКС 5 ФИНАНС обб3П21 RU000A10F9D3 RU000A10F9D3 4B02-21-36241-R-003P обл прочих рез Общество с ограниченной
4.2 Информация об инструментах, не квалифицированных в качестве ценной бумаги
"""


def test_detects_tbank_report():
    assert is_tbank_report(REPORT_TEXT)
    assert not is_tbank_report("Выписка по счёту Сбербанк")


def test_not_a_report_raises():
    with pytest.raises(TBankReportError):
        parse_tbank_text("что-то совсем другое")


def test_meta_and_cash():
    r = parse_tbank_text(REPORT_TEXT)
    assert r.calc_date == date(2026, 11, 7)
    assert (r.period_start, r.period_end) == (date(2026, 11, 1), date(2026, 11, 2))
    assert r.cash["RUB"] == D("25.83")
    assert not r.has_trades
    assert r.warnings == []


def test_positions_code_and_isin_are_not_mixed_up():
    r = parse_tbank_text(REPORT_TEXT)
    by_code = {p.code: p for p in r.positions}
    assert set(by_code) == {"SU29020RMFS3", "SU26236RMFS8", "TPAY", "RU000A10F9D3"}
    ofz = by_code["SU29020RMFS3"]
    assert ofz.isin == "RU000A102BV4" and ofz.name == "ОФЗ 29020" and ofz.qty_out == D("2")
    assert by_code["RU000A10F9D3"].name == "ИКС 5 ФИНАНС обб3П21"


def test_asset_types_come_from_section_41():
    r = parse_tbank_text(REPORT_TEXT)
    types = {p.code: p.asset_type for p in r.positions}
    assert types == {
        "SU29020RMFS3": "bond", "SU26236RMFS8": "bond",
        "TPAY": "fund", "RU000A10F9D3": "bond",
    }


def test_zero_balance_goes_to_closed():
    text = REPORT_TEXT.replace(
        "TPAY ETF TPAY RU000A108WX3 ТБанк 7 0 0 7 7", "TPAY ETF TPAY RU000A108WX3 ТБанк 7 0 7 0 0", 1
    )
    r = parse_tbank_text(text)
    assert r.closed_codes == ["TPAY"]
    assert "TPAY" not in {p.code for p in r.positions}


def test_trades_trigger_warning():
    text = REPORT_TEXT.replace("Вид сделки", "Вид сделки\nПокупка", 1)
    r = parse_tbank_text(text)
    assert r.has_trades and r.warnings


def test_unparsed_line_in_31_is_reported_not_silently_lost():
    text = REPORT_TEXT.replace("3.2 Движение", "очень длинное название, которое перенеслось 5\n3.2 Движение", 1)
    r = parse_tbank_text(text)
    assert any("3.1" in w for w in r.warnings)
