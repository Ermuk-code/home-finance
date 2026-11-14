"""Разбор PDF-отчёта брокера «ТБанк» («Отчет о сделках и операциях»).

Модуль намеренно «чистый»: без БД, pydantic и сети — только pdfplumber и стандартная
библиотека, поэтому его легко тестировать на живых отчётах.

Что берём из отчёта:
  * раздел 2   — остатки денег по валютам;
  * раздел 3.1 — движение по ценным бумагам (берём «исходящий остаток» как текущее количество);
  * раздел 4.1 — тип бумаги (облигация / ПАИ / акция) — чтобы не гадать по тикеру;
  * раздел 1.1 — сделки: пока только определяем, что они есть (цену покупки из них не считаем).

Чего в отчёте нет: рыночных цен, средней цены покупки, купонов. Их даёт MOEX (см. moex_data.py)
либо пользователь вручную.

ФИО инвестора и номер договора НЕ извлекаются — в приложение они не нужны.
"""
from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Dict, List, Optional

import pdfplumber


class TBankReportError(Exception):
    """Файл не похож на отчёт ТБанка или не содержит нужных разделов."""


@dataclass
class TBankPosition:
    name: str
    code: str            # «Код актива»: тикер (TPAY), SECID ОФЗ (SU29020RMFS3) или ISIN корпоративной облигации
    isin: str
    location: str        # место хранения (ТБанк, НРД, ...)
    qty_in: Decimal
    credited: Decimal
    debited: Decimal
    qty_out: Decimal     # исходящий остаток — текущее количество
    asset_type: str      # 'bond' | 'stock' | 'fund'
    type_raw: str = ""   # как написано в разделе 4.1, для диагностики


@dataclass
class TBankReport:
    calc_date: Optional[date]
    period_start: Optional[date]
    period_end: Optional[date]
    positions: List[TBankPosition] = field(default_factory=list)
    cash: Dict[str, Decimal] = field(default_factory=dict)   # валюта -> исходящий остаток
    has_trades: bool = False
    # Бумаги, которые в отчёте перечислены, но с нулевым остатком на конец периода (проданы полностью)
    closed_codes: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)


_NUM = r"-?\d+(?:[.,]\d+)?"
_ISIN = r"[A-Z]{2}[A-Z0-9]{9}\d"
_POSITION_RE = re.compile(
    rf"^(?P<name>.+?)\s+(?P<code>[A-Za-z0-9]{{3,20}})\s+(?P<isin>{_ISIN})\s+"
    rf"(?!{_ISIN}\b)(?P<loc>.+?)\s+(?P<qin>{_NUM})\s+(?P<cr>{_NUM})\s+(?P<db>{_NUM})\s+(?P<qout>{_NUM})\s+(?P<plan>{_NUM})$"
)
_CASH_RE = re.compile(rf"^(?P<cur>[A-Z]{{3}})\s+(?P<inc>{_NUM})\s+(?P<out>{_NUM})(?:\s+{_NUM})*$")
_DATE_RE = re.compile(r"(\d{2})\.(\d{2})\.(\d{4})")
_HEADER_HINTS = ("Наименование актива", "остаток", "ISIN", "Место хранения")


def is_tbank_report(text: str) -> bool:
    up = text.upper()
    return "ТБАНК" in up and "ОТЧЕТ О СДЕЛКАХ И ОПЕРАЦИЯХ" in up.replace("Ё", "Е")


def _dec(raw: str) -> Decimal:
    try:
        return Decimal(raw.replace(",", "."))
    except InvalidOperation as e:  # pragma: no cover - защитный путь
        raise TBankReportError(f"Не удалось прочитать число: {raw!r}") from e


def _to_date(match: Optional[re.Match]) -> Optional[date]:
    if not match:
        return None
    d, m, y = (int(x) for x in match.groups())
    try:
        return date(y, m, d)
    except ValueError:
        return None


def _section(text: str, start_marker: str, end_markers: tuple) -> str:
    """Текст между заголовком раздела и первым из следующих заголовков."""
    start = text.find(start_marker)
    if start < 0:
        return ""
    start += len(start_marker)
    end = len(text)
    for marker in end_markers:
        pos = text.find(marker, start)
        if pos >= 0:
            end = min(end, pos)
    return text[start:end]


def _classify(type_raw: str, code: str, isin: str) -> str:
    """Тип бумаги по разделу 4.1; если раздела нет — по коду."""
    t = type_raw.lower()
    if re.search(r"\bобл\b", t) or "облигац" in t:
        return "bond"
    if "паи" in t or "пай" in t or "фонд" in t:
        return "fund"
    if "акц" in t:
        return "stock"
    if code.upper().startswith("SU") or code.upper().startswith("RU000"):
        return "bond"
    return "stock"


def _types_from_section_41(section: str) -> Dict[str, str]:
    """ISIN -> «сырой» фрагмент строки раздела 4.1 (включая перенос на следующую строку)."""
    lines = [ln.strip() for ln in section.splitlines() if ln.strip()]
    result: Dict[str, str] = {}
    for i, ln in enumerate(lines):
        m = re.search(r"\b([A-Z]{2}[A-Z0-9]{9}\d)\b", ln)
        if m:
            tail = ln[m.end():]
            if i + 1 < len(lines) and not re.search(r"\b[A-Z]{2}[A-Z0-9]{9}\d\b", lines[i + 1]):
                tail += " " + lines[i + 1]
            result.setdefault(m.group(1), tail)
    return result


def parse_tbank_text(text: str) -> TBankReport:
    """Разбор уже извлечённого текста PDF (удобно для тестов)."""
    if not is_tbank_report(text):
        raise TBankReportError("Это не похоже на отчёт ТБанка «Отчет о сделках и операциях».")

    calc_date = _to_date(re.search(r"Дата расчета:\s*(\d{2})\.(\d{2})\.(\d{4})", text))
    period = re.search(
        r"за период\s+(\d{2})\.(\d{2})\.(\d{4})\s*-\s*(\d{2})\.(\d{2})\.(\d{4})", text
    )
    p_start = p_end = None
    if period:
        g = period.groups()
        p_start = _to_date(re.match(r"(\d{2})\.(\d{2})\.(\d{4})", f"{g[0]}.{g[1]}.{g[2]}"))
        p_end = _to_date(re.match(r"(\d{2})\.(\d{2})\.(\d{4})", f"{g[3]}.{g[4]}.{g[5]}"))

    report = TBankReport(calc_date=calc_date, period_start=p_start, period_end=p_end)

    # --- раздел 1.1: есть ли сделки (заголовки таблицы тоже попадают в текст, поэтому ищем виды сделок)
    sec_11 = _section(text, "1.1 Информация о совершенных", ("1.2 Информация о неисполненных",))
    report.has_trades = bool(re.search(r"\b(Покупка|Продажа|Buy|Sell)\b", sec_11, re.IGNORECASE))
    if report.has_trades:
        report.warnings.append(
            "В отчёте есть сделки (раздел 1.1). Количество бумаг взято из остатков, "
            "а цена покупки по сделкам пока не учитывается — укажите её вручную в дашборде."
        )

    # --- раздел 2: деньги
    sec_2 = _section(text, "2. Операции с денежными средствами", ("3.1 Движение по ценным бумагам",))
    for ln in (l.strip() for l in sec_2.splitlines()):
        m = _CASH_RE.match(ln)
        if m:
            report.cash[m.group("cur")] = _dec(m.group("out"))

    # --- раздел 4.1: типы
    sec_41 = _section(
        text, "4.1 Информация о ценных бумагах", ("4.2 Информация об инструментах",)
    )
    types = _types_from_section_41(sec_41)

    # --- раздел 3.1: позиции
    sec_31 = _section(
        text, "3.1 Движение по ценным бумагам инвестора", ("3.2 Движение по производным",)
    )
    if not sec_31.strip():
        raise TBankReportError("Не найден раздел 3.1 «Движение по ценным бумагам инвестора».")

    unparsed: List[str] = []
    for raw in sec_31.splitlines():
        ln = raw.strip()
        if not ln:
            continue
        m = _POSITION_RE.match(ln)
        if not m:
            if not any(h in ln for h in _HEADER_HINTS):
                unparsed.append(ln)
            continue
        isin = m.group("isin")
        code = m.group("code")
        type_raw = types.get(isin, "")
        qty_out = _dec(m.group("qout"))
        if qty_out <= 0:
            report.closed_codes.append(code)
            continue
        report.positions.append(
            TBankPosition(
                name=m.group("name").strip(),
                code=code,
                isin=isin,
                location=m.group("loc").strip(),
                qty_in=_dec(m.group("qin")),
                credited=_dec(m.group("cr")),
                debited=_dec(m.group("db")),
                qty_out=qty_out,
                asset_type=_classify(type_raw, code, isin),
                type_raw=type_raw.strip(),
            )
        )

    if unparsed:
        report.warnings.append(
            "Не удалось разобрать строки раздела 3.1 (возможно, длинное название перенеслось): "
            + "; ".join(unparsed[:3])
        )
    if not report.positions and not unparsed:
        report.warnings.append("В разделе 3.1 нет ни одной бумаги с ненулевым остатком.")
    return report


def parse_tbank_pdf(file_bytes: bytes) -> TBankReport:
    try:
        with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
            text = "\n".join(page.extract_text() or "" for page in pdf.pages)
    except Exception as e:
        raise TBankReportError("Не удалось прочитать PDF (файл повреждён?).") from e
    if not text.strip():
        raise TBankReportError("В PDF нет текстового слоя.")
    return parse_tbank_text(text)
