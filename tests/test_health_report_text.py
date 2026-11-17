from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from app.models import ActivityLog, DailyNutrition, User, WeightLog
from app.services.health_report_text import aggregate_health, format_health_lines, previous_period
from app.services.reporting import build_report_data, format_period_summary
from tests import test_health_analytics as fixtures

api=fixtures.api
store=fixtures.store
base_store=fixtures.base_store


def day(value='2026-11-16',**changes):
    result={'date':value,'weight_kg':80,'steps':8000,'tdee':2500,'active_calories':500,
            'calories':2000,'protein':100,'fat':60,'carbs':250,'is_partial':False,
            'activity_calories_uncertain':False,'weight_at':value+'T12:00:00+03:00'}
    return {**result,**changes}


def summary(*days):return {'days':list(days),'timezone':'Europe/Moscow'}


def context(current,previous=None,period='day'):
    return {'period':period,'current':aggregate_health(current,period),
            'previous':aggregate_health(previous,period) if previous else None,
            'comparison_label':'пред. день' if period=='day' else 'пред. месяц',
            'timezone':'Europe/Moscow'}


def test_daily_values_previous_day_deltas_and_kbju():
    current=summary(day(weight_kg=79.5,steps=9000,tdee=2600,calories=2150,protein=120,fat=65,carbs=260,is_partial=True))
    previous=summary(day('2026-11-15'))
    text='\n'.join(format_health_lines(context(current,previous)))
    assert '79,50 кг' in text and '80,00 кг' in text and 'Δ −0,50 кг' in text
    assert '9 000 шагов' in text and 'Δ +1 000 шагов' in text
    assert 'Расход калорий за день' in text and 'Δ +100 ккал' in text
    assert 'Потреблено калорий за день' in text and 'Δ +150 ккал' in text
    assert 'Белки' in text and 'Δ +20,0 г' in text and 'Углеводы' in text


def test_device_total_in_report_without_active_calories():
    values = aggregate_health(summary(day(tdee=1850.72, total_calories=1850.72, active_calories=0,
                                         activity_calories_uncertain=True, expenditure_uncertain=False)), 'day')
    assert values['tdee'] == 1850.72 and values['active_calories'] is None


def test_missing_values_and_real_zero_not_invented_delta():
    current=summary(day(calories=0,steps=None,protein=0))
    text='\n'.join(format_health_lines(context(current)))
    assert 'Потреблено калорий за день: <b>0 ккал</b>' in text
    assert 'Шаги за день: <b>нет данных</b>' in text
    assert 'Δ нет данных' in text and 'Δ +0' not in text


def test_month_sums_and_end_weight_not_weight_sum():
    current=aggregate_health(summary(day('2026-11-15',weight_kg=80),day(weight_kg=79)), 'month')
    assert current['weight_kg']==79
    assert current['steps']==16000 and current['calories']==4000 and current['protein']==200
    assert current['sample_days']['steps']==2


def test_year_averages_not_carried_weights_and_no_comparison():
    days=summary(day('2026-11-13',weight_at='2026-11-12T12:00:00+03:00'),
                 day('2026-11-14',weight_kg=79,steps=1000),
                 day('2026-11-15',weight_kg=78,steps=3000),
                 day('2026-11-16',weight_kg=78,steps=100,is_partial=True,weight_at='2026-11-15T12:00:00+03:00'))
    data=context(days,period='year')
    assert data['current']['weight_kg']==78.5
    assert data['current']['steps']==4000  # (8000+1000+3000)/3, not total or today's partial count
    text='\n'.join(format_health_lines(data))
    assert 'Вес в среднем за год' in text and 'Белки' in text
    assert 'пред. день' not in text and 'Δ' not in text


def test_year_weight_timezone_and_incomplete_tdee():
    data=aggregate_health(summary(day('2026-11-16',weight_at='2026-11-15T21:30:00',activity_calories_uncertain=True)), 'year')
    assert data['weight_kg']==80 and data['tdee'] is None


def test_previous_month_year_boundary_and_short_february():
    assert previous_period(date(2026,1,1),date(2026,1,17),'month')==(date(2025,12,1),date(2025,12,17))
    assert previous_period(date(2024,3,1),date(2024,3,31),'month')==(date(2024,2,1),date(2024,2,29))
    assert previous_period(date(2026,11,17),date(2026,11,17),'day')==(date(2026,11,16),date(2026,11,16))
    assert previous_period(date(2026,1,1),date(2026,11,17),'year') is None


@pytest.mark.asyncio
async def test_month_previous_data_and_profile_isolation(api,store):
    user=await store.get(User,1)
    user.gender,user.height_cm,user.birth_date='male',184,date(1996,1,1)
    store.session.add(WeightLog(user_id=1,weight_kg=80,timestamp=datetime(2026,10,1,12,tzinfo=timezone.utc)))
    for month,calories,steps in [(10,1500,4000),(11,2000,5000)]:
        store.session.add(DailyNutrition(user_id=1,date=date(2026,month,16),calories=calories,
                                         protein=Decimal(100),fat=Decimal(60),carbs=Decimal(250)))
        store.session.add(ActivityLog(user_id=1,date=date(2026,month,16),steps=steps,active_calories=500))
    store.session.commit()
    data=await build_report_data(store,user,'health',date(2026,11,1),date(2026,11,16),period='month')
    assert data['health_text']['previous_start']=='2026-10-01'
    assert data['health_text']['previous_end']=='2026-10-16'
    text=format_period_summary(data)
    assert 'Δ +1 000 шагов' in text and 'Δ +500 ккал' in text
    partner=await store.get(User,2)
    other=await build_report_data(store,partner,'health',date(2026,11,1),date(2026,11,16),period='month')
    assert other['health_text']['current']['steps'] is None and other['health_text']['previous']['steps'] is None


@pytest.mark.asyncio
async def test_custom_single_day_has_daily_comparison(api,store):
    user=await store.get(User,2)
    data=await build_report_data(store,user,'health',date(2026,11,16),date(2026,11,16),period='custom')
    assert data['health_text']['period']=='day' and data['health_text']['previous_start']=='2026-11-15'
