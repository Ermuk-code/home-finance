"""PDF-«досье» портфеля и промпт для внешней нейросети.

PDF устроен как досье: все данные — текстом (не картинками), в конце — готовый промпт с уже
подставленными цифрами. Персональных данных (ФИО, номер договора) здесь нет и быть не должно.
Тот же промпт отдаётся отдельным .txt — копировать из PDF неудобно из-за переносов строк.
"""
from __future__ import annotations

import io
import os
from datetime import date
from decimal import Decimal
from typing import Any, Dict, List, Optional

from reportlab.graphics.charts.barcharts import VerticalBarChart
from reportlab.graphics.shapes import Drawing
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Paragraph, SimpleDocTemplate, Table, TableStyle

_FONT_DIRS = ("/usr/share/fonts/truetype/dejavu/", "C:/Windows/Fonts/")
_REGULAR = ("DejaVuSans.ttf", "arial.ttf")
_BOLD = ("DejaVuSans-Bold.ttf", "arialbd.ttf")

TYPE_LABEL = {"bond": "Облигация", "stock": "Акция", "fund": "Фонд"}
MONTHS_RU = ["янв", "фев", "мар", "апр", "май", "июн", "июл", "авг", "сен", "окт", "ноя", "дек"]


# ---------------------------------------------------------------- форматирование

def money(v: Optional[Decimal], suffix: str = "") -> str:
    if v is None:
        return "—"
    s = f"{Decimal(v):,.2f}".replace(",", " ").replace(".", ",")
    return f"{s}{suffix}"


def pct(v: Optional[Decimal]) -> str:
    return "—" if v is None else f"{Decimal(v):.2f}".replace(".", ",") + "%"


def d_ru(d: Optional[date]) -> str:
    return d.strftime("%d.%m.%Y") if d else "—"


def month_ru(label: str) -> str:
    y, m = label.split("-")
    return f"{MONTHS_RU[int(m) - 1]} {y[2:]}"


# ---------------------------------------------------------------- промпт

def build_ai_prompt(report: Dict[str, Any]) -> str:
    """Готовый текст промпта: инструкция + профиль для заполнения + данные портфеля."""
    lines: List[str] = []
    a = lines.append
    a("Ты опытный независимый аналитик по российскому фондовому рынку. Объясняй простым языком: я новичок в акциях и облигациях.")
    a("")
    a("МОЯ ЦЕЛЬ: стабильный пассивный доход (купоны и дивиденды).")
    a("МОЙ ПРОФИЛЬ (заполни или уточни у меня перед выводами):")
    a("- Горизонт инвестирования: [__ лет]")
    a("- Допустимая просадка портфеля: [__ %]")
    a("- Сколько могу докладывать ежемесячно: [__ руб.]")
    a("- Нужны ли эти деньги в ближайший год: [да/нет]")
    a("")
    a(f"ДАННЫЕ МОЕГО ПОРТФЕЛЯ на {d_ru(report.get('as_of'))} (цены — MOEX, суммы в рублях):")
    a(f"Стоимость портфеля: {money(report['total_current'])}. Свободные деньги на счёте: {money(report.get('cash_rub'))}.")
    a("Позиции:")
    for p in report["positions"]:
        parts = [
            f"{p['name']} ({p['ticker']})",
            TYPE_LABEL.get(p["asset_type"], p["asset_type"]).lower(),
            f"{p['amount']} шт.",
            f"цена {money(p['current_price']) if p['price_known'] else 'нет данных'}",
            f"стоимость {money(p['current_value'])}",
            f"доля {pct(p['share_pct'])}",
        ]
        if p["asset_type"] == "bond":
            parts.append("корпоративная" if p.get("is_corporate") else "государственная (ОФЗ)")
            if p.get("mat_date"):
                parts.append(f"погашение {d_ru(p['mat_date'])}")
            if p.get("yield_pct") is not None:
                parts.append(f"доходность к погашению {pct(p['yield_pct'])}")
        if p["cost_known"]:
            parts.append(f"средняя цена покупки {money(p['avg_buy_price'])}")
        a("- " + "; ".join(parts))
    fc = report["income_forecast"]
    a("")
    a(f"Ожидаемый доход (брутто, до налога) на 12 месяцев: {money(fc['total'])} руб., в среднем {money(fc['monthly_avg'])} руб./мес.")
    a("По месяцам: " + ", ".join(f"{month_ru(m['label'])} {money(m['amount'])}" for m in fc["months"]))
    st = report["statistics"]
    a(f"Доход к стоимости портфеля: {pct(st['income_yield_pct'])}. Средняя доходность облигаций: {pct(st['bonds_avg_yield_pct'])}. "
      f"Средний срок до погашения облигаций: {st['bonds_avg_years'] if st['bonds_avg_years'] is not None else '—'} лет.")
    a(f"Структура: акции {pct(report['stocks_share_pct'])}, облигации {pct(report['bonds_share_pct'])}, фонды {pct(report['funds_share_pct'])}.")
    if report["findings"]:
        a("Автоматические наблюдения приложения:")
        for f in report["findings"]:
            a(f"- {f['text']}")
    a("")
    a("ЗАДАЧИ:")
    a("1. Оцени портфель: надёжность, диверсификацию, зависимость от ключевой ставки.")
    a("2. Назови главные риски простыми словами.")
    a("3. Оцени, насколько реалистичен прогноз пассивного дохода.")
    a("4. По каждой позиции предложи: держать / докупить / сократить — с причиной.")
    a("5. Предложи, чем пополнить портфель, чтобы он лучше подходил моей цели (типы активов и ориентиры, а не «купи X, потому что вырастет»).")
    a("6. Составь пошаговый план действий на ближайшие 3 месяца.")
    a("")
    a("ПРАВИЛА:")
    a("- Опирайся только на мои данные. Если нужна свежая информация (ставка ЦБ, рейтинги, новости эмитентов) — скажи об этом явно и не выдумывай цифры.")
    a("- Если данных для вывода не хватает, сначала задай уточняющие вопросы.")
    a("- Отдельно перечисли допущения и неопределённости.")
    a("- Не обещай доходность. Это не индивидуальная инвестиционная рекомендация, решения принимаю я.")
    a("")
    a("ФОРМАТ ОТВЕТА: краткий вывод (3–5 строк), затем разбор по пунктам выше, в конце таблица «позиция → действие → причина → риск».")
    return "\n".join(lines)


# ---------------------------------------------------------------- PDF

def _fonts() -> tuple:
    def reg(names, alias):
        for d in _FONT_DIRS:
            for n in names:
                path = os.path.join(d, n)
                if os.path.exists(path):
                    pdfmetrics.registerFont(TTFont(alias, path))
                    return True
        return False

    if reg(_REGULAR, "PF-Regular"):
        bold = "PF-Bold" if reg(_BOLD, "PF-Bold") else "PF-Regular"
        pdfmetrics.registerFontFamily("PF-Regular", normal="PF-Regular", bold=bold, italic="PF-Regular", boldItalic=bold)
        return "PF-Regular", bold
    return "Helvetica", "Helvetica-Bold"


def _table(data: List[List[str]], font: str, bold: str, col_widths=None, right_from: int = 1) -> Table:
    if col_widths is None:
        # Таблица на всю ширину страницы: первая колонка (название) шире остальных
        total_w = A4[0] - 56
        n = len(data[0])
        first = min(130.0, total_w * 0.22) if n <= 8 else 38.0
        col_widths = [first] + [(total_w - first) / (n - 1)] * (n - 1)
    t = Table(data, colWidths=col_widths, repeatRows=1, hAlign="LEFT")
    t.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, -1), font),
        ("FONTNAME", (0, 0), (-1, 0), bold),
        ("FONTSIZE", (0, 0), (-1, -1), 7.5),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1F2937")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.whitesmoke),
        ("ALIGN", (right_from, 1), (-1, -1), "RIGHT"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#D1D5DB")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F9FAFB")]),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    return t


def _income_chart(months: List[Dict[str, Any]]) -> Drawing:
    d = Drawing(500, 150)
    ch = VerticalBarChart()
    ch.x, ch.y, ch.width, ch.height = 40, 25, 440, 110
    ch.data = [[float(m["amount"]) for m in months]]
    ch.categoryAxis.categoryNames = [month_ru(m["label"]) for m in months]
    ch.categoryAxis.labels.fontName = "PF-Regular" if "PF-Regular" in pdfmetrics.getRegisteredFontNames() else "Helvetica"
    ch.categoryAxis.labels.fontSize = 6.5
    ch.valueAxis.labels.fontSize = 6.5
    ch.valueAxis.valueMin = 0
    ch.bars[0].fillColor = colors.HexColor("#2563EB")
    d.add(ch)
    return d


def generate_portfolio_pdf(report: Dict[str, Any]) -> bytes:
    font, bold = _fonts()
    base = getSampleStyleSheet()
    h1 = ParagraphStyle("h1", parent=base["Heading1"], fontName=bold, fontSize=15, leading=19, spaceAfter=6)
    h2 = ParagraphStyle("h2", parent=base["Heading2"], fontName=bold, fontSize=11.5, leading=15, spaceBefore=12, spaceAfter=5)
    body = ParagraphStyle("body", parent=base["Normal"], fontName=font, fontSize=9, leading=12.5, spaceAfter=4)
    small = ParagraphStyle("small", parent=body, fontSize=7.5, leading=10, textColor=colors.HexColor("#6B7280"))
    mono = ParagraphStyle("prompt", parent=body, fontSize=8, leading=10.5, spaceAfter=0)

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=28, rightMargin=28, topMargin=28, bottomMargin=28,
                            title="Портфель: сводка и промпт для ИИ", author="home-finance")
    el: List[Any] = []
    positions = report["positions"]
    fc = report["income_forecast"]
    st = report["statistics"]

    el.append(Paragraph(f"Досье инвестиционного портфеля — {d_ru(report.get('as_of'))}", h1))
    el.append(Paragraph("Данные собраны автоматически: количество бумаг — из брокерского отчёта, цены и выплаты — с Московской биржи (MOEX ISS). "
                        "Документ не содержит персональных данных и не является инвестиционной рекомендацией.", small))

    # 1. Сводка
    el.append(Paragraph("1. Сводка", h2))
    pnl_line = "не считается (в отчёте нет цены покупки)"
    if report.get("total_pnl") is not None and report.get("cost_known_count"):
        sign = "+" if report["total_pnl"] >= 0 else ""
        pnl_line = f"{sign}{money(report['total_pnl'])} руб. ({sign}{pct(report['total_pnl_pct'])}) — по бумагам с известной ценой покупки"
    el.append(Paragraph(
        f"<b>Стоимость портфеля:</b> {money(report['total_current'])} руб. &nbsp;|&nbsp; "
        f"<b>Свободные деньги:</b> {money(report.get('cash_rub'))} руб. &nbsp;|&nbsp; <b>Позиций:</b> {len(positions)}<br/>"
        f"<b>Прибыль/убыток:</b> {pnl_line}<br/>"
        f"<b>Ожидаемый доход за 12 мес.:</b> {money(fc['total'])} руб. (≈ {money(fc['monthly_avg'])} руб./мес.)", body))

    # 2. Позиции
    el.append(Paragraph("2. Позиции", h2))
    rows = [["Бумага", "Тип", "Кол-во", "Цена", "Стоимость", "Доля", "Ср. цена покупки", "P&L"]]
    for p in positions:
        rows.append([
            f"{p['name']}\n{p['ticker']}", TYPE_LABEL.get(p["asset_type"], p["asset_type"]), str(p["amount"]),
            money(p["current_price"]) if p["price_known"] else "нет данных", money(p["current_value"]), pct(p["share_pct"]),
            money(p["avg_buy_price"]) if p["cost_known"] else "—",
            (("+" if p["pnl"] >= 0 else "") + money(p["pnl"])) if p["pnl"] is not None else "—",
        ])
    el.append(_table(rows, font, bold, right_from=2))

    # 3. Облигации
    bonds = [p for p in positions if p["asset_type"] == "bond"]
    el.append(Paragraph("3. Облигации", h2))
    if bonds:
        rows = [["Облигация", "Кому должна", "Погашение", "Ближайший купон", "Купон, руб.", "Доходность", "Доход за 12 мес."]]
        for p in bonds:
            rows.append([
                p["name"], "Минфин (ОФЗ)" if not p.get("is_corporate") else "Компания", d_ru(p.get("mat_date")),
                d_ru(p.get("next_coupon_date")), money(p.get("coupon_value")), pct(p.get("yield_pct")),
                money(p.get("annual_income")),
            ])
        el.append(_table(rows, font, bold, right_from=2))
        el.append(Paragraph("Доходность — к погашению, по данным биржи. «—» значит, что биржа не отдала значение.", small))
    else:
        el.append(Paragraph("Облигаций нет.", body))

    # 4. Акции и фонды
    other = [p for p in positions if p["asset_type"] != "bond"]
    el.append(Paragraph("4. Акции и фонды", h2))
    if other:
        rows = [["Бумага", "Тип", "Выплаты за 12 мес. (прогноз)"]]
        for p in other:
            rows.append([f"{p['name']} ({p['ticker']})", TYPE_LABEL.get(p["asset_type"], ""), money(p.get("annual_income"))])
        el.append(_table(rows, font, bold, right_from=2))
        el.append(Paragraph("Прогноз по акциям и фондам — повтор выплат прошлого года; это оценка, а не обещание.", small))
    else:
        el.append(Paragraph("Акций и фондов нет.", body))

    # 5. Прогноз дохода
    el.append(Paragraph("5. Ожидаемый пассивный доход по месяцам (брутто, до налога)", h2))
    el.append(_income_chart(fc["months"]))
    rows = [["Месяц"] + [month_ru(m["label"]) for m in fc["months"]]]
    rows.append(["Руб."] + [f"{Decimal(m['amount']):.0f}" for m in fc["months"]])
    el.append(_table(rows, font, bold))

    # 6. Статистика
    el.append(Paragraph("6. Статистика", h2))
    el.append(Paragraph(
        f"Акции: <b>{pct(report['stocks_share_pct'])}</b> | Облигации: <b>{pct(report['bonds_share_pct'])}</b> | Фонды: <b>{pct(report['funds_share_pct'])}</b><br/>"
        f"Крупнейшая позиция: {pct(st['top_position_share_pct'])} портфеля<br/>"
        f"Доход к стоимости портфеля: {pct(st['income_yield_pct'])}<br/>"
        f"Средняя доходность облигаций: {pct(st['bonds_avg_yield_pct'])}; средний срок до погашения: "
        f"{st['bonds_avg_years'] if st['bonds_avg_years'] is not None else '—'} лет", body))

    # 7. Наблюдения
    el.append(Paragraph("7. Наблюдения приложения (факты, а не советы)", h2))
    if report["findings"]:
        for f in report["findings"]:
            el.append(Paragraph(("⚠ " if f["level"] == "warn" else "• ") + f["text"], body))
    else:
        el.append(Paragraph("Автоматические правила ничего не отметили.", body))
    if report.get("rebalance_advice"):
        el.append(Paragraph("Условный ориентир по балансу акций и облигаций (целевые доли заданы по умолчанию и не являются советом):", small))
        for item in report["rebalance_advice"]:
            el.append(Paragraph("• " + item, small))

    # 8. Промпт
    el.append(Paragraph("8. Промпт для нейросети", h2))
    el.append(Paragraph("Скопируйте текст ниже в ChatGPT / Claude / Gemini (или возьмите файл prompt.txt, он приходит вместе с PDF).", small))
    for line in build_ai_prompt(report).split("\n"):
        el.append(Paragraph(line.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;") or "&nbsp;", mono))

    doc.build(el)
    return buf.getvalue()
