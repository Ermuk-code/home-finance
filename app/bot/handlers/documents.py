import io
import logging
from html import escape
from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.handlers.base import get_or_create_user
from app.core.config import settings
from app.models import StatementImport, User
from app.finance.services.broker_parser import BrokerParserError, BrokerReportParser, sync_parsed_assets
from app.finance.services.budget import check_category_limits
from app.finance.services.parser import BankStatementParser, StatementParserError
from app.finance.services.statement_imports import (
    MAX_UPLOAD_BYTES, ImportErrorBase, commit_import, owned_import, preview_import, revert_import, stage_import,
)

logger = logging.getLogger(__name__)
router = Router(name="documents_router")


def import_keyboard(data: dict) -> InlineKeyboardMarkup:
    import_id = data['import_id']
    buttons = []
    if data['status'] == 'pending':
        if not data['review_count'] and not data['conflict_count']:
            buttons.append([InlineKeyboardButton(text='✅ Сохранить операции',
                callback_data=f"statement:confirm:{import_id}:{data['revision'][:16]}")])
        buttons.append([InlineKeyboardButton(text='🔎 Проверить в веб-панели',
            url=f"{settings.WEB_BASE_URL.rstrip('/')}/upload?import_id={import_id}")])
    if data['status'] != 'reverted':
        buttons.append([InlineKeyboardButton(text='↩️ Отменить загрузку', callback_data=f'statement:revert:{import_id}')])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def import_text(data: dict) -> str:
    lines = [f"📥 <b>Выписка #{data['import_id']}</b> — {escape(data['filename'])}",
             f"🏦 {escape(data['bank_name'])} · операций в файле: {data['total_found']}"]
    if data['status'] == 'pending':
        lines += [f"Новых: {data['new_count']} · точных повторов по банковскому номеру: {data['duplicate_count']}",
                  f"Требуют проверки: {data['review_count']} · конфликтов: {data['conflict_count']}",
                  '<b>Операции ещё не сохранены.</b>']
        if data['review_count'] or data['conflict_count']:
            lines.append('Проверьте совпадения в веб-панели и выберите, какие строки добавить или пропустить. '
                         'Для входа в панель используйте /web.')
        lines.extend(escape(w) for w in data['warnings'])
    elif data['status'] == 'committed':
        lines += [f"✅ Добавлено: {data['saved_new']} · пропущено: {data['skipped_duplicates']}",
                  f"Без категории: {data['uncategorized']}. Для разметки: /uncategorized.",
                  'Повторная отправка того же файла не добавляет операции.']
    else:
        lines.append('↩️ Загрузка отменена.')
    return '\n'.join(lines)


@router.callback_query(F.data.startswith('statement:'))
async def handle_import_action(callback: CallbackQuery, session: AsyncSession, current_user: User):
    parts = (callback.data or '').split(':')
    if len(parts) not in (3, 4) or not parts[2].isdigit():
        await callback.answer('Некорректная команда.', show_alert=True)
        return
    try:
        batch = await owned_import(session, int(parts[2]), current_user.id)
        if parts[1] == 'revert':
            result = await revert_import(session, batch.id, current_user.id)
            text = f"↩️ Загрузка #{batch.id} отменена. Удалено операций: {result['deleted_transactions']}."
            await callback.answer('Загрузка отменена.')
            if callback.message:
                await callback.message.edit_text(text)
            return
        data = await preview_import(session, batch)
        if parts[1] != 'confirm' or len(parts) != 4:
            raise ImportErrorBase('Неизвестная команда.')
        if batch.status == 'pending':
            if data['revision'][:16] != parts[3]:
                if callback.message:
                    await callback.message.edit_text(import_text(data), parse_mode='HTML', reply_markup=import_keyboard(data))
                await callback.answer('Данные изменились. Проверьте обновлённый предпросмотр.', show_alert=True)
                return
            data = await commit_import(session, batch.id, current_user.id, data['revision'])
        elif batch.status != 'committed':
            raise ImportErrorBase('Загрузка отменена. Отправьте файл заново.')
        await callback.answer('Операции сохранены.')
        if callback.message:
            await callback.message.edit_text(import_text(data), parse_mode='HTML', reply_markup=import_keyboard(data))
            if data['saved_new']:
                try:
                    warnings = await check_category_limits(session, current_user.id)
                    if warnings:
                        await callback.message.answer('\n'.join(warnings), parse_mode='HTML')
                except Exception:
                    logger.warning('Не удалось проверить лимиты после импорта', exc_info=True)
    except ImportErrorBase as exc:
        await session.rollback()
        await callback.answer(str(exc), show_alert=True)


@router.message(Command('imports'))
async def import_history(message: Message, session: AsyncSession, current_user: User):
    batches = (await session.execute(select(StatementImport).where(StatementImport.user_id == current_user.id)
        .order_by(StatementImport.id.desc()).limit(10))).scalars().all()
    if not batches:
        await message.answer('Загрузок выписок пока нет.')
    for batch in batches:
        data = await preview_import(session, batch)
        await message.answer(import_text(data), parse_mode='HTML', reply_markup=import_keyboard(data))


@router.message(F.document)
async def handle_document(message: Message, bot: Bot, session: AsyncSession) -> None:
    if not message.from_user or not message.document:
        return

    user = await get_or_create_user(session, message.from_user.id)
    filename = message.document.file_name or "document.pdf"
    if (message.document.file_size or 0) > MAX_UPLOAD_BYTES:
        await message.answer('Максимальный размер файла — 20 МБ.')
        return
    status_msg = await message.answer(f"⏳ Загружаю и анализирую файл <code>{escape(filename)}</code>...", parse_mode="HTML")

    try:
        file_io = io.BytesIO()
        await bot.download(message.document, destination=file_io)
        file_bytes = file_io.getvalue()
        if len(file_bytes) > MAX_UPLOAD_BYTES:
            await status_msg.edit_text('Максимальный размер файла — 20 МБ.')
            return
    except Exception as e:
        logger.exception("Ошибка скачивания файла из Telegram: %s", e)
        await status_msg.edit_text("❌ Не удалось скачать файл из Telegram. Попробуйте еще раз.")
        return

    # 1. Пробуем распарсить как банковскую выписку (Сбер, Альфа, Т-Банк)
    try:
        parsed_txs = await BankStatementParser.parse_file_async(file_bytes, filename)
    except StatementParserError as bank_err:
        logger.info("Файл %s не является банковской выпиской (%s), пробуем брокерский парсер...", filename, bank_err)
        parsed_txs = []
    except Exception as e:
        logger.exception("Ошибка банковского парсера %s: %s", filename, e)
        await status_msg.edit_text('Не удалось обработать выписку из-за внутренней ошибки. Попробуйте позже.')
        return

    if parsed_txs:
        try:
            batch = await stage_import(session, user.id, filename, file_bytes, parsed_txs)
            data = await preview_import(session, batch)
            await status_msg.edit_text(import_text(data), parse_mode='HTML', reply_markup=import_keyboard(data))
        except ImportErrorBase as exc:
            await session.rollback()
            await status_msg.edit_text(escape(str(exc)), parse_mode='HTML')
        except Exception:
            await session.rollback()
            logger.exception('Не удалось создать предпросмотр выписки')
            await status_msg.edit_text('Не удалось подготовить выписку. Операции не сохранены; попробуйте позже.')
        return

    # 2. Если банковских транзакций не найдено, пробуем разобрать как брокерский отчет
    try:
        parsed_assets = await BrokerReportParser.parse_file_async(file_bytes, filename)
        if parsed_assets or getattr(parsed_assets, "closed_tickers", None):
            added, updated = await sync_parsed_assets(session, user.id, parsed_assets)
            tickers_preview = ", ".join(a.ticker for a in parsed_assets[:8])
            if len(parsed_assets) > 8:
                tickers_preview += "..."

            notes = ""
            cash_rub = getattr(parsed_assets, "cash_rub", None)
            if cash_rub is not None:
                notes += f"💵 Свободно на счёте: <b>{cash_rub} ₽</b>\n"
            closed = getattr(parsed_assets, "closed_tickers", [])
            if closed:
                notes += f"🗑 Убрано проданных бумаг: <code>{', '.join(closed)}</code>\n"
            for w in getattr(parsed_assets, "warnings", []):
                notes += f"⚠️ {escape(w)}\n"
            if getattr(parsed_assets, "from_snapshot", False):
                notes += "ℹ️ В отчёте ТБанка нет цены покупки, поэтому P&L не считается (цену можно указать в дашборде).\n"

            await status_msg.edit_text(
                f"📈 <b>Брокерский отчет успешно обработан!</b>\n\n"
                f"➕ Новых тикеров добавлено: <b>{added}</b>\n"
                f"🔄 Существующих позиций обновлено: <b>{updated}</b>\n"
                f"🧾 Бумаги: <code>{tickers_preview}</code>\n"
                f"{notes}\n"
                f"Используйте /portfolio для аналитики или /export_portfolio для PDF с промптом для нейросети.",
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
        "либо брокерский отчет ТБанка (PDF «Отчет о сделках и операциях») или таблицу (CSV/XLSX с колонками Тикер, Количество, Средняя цена).",
        parse_mode="HTML",
    )
