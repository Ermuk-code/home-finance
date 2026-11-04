import logging
from datetime import datetime
from aiogram import Router
from aiogram.filters import Command
from aiogram.types import BufferedInputFile, Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.handlers.base import get_or_create_user
from services.analytics import analyze_portfolio, generate_portfolio_pdf_async

logger = logging.getLogger(__name__)
router = Router(name="invest_router")


def _format_portfolio_message(report: dict) -> str:
    """Форматирует словарь от analyze_portfolio в HTML-сообщение для Telegram."""
    if not report.get("positions"):
        return (
            "📊 <b>Ваш инвестиционный портфель пуст.</b>\n\n"
            "Отправьте мне файл брокерского отчета (<code>.csv</code>, <code>.xlsx</code> или <code>.pdf</code>), "
            "чтобы загрузить ваши ценные бумаги."
        )

    pnl = report["total_pnl"]
    pnl_pct = report["total_pnl_pct"]
    sign = "+" if pnl >= 0 else ""
    indicator = "🟢" if pnl >= 0 else "🔴"

    lines = [
        "📈 <b>Аналитика инвестиционного портфеля (MOEX):</b>\n",
        f"💼 Вложено: <b>{report['total_invested']} ₽</b>",
        f"💰 Текущая стоимость: <b>{report['total_current']} ₽</b>",
        f"{indicator} Бумажный PnL: <b>{sign}{pnl} ₽ ({sign}{pnl_pct}%)</b>",
        f"⚖️ Классы: Акции <b>{report['stocks_share_pct']}%</b> | Облигации <b>{report['bonds_share_pct']}%</b>\n",
        "<b>Позиции в портфеле:</b>",
    ]

    for pos in report["positions"]:
        p_sign = "+" if pos["pnl"] >= 0 else ""
        p_icon = "🟢" if pos["pnl"] >= 0 else "🔴"
        asset_label = "Акция" if pos["asset_type"] == "stock" else "Облигация"
        lines.append(
            f"{p_icon} <b>{pos['ticker']}</b> ({asset_label}) — {pos['amount']} шт.\n"
            f"   Ср: {pos['avg_buy_price']} ₽ ➔ Тек: {pos['current_price']} ₽\n"
            f"   Оценка: <b>{pos['current_value']} ₽</b> ({pos['share_pct']}%) | "
            f"PnL: {p_sign}{pos['pnl']} ₽ ({p_sign}{pos['pnl_pct']}%)"
        )

    lines.append("\n📐 <b>Рекомендации по ребалансировке (без ИИ):</b>")
    for advice in report.get("rebalance_advice", []):
        lines.append(f"▫️ {advice}")

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
        filename = f"portfolio_moex_{datetime.now().strftime('%Y_%m_%d')}.pdf"
        input_file = BufferedInputFile(pdf_bytes, filename=filename)

        await message.answer_document(
            document=input_file,
            caption=(
                "📊 <b>Готовая PDF-сводка вашего инвестиционного портфеля.</b>\n\n"
                "Внизу документа уже встроен шаблон промпта — просто перешлите этот файл "
                "в ChatGPT, Claude или Gemini для глубокого фундаментального разбора."
            ),
            parse_mode="HTML",
        )
        await wait_msg.delete()
    except Exception as e:
        logger.exception("Ошибка генерации PDF портфеля: %s", e)
        await wait_msg.edit_text("❌ Не удалось сформировать PDF-файл. Проверьте логи сервиса.")