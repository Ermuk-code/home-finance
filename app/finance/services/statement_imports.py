"""Transactional bank import workflow shared by HTTP and Telegram.

A missing bank ID is NOT proof that two equal purchases are the same operation.
Such overlaps require a decision; repeated equal rows within a file retain their
multiplicity. All mutations lock the owner row, serializing concurrent imports
across processes. Database constraints are the second line of defence.
"""
import hashlib
import json
import re
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional
from zoneinfo import ZoneInfo

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.finance.services.categorizer import (
    TRANSFER_CATEGORY_NAMES, _is_date_in_vacations, match_category, seed_default_categories,
)
from app.finance.services.parser import ParsedTransaction
from app.models import Category, StatementImport, StatementImportRow, Transaction, User, Vacation

STATEMENT_TZ = ZoneInfo("Europe/Moscow")
MAX_UPLOAD_BYTES = 20 * 1024 * 1024
MAX_IMPORT_ROWS = 10000


class ImportErrorBase(ValueError):
    status_code = 422


class ImportNotFound(ImportErrorBase):
    status_code = 404


class ImportChanged(ImportErrorBase):
    status_code = 409


def normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", value)).strip()


def utc(value: datetime) -> datetime:
    # DB rows are UTC; SQLite test databases strip tzinfo from an aware value.
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def content_key(row) -> tuple:
    return (utc(row.date).isoformat(), str(Decimal(row.amount).quantize(Decimal("0.01"))),
            row.bank_name, row.account_key, normalize_text(row.description).casefold())


def fingerprint(row) -> str:
    identity = ("bank-id", row.bank_name, row.account_key, row.external_id) if row.external_id else content_key(row)
    return hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode()).hexdigest()


@dataclass
class PlannedRow:
    row: StatementImportRow
    action: str
    candidates: list = field(default_factory=list)
    duplicate_of: Optional[int] = None


def plan_rows(rows: list, existing: list) -> list[PlannedRow]:
    """Pure matching; no writes and no lossy prefix comparison."""
    consumed = set()
    seen_ids = {}
    plan = []
    by_identity, by_content, by_bank_amount = defaultdict(list), defaultdict(list), defaultdict(list)
    for tx in existing:
        by_identity[(tx.bank_name, tx.account_key, tx.external_id)].append(tx)
        by_content[content_key(tx)].append(tx)
        by_bank_amount[(tx.bank_name, tx.amount)].append(tx)
    for row in rows:
        identity = (row.bank_name, row.account_key, row.external_id)
        if row.external_id and identity in seen_ids:
            first = seen_ids[identity]
            if content_key(first) != content_key(row):
                raise ImportErrorBase(f"Строки {first.row_number} и {row.row_number}: один номер операции с разными данными.")
            plan.append(PlannedRow(row, "duplicate_in_file", duplicate_of=first.row_number))
            continue
        if row.external_id:
            seen_ids[identity] = row
        same_id = by_identity[identity] if row.external_id else []
        if same_id:
            action = "duplicate" if content_key(same_id[0]) == content_key(row) else "conflict"
            plan.append(PlannedRow(row, action, same_id))
            continue
        exact = [tx for tx in by_content[content_key(row)]
                 if not (row.external_id and tx.external_id and row.external_id != tx.external_id)]
        available = [tx for tx in exact if tx.id not in consumed]
        if available:
            consumed.add(available[0].id)
            plan.append(PlannedRow(row, "review", available[:5]))
        elif exact:
            # The file contains MORE identical purchases than are already recorded.
            plan.append(PlannedRow(row, "new"))
        else:
            possible = [tx for tx in by_bank_amount[(row.bank_name, row.amount)]
                        if (tx.account_key == row.account_key or not tx.account_key or not row.account_key)
                        and not (row.external_id and tx.external_id and row.external_id != tx.external_id)
                        and abs((utc(tx.date) - utc(row.date)).total_seconds()) <= 86400]
            plan.append(PlannedRow(row, "review" if possible else "new", possible[:5]))
    return plan


async def lock_owner(session: AsyncSession, user_id: int) -> None:
    owner = (await session.execute(select(User.id).where(User.id == user_id).with_for_update())).scalar_one_or_none()
    if owner is None:
        raise ImportNotFound("Пользователь не найден.")


async def owned_import(session: AsyncSession, import_id: int, user_id: int) -> StatementImport:
    batch = (await session.execute(select(StatementImport).where(
        StatementImport.id == import_id, StatementImport.user_id == user_id
    ).execution_options(populate_existing=True))).scalar_one_or_none()
    if batch is None:
        raise ImportNotFound("Загрузка не найдена.")
    return batch


async def import_rows(session: AsyncSession, batch: StatementImport) -> list:
    return list((await session.execute(select(StatementImportRow).where(
        StatementImportRow.import_id == batch.id).order_by(StatementImportRow.row_number))).scalars().all())


async def existing_rows(session: AsyncSession, batch: StatementImport, rows: list) -> list:
    start = min(utc(row.date) for row in rows) - timedelta(days=1)
    end = max(utc(row.date) for row in rows) + timedelta(days=1)
    ids = [row.external_id for row in rows if row.external_id]
    stmt = select(Transaction).where(Transaction.user_id == batch.user_id,
        Transaction.bank_name.in_({row.bank_name for row in rows}),
        or_(Transaction.date.between(start, end), Transaction.external_id.in_(ids)))
    return list((await session.execute(stmt.order_by(Transaction.id).execution_options(populate_existing=True))).scalars().all())


def revision(plan: list[PlannedRow]) -> str:
    data = [(p.row.row_number, p.action, p.duplicate_of,
             [(tx.id, content_key(tx), tx.external_id) for tx in p.candidates]) for p in plan]
    return hashlib.sha256(json.dumps(data, ensure_ascii=False).encode()).hexdigest()


def summary(batch: StatementImport) -> dict:
    return {"import_id": batch.id, "user_id": batch.user_id, "filename": batch.filename,
            "bank_name": batch.bank_name, "account_key": batch.account_key, "status": batch.status,
            "file_type": "bank_statement", "saved_new": batch.saved_count,
            "categorized_automatically": batch.categorized_count,
            "uncategorized": batch.saved_count - batch.categorized_count,
            "skipped_duplicates": batch.skipped_count,
            "created_at": batch.created_at.isoformat(),
            "committed_at": batch.committed_at.isoformat() if batch.committed_at else None}


async def preview_import(session: AsyncSession, batch: StatementImport) -> dict:
    rows = await import_rows(session, batch)
    payload = {**summary(batch), "total_found": len(rows)}
    if batch.status != "pending":
        return payload
    existing = await existing_rows(session, batch, rows)
    plan = plan_rows(rows, existing)
    counts = Counter(p.action for p in plan)
    payload.update({"revision": revision(plan), "new_count": counts["new"],
                    "review_count": counts["review"], "conflict_count": counts["conflict"],
                    "duplicate_count": counts["duplicate"] + counts["duplicate_in_file"],
                    "warnings": [] if all(r.account_key for r in rows) else [
                        "Счёт не указан. Для разных счетов одного банка задавайте разные постоянные метки."],
                    "rows": [{"row_number": p.row.row_number, "date": utc(p.row.date).isoformat(),
                              "amount": str(p.row.amount), "description": p.row.description,
                              "account_key": p.row.account_key, "external_id": p.row.external_id,
                              "action": p.action, "duplicate_of": p.duplicate_of,
                              "candidates": [{"id": tx.id, "date": utc(tx.date).isoformat(),
                                  "amount": str(tx.amount), "description": tx.description,
                                  "account_key": tx.account_key} for tx in p.candidates]} for p in plan]})
    return payload


async def stage_import(session: AsyncSession, user_id: int, filename: str, file_bytes: bytes,
                       parsed: list[ParsedTransaction], account_key: str = "") -> StatementImport:
    if not parsed or len(parsed) > MAX_IMPORT_ROWS:
        raise ImportErrorBase(f"В выписке должно быть от 1 до {MAX_IMPORT_ROWS} операций.")
    account_key = normalize_text(account_key)
    if len(account_key) > 100:
        raise ImportErrorBase("Метка счёта слишком длинная (максимум 100 символов).")
    await lock_owner(session, user_id)
    digest = hashlib.sha256(file_bytes).hexdigest()
    previous = (await session.execute(select(StatementImport).where(
        StatementImport.user_id == user_id, StatementImport.file_sha256 == digest,
        StatementImport.account_key == account_key, StatementImport.status != "reverted"
    ))).scalar_one_or_none()
    if previous is not None:
        await session.commit()
        return previous
    batch = StatementImport(user_id=user_id, filename=filename[:255], file_sha256=digest,
                            account_key=account_key, bank_name=parsed[0].bank_name)
    session.add(batch)
    await session.flush()
    staged = []
    for number, item in enumerate(parsed, 1):
        amount = item.amount
        if not amount.is_finite() or abs(amount) >= Decimal("10000000000") or amount != amount.quantize(Decimal(".01")):
            raise ImportErrorBase(f"Строка {number}: сумма не помещается в формат рублей и копеек.")
        stamp = item.date.replace(tzinfo=STATEMENT_TZ) if item.date.tzinfo is None else item.date
        row = StatementImportRow(import_id=batch.id, row_number=number, date=utc(stamp), amount=amount,
            description=item.description, bank_name=item.bank_name, account_key=account_key or item.account_key,
            external_id=item.external_id or None)
        session.add(row)
        staged.append(row)
    plan_rows(staged, [])  # Reject inconsistent repeated bank IDs before persisting the preview.
    await session.commit()
    return batch


async def commit_import(session: AsyncSession, import_id: int, user_id: int, expected_revision: str,
                        choices: Optional[dict[int, str]] = None) -> dict:
    await lock_owner(session, user_id)
    batch = await owned_import(session, import_id, user_id)
    if batch.status == "committed":
        return await preview_import(session, batch)  # Retried request, including Telegram double-clicks.
    if batch.status != "pending":
        raise ImportChanged("Загрузка отменена. Загрузите файл ещё раз.")
    rows = await import_rows(session, batch)
    existing = await existing_rows(session, batch, rows)
    plan = plan_rows(rows, existing)
    if revision(plan) != expected_revision:
        raise ImportChanged("Данные изменились после предпросмотра. Обновите его и проверьте совпадения.")
    choices = choices or {}
    if set(choices) - {r.row_number for r in rows} or any(v not in ("add", "skip") for v in choices.values()):
        raise ImportErrorBase("Некорректные решения по строкам выписки.")
    for p in plan:
        decision = choices.get(p.row.row_number)
        if p.action in ("review", "conflict") and decision is None:
            raise ImportErrorBase(f"Строка {p.row.row_number}: выберите «добавить» или «пропустить».")
        if p.action == "conflict" and decision != "skip":
            raise ImportErrorBase(f"Строка {p.row.row_number}: этот банковский номер уже есть с другими данными; можно только пропустить.")
        if p.action in ("duplicate", "duplicate_in_file") and decision == "add":
            raise ImportErrorBase("Операция с таким банковским номером уже существует.")
    # No helper may commit here: that would release the owner lock halfway through an import.
    await seed_default_categories(session, commit=False)
    categories = list((await session.execute(select(Category).order_by(Category.id))).scalars().all())
    vacations = list((await session.execute(select(Vacation).where(Vacation.user_id == user_id))).scalars().all())
    travel = next((c for c in categories if c.name == "Путешествия"), None)
    slots = defaultdict(int)
    for tx in existing:
        if tx.import_fingerprint:
            slots[tx.import_fingerprint] = max(slots[tx.import_fingerprint], tx.import_occurrence or 0)
    by_number = {}
    for p in plan:
        row = p.row
        decision = choices.get(row.row_number)
        if p.action == "duplicate_in_file":
            row.transaction_id = by_number[p.duplicate_of].transaction_id
            row.outcome = "duplicate"
        elif p.action == "duplicate" or decision == "skip":
            row.transaction_id = p.candidates[0].id if p.candidates else None
            row.outcome = "duplicate" if p.action == "duplicate" else "skipped"
        else:
            category = match_category(row.description, row.amount > 0, categories, bank_name=row.bank_name)
            if row.amount < 0 and travel and _is_date_in_vacations(row.date, vacations):
                if not category or not (category.is_mandatory or category.name in TRANSFER_CATEGORY_NAMES):
                    category = travel
            fp = fingerprint(row)
            slots[fp] += 1
            tx = Transaction(user_id=user_id, category_id=category.id if category else None, amount=row.amount,
                description=row.description, date=utc(row.date), bank_name=row.bank_name,
                is_parsed_automatically=category is not None, import_id=batch.id,
                account_key=row.account_key, external_id=row.external_id,
                import_fingerprint=fp, import_occurrence=slots[fp])
            session.add(tx)
            await session.flush()
            row.transaction_id, row.outcome = tx.id, "added"
            batch.saved_count += 1
            batch.categorized_count += int(category is not None)
        if row.outcome != "added":
            batch.skipped_count += 1
        by_number[row.row_number] = row
    batch.status, batch.committed_at = "committed", datetime.now(timezone.utc)
    await session.commit()
    return {**summary(batch), "total_found": len(rows)}


async def revert_import(session: AsyncSession, import_id: int, user_id: int) -> dict:
    await lock_owner(session, user_id)
    batch = await owned_import(session, import_id, user_id)
    if batch.status == "reverted":
        return {**summary(batch), "deleted_transactions": 0}
    rows = await import_rows(session, batch)
    batch.status, batch.reverted_at = "reverted", datetime.now(timezone.utc)
    await session.flush()
    ids = {r.transaction_id for r in rows if r.transaction_id is not None}
    deleted = 0
    if ids:
        retained = set((await session.execute(select(StatementImportRow.transaction_id)
            .join(StatementImport, StatementImport.id == StatementImportRow.import_id)
            .where(StatementImport.status == "committed", StatementImportRow.transaction_id.in_(ids))
        )).scalars().all())
        removable = list((await session.execute(select(Transaction).where(
            Transaction.id.in_(ids - retained), Transaction.user_id == user_id, Transaction.import_id.is_not(None)
        ))).scalars().all())
        for tx in removable:
            await session.delete(tx)
            deleted += 1
    await session.commit()
    return {**summary(batch), "deleted_transactions": deleted,
            "retained_transactions": len(ids) - deleted}
