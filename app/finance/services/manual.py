"""Ручной ввод операций: наличные и любые карты.

Наличные — это обычный «источник» операций: транзакции с ``bank_name == "Наличные"``. Поэтому они
сами попадают во все отчёты, лимиты и советы. Баланс кошелька — сумма всех таких операций.

Чтобы наличные не считались доходом/расходом дважды, перемещения между картой и кошельком
записываются с категориями-переводами (они не входят в «чистые» доходы и расходы):

* ``withdraw`` — «снял наличные»: приход в кошелёк. Расходная половина («Выдача наличных») придёт с
  выпиской карты, поэтому второй раз её вручную не вводим.
* ``topup`` — «внёс наличные на карту»: расход из кошелька (приходная половина придёт с выпиской).
"""
import re
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation
from typing import List, Optional, Tuple

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Category, Transaction
from app.finance.services.budget import LOCAL_TZ, local_today
from app.finance.services.categorizer import match_category, seed_default_categories

CASH_BANK_NAME = "Наличные"
# Названия совпадают с теми, что проставляет парсер выписок, чтобы банки не дробились в статистике.
CARD_BANKS = ["СберБанк", "Альфа-Банк", "Т-Банк"]

KIND_EXPENSE = "expense"
KIND_INCOME = "income"
KIND_WITHDRAW = "withdraw"
KIND_TOPUP = "topup"
VALID_KINDS = (KIND_EXPENSE, KIND_INCOME, KIND_WITHDRAW, KIND_TOPUP)
TRANSFER_KINDS = (KIND_WITHDRAW, KIND_TOPUP)

KIND_LABELS = {
    KIND_EXPENSE: "Расход",
    KIND_INCOME: "Доход",
    KIND_WITHDRAW: "Снятие наличных",
    KIND_TOPUP: "Внесение наличных на карту",
}

TRANSFER_IN_CATEGORY = "Переводы и Пополнения (Входящие)"
TRANSFER_OUT_CATEGORY = "Переводы и Сбережения (Исходящие)"

MAX_AMOUNT = Decimal("1000000000")
MAX_SOURCE_LEN = 50  # Transaction.bank_name = String(50)
MAX_DESCRIPTION_LEN = 300

_ALIASES = {
    "нал": CASH_BANK_NAME,
    "наличка": CASH_BANK_NAME,
    "наличные": CASH_BANK_NAME,
    "наличными": CASH_BANK_NAME,
    "cash": CASH_BANK_NAME,
    "сбер": "СберБанк",
    "сбербанк": "СберБанк",
    "сбер банк": "СберБанк",
    "альфа": "Альфа-Банк",
    "альфа-банк": "Альфа-Банк",
    "альфа банк": "Альфа-Банк",
    "альфабанк": "Альфа-Банк",
    "т-банк": "Т-Банк",
    "т банк": "Т-Банк",
    "тбанк": "Т-Банк",
    "тинькофф": "Т-Банк",
    "tinkoff": "Т-Банк",
}


class ManualTxError(ValueError):
    """Некорректный ввод; текст безопасно показывать пользователю."""


def normalize_source(raw: str) -> str:
    """«нал», «сбер», «Тинькофф» -> канонические названия; любое другое имя карты остаётся как введено."""
    text = re.sub(r"\s+", " ", (raw or "").strip())
    if not text:
        raise ManualTxError("Укажите источник: наличные или название карты/банка.")
    if len(text) > MAX_SOURCE_LEN:
        raise ManualTxError(f"Слишком длинное название (максимум {MAX_SOURCE_LEN} символов).")
    return _ALIASES.get(text.lower(), text)


def parse_amount(raw: str) -> Decimal:
    """«1 500», «350,50», «200 ₽» -> Decimal больше нуля."""
    text = re.sub(r"(?i)(руб(л\w*)?\.?|р\.?|₽)\s*$", "", (raw or "").strip().lower()).strip()
    text = re.sub(r"[\s ]", "", text).replace(",", ".")
    if not re.fullmatch(r"\d+(\.\d+)?", text):
        raise ManualTxError("Не получилось разобрать сумму. Пример: 350 или 1250,50")
    try:
        value = Decimal(text).quantize(Decimal("0.01"))
    except InvalidOperation:
        raise ManualTxError("Не получилось разобрать сумму. Пример: 350 или 1250,50")
    if value <= 0:
        raise ManualTxError("Сумма должна быть больше нуля.")
    if value > MAX_AMOUNT:
        raise ManualTxError("Слишком большая сумма.")
    return value


def parse_tx_date(raw: str, today: Optional[date] = None) -> date:
    """«сегодня», «вчера», «05.11.2026», «05.11», «2026-11-05». Будущие даты не принимаются."""
    today = today or local_today()
    text = (raw or "").strip().lower()
    if text in ("сегодня", "сег", "today"):
        return today
    if text in ("вчера", "yesterday"):
        return date.fromordinal(today.toordinal() - 1)

    parsed: Optional[date] = None
    for fmt in ("%d.%m.%Y", "%d.%m.%y", "%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            parsed = datetime.strptime(text, fmt).date()
            break
        except ValueError:
            continue
    if parsed is None:
        # «05.11» — текущий год
        m = re.fullmatch(r"(\d{1,2})\.(\d{1,2})", text)
        if m:
            try:
                parsed = date(today.year, int(m.group(2)), int(m.group(1)))
            except ValueError:
                parsed = None
    if parsed is None or parsed.year < 2000:
        raise ManualTxError("Не получилось разобрать дату. Пример: 05.11.2026, «сегодня» или «вчера»")
    if parsed > today:
        raise ManualTxError("Дата в будущем. Введите сегодняшнюю или прошедшую дату.")
    return parsed


def clean_description(raw: Optional[str], default: Optional[str] = None) -> str:
    text = re.sub(r"\s+", " ", (raw or "").strip())
    if text in ("", "-", "—"):
        text = default or ""
    if not text:
        raise ManualTxError("Добавьте описание операции, например «Продукты на рынке».")
    if len(text) > MAX_DESCRIPTION_LEN:
        raise ManualTxError(f"Слишком длинное описание (максимум {MAX_DESCRIPTION_LEN} символов).")
    return text


def tx_datetime(day: date) -> datetime:
    """Полдень по Москве: так дата одинаково читается и в UTC, и по местному времени."""
    return datetime.combine(day, time(12, 0), tzinfo=LOCAL_TZ)


def signed_amount(kind: str, amount: Decimal) -> Decimal:
    return amount if kind in (KIND_INCOME, KIND_WITHDRAW) else -amount


async def add_manual_transaction(
    session: AsyncSession,
    user_id: int,
    kind: str,
    amount: Decimal,
    source: str,
    description: Optional[str],
    tx_date: Optional[date] = None,
    category_id: Optional[int] = None,
) -> Tuple[Transaction, Optional[Category]]:
    """Валидирует и сохраняет ручную операцию. ``category_id=None`` — определить по описанию.

    Возвращает (операция, категория или None, если определить не удалось).
    """
    if kind not in VALID_KINDS:
        raise ManualTxError("Неизвестный тип операции.")
    if amount <= 0:
        raise ManualTxError("Сумма должна быть больше нуля.")

    await seed_default_categories(session)
    categories = list((await session.execute(select(Category))).scalars().all())
    by_name = {c.name: c for c in categories}

    if kind in TRANSFER_KINDS:
        source = CASH_BANK_NAME
        default_text = "Снятие наличных" if kind == KIND_WITHDRAW else "Внесение наличных на карту"
        text = clean_description(description, default=default_text)
        category = by_name.get(TRANSFER_IN_CATEGORY if kind == KIND_WITHDRAW else TRANSFER_OUT_CATEGORY)
    else:
        source = normalize_source(source)
        text = clean_description(description)
        if category_id is not None:
            category = next((c for c in categories if c.id == category_id), None)
            if category is None:
                raise ManualTxError("Категория не найдена.")
            if category.type != kind:
                raise ManualTxError("Тип категории не совпадает с типом операции.")
        else:
            category = match_category(text, kind == KIND_INCOME, categories)
            # match_category при отсутствии совпадения по типу берёт любую категорию — для ручного
            # ввода это путаница (расход в «Зарплате»), поэтому такие совпадения отбрасываем.
            if category is not None and category.type != kind:
                category = None

    tx = Transaction(
        user_id=user_id,
        category_id=category.id if category else None,
        amount=signed_amount(kind, amount),
        description=text,
        date=tx_datetime(tx_date or local_today()),
        bank_name=source,
        is_parsed_automatically=False,
    )
    session.add(tx)
    await session.commit()
    await session.refresh(tx)
    return tx, category


async def guess_category(
    session: AsyncSession, kind: str, description: str
) -> Tuple[Optional[Category], List[Category]]:
    """(подходящая категория или None, все категории нужного типа) — для диалога в боте."""
    await seed_default_categories(session)
    categories = list((await session.execute(select(Category))).scalars().all())
    matched = match_category(description, kind == KIND_INCOME, categories)
    if matched is not None and matched.type != kind:
        matched = None
    same_type = sorted((c for c in categories if c.type == kind), key=lambda c: c.name)
    return matched, same_type


async def get_cash_balance(session: AsyncSession, user_id: Optional[int] = None) -> Decimal:
    """Текущий остаток наличных = сумма всех операций с источником «Наличные»."""
    stmt = select(func.coalesce(func.sum(Transaction.amount), 0)).where(
        Transaction.bank_name == CASH_BANK_NAME
    )
    if user_id is not None:
        stmt = stmt.where(Transaction.user_id == user_id)
    return Decimal(str((await session.execute(stmt)).scalar() or 0))


async def get_recent_cash_transactions(
    session: AsyncSession, user_id: int, limit: int = 5
) -> List[Transaction]:
    stmt = (
        select(Transaction)
        .where(Transaction.user_id == user_id, Transaction.bank_name == CASH_BANK_NAME)
        .order_by(Transaction.date.desc(), Transaction.id.desc())
        .limit(limit)
    )
    return list((await session.execute(stmt)).scalars().all())
