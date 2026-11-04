import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from db.base import get_async_session
from db.models import User
from services.broker_parser import BrokerParserError, BrokerReportParser, sync_parsed_assets
from services.categorizer import save_parsed_transactions
from services.parser import BankStatementParser, StatementParserError
from web.auth import get_current_admin

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api",
    tags=["Uploads"],
    dependencies=[Depends(get_current_admin)],
)


async def _resolve_target_user(session: AsyncSession, user_id: Optional[int] = None) -> User:
    """Находит пользователя для привязки загружаемого файла (или создает запись для ADMIN_TELEGRAM_ID)."""
    if user_id is not None:
        user = await session.get(User, user_id)
        if not user:
            raise HTTPException(status_code=404, detail=f"Пользователь с id={user_id} не найден.")
        return user

    # Ищем первого пользователя в БД
    first_user = (await session.execute(select(User).order_by(User.id.asc()))).scalars().first()
    if first_user:
        return first_user

    # Если таблица users еще пуста, создаем пользователя на основе ADMIN_TELEGRAM_ID
    tg_id = settings.ADMIN_TELEGRAM_ID or 1
    new_user = User(telegram_id=tg_id)
    session.add(new_user)
    await session.commit()
    await session.refresh(new_user)
    return new_user


@router.post("/upload")
async def upload_financial_file(
    file: UploadFile = File(...),
    user_id: Optional[int] = Form(default=None),
    bank_hint: Optional[str] = Form(default=None),
    session: AsyncSession = Depends(get_async_session),
) -> Dict[str, Any]:
    """
    Принимает файл выписки или брокерского отчета (PDF, CSV, XLSX).
    Автоматически распознает тип документа и сохраняет данные в БД.
    """
    filename = file.filename or "uploaded_file"
    file_bytes = await file.read()

    if not file_bytes:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Загружен пустой файл.",
        )

    target_user = await _resolve_target_user(session, user_id)

    # 1. Пытаемся распарсить как банковскую выписку (Сбер, Альфа-Банк, Т-Банк)
    try:
        parsed_txs = await BankStatementParser.parse_file_async(file_bytes, filename, bank_hint)
        if parsed_txs:
            saved_count, categorized_count, skipped_dups = await save_parsed_transactions(
                session, target_user.id, parsed_txs
            )
            return {
                "status": "ok",
                "file_type": "bank_statement",
                "bank_name": parsed_txs[0].bank_name,
                "filename": filename,
                "total_found": len(parsed_txs),
                "saved_new": saved_count,
                "categorized_automatically": categorized_count,
                "uncategorized": saved_count - categorized_count,
                "skipped_duplicates": skipped_dups,
            }
    except StatementParserError as e:
        logger.info("Файл %s не подошел под банковскую выписку (%s), пробуем брокерский парсер.", filename, e)
    except Exception as e:
        logger.warning("Ошибка при попытке банковского парсинга %s: %s", filename, e)

    # 2. Пытаемся распарсить как брокерский отчет (CSV, XLSX, PDF)
    try:
        parsed_assets = await BrokerReportParser.parse_file_async(file_bytes, filename)
        if parsed_assets:
            added, updated = await sync_parsed_assets(session, target_user.id, parsed_assets)
            return {
                "status": "ok",
                "file_type": "broker_report",
                "filename": filename,
                "total_found": len(parsed_assets),
                "assets_added": added,
                "assets_updated": updated,
                "tickers": [a.ticker for a in parsed_assets],
            }
    except BrokerParserError as e:
        logger.info("Файл %s не распознан брокерским парсером: %s", filename, e)
    except Exception as e:
        logger.exception("Ошибка брокерского парсера на файле %s: %s", filename, e)

    raise HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        detail=(
            f"Не удалось распознать структуру файла '{filename}'. "
            "Поддерживаются выписки Сбера, Альфа-Банка, Т-Банка (PDF/CSV) и брокерские отчеты (CSV/XLSX/PDF)."
        ),
    )