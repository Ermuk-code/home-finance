import io
import logging
from aiogram import Bot, F, Router
from aiogram.types import Message
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.handlers.base import get_or_create_user
from bot.keyboards import build_uncategorized_prompt_kb
from db.models import Transaction
from services.broker_parser import BrokerParserError, BrokerReportParser, sync_parsed_assets
from services.categorizer import save_parsed_transactions
from services.parser import BankStatementParser, StatementParserError

logger = logging.getLogger(__name__)
router = Router(name="documents_router")


@router.message(F.document)
async def handle_document(message: Message, bot: Bot, session: AsyncSession) -> None:
    if not message.from_user or not message.document:
        return

    user = await get_or_create_user(session, message.from_user.id)
    filename = message.document.file_name or "document.pdf"
    status_msg = await message.answer(f"⏳ Загружаю и анализирую файл <code>{filename}</code>...", parse_mode="HTML")

    try:
        file_io = io.BytesIO()
        await bot.download(message.document, destination=file_io)
        file_bytes = file_io.getvalue()
    except Exception as e:
        logger.exception("Ошибка скачивания файла из Telegram: %s", e)
        await status_msg.edit_text("❌ Не удалось скачать файл из Telegram. Попробуйте еще раз.")
        return

    # 1. Пробуем распарсить как банковскую выписку (Сбер, Альфа, Т-Банк)
    try:
        parsed_txs = await BankStatementParser.parse_file_async(file_bytes, filename)
        if parsed_txs:
            saved_count, categorized_count, skipped_dups = await save_parsed_transactions(
                session, user.id, parsed_txs
            )
            uncategorized_in_file = saved_count - categorized_count

            # Считаем общее количество неразобранных операций пользователя в БД
            total_uncat_res = await session.execute(
                select(func.count(Transaction.id)).where(
                    Transaction.user_id == user.id,
                    Transaction.category_id.is_(None),
                )
            )
            total_uncat = total_uncat_res.scalar() or 0

            bank_title = parsed_txs[0].bank_name
            reply_text = (
                f"✅ <b>Банковская выписка обработана!</b>\n"
                f"🏦 Банк: <b>{bank_title}</b>\n"
                f"📥 Найдено в файле: <b>{len(parsed_txs)}</b> опер.\n"
                f"➕ Добавлено новых: <b>{saved_count}</b>\n"
                f"🔄 Пропущено дубликатов: <b>{skipped_dups}</b>\n"
                f"🏷 Автоматически распознано: <b>{categorized_count}</b> из {saved_count}\n"
            )

            if total_uncat > 0:
                reply_text += (
                    f"\n⚠️ Нераспознанных операций в базе: <b>{total_uncat} шт.</b>\n"
                    f"Нажмите кнопку ниже или введите /uncategorized, чтобы указать их категории и обучить парсер."
                )
                await status_msg.edit_text(
                    reply_text,
                    parse_mode="HTML",
                    reply_markup=build_uncategorized_prompt_kb(total_uncat),
                )
            else:
                await status_msg.edit_text(reply_text, parse_mode="HTML")
            return
    except StatementParserError as bank_err:
        logger.info("Файл %s не является банковской выпиской (%s), пробуем брокерский парсер...", filename, bank_err)
    except Exception as e:
        logger.warning("Ошибка при попытке банковского парсинга %s: %s", filename, e)

    # 2. Если банковских транзакций не найдено, пробуем разобрать как брокерский отчет
    try:
        parsed_assets = await BrokerReportParser.parse_file_async(file_bytes, filename)
        if parsed_assets:
            added, updated = await sync_parsed_assets(session, user.id, parsed_assets)
            tickers_preview = ", ".join(a.ticker for a in parsed_assets[:8])
            if len(parsed_assets) > 8:
                tickers_preview += "..."

            await status_msg.edit_text(
                f"📈 <b>Брокерский отчет успешно обработан!</b>\n\n"
                f"➕ Новых тикеров добавлено: <b>{added}</b>\n"
                f"🔄 Существующих позиций обновлено: <b>{updated}</b>\n"
                f"🧾 Бумаги: <code>{tickers_preview}</code>\n\n"
                f"Используйте /portfolio для просмотра аналитики и PnL или /export_portfolio для выгрузки PDF.",
                parse_mode="HTML",
            )
            return
    except BrokerParserError as broker_err:
        logger.info("Файл %s не распознан брокерским парсером: %s", filename, broker_err)
    except Exception as e:
        logger.exception("Непредвиденная ошибка брокерского парсера для %s: %s", filename, e)

    # 3. Если ни один парсер не подошел
    await status_msg.edit_text(
        "⚠️ <b>Не удалось распознать документ.</b>\n\n"
        "Убедитесь, что вы отправили поддерживаемую выписку (PDF/CSV от Сбера, Альфа-Банка или Т-Банка) "
        "либо таблицу брокерского отчета (CSV/XLSX/PDF с колонками Тикер, Количество, Средняя цена).",
        parse_mode="HTML",
    )