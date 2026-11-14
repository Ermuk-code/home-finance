from datetime import datetime, timedelta, timezone

import pytest

from app.finance.services.recurrence import (
    RecurrenceError, anchor_day_of, describe, next_occurrence, validate_recurrence,
)

MSK = timezone(timedelta(hours=3))


def dt(y, m, d, h=9, mi=0):
    return datetime(y, m, d, h, mi, tzinfo=MSK)


def test_validate():
    assert validate_recurrence(None) is None and validate_recurrence("") is None
    assert validate_recurrence(" Weekly ") == "weekly"
    with pytest.raises(RecurrenceError):
        validate_recurrence("hourly")


def test_weekly_next_is_same_weekday():
    t = dt(2026, 11, 10)                       # вторник
    nxt = next_occurrence(t, "weekly", dt(2026, 11, 10, 9, 1))
    assert nxt == dt(2026, 11, 17) and nxt.weekday() == 1


def test_daily():
    assert next_occurrence(dt(2026, 11, 10), "daily", dt(2026, 11, 10, 9, 1)) == dt(2026, 11, 11)


def test_missed_periods_are_skipped_not_replayed():
    # бот был выключен 3 недели: приходит одно ближайшее будущее напоминание
    nxt = next_occurrence(dt(2026, 11, 10), "weekly", dt(2026, 12, 2, 12))
    assert nxt == dt(2026, 12, 8)


def test_monthly_keeps_day_and_clamps_short_months():
    jan31 = dt(2026, 1, 31)
    feb = next_occurrence(jan31, "monthly", dt(2026, 1, 31, 10), anchor_day=31)
    assert feb == dt(2026, 2, 28)
    # якорь 31 не «дрейфует»: после февраля снова 31 марта
    assert next_occurrence(feb, "monthly", dt(2026, 2, 28, 10), anchor_day=31) == dt(2026, 3, 31)


def test_monthly_crosses_year():
    assert next_occurrence(dt(2026, 12, 15), "monthly", dt(2026, 12, 15, 10)) == dt(2027, 1, 15)


def test_yearly_leap_day():
    t = dt(2028, 2, 29)
    assert next_occurrence(t, "yearly", dt(2028, 3, 1), anchor_day=29) == dt(2029, 2, 28)


def test_future_trigger_is_returned_for_daily_weekly():
    t = dt(2026, 12, 1)
    assert next_occurrence(t, "weekly", dt(2026, 11, 1)) == t


def test_naive_datetime_treated_as_utc():
    naive = datetime(2026, 11, 10, 6, 0)       # 09:00 МСК
    nxt = next_occurrence(naive, "daily", datetime(2026, 11, 10, 6, 1, tzinfo=timezone.utc))
    assert nxt == datetime(2026, 11, 11, 6, 0, tzinfo=timezone.utc)


def test_anchor_day_uses_moscow_time():
    # 22:00 UTC 30-го = 01:00 МСК 31-го
    assert anchor_day_of(datetime(2026, 1, 30, 22, 0, tzinfo=timezone.utc)) == 31


def test_describe():
    t = dt(2026, 11, 10)
    assert describe("weekly", t) == "каждый вторник"
    assert describe("weekly", dt(2026, 11, 11)) == "каждую среду"
    assert describe("monthly", t) == "каждый месяц, 10-го числа"
    assert describe("yearly", t) == "каждый год, 10.11"
    assert describe("daily", t) == "каждый день"
    assert describe(None, t) == ""


def test_every_n_days_and_weeks():
    assert next_occurrence(dt(2026, 11, 10), "daily", dt(2026, 11, 10, 9, 1), every=3) == dt(2026, 11, 13)
    nxt = next_occurrence(dt(2026, 11, 10), "weekly", dt(2026, 11, 10, 9, 1), every=2)
    assert nxt == dt(2026, 11, 24) and nxt.weekday() == 1


def test_every_n_missed_periods_stay_on_the_grid():
    # раз в 2 недели от 10.11: узлы сетки 24.11, 08.12, 22.12 — после 10.12 ближайший 22.12, а не «через 2 недели от сегодня»
    assert next_occurrence(dt(2026, 11, 10), "weekly", dt(2026, 12, 10), every=2) == dt(2026, 12, 22)


def test_every_n_months_and_years():
    assert next_occurrence(dt(2026, 11, 15), "monthly", dt(2026, 11, 15, 10), every=3) == dt(2027, 2, 15)
    assert next_occurrence(dt(2026, 1, 31), "monthly", dt(2026, 1, 31, 10), anchor_day=31, every=2) == dt(2026, 3, 31)
    assert next_occurrence(dt(2026, 11, 10), "yearly", dt(2026, 11, 10, 10), every=2) == dt(2028, 11, 10)


def test_validate_every():
    from app.finance.services.recurrence import validate_every
    assert validate_every(None) == 1 and validate_every("") == 1 and validate_every("7") == 7
    for bad in (0, -1, 366, "abc", 2.5 if False else "x"):
        with pytest.raises(RecurrenceError):
            validate_every(bad)


def test_describe_every_n_with_russian_plurals():
    t = dt(2026, 11, 10)
    assert describe("daily", t, 2) == "каждые 2 дня"
    assert describe("daily", t, 5) == "каждые 5 дней"
    assert describe("weekly", t, 2) == "каждые 2 недели, во вторник"
    assert describe("monthly", t, 3) == "каждые 3 месяца, 10-го числа"
    assert describe("yearly", t, 5) == "каждые 5 лет, 10.11"
