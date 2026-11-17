"""Comparable health metrics for Telegram: daily values, monthly sums, yearly means."""
from calendar import monthrange
from datetime import date, datetime, timedelta, timezone
from statistics import mean
from zoneinfo import ZoneInfo


METRICS = ('weight_kg','steps','tdee','active_calories','calories','protein','fat','carbs')


def measurement_day(timestamp, tz):
    value=datetime.fromisoformat(timestamp)
    if value.tzinfo is None:
        value=value.replace(tzinfo=timezone.utc)
    return value.astimezone(tz).date()


def previous_period(start: date, end: date, period: str) -> tuple[date,date] | None:
    if period == 'year':
        return None
    if period == 'month':
        def shift(value):
            year,month = (value.year-1,12) if value.month == 1 else (value.year,value.month-1)
            return date(year,month,min(value.day,monthrange(year,month)[1]))
        return shift(start),shift(end)
    length=(end-start).days+1
    return start-timedelta(days=length),start-timedelta(days=1)


def aggregate_health(summary: dict, period: str) -> dict:
    days=summary['days']
    result={}
    counts={}
    for key in METRICS:
        eligible=days
        if period == 'day':
            eligible=days[-1:]
        elif period in ('year','week','custom') and key!='weight_kg':
            eligible=[d for d in days if not d['is_partial']]
        if key=='tdee':
            eligible=[d for d in eligible if not d.get('expenditure_uncertain', d.get('activity_calories_uncertain'))]
        if key=='active_calories':
            eligible=[d for d in eligible if not (d.get('total_calories') is not None and d.get('activity_calories_uncertain'))]
        if key=='weight_kg' and period=='year':
            # Only actual daily measurements, not a carried weight repeated for many days.
            tz=ZoneInfo(summary['timezone'])
            eligible=[d for d in days if d.get('weight_at') and
                      measurement_day(d['weight_at'],tz).isoformat()==d['date']]
        values=[d[key] for d in eligible if d.get(key) is not None]
        counts[key]=len(values)
        if not values:
            result[key]=None
        elif key=='weight_kg' and period!='year':
            result[key]=values[-1]
        elif period in ('day','month'):
            result[key]=round(sum(values),2)
        else:
            result[key]=round(mean(values),2)
    result['sample_days']=counts
    result['weight_at']=next((d.get('weight_at') for d in reversed(days) if d.get('weight_at')),None)
    return result


def number(value, digits=0):
    if value is None:
        return 'нет данных'
    return f'{value:,.{digits}f}'.replace(',',' ').replace('.',',')


def metric_line(label, key, unit, context, digits=0, prefix=''):
    current=context['current'][key]
    value=number(current,digits)+(f' {unit}' if current is not None and unit else '')
    if context['period']=='year':
        return f'{prefix}{label}: <b>{value}</b>'
    previous=context['previous'][key] if context.get('previous') else None
    prior=number(previous,digits)+(f' {unit}' if previous is not None and unit else '')
    delta='нет данных'
    if current is not None and previous is not None:
        change=round(current-previous,digits)
        sign='+' if change>0 else '−' if change<0 else ''
        delta=sign+number(abs(change),digits)+(f' {unit}' if unit else '')
    return f'{prefix}{label}: <b>{value}</b> ({context["comparison_label"]}: {prior}; Δ {delta})'


def format_health_lines(context: dict) -> list[str]:
    period=context['period']
    suffix={'day':'за день','month':'за месяц','year':'в среднем за год','week':'в среднем за день','custom':'в среднем за день'}[period]
    weight='Вес в среднем за год' if period=='year' else 'Вес'
    lines=[
        metric_line(weight,'weight_kg','кг',context,2,'⚖️ '),
        metric_line('Шаги '+suffix,'steps','шагов',context,prefix='👣 '),
        metric_line('Расход калорий '+suffix,'tdee','ккал',context,prefix='🔥 '),
        metric_line('Активные калории '+suffix,'active_calories','ккал',context),
        metric_line('Потреблено калорий '+suffix,'calories','ккал',context,prefix='🍽 '),
        '🥗 <b>КБЖУ '+suffix+':</b>',
    ]
    lines.extend(metric_line(label,key,unit,context,digits) for label,key,unit,digits in (
        ('Калории','calories','ккал',0),('Белки','protein','г',1),('Жиры','fat','г',1),('Углеводы','carbs','г',1)))
    if period=='year':
        lines.append('Средние по дням с данными; вес — по реальным дневным замерам. Текущие неполные сутки исключены из средних активности и питания.')
    elif context.get('previous_start'):
        lines.append(f'Сравнение: {context["previous_start"]} — {context["previous_end"]}.')
    if period in ('day','month'):
        lines.append('Сегодняшние значения — по последней синхронизации, сутки могут быть неполными.')
    if context['current'].get('weight_at') and period!='year':
        day=measurement_day(context['current']['weight_at'],ZoneInfo(context['timezone'])).strftime('%d.%m.%Y')
        lines.append(f'Последний замер веса: {day}.')
    counts=context['current']['sample_days']
    lines.append(f'Дней с данными: шаги — {counts["steps"]}, расход — {counts["tdee"]}, питание — {counts["calories"]}.')
    lines.append('Расход: общий итог HA, если он передан; иначе BMR за 24 часа + активные ккал + 10% питания. К общему итогу HA базовый обмен повторно не прибавляется. Нет данных — не ноль.')
    return lines
