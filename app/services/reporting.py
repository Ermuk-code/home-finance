"""Shared period boundaries, report data and Telegram summaries."""
import re
from datetime import date, datetime, time, timedelta
from html import escape
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.finance.services.reports import fmt_money, summarize_transactions
from app.health.activity import local_today
from app.health.analytics import HealthAnalytics
from app.models import Category, Transaction, User


def validate_period(start: date, end: date) -> None:
    if end < start:
        raise ValueError('Дата начала должна быть не позже даты окончания.')
    if end > local_today():
        raise ValueError('Дата окончания не может быть в будущем.')
    if (end-start).days > 365:
        raise ValueError('Выберите период не длиннее 366 дней.')


def report_period(period: str, today: date | None = None) -> tuple[date, date]:
    today = today or local_today()
    if period == 'day':
        return today, today
    if period == 'month':
        return today.replace(day=1), today
    if period == 'year':
        return today.replace(month=1, day=1), today
    raise ValueError('Неизвестный период отчета.')


def parse_custom_period(text: str) -> tuple[date, date]:
    match = re.fullmatch(r'\s*(\d{2}\.\d{2}\.\d{4})\s*[-–—]\s*(\d{2}\.\d{2}\.\d{4})\s*', text)
    if not match:
        raise ValueError('Введите даты в формате ДД.ММ.ГГГГ-ДД.ММ.ГГГГ, например 01.11.2026-16.11.2026.')
    try:
        start, end = (datetime.strptime(part, '%d.%m.%Y').date() for part in match.groups())
    except ValueError as exc:
        raise ValueError('Проверьте даты: такого дня в календаре нет.') from exc
    validate_period(start, end)
    return start, end


async def build_report_data(session: AsyncSession, user: User, kind: str, start: date, end: date) -> dict:
    validate_period(start, end)
    result = {'kind': kind, 'name': user.name, 'start_date': start.isoformat(), 'end_date': end.isoformat(),
              'timezone': settings.ACTIVITY_TZ}
    if kind == 'health':
        result['health'] = await HealthAnalytics(session).get_summary(user.id, start, end)
        return result
    if kind != 'finance':
        raise ValueError('Неизвестный тип отчета.')
    tz = ZoneInfo(settings.ACTIVITY_TZ)
    lower, upper = datetime.combine(start, time.min, tzinfo=tz), datetime.combine(end+timedelta(days=1), time.min, tzinfo=tz)
    rows = (await session.execute(select(Category.name, Transaction.amount, Category.type, Transaction.date)
        .outerjoin(Category, Transaction.category_id == Category.id)
        .where(Transaction.user_id == user.id, Transaction.date >= lower, Transaction.date < upper)
        .order_by(Transaction.date, Transaction.id))).all()
    summary = summarize_transactions([(name, amount, cat_type) for name, amount, cat_type, _ in rows])
    categories = summary['expenses_by_category']
    top = [(label, float(amount)) for label, amount in categories[:8]]
    if len(categories) > 8:
        top.append(('Остальные категории', float(sum(amount for _, amount in categories[8:]))))
    daily_rows = {}
    from datetime import timezone
    for name, amount, cat_type, ts in rows:
        ts = ts.replace(tzinfo=timezone.utc) if ts.tzinfo is None else ts
        daily_rows.setdefault(ts.astimezone(tz).date(), []).append((name, amount, cat_type))
    daily = []
    for offset in range((end-start).days+1):
        day = start+timedelta(days=offset)
        item = summarize_transactions(daily_rows.get(day, []))
        daily.append({'date': day.isoformat(), 'income': float(item['clean_income']), 'expense': float(item['clean_expense'])})
    result['finance'] = {key: float(summary[key]) for key in ('income_all','expense_all','clean_income','clean_expense','net')}
    result['finance'].update(transaction_count=len(rows), categories=top, days=daily)
    return result


def format_period_summary(data: dict) -> str:
    heading = '📊 Финансы' if data['kind'] == 'finance' else '⚖️ Здоровье'
    lines = [f"{heading} — <b>{escape(data['name'])}</b>", f"{data['start_date']} — {data['end_date']}"]
    if data['kind'] == 'finance':
        s = data['finance']
        lines += [f"Доходы: <b>{fmt_money(s['clean_income'])} ₽</b>",
                  f"Расходы: <b>{fmt_money(s['clean_expense'])} ₽</b>",
                  f"Итог: <b>{fmt_money(s['net'])} ₽</b>", f"Операций: {s['transaction_count']}.",
                  'Переводы между своими счетами исключены из итогов.']
    else:
        s = data['health']
        avg, w = s['averages'], s['weight_change']
        def fmt(value):
            return f'{value:,.0f}'.replace(',', ' ') if value is not None else 'нет данных'
        if w['start_kg'] is not None:
            lines.append(f"Вес: {w['start_kg']:.1f} → {w['end_kg']:.1f} кг ({w['delta_kg']:+.1f} кг).")
        else:
            lines.append('Вес: нет данных.')
        lines += [f"Средние шаги: {fmt(avg['steps'])}/день.", f"Среднее питание: {fmt(avg['calories'])} ккал/день.",
                  f"Расчетный расход: {fmt(avg['tdee'])} ккал/день.",
                  f"Средний баланс: {fmt(avg['balance'])} ккал/день.",
                  f"Полных дней для баланса: {s['complete_days']}. Сегодня и пропуски исключены из средних."]
    return '\n'.join(lines)
