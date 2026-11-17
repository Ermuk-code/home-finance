from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import func, select

from app.core.database import get_async_session
from app.health import analytics
from app.health.analytics import HealthAnalytics, age_on, calculate_bmr, format_weekly_summary
from app.health.router import router as health_router
from app.models import ActivityLog, BodyMeasurement, DailyNutrition, User, WeightLog
from app.users.router import PhysicalParamsSchema, router as users_router
from tests.test_activity import auth, store as base_store  # noqa: F401


@pytest.fixture
def store(request):
    base = request.getfixturevalue('base_store')
    for model in (DailyNutrition, WeightLog, BodyMeasurement):
        model.__table__.create(base.session.get_bind())
    return base


@pytest_asyncio.fixture
async def api(store, monkeypatch):
    app = FastAPI()
    app.include_router(health_router)
    app.include_router(users_router)
    from app.health.dashboard import router as dashboard_router
    app.include_router(dashboard_router)
    from app.health.abs_forecast import router as abs_router
    app.include_router(abs_router)
    from app.web.report_render import router as render_router
    app.include_router(render_router)
    async def get_session():
        yield store
    app.dependency_overrides[get_async_session] = get_session
    async def refresh(obj):
        store.session.refresh(obj)
    store.refresh = refresh
    store.add = store.session.add
    monkeypatch.setattr(analytics, 'local_today', lambda: date(2026, 11, 16))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        yield client


def populate(store):
    user = store.session.get(User, 1)
    user.gender, user.height_cm, user.birth_date = 'male', 184, date(1996, 1, 1)
    store.session.add(WeightLog(user_id=1, weight_kg=Decimal('80'),
                               timestamp=datetime(2026, 11, 8, 12, tzinfo=timezone.utc)))
    for offset in range(7):
        day = date(2026, 11, 9) + timedelta(days=offset)
        store.session.add(DailyNutrition(user_id=1, date=day, calories=2150))
        store.session.add(ActivityLog(user_id=1, date=day, steps=8400, active_calories=500))
    store.session.commit()


def test_math_both_formulas_and_dynamic_age():
    assert calculate_bmr(80, 184, 30, 'male') == 1805
    assert calculate_bmr(80, 184, 30, 'female') == 1639
    assert age_on(date(1996, 11, 17), date(2026, 11, 16)) == 29
    assert age_on(date(1996, 11, 17), date(2026, 11, 17)) == 30
    assert age_on(date(2000, 2, 29), date(2025, 2, 28)) == 24
    with pytest.raises(ValueError):
        calculate_bmr(80, 184, 17, 'male')


@pytest.mark.asyncio
async def test_complete_week_math_and_no_writes(api, store):
    populate(store)
    before = store.session.scalar(select(func.count(WeightLog.id)))
    response = await api.get('/api/health/summary?start_date=2026-11-09&end_date=2026-11-15', headers=auth(1))
    assert response.status_code == 200
    data = response.json()
    assert data['complete_days'] == 7
    assert data['averages']['bmr'] == 1805
    assert data['averages']['tdee'] == 2520
    assert data['averages']['balance'] == -370
    assert data['forecast_7d_kg'] == -0.34
    assert data['days'][0]['tef'] == 215
    assert store.session.scalar(select(func.count(WeightLog.id))) == before
    text = format_weekly_summary(data)
    assert '370' in text and 'дефицит' in text and 'Один известный замер' in text


@pytest.mark.asyncio
async def test_device_total_does_not_add_bmr_or_tef(api, store):
    populate(store)
    row = store.session.scalar(select(ActivityLog).where(ActivityLog.date == date(2026, 11, 15)))
    row.total_calories = Decimal('1850.72')
    row.active_calories = 0
    store.session.commit()
    result = (await api.get('/api/health/summary?start_date=2026-11-15&end_date=2026-11-15', headers=auth(1))).json()
    day = result['days'][0]
    assert day['bmr'] == 1805 and day['tef'] == 215
    assert day['tdee'] == 1850.72 and day['balance'] == 299.28
    assert day['expenditure_source'] == 'device_total' and not day['expenditure_uncertain']
    assert day['activity_calories_uncertain'] and day['complete']
    assert result['averages']['tdee'] == 1850.72


@pytest.mark.asyncio
async def test_total_without_physical_params_or_food_is_still_displayable(api, store):
    store.session.add(ActivityLog(user_id=2, date=date(2026, 11, 15), steps=100, total_calories=Decimal('1200.25')))
    store.session.commit()
    day = (await api.get('/api/health/summary?start_date=2026-11-15&end_date=2026-11-15', headers=auth(2))).json()['days'][0]
    assert day['tdee'] == 1200.25 and day['bmr'] is None and day['balance'] is None
    assert not day['expenditure_uncertain']


@pytest.mark.asyncio
async def test_weight_asof_day_timezone_and_no_future_leak(api, store):
    populate(store)
    # 21:30 UTC is already next day in Moscow.
    store.session.add_all([
        WeightLog(user_id=1, weight_kg=79, timestamp=datetime(2026, 11, 10, 21, 30, tzinfo=timezone.utc)),
        WeightLog(user_id=1, weight_kg=77, timestamp=datetime(2026, 11, 17, 12, tzinfo=timezone.utc)),
        WeightLog(user_id=2, weight_kg=60, timestamp=datetime(2026, 11, 8, 12, tzinfo=timezone.utc)),
    ])
    store.session.commit()
    data = (await api.get('/api/health/summary?start_date=2026-11-10&end_date=2026-11-12', headers=auth(1))).json()
    assert [d['weight_kg'] for d in data['days']] == [80, 79, 79]
    assert data['weight_change']['delta_kg'] == -1


@pytest.mark.asyncio
async def test_missing_sources_not_zero_and_today_excluded(api, store):
    populate(store)
    store.session.add(DailyNutrition(user_id=1, date=date(2026, 11, 16), calories=100))
    store.session.add(ActivityLog(user_id=1, date=date(2026, 11, 16), steps=100, active_calories=20))
    store.session.commit()
    data = (await api.get('/api/health/summary?start_date=2026-11-15&end_date=2026-11-16', headers=auth(1))).json()
    assert data['averages']['calories'] == 2150 and data['sample_days']['calories'] == 1
    assert data['days'][-1]['is_partial'] and data['complete_days'] == 1
    assert data['forecast_7d_kg'] is None
    partner = (await api.get('/api/health/summary', headers=auth(2))).json()
    assert partner['averages']['balance'] is None
    assert all(d['weight_kg'] is None and d['calories'] is None for d in partner['days'])


@pytest.mark.asyncio
async def test_zero_calories_uncertain_but_zero_intake_is_a_record(api, store):
    populate(store)
    row = store.session.scalar(select(ActivityLog).where(ActivityLog.user_id == 1))
    row.active_calories = 0
    store.session.scalar(select(DailyNutrition).where(DailyNutrition.date == row.date)).calories = 0
    store.session.commit()
    data = await HealthAnalytics(store).get_daily_summary(1, row.date)
    assert data['calories'] == 0 and 'nutrition' not in data['missing']
    assert data['tdee'] == 1805 and data['activity_calories_uncertain'] and not data['complete']


@pytest.mark.asyncio
async def test_summary_permissions_and_period_validation(api, store):
    assert (await api.get('/api/health/summary')).status_code == 401
    for query in ('target_user_id=1', 'all_users=true'):
        assert (await api.get('/api/health/summary?' + query, headers=auth(2))).status_code == 403
    assert (await api.get('/api/health/summary?all_users=true', headers=auth(1))).status_code == 400
    for query in ('start_date=2026-11-16&end_date=2026-11-15', 'start_date=2020-01-01',
                  'end_date=2099-01-01', 'start_date=bad'):
        assert (await api.get('/api/health/summary?' + query, headers=auth(1))).status_code == 422
    assert (await api.get('/api/health/summary?target_user_id=2', headers=auth(1))).json()['user_id'] == 2


@pytest.mark.asyncio
async def test_physical_params_edit_only_self(api, store):
    response = await api.patch('/api/users/me/physical', headers=auth(2), json={
        'gender': 'female', 'height_cm': 165, 'birth_date': '1996-11-17', 'user_id': 1,
    })
    assert response.status_code == 200 and response.json()['id'] == 2
    assert store.session.get(User, 1).height_cm is None
    assert store.session.get(User, 2).height_cm == 165
    assert (await api.patch('/api/users/1', headers=auth(2), json={'height_cm': 165})).status_code == 403
    assert (await api.patch('/api/users/2', headers=auth(1), json={'height_cm': 166})).status_code == 200
    assert (await api.patch('/api/users/me/physical', headers=auth(2), json={'height_cm': None})).status_code == 200
    assert store.session.get(User, 2).height_cm is None


@pytest.mark.parametrize('data', [{'height_cm': 10}, {'gender': 'invalid'}, {'birth_date': '2099-01-01'}])
def test_invalid_physical(data):
    with pytest.raises(ValueError):
        PhysicalParamsSchema(**data)


@pytest.mark.asyncio
async def test_stats_own_user_and_seven_completed_days(api, store, monkeypatch):
    from app.bot.handlers import health
    from app.bot.handlers import reports
    populate(store)
    monkeypatch.setattr(health, 'local_today', lambda: date(2026, 11, 16))
    message = SimpleNamespace(from_user=SimpleNamespace(id=222), answer=AsyncMock())
    sender=AsyncMock()
    monkeypatch.setattr(reports,'send_report',sender)
    await health.cmd_stats(message, store)
    assert sender.call_args.args[1].id==2
    assert sender.call_args.args[3:]==('health',date(2026,11,9),date(2026,11,15))
