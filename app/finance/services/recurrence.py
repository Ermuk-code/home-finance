"""Повторяющиеся напоминания: «каждый день / неделю / месяц / год».

Чистые функции без БД и сети. Правила считаются в местном времени (МСК, UTC+3, как в services/budget.py),
чтобы «каждое 1-е число в 09:00» не уезжало на другой день из-за UTC.

Если бот был выключен и несколько повторов пропущено, следующим берётся ближайший будущий повтор:
напоминание придёт один раз, а не пачкой.
"""
from __future__ import annotations

import calendar
from datetime import datetime, timedelta, timezone
from typing import Optional

LOCAL_TZ = timezone(timedelta(hours=3))

DAILY, WEEKLY, MONTHLY, YEARLY = "daily", "weekly", "monthly", "yearly"
RECURRENCES = (DAILY, WEEKLY, MONTHLY, YEARLY)
MAX_EVERY = 365   # «каждые N …»: разумный предел


def validate_every(value) -> int:
    """Интервал повтора: целое от 1 до 365 (пусто = 1)."""
    if value is None or value == "":
        return 1
    try:
        n = int(value)
    except (TypeError, ValueError) as e:
        raise RecurrenceError(f"Интервал должен быть целым числом, получено: {value!r}.") from e
    if not 1 <= n <= MAX_EVERY:
        raise RecurrenceError(f"Интервал должен быть от 1 до {MAX_EVERY}.")
    return n


def _plural(n: int, one: str, few: str, many: str) -> str:
    if 11 <= n % 100 <= 14:
        return many
    return one if n % 10 == 1 else few if 2 <= n % 10 <= 4 else many

_EVERY_WEEKDAY = [
    "каждый понедельник", "каждый вторник", "каждую среду", "каждый четверг",
    "каждую пятницу", "каждую субботу", "каждое воскресенье",
]


class RecurrenceError(ValueError):
    pass


def validate_recurrence(value: Optional[str]) -> Optional[str]:
    """Пустое значение = без повтора. Неизвестное правило — ошибка."""
    if value is None or str(value).strip() == "":
        return None
    v = str(value).strip().lower()
    if v not in RECURRENCES:
        raise RecurrenceError(f"Неизвестный тип повторения: {value!r}. Допустимо: {', '.join(RECURRENCES)}.")
    return v


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _add_months(local: datetime, months: int, anchor_day: int) -> datetime:
    """Сдвиг на N месяцев с сохранением «якорного» числа (31-е -> последний день короткого месяца)."""
    y, m = divmod(local.year * 12 + (local.month - 1) + months, 12)
    m += 1
    day = min(anchor_day, calendar.monthrange(y, m)[1])
    return local.replace(year=y, month=m, day=day)


def anchor_day_of(trigger: datetime) -> int:
    """Число месяца (по МСК), которое надо сохранять при ежемесячном/ежегодном повторе."""
    return _aware(trigger).astimezone(LOCAL_TZ).day


def next_occurrence(
    trigger: datetime, rule: str, after: datetime, anchor_day: Optional[int] = None, every: int = 1
) -> datetime:
    """Первый повтор строго позже `after`. `trigger` — последнее (или первое) срабатывание."""
    rule = validate_recurrence(rule) or ""
    if not rule:
        raise RecurrenceError("У напоминания нет правила повторения.")
    trigger, after = _aware(trigger), _aware(after)
    every = validate_every(every)

    if rule in (DAILY, WEEKLY):
        step = timedelta(days=(1 if rule == DAILY else 7) * every)
        if trigger > after:
            return trigger
        n = (after - trigger) // step + 1
        return trigger + step * n

    local = trigger.astimezone(LOCAL_TZ)
    anchor = anchor_day or local.day
    step_months = (1 if rule == MONTHLY else 12) * every
    cur, k = local, 0
    while cur.astimezone(timezone.utc) <= after:
        k += 1
        if k > 20000:  # защита от бесконечного цикла при кривых данных
            raise RecurrenceError("Не удалось вычислить следующее повторение.")
        cur = _add_months(local, k * step_months, anchor)
    return cur.astimezone(trigger.tzinfo)


_ON_WEEKDAY = [
    "в понедельник", "во вторник", "в среду", "в четверг", "в пятницу", "в субботу", "в воскресенье",
]


def describe(rule: Optional[str], trigger: datetime, every: int = 1) -> str:
    """Человекочитаемо: «каждый вторник», «каждые 2 недели, во вторник», «каждый месяц, 15-го числа»."""
    rule = validate_recurrence(rule)
    if rule is None:
        return ""
    every = validate_every(every)
    local = _aware(trigger).astimezone(LOCAL_TZ)

    if every == 1:
        if rule == DAILY:
            return "каждый день"
        if rule == WEEKLY:
            return _EVERY_WEEKDAY[local.weekday()]
        if rule == MONTHLY:
            return f"каждый месяц, {local.day}-го числа"
        return f"каждый год, {local.strftime('%d.%m')}"

    if rule == DAILY:
        return f"каждые {every} {_plural(every, 'день', 'дня', 'дней')}"
    if rule == WEEKLY:
        return f"каждые {every} {_plural(every, 'неделю', 'недели', 'недель')}, {_ON_WEEKDAY[local.weekday()]}"
    if rule == MONTHLY:
        return f"каждые {every} {_plural(every, 'месяц', 'месяца', 'месяцев')}, {local.day}-го числа"
    return f"каждые {every} {_plural(every, 'год', 'года', 'лет')}, {local.strftime('%d.%m')}"
