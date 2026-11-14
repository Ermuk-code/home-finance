"""Автоматические финансовые советы. Только арифметика, без ИИ/LLM.

Алгоритм:
1. По каждой необязательной категории считаем среднемесячные траты за 3 последних полных месяца.
2. Расходы текущего месяца экстраполируем до конца месяца:
   ``расход_на_сегодня / текущий_день * дней_в_месяце``.
3. Если прогноз заметно выше среднего — советуем сократить категорию; если заметно ниже — отмечаем это.

Не учитываются: обязательные категории (ЖКХ, связь и т.п.), переводы между своими счетами
и операции без категории.
"""
import calendar
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from html import escape
from typing import Dict, List, Optional, Tuple

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Category, Transaction
from app.finance.services.budget import LOCAL_TZ, local_today, month_start, next_month
from app.finance.services.categorizer import TRANSFER_CATEGORY_NAMES
from app.finance.services.reports import fmt_money

HISTORY_MONTHS = 3
# Изменение прогноза относительно среднего, начиная с которого даём совет.
GROWTH_THRESHOLD_PCT = Decimal("15")
DROP_THRESHOLD_PCT = Decimal("20")
# Минимальная разница в рублях: чтобы не советовать из-за копеечных колебаний.
MIN_DIFF_RUB = Decimal("500")
# В первые дни месяца экстраполяция слишком шумная (1 покупка × 30), поэтому раньше этого дня советов нет.
MIN_DAY_FOR_FORECAST = 5

_ZERO = Decimal("0")
_HUNDRED = Decimal("100")


@dataclass(frozen=True)
class CategoryTrend:
    name: str
    average: Decimal  # среднее за месяц по истории
    spent: Decimal  # потрачено в текущем месяце на сегодня
    forecast: Decimal  # прогноз на конец месяца
    change_pct: Decimal  # (прогноз − среднее) / среднее × 100


def shift_month(year: int, month: int, delta: int) -> Tuple[int, int]:
    idx = year * 12 + (month - 1) + delta
    y, m = divmod(idx, 12)
    return y, m + 1


def extrapolate(spent: Decimal, today: date) -> Decimal:
    """текущий расход / текущий день * количество дней в месяце."""
    days_in_month = calendar.monthrange(today.year, today.month)[1]
    return spent / Decimal(today.day) * Decimal(days_in_month)


def calc_trends(
    history_totals: Dict[str, Decimal],
    months_with_data: int,
    current_spent: Dict[str, Decimal],
    today: date,
) -> List[CategoryTrend]:
    """Чистая арифметика: среднее по истории против прогноза текущего месяца.

    ``history_totals`` — суммы по категориям за все месяцы истории, ``months_with_data`` — сколько из них
    содержат хоть какие-то операции пользователя (делим на него, а не на 3: иначе при короткой истории
    среднее занижено и «рост» ложный). Категории без истории пропускаем — сравнивать не с чем.
    """
    if months_with_data <= 0:
        return []

    trends: List[CategoryTrend] = []
    for name, spent in current_spent.items():
        total = history_totals.get(name, _ZERO)
        if total <= 0:
            continue
        average = total / Decimal(months_with_data)
        forecast = extrapolate(spent, today)
        change = (forecast - average) / average * _HUNDRED
        trends.append(CategoryTrend(name, average, spent, forecast, change))
    trends.sort(key=lambda t: t.change_pct, reverse=True)
    return trends


def format_advice(trend: CategoryTrend, months: int = HISTORY_MONTHS) -> Optional[str]:
    """Текст совета (HTML) по одной категории или None, если отклонение несущественное."""
    diff = trend.forecast - trend.average
    pct = int(abs(trend.change_pct).quantize(Decimal("1")))
    name = escape(trend.name.lower())
    numbers = f"(прогноз до конца месяца: {fmt_money(trend.forecast)} ₽ против {fmt_money(trend.average)} ₽ в среднем)"

    if trend.change_pct >= GROWTH_THRESHOLD_PCT and diff >= MIN_DIFF_RUB:
        return (
            f"📈 В этом месяце ваши расходы на «{name}» выросли на {pct}% по сравнению со средним "
            f"за {months} мес. {numbers}. Рекомендуем сократить эту категорию."
        )
    if trend.change_pct <= -DROP_THRESHOLD_PCT and -diff >= MIN_DIFF_RUB:
        return (
            f"📉 Расходы на «{name}» в этом месяце ниже среднего за {months} мес. на {pct}% {numbers}. "
            f"Так держать!"
        )
    return None


async def _history_months(session: AsyncSession, user_id: int, today: date) -> List[Tuple[int, int]]:
    """Последние HISTORY_MONTHS полных месяцев перед текущим, в которых у пользователя есть операции."""
    first = shift_month(today.year, today.month, -HISTORY_MONTHS)
    start = month_start(*first)
    end = month_start(today.year, today.month)

    stmt = select(Transaction.date).where(
        Transaction.user_id == user_id,
        Transaction.date >= start,
        Transaction.date < end,
    )
    dates = (await session.execute(stmt)).scalars().all()
    local = [d.astimezone(LOCAL_TZ) for d in dates]
    return sorted({(d.year, d.month) for d in local})


async def _expenses_by_category(
    session: AsyncSession, user_id: int, start, end
) -> Dict[str, Decimal]:
    """Суммы расходов по необязательным категориям (без переводов и неразобранного) за [start, end)."""
    stmt = (
        select(Category.name, func.sum(-Transaction.amount))
        .join(Transaction, Transaction.category_id == Category.id)
        .where(
            Transaction.user_id == user_id,
            Transaction.amount < 0,
            Transaction.date >= start,
            Transaction.date < end,
            Category.is_mandatory.is_(False),
            Category.name.not_in(list(TRANSFER_CATEGORY_NAMES)),
        )
        .group_by(Category.name)
    )
    return {name: Decimal(str(total)) for name, total in (await session.execute(stmt)).all()}


async def get_spending_trends(
    session: AsyncSession, user_id: int, today: Optional[date] = None
) -> List[CategoryTrend]:
    today = today or local_today()
    if today.day < MIN_DAY_FOR_FORECAST:
        return []

    months = await _history_months(session, user_id, today)
    if not months:
        return []

    hist_start = month_start(*shift_month(today.year, today.month, -HISTORY_MONTHS))
    cur_start = month_start(today.year, today.month)
    cur_end = month_start(*next_month(today.year, today.month))

    history = await _expenses_by_category(session, user_id, hist_start, cur_start)
    current = await _expenses_by_category(session, user_id, cur_start, cur_end)
    return calc_trends(history, len(months), current, today)


async def generate_smart_advice(
    session: AsyncSession, user_id: int, today: Optional[date] = None
) -> List[str]:
    """Список текстовых советов (HTML для Telegram). Пустой список — отклонений нет или мало данных."""
    trends = await get_spending_trends(session, user_id, today)
    advice = (format_advice(t) for t in trends)
    return [text for text in advice if text]
