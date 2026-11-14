import logging
from datetime import datetime
from aiogram import Router
from aiogram.filters import Command
from aiogram.types import BufferedInputFile, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.handlers.base import get_or_create_user
from app.finance.services.analytics import analyze_portfolio, build_prompt_text, generate_portfolio_pdf_async

logger = logging.getLogger(__name__)
router = Router(name="invest_router")


def _fmt(v) -> str:
    return f"{v:,.2f}".replace(",", " ").replace(".", ",")


_TYPE_LABEL = {"bond": "Облигация", "stock": "Акция", "fund": "Фонд"}


def _format_portfolio_message(report: dict) -> str:
    """Форматирует словарь от analyze_portfolio в HTML-сообщение для Telegram."""
    if not report.get("positions"):
        return (
            "📊 <b>Ваш инвестиционный портфель пуст.</b>\n\n"
            "Отправьте мне брокерский отчет ТБанка (<code>.pdf</code>) или таблицу "
            "(<code>.csv</code>, <code>.xlsx</code>), чтобы загрузить ваши ценные бумаги."
        )

    fc = report["income_forecast"]
    lines = [
        "📈 <b>Портфель (цены MOEX):</b>\n",
        f"💰 Стоимость: <b>{_fmt(report['total_current'])} ₽</b>",
    ]
    if report.get("cash_rub") is not None:
        lines.append(f"💵 Свободно на счёте: <b>{_fmt(report['cash_rub'])} ₽</b>")
    if report.get("total_pnl") is not None and report.get("cost_known_count"):
        pnl, pnl_pct = report["total_pnl"], report["total_pnl_pct"]
        sign = "+" if pnl >= 0 else ""
        lines.append(f"{'🟢' if pnl >= 0 else '🔴'} P&L (по бумагам с известной ценой покупки): "
                     f"<b>{sign}{_fmt(pnl)} ₽ ({sign}{pnl_pct}%)</b>")
    else:
        lines.append("ℹ️ P&L не считается: в отчёте ТБанка нет цены покупки.")
    lines.append(f"💸 Ожидаемый доход за 12 мес.: <b>{_fmt(fc['total'])} ₽</b> (≈ {_fmt(fc['monthly_avg'])} ₽/мес., до налога)")
    lines.append(
        f"⚖️ Структура: акции <b>{report['stocks_share_pct']}%</b> | облигации <b>{report['bonds_share_pct']}%</b> "
        f"| фонды <b>{report['funds_share_pct']}%</b>\n"
    )
    lines.append("<b>Позиции:</b>")
    for pos in report["positions"]:
        price = f"{_fmt(pos['current_price'])} ₽" if pos["price_known"] else "нет цены"
        extra = ""
        if pos["pnl"] is not None:
            p_sign = "+" if pos["pnl"] >= 0 else ""
            extra = f" | P&L: {p_sign}{_fmt(pos['pnl'])} ₽ ({p_sign}{pos['pnl_pct']}%)"
        lines.append(
            f"▫️ <b>{pos['name']}</b> ({_TYPE_LABEL.get(pos['asset_type'], '')}) — {pos['amount']} шт.\n"
            f"   Цена: {price} | Оценка: <b>{_fmt(pos['current_value'])} ₽</b> ({pos['share_pct']}%){extra}"
        )

    if report.get("findings"):
        lines.append("\n🔎 <b>Наблюдения:</b>")
        for f in report["findings"]:
            lines.append(("⚠️ " if f["level"] == "warn" else "• ") + f["text"])

    lines.append("\nЧтобы получить разбор от нейросети: /export_portfolio — пришлю PDF и готовый промпт.")
    return "\n".join(lines)


@router.message(Command("portfolio"))
async def cmd_portfolio(message: Message, session: AsyncSession) -> None:
    if not message.from_user:
        return

    user = await get_or_create_user(session, message.from_user.id)
    wait_msg = await message.answer("⏳ Запрашиваю котировки с Московской биржи (MOEX ISS)...")

    try:
        report = await analyze_portfolio(session, user.id)
        text = _format_portfolio_message(report)
        await wait_msg.edit_text(text, parse_mode="HTML")
    except Exception as e:
        logger.exception("Ошибка при расчете портфеля: %s", e)
        await wait_msg.edit_text(
            "⚠️ Произошла ошибка при получении аналитики портфеля. Попробуйте чуть позже."
        )


@router.message(Command("export_portfolio"))
async def cmd_export_portfolio(message: Message, session: AsyncSession) -> None:
    if not message.from_user:
        return

    user = await get_or_create_user(session, message.from_user.id)
    wait_msg = await message.answer("📄 Формирую PDF-сводку портфеля и промпт для нейросети...")

    try:
        report = await analyze_portfolio(session, user.id)
        if not report.get("positions"):
            await wait_msg.edit_text(
                "📊 Ваш портфель пуст. Сначала загрузите брокерский отчет (.csv, .xlsx или .pdf)."
            )
            return

        pdf_bytes = await generate_portfolio_pdf_async(report)
        stamp = datetime.now().strftime('%Y_%m_%d')
        filename = f"portfolio_moex_{stamp}.pdf"
        input_file = BufferedInputFile(pdf_bytes, filename=filename)

        await message.answer_document(
            document=input_file,
            caption=(
                "📊 <b>Готовая PDF-сводка вашего инвестиционного портфеля.</b>\n\n"
                "Внизу документа встроен готовый промпт с вашими данными — отправьте этот файл "
                "в ChatGPT, Claude или Gemini, и он предложит, что делать с бумагами. "
                "Персональных данных (ФИО, номер договора) в файле нет."
            ),
            parse_mode="HTML",
        )
        # Тот же промпт отдельным файлом: копировать текст из PDF неудобно
        await message.answer_document(
            document=BufferedInputFile(build_prompt_text(report).encode("utf-8"), filename=f"prompt_{stamp}.txt"),
            caption="📝 Промпт текстом — можно сразу вставить в чат с нейросетью.",
        )
        await wait_msg.delete()
    except Exception as e:
        logger.exception("Ошибка генерации PDF портфеля: %s", e)
        await wait_msg.edit_text("❌ Не удалось сформировать PDF-файл. Проверьте логи сервиса.")