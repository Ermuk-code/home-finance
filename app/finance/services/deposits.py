"""Расчёты по вкладам и накопительным счетам.

Два типа продуктов:

* ``deposit`` — срочный вклад: сумма фиксирована (без пополнения и снятия), задаются дата
  открытия и срок в месяцах. Проценты либо выплачиваются в конце срока (простые проценты),
  либо ежемесячно капитализируются (``capitalization=True``).
* ``savings`` — накопительный счёт: сумму можно менять, проценты на остаток считаются
  каждый день, а зачисляются на счёт 12-го числа каждого месяца (и дальше сами приносят доход).

Проценты считаются по фактическому числу дней: ставка / число дней в году (365 или 366).
"""
import calendar
import re
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Dict, Optional, Tuple

KIND_DEPOSIT = "deposit"
KIND_SAVINGS = "savings"
VALID_KINDS = (KIND_DEPOSIT, KIND_SAVINGS)

SAVINGS_CREDIT_DAY = 12
MAX_TERM_MONTHS = 120

_KOPECK = Decimal("0.01")
_ZERO = Decimal("0")


def money(value: Decimal) -> Decimal:
    """Округление до копеек (как округляет банк при зачислении процентов)."""
    return value.quantize(_KOPECK, rounding=ROUND_HALF_UP)


def add_months(d: date, months: int) -> date:
    """Прибавляет месяцы; если в целевом месяце нет такого дня (31-е, 29 февраля) — берёт последний."""
    idx = d.year * 12 + (d.month - 1) + months
    year, month = divmod(idx, 12)
    month += 1
    return date(year, month, min(d.day, calendar.monthrange(year, month)[1]))


def _days_in_year(year: int) -> int:
    return 366 if calendar.isleap(year) else 365


def accrue(principal: Decimal, rate_pct: Decimal, start: date, end: date) -> Decimal:
    """Проценты на ``principal`` за период [start, end) без округления.

    Период режется по календарным годам, чтобы для високосного года делить на 366.
    """
    if principal <= 0 or end <= start:
        return _ZERO
    total = _ZERO
    cur = start
    while cur < end:
        seg_end = min(end, date(cur.year + 1, 1, 1))
        days = (seg_end - cur).days
        total += principal * rate_pct / Decimal(100) * days / _days_in_year(cur.year)
        cur = seg_end
    return total


# ---------------------------------------------------------------------------
# Проверка и разбор введённых данных (общие для веб-API и Telegram-бота)
# ---------------------------------------------------------------------------
class DepositInputError(ValueError):
    """Некорректные данные при создании вклада; текст безопасно показывать пользователю."""


def validate_deposit_input(
    kind: str,
    amount: Decimal,
    interest_rate: Decimal,
    opened_at: Optional[date] = None,
    term_months: Optional[int] = None,
    capitalization: bool = False,
) -> Dict[str, Any]:
    """Проверяет данные и возвращает поля для записи в БД (включая вычисленную дату окончания)."""
    kind = (kind or KIND_DEPOSIT).strip().lower()
    if kind not in VALID_KINDS:
        raise DepositInputError("Тип должен быть «вклад» или «накопительный счёт».")
    if amount <= 0:
        raise DepositInputError("Сумма должна быть больше нуля.")
    if interest_rate < 0 or interest_rate > 100:
        raise DepositInputError("Ставка должна быть от 0 до 100% годовых.")

    if kind == KIND_SAVINGS:
        return {
            "kind": kind,
            "opened_at": None,
            "term_months": None,
            "capitalization": False,
            "end_date": None,
        }

    if opened_at is None:
        raise DepositInputError("Для вклада укажите дату открытия.")
    if not term_months or not (1 <= term_months <= MAX_TERM_MONTHS):
        raise DepositInputError(f"Срок вклада — от 1 до {MAX_TERM_MONTHS} месяцев.")
    return {
        "kind": kind,
        "opened_at": opened_at,
        "term_months": term_months,
        "capitalization": bool(capitalization),
        "end_date": deposit_end_date(opened_at, term_months),
    }


def parse_decimal_input(raw: str) -> Decimal:
    """«300 000», «300000,50», «16.5%», «1 000 ₽» -> Decimal. Бросает DepositInputError."""
    cleaned = re.sub(r"[\s\u00a0₽%рР.]*$", "", (raw or "").strip())  # хвост: ₽, %, «руб»
    cleaned = re.sub(r"(?i)руб(л\w*)?\.?$", "", cleaned).strip()
    cleaned = re.sub(r"[\s\u00a0]", "", cleaned).replace(",", ".")
    if not re.fullmatch(r"\d+(\.\d+)?", cleaned):
        raise DepositInputError("Не получилось разобрать число. Пример: 300000 или 16,5")
    try:
        return Decimal(cleaned)
    except InvalidOperation:
        raise DepositInputError("Не получилось разобрать число. Пример: 300000 или 16,5")


def parse_date_input(raw: str, today: Optional[date] = None) -> date:
    """«05.11.2026», «05.11.26», «2026-11-05», «сегодня»."""
    text = (raw or "").strip().lower()
    if text in ("сегодня", "today", "сег"):
        return today or date.today()
    for fmt in ("%d.%m.%Y", "%d.%m.%y", "%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            parsed = datetime.strptime(text, fmt).date()
        except ValueError:
            continue
        if parsed.year < 2000:
            break
        return parsed
    raise DepositInputError("Не получилось разобрать дату. Пример: 05.11.2026")


def parse_term_input(raw: str) -> int:
    """«12», «6 мес.» -> 12 / 6."""
    m = re.fullmatch(r"\s*(\d{1,3})\s*(?:мес\w*\.?|m)?\s*", (raw or "").lower())
    if not m:
        raise DepositInputError("Укажите срок целым числом месяцев. Пример: 12")
    value = int(m.group(1))
    if not (1 <= value <= MAX_TERM_MONTHS):
        raise DepositInputError(f"Срок вклада — от 1 до {MAX_TERM_MONTHS} месяцев.")
    return value


# ---------------------------------------------------------------------------
# Срочный вклад
# ---------------------------------------------------------------------------
def deposit_end_date(opened_at: date, term_months: int) -> date:
    return add_months(opened_at, term_months)


def deposit_interest(
    amount: Decimal,
    rate_pct: Decimal,
    opened_at: date,
    term_months: int,
    capitalization: bool,
    upto: Optional[date] = None,
) -> Decimal:
    """Начисленные проценты по вкладу на дату ``upto`` (по умолчанию — за весь срок)."""
    end = deposit_end_date(opened_at, term_months)
    upto = min(upto, end) if upto else end
    if upto <= opened_at:
        return _ZERO

    if not capitalization:
        return money(accrue(amount, rate_pct, opened_at, upto))

    # Ежемесячная капитализация: каждый месяц проценты прибавляются к телу вклада.
    balance = amount
    for i in range(term_months):
        start = add_months(opened_at, i)
        stop = add_months(opened_at, i + 1)
        if upto >= stop:
            balance += money(accrue(balance, rate_pct, start, stop))
        else:
            # Незавершённый месяц: проценты накоплены, но ещё не прибавлены к телу.
            balance += money(accrue(balance, rate_pct, start, upto))
            break
    return balance - amount


# ---------------------------------------------------------------------------
# Накопительный счёт
# ---------------------------------------------------------------------------
def savings_credit_dates(today: date) -> Tuple[date, date]:
    """(последнее зачисление, ближайшее зачисление) — 12-е число."""
    this_month = date(today.year, today.month, SAVINGS_CREDIT_DAY)
    last = this_month if today >= this_month else add_months(this_month, -1)
    return last, add_months(last, 1)


def savings_projection(balance: Decimal, rate_pct: Decimal, today: date) -> Dict[str, Any]:
    """Оценка по накопительному счёту при условии, что остаток не меняется."""
    last_credit, next_credit = savings_credit_dates(today)

    daily = money(balance * rate_pct / Decimal(100) / _days_in_year(today.year))
    accrued_since_last = money(accrue(balance, rate_pct, last_credit, today))
    next_credit_amount = money(accrue(balance, rate_pct, last_credit, next_credit))

    # 12 месяцев вперёд: на каждое 12-е проценты зачисляются и дальше сами приносят доход.
    running = balance
    start = last_credit
    for _ in range(12):
        stop = add_months(start, 1)
        running += money(accrue(running, rate_pct, start, stop))
        start = stop

    return {
        "daily_income": daily,
        "last_credit_date": last_credit,
        "next_credit_date": next_credit,
        "accrued_since_last_credit": accrued_since_last,
        "next_credit_amount": next_credit_amount,
        "yearly_income": running - balance,
    }


# ---------------------------------------------------------------------------
# Описание записи для дашборда
# ---------------------------------------------------------------------------
def _fmt(d: Optional[date]) -> Optional[str]:
    return d.strftime("%d.%m.%Y") if d else None


def describe_deposit(dep: Any, today: Optional[date] = None) -> Dict[str, Any]:
    """Собирает данные по вкладу/накопительному счёту для JSON-ответа дашборда."""
    today = today or date.today()
    kind = getattr(dep, "kind", None) or KIND_DEPOSIT
    amount: Decimal = dep.amount
    rate: Decimal = dep.interest_rate
    flat_yearly = money(amount * rate / Decimal(100))

    result: Dict[str, Any] = {
        "id": dep.id,
        "bank_name": dep.bank_name,
        "title": dep.title,
        "kind": kind,
        "amount": float(amount),
        "interest_rate": float(rate),
        "capitalization": bool(getattr(dep, "capitalization", False)),
        "opened_at": _fmt(getattr(dep, "opened_at", None)),
        "term_months": getattr(dep, "term_months", None),
        "expected_yearly_income": float(flat_yearly),
    }

    if kind == KIND_SAVINGS:
        proj = savings_projection(amount, rate, today)
        result.update(
            {
                "end_date": None,
                "daily_income": float(proj["daily_income"]),
                "accrued_since_last_credit": float(proj["accrued_since_last_credit"]),
                "last_credit_date": _fmt(proj["last_credit_date"]),
                "next_credit_date": _fmt(proj["next_credit_date"]),
                "next_credit_amount": float(proj["next_credit_amount"]),
                "expected_yearly_income": float(proj["yearly_income"]),
            }
        )
        return result

    opened_at: Optional[date] = getattr(dep, "opened_at", None)
    term: Optional[int] = getattr(dep, "term_months", None)
    if opened_at and term:
        end = deposit_end_date(opened_at, term)
        total_interest = deposit_interest(amount, rate, opened_at, term, result["capitalization"])
        accrued_now = deposit_interest(amount, rate, opened_at, term, result["capitalization"], upto=today)
        total_days = (end - opened_at).days
        passed = max(0, min((today - opened_at).days, total_days))
        result.update(
            {
                "has_schedule": True,
                "end_date": _fmt(end),
                "total_days": total_days,
                "days_left": max(0, (end - today).days),
                "progress_pct": round(passed / total_days * 100, 1) if total_days else 100.0,
                "is_finished": today >= end,
                "not_started": today < opened_at,
                "interest_total": float(total_interest),
                "final_amount": float(amount + total_interest),
                "accrued_to_date": float(accrued_now),
            }
        )
    else:
        # Вклад, заведённый до появления даты открытия и срока: считаем по-старому.
        legacy_end = getattr(dep, "end_date", None)
        result.update(
            {
                "has_schedule": False,
                "end_date": _fmt(legacy_end),
            }
        )
    return result
