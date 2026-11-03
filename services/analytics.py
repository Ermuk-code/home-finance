import asyncio
import io
import os
from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, List

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import Asset
from services.invest_api import MoexClient


async def analyze_portfolio(
    session: AsyncSession,
    user_id: int,
    target_stocks_pct: Decimal = Decimal("70.0"),
    target_bonds_pct: Decimal = Decimal("30.0"),
) -> Dict[str, Any]:
    """
    Рассчитывает полную математическую аналитику портфеля пользователя:
    - стоимость каждой позиции, бумажный PnL (в рублях и %) и долю в портфеле;
    - текущее соотношение Акции / Облигации;
    - строгий список рекомендаций по ребалансировке (без использования ИИ).
    """
    result = await session.execute(select(Asset).where(Asset.user_id == user_id))
    assets = list(result.scalars().all())

    if not assets:
        return {
            "positions": [],
            "total_invested": Decimal("0.00"),
            "total_current": Decimal("0.00"),
            "total_pnl": Decimal("0.00"),
            "total_pnl_pct": Decimal("0.00"),
            "stocks_value": Decimal("0.00"),
            "bonds_value": Decimal("0.00"),
            "stocks_share_pct": Decimal("0.00"),
            "bonds_share_pct": Decimal("0.00"),
            "target_stocks_pct": target_stocks_pct,
            "target_bonds_pct": target_bonds_pct,
            "rebalance_advice": ["Портфель пуст. Загрузите брокерский отчет для расчета аналитики."],
        }

    # Пакетный запрос текущих цен с Московской биржи
    query_items = [{"ticker": a.ticker, "asset_type": a.asset_type} for a in assets]
    live_prices = await MoexClient.get_prices_for_tickers(query_items)

    positions: List[Dict[str, Any]] = []
    total_invested = Decimal("0.00")
    total_current = Decimal("0.00")
    stocks_value = Decimal("0.00")
    bonds_value = Decimal("0.00")

    for asset in assets:
        qty = Decimal(str(asset.amount))
        avg_buy = Decimal(str(asset.average_buy_price))
        market_price = live_prices.get(asset.ticker.upper())
        # Если биржа недоступна или тикер не найден, используем среднюю цену покупки как fallback
        current_price = market_price if market_price is not None else avg_buy

        invested_val = (qty * avg_buy).quantize(Decimal("0.01"))
        current_val = (qty * current_price).quantize(Decimal("0.01"))
        pnl_rub = (current_val - invested_val).quantize(Decimal("0.01"))
        pnl_pct = (
            ((pnl_rub / invested_val) * Decimal("100")).quantize(Decimal("0.01"))
            if invested_val > 0
            else Decimal("0.00")
        )

        total_invested += invested_val
        total_current += current_val

        if asset.asset_type == "bond":
            bonds_value += current_val
        else:
            stocks_value += current_val

        positions.append(
            {
                "ticker": asset.ticker.upper(),
                "asset_type": asset.asset_type,
                "amount": qty.quantize(Decimal("0.01")) if qty % 1 != 0 else Decimal(int(qty)),
                "avg_buy_price": avg_buy.quantize(Decimal("0.01")),
                "current_price": current_price.quantize(Decimal("0.01")),
                "price_from_moex": market_price is not None,
                "invested_value": invested_val,
                "current_value": current_val,
                "pnl": pnl_rub,
                "pnl_pct": pnl_pct,
                "share_pct": Decimal("0.00"),
            }
        )

    # Расчет доли каждой бумаги в общей стоимости портфеля
    for pos in positions:
        if total_current > 0:
            pos["share_pct"] = (
                (pos["current_value"] / total_current) * Decimal("100")
            ).quantize(Decimal("0.01"))

    # Сортируем от самой крупной позиции к самой малой
    positions.sort(key=lambda x: x["current_value"], reverse=True)

    total_pnl = (total_current - total_invested).quantize(Decimal("0.01"))
    total_pnl_pct = (
        ((total_pnl / total_invested) * Decimal("100")).quantize(Decimal("0.01"))
        if total_invested > 0
        else Decimal("0.00")
    )

    stocks_share_pct = (
        ((stocks_value / total_current) * Decimal("100")).quantize(Decimal("0.01"))
        if total_current > 0
        else Decimal("0.00")
    )
    bonds_share_pct = (
        ((bonds_value / total_current) * Decimal("100")).quantize(Decimal("0.01"))
        if total_current > 0
        else Decimal("0.00")
    )

    rebalance_advice = _build_rebalance_recommendations(
        positions=positions,
        total_current=total_current,
        stocks_value=stocks_value,
        bonds_value=bonds_value,
        stocks_share_pct=stocks_share_pct,
        bonds_share_pct=bonds_share_pct,
        target_stocks_pct=target_stocks_pct,
        target_bonds_pct=target_bonds_pct,
    )

    return {
        "positions": positions,
        "total_invested": total_invested,
        "total_current": total_current,
        "total_pnl": total_pnl,
        "total_pnl_pct": total_pnl_pct,
        "stocks_value": stocks_value,
        "bonds_value": bonds_value,
        "stocks_share_pct": stocks_share_pct,
        "bonds_share_pct": bonds_share_pct,
        "target_stocks_pct": target_stocks_pct,
        "target_bonds_pct": target_bonds_pct,
        "rebalance_advice": rebalance_advice,
    }


def _build_rebalance_recommendations(
    positions: List[Dict[str, Any]],
    total_current: Decimal,
    stocks_value: Decimal,
    bonds_value: Decimal,
    stocks_share_pct: Decimal,
    bonds_share_pct: Decimal,
    target_stocks_pct: Decimal,
    target_bonds_pct: Decimal,
) -> List[str]:
    """
    Математический алгоритм расчета рекомендаций по ребалансировке портфеля.
    Возвращает конкретный список: что купить/продать и в каком количестве.
    """
    if total_current <= 0:
        return ["Недостаточно данных для расчета баланса."]

    advice: List[str] = []
    target_stocks_val = (total_current * (target_stocks_pct / Decimal("100"))).quantize(Decimal("0.01"))
    target_bonds_val = (total_current * (target_bonds_pct / Decimal("100"))).quantize(Decimal("0.01"))

    diff_stocks = (target_stocks_val - stocks_value).quantize(Decimal("0.01"))
    diff_bonds = (target_bonds_val - bonds_value).quantize(Decimal("0.01"))

    advice.append(
        f"Текущая структура: Акции {stocks_share_pct}% ({stocks_value} ₽) | "
        f"Облигации {bonds_share_pct}% ({bonds_value} ₽). "
        f"Целевой ориентир: {target_stocks_pct}% / {target_bonds_pct}%."
    )

    # Если отклонение меньше 2%, портфель сбалансирован
    if abs(stocks_share_pct - target_stocks_pct) <= Decimal("2.0"):
        advice.append("Баланс классов активов в норме (отклонение менее 2%). Ребалансировка не требуется.")
    else:
        stock_positions = [p for p in positions if p["asset_type"] == "stock" and p["current_price"] > 0]
        bond_positions = [p for p in positions if p["asset_type"] == "bond" and p["current_price"] > 0]

        if diff_stocks > 0:
            # Не хватает акций (перевес в облигациях)
            advice.append(
                f"Дефицит акций: необходимо увеличить долю акций на {diff_stocks} ₽ "
                f"(или сократить облигации на эту же сумму)."
            )
            if stock_positions:
                underweight_stock = min(stock_positions, key=lambda x: x["share_pct"])
                qty_to_buy = int(diff_stocks // underweight_stock["current_price"])
                if qty_to_buy > 0:
                    advice.append(
                        f"КУПИТЬ: {underweight_stock['ticker']} (наименьшая доля среди акций) — "
                        f"{qty_to_buy} шт. по ~{underweight_stock['current_price']} ₽ "
                        f"(на сумму {(qty_to_buy * underweight_stock['current_price']):.2f} ₽)."
                    )
            if bond_positions:
                overweight_bond = max(bond_positions, key=lambda x: x["share_pct"])
                qty_to_sell = int(diff_stocks // overweight_bond["current_price"])
                if qty_to_sell > 0:
                    advice.append(
                        f"Или ПРОДАТЬ (при ребалансировке без пополнения): {overweight_bond['ticker']} — "
                        f"{qty_to_sell} шт. по ~{overweight_bond['current_price']} ₽."
                    )
        elif diff_bonds > 0:
            # Не хватает облигаций (перевес в акциях)
            advice.append(
                f"Дефицит облигаций: необходимо увеличить защитную часть (облигации) на {diff_bonds} ₽."
            )
            if bond_positions:
                underweight_bond = min(bond_positions, key=lambda x: x["share_pct"])
                qty_to_buy = int(diff_bonds // underweight_bond["current_price"])
                if qty_to_buy > 0:
                    advice.append(
                        f"КУПИТЬ: {underweight_bond['ticker']} (наименьшая доля среди облигаций) — "
                        f"{qty_to_buy} шт. по ~{underweight_bond['current_price']} ₽ "
                        f"(на сумму {(qty_to_buy * underweight_bond['current_price']):.2f} ₽)."
                    )
            if stock_positions:
                overweight_stock = max(stock_positions, key=lambda x: x["share_pct"])
                qty_to_sell = int(diff_bonds // overweight_stock["current_price"])
                if qty_to_sell > 0:
                    advice.append(
                        f"Или ПРОДАТЬ (для фиксации перевеса в акциях): {overweight_stock['ticker']} — "
                        f"{qty_to_sell} шт. по ~{overweight_stock['current_price']} ₽."
                    )

    # Проверка риска концентрации на один тикер (если одна бумага > 35% портфеля)
    if len(positions) > 1:
        for pos in positions:
            if pos["share_pct"] > Decimal("35.0") and pos["current_price"] > 0:
                excess_val = (((pos["share_pct"] - Decimal("25.0")) / Decimal("100")) * total_current).quantize(Decimal("0.01"))
                qty_trim = int(excess_val // pos["current_price"])
                if qty_trim > 0:
                    advice.append(
                        f"Риск концентрации: бумага {pos['ticker']} занимает {pos['share_pct']}% портфеля. "
                        f"Для снижения доли до безопасных 25% рекомендуется сократить позицию на {qty_trim} шт. (~{excess_val} ₽)."
                    )

    return advice


def _register_cyrillic_font() -> str:
    """Поиск и регистрация TTF-шрифта с поддержкой кириллицы для ReportLab."""
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "C:/Windows/Fonts/arial.ttf",
        "C:/Windows/Fonts/calibri.ttf",
    ]
    for path in candidates:
        if os.path.exists(path):
            try:
                pdfmetrics.registerFont(TTFont("CyrillicRegular", path))
                return "CyrillicRegular"
            except Exception:
                continue
    return "Helvetica"


async def generate_portfolio_pdf_async(report: Dict[str, Any]) -> bytes:
    """Асинхронная обертка генерации PDF-сводки."""
    return await asyncio.to_thread(generate_portfolio_pdf, report)


def generate_portfolio_pdf(report: Dict[str, Any]) -> bytes:
    """
    Генерирует PDF-сводку портфеля с помощью reportlab.
    Внизу документа добавляет готовый текстовый промпт для анализа во внешних LLM.
    """
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        rightMargin=30,
        leftMargin=30,
        topMargin=30,
        bottomMargin=30,
    )

    font_name = _register_cyrillic_font()
    styles = getSampleStyleSheet()

    title_style = ParagraphStyle(
        "DocTitle",
        parent=styles["Heading1"],
        fontName=font_name,
        fontSize=15,
        leading=19,
        spaceAfter=10,
    )
    h2_style = ParagraphStyle(
        "SectionHeader",
        parent=styles["Heading2"],
        fontName=font_name,
        fontSize=12,
        leading=15,
        spaceBefore=12,
        spaceAfter=6,
    )
    body_style = ParagraphStyle(
        "BodyCyr",
        parent=styles["Normal"],
        fontName=font_name,
        fontSize=9.5,
        leading=13,
        spaceAfter=5,
    )
    prompt_box_style = ParagraphStyle(
        "PromptBox",
        parent=styles["Normal"],
        fontName=font_name,
        fontSize=9,
        leading=13,
        backColor=colors.HexColor("#F4F6F7"),
        borderColor=colors.HexColor("#BDC3C7"),
        borderWidth=0.7,
        borderPadding=8,
        spaceBefore=6,
    )

    elements = []
    date_str = datetime.now().strftime("%d.%m.%Y %H:%M")
    elements.append(Paragraph(f"Инвестиционная сводка портфеля (MOEX) — {date_str}", title_style))

    pnl_sign = "+" if report["total_pnl"] >= 0 else ""
    summary_html = (
        f"<b>Сумма инвестиций:</b> {report['total_invested']} RUB &nbsp;|&nbsp; "
        f"<b>Текущая стоимость:</b> {report['total_current']} RUB<br/>"
        f"<b>Бумажная прибыль (PnL):</b> {pnl_sign}{report['total_pnl']} RUB ({pnl_sign}{report['total_pnl_pct']}%)<br/>"
        f"<b>Аллокация классов:</b> Акции {report['stocks_share_pct']}% ({report['stocks_value']} RUB) / "
        f"Облигации {report['bonds_share_pct']}% ({report['bonds_value']} RUB)"
    )
    elements.append(Paragraph(summary_html, body_style))
    elements.append(Spacer(1, 8))

    # Таблица позиций портфеля
    table_data = [
        ["Тикер", "Тип", "Кол-во", "Ср. цена", "Тек. цена", "Оценка (RUB)", "PnL (RUB)", "PnL (%)", "Доля (%)"]
    ]
    for p in report["positions"]:
        sign = "+" if p["pnl"] >= 0 else ""
        table_data.append(
            [
                p["ticker"],
                "Акция" if p["asset_type"] == "stock" else "Облигация",
                str(p["amount"]),
                str(p["avg_buy_price"]),
                str(p["current_price"]),
                str(p["current_value"]),
                f"{sign}{p['pnl']}",
                f"{sign}{p['pnl_pct']}%",
                f"{p['share_pct']}%",
            ]
        )

    if len(table_data) == 1:
        table_data.append(["—", "—", "0", "0.00", "0.00", "0.00", "0.00", "0.00%", "0.00%"])

    portfolio_table = Table(table_data, repeatRows=1)
    portfolio_table.setStyle(
        TableStyle(
            [
                ("FONTNAME", (0, 0), (-1, -1), font_name),
                ("FONTSIZE", (0, 0), (-1, -1), 8),
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1F2937")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.whitesmoke),
                ("ALIGN", (2, 1), (-1, -1), "RIGHT"),
                ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#D1D5DB")),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F9FAFB")]),
                ("TOPPADDING", (0, 0), (-1, -1), 5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ]
        )
    )
    elements.append(portfolio_table)

    # Блок алгоритмических рекомендаций по ребалансировке
    elements.append(Paragraph("Математический расчет ребалансировки (без ИИ):", h2_style))
    for item in report["rebalance_advice"]:
        elements.append(Paragraph(f"• {item}", body_style))

    # Шаблон-промпт для пересылки PDF во внешнюю нейросеть
    elements.append(
        Paragraph("Готовый промпт для анализа во внешней нейросети (ChatGPT / Claude / Gemini):", h2_style)
    )
    llm_prompt_text = (
        "<b>Инструкция для ИИ-аналитика:</b><br/>"
        "Проанализируй мой инвестиционный портфель на Московской бирже (MOEX), представленный в таблице выше. "
        "Выполни следующие шаги:<br/>"
        "1. Оцени диверсификацию портфеля по секторам экономики и эмитентам (выяви перекосы и риски концентрации).<br/>"
        "2. Прокомментируй текущее соотношение акций и облигаций с учетом текущей ключевой ставки ЦБ РФ.<br/>"
        "3. Разбери убыточные и прибыльные позиции (PnL): какие бумаги имеют фундаментальный потенциал восстановления, "
        "а какие стоит рассмотреть к замене.<br/>"
        "4. Предложи 3–5 конкретных идей (с тикерами MOEX) для следующей докупки в портфель."
    )
    elements.append(Paragraph(llm_prompt_text, prompt_box_style))

    doc.build(elements)
    return buffer.getvalue()