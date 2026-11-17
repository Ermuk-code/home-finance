import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, status
from pydantic import BaseModel, Field
from typing import Literal
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_async_session
from app.core.security import DataScope, get_current_user, get_scope
from app.models import ROLE_ADMIN, StatementImport, User
from app.finance.services.broker_parser import BrokerParserError, BrokerReportParser, sync_parsed_assets
from app.finance.services.parser import BankStatementParser, StatementParserError
from app.finance.services.statement_imports import (
    MAX_UPLOAD_BYTES, ImportErrorBase, commit_import, preview_import, revert_import, stage_import, summary,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["Uploads"])


class ImportConfirmation(BaseModel):
    revision: str = Field(pattern=r"^[0-9a-f]{64}$")
    choices: Dict[int, Literal["add", "skip"]] = Field(default_factory=dict)


async def _scoped_import(session: AsyncSession, scope: DataScope, import_id: int) -> StatementImport:
    batch = (await session.execute(scope.apply(select(StatementImport).where(
        StatementImport.id == import_id), StatementImport))).scalar_one_or_none()
    if batch is None:
        raise HTTPException(404, "Загрузка не найдена.")
    return batch


@router.get("/imports")
async def list_imports(scope: DataScope = Depends(get_scope), session: AsyncSession = Depends(get_async_session),
                       limit: int = Query(default=30, ge=1, le=100), offset: int = Query(default=0, ge=0)):
    batches = (await session.execute(scope.apply(select(StatementImport), StatementImport)
        .order_by(StatementImport.id.desc()).limit(limit).offset(offset))).scalars().all()
    return [summary(batch) for batch in batches]


@router.get("/imports/{import_id}")
async def get_import(import_id: int, scope: DataScope = Depends(get_scope),
                     session: AsyncSession = Depends(get_async_session)):
    batch = await _scoped_import(session, scope, import_id)
    try:
        return await preview_import(session, batch)
    except ImportErrorBase as exc:
        raise HTTPException(exc.status_code, str(exc)) from exc


@router.post("/imports/{import_id}/confirm")
async def confirm_import(import_id: int, payload: ImportConfirmation, scope: DataScope = Depends(get_scope),
                         session: AsyncSession = Depends(get_async_session)):
    batch = await _scoped_import(session, scope, import_id)
    try:
        return await commit_import(session, batch.id, batch.user_id, payload.revision, payload.choices)
    except ImportErrorBase as exc:
        await session.rollback()
        raise HTTPException(exc.status_code, str(exc)) from exc


@router.post("/imports/{import_id}/revert")
async def undo_import(import_id: int, scope: DataScope = Depends(get_scope),
                      session: AsyncSession = Depends(get_async_session)):
    batch = await _scoped_import(session, scope, import_id)
    try:
        return await revert_import(session, batch.id, batch.user_id)
    except ImportErrorBase as exc:
        await session.rollback()
        raise HTTPException(exc.status_code, str(exc)) from exc


async def _resolve_target_user(session: AsyncSession, current_user: User, user_id: Optional[int] = None) -> User:
    """Кому принадлежат загружаемые данные: себе; чужому профилю — только admin."""
    if user_id is None or user_id == current_user.id:
        return current_user
    if current_user.role != ROLE_ADMIN:
        raise HTTPException(status_code=403, detail="Нельзя загружать данные от имени другого пользователя.")
    user = await session.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail=f"Пользователь с id={user_id} не найден.")
    return user


@router.post("/upload")
async def upload_financial_file(
    file: UploadFile = File(...),
    user_id: Optional[int] = Form(default=None),
    bank_hint: Optional[str] = Form(default=None),
    account_key: str = Form(default="", max_length=100),
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
) -> Dict[str, Any]:
    """
    Принимает файл выписки или брокерского отчета (PDF, CSV, XLSX).
    Банковская выписка сначала создаёт предпросмотр; сохранение — отдельным подтверждением.
    Брокерские отчёты сохраняются сразу, как раньше.
    """
    filename = file.filename or "uploaded_file"
    file_bytes = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(file_bytes) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "Максимальный размер файла — 20 МБ.")

    if not file_bytes:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Загружен пустой файл.",
        )

    target_user = await _resolve_target_user(session, current_user, user_id)

    # 1. Пытаемся распарсить как банковскую выписку (Сбер, Альфа-Банк, Т-Банк)
    try:
        parsed_txs = await BankStatementParser.parse_file_async(file_bytes, filename, bank_hint)
    except StatementParserError as e:
        logger.info("Файл %s не подошел под банковскую выписку (%s), пробуем брокерский парсер.", filename, e)
        parsed_txs = []

    if parsed_txs:
        try:
            batch = await stage_import(session, target_user.id, filename, file_bytes, parsed_txs, account_key)
            return await preview_import(session, batch)
        except ImportErrorBase as exc:
            await session.rollback()
            raise HTTPException(exc.status_code, str(exc)) from exc

    # 2. Пытаемся распарсить как брокерский отчет (CSV, XLSX, PDF)
    try:
        parsed_assets = await BrokerReportParser.parse_file_async(file_bytes, filename)
        if parsed_assets or getattr(parsed_assets, "closed_tickers", None):
            added, updated = await sync_parsed_assets(session, target_user.id, parsed_assets)
            return {
                "status": "ok",
                "file_type": "broker_report",
                "filename": filename,
                "total_found": len(parsed_assets),
                "assets_added": added,
                "assets_updated": updated,
                "tickers": [a.ticker for a in parsed_assets],
                "cash_rub": float(parsed_assets.cash_rub) if getattr(parsed_assets, "cash_rub", None) is not None else None,
                "closed_tickers": list(getattr(parsed_assets, "closed_tickers", [])),
                "warnings": list(getattr(parsed_assets, "warnings", [])),
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
