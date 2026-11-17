from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.health import dashboard as dashboard_module
from app.health.planning import build_plan, measurement_trend
from app.models import ActivityLog, BodyMeasurement, DailyNutrition, User, WeightLog
from tests import test_health_analytics as fixtures
from tests.test_activity import auth

api = fixtures.api
store = fixtures.store
base_store = fixtures.base_store
populate = fixtures.populate


@pytest.mark.asyncio
async def test_dashboard_profile_visibility(api):
    data = (await api.get('/api/health/profiles', headers=auth(2))).json()
    assert [p['id'] for p in data] == [2]
    assert len((await api.get('/api/health/profiles', headers=auth(1))).json()) == 3
    assert (await api.get('/api/health/profiles')).status_code == 401
    for path in ('dashboard', 'profile', 'measurements'):
        method = api.get if path == 'dashboard' else api.patch if path == 'profile' else api.post
        body = {} if path == 'dashboard' else {'json': {'height_cm': 180, 'date': '2026-11-10'}}
        assert (await method('/api/health/'+path+'?target_user_id=1', headers=auth(2), **body)).status_code == 403
        assert (await method('/api/health/'+path+'?all_users=true', headers=auth(1), **body)).status_code == 400


@pytest.mark.asyncio
async def test_comparison_today_separate_from_period_and_profile(api, store, monkeypatch):
    monkeypatch.setattr(dashboard_module, 'local_today', lambda: date(2026, 11, 16))
    populate(store)
    store.session.add_all([
        DailyNutrition(user_id=1, date=date(2026, 11, 16), calories=900),
        ActivityLog(user_id=1, date=date(2026, 11, 16), steps=6202, total_calories=Decimal('1850.72')),
        ActivityLog(user_id=2, date=date(2026, 11, 16), steps=3606, total_calories=Decimal('1300.50')),
    ])
    store.session.commit()
    data = (await api.get('/api/health/dashboard?start_date=2026-11-10&end_date=2026-11-11', headers=auth(1))).json()
    assert data['summary']['averages']['calories'] == 2150
    assert data['today']['date'] == '2026-11-16'
    assert data['today']['calories'] == 900 and data['today']['tdee'] == 1850.72
    partner = (await api.get('/api/health/dashboard?target_user_id=2', headers=auth(1))).json()['today']
    assert partner['tdee'] == 1300.50 and partner['calories'] is None
    missing = (await api.get('/api/health/dashboard', headers=auth(3))).json()['today']
    assert missing['calories'] is None and missing['tdee'] is None


@pytest.mark.asyncio
async def test_measurement_partial_upsert_and_weight_atomic(api, store, monkeypatch):
    monkeypatch.setattr(dashboard_module, 'local_today', lambda: date(2026,11,16))
    first = await api.post('/api/health/measurements?target_user_id=2', headers=auth(1),
                           json={'date':'2026-11-10','height_cm':165,'waist_cm':80,'weight_kg':65})
    assert first.status_code == 201
    again = await api.post('/api/health/measurements', headers=auth(2), json={'date':'2026-11-10','waist_cm':79})
    assert again.status_code == 201
    rows = store.session.scalars(select(BodyMeasurement)).all()
    assert len(rows) == 1 and rows[0].height_cm == Decimal('165') and rows[0].waist_cm == Decimal('79')
    weight = store.session.scalar(select(WeightLog))
    assert weight.user_id == 2 and weight.weight_kg == 65
    assert store.session.get(User, 1).height_cm is None


@pytest.mark.asyncio
async def test_historical_height_and_waist_not_applied_early(api, store):
    populate(store)
    store.session.add_all([
        BodyMeasurement(user_id=1,date=date(2026,11,11),height_cm=180,waist_cm=90),
        BodyMeasurement(user_id=2,date=date(2026,11,9),height_cm=160,waist_cm=60),
        BodyMeasurement(user_id=1,date=date(2026,11,20),height_cm=170,waist_cm=80),
    ])
    store.session.commit()
    data = (await api.get('/api/health/summary?start_date=2026-11-10&end_date=2026-11-12',headers=auth(1))).json()
    assert [d['height_cm'] for d in data['days']] == [184,180,180]
    assert [d['waist_cm'] for d in data['days']] == [None,90,90]
    assert data['days'][1]['bmr'] == 1780


@pytest.mark.asyncio
async def test_nutrition_plan_and_macro_energy(api, store, monkeypatch):
    monkeypatch.setattr(dashboard_module,'local_today',lambda:date(2026,11,16))
    populate(store)
    await api.patch('/api/health/profile',headers=auth(1),json={'health_goal':'lose','deficit_percent':15,'goal_weight_kg':75})
    data = (await api.get('/api/health/dashboard',headers=auth(1))).json()
    p = data['plan']
    assert p['maintenance_source'] == 'telemetry_28d'
    assert p['target_calories'] == 2142
    assert p['planned_loss_kg_week'] == .309
    assert len(p['forecast']) == 5 and p['goal_weeks'] > 0
    m = p['macros']
    assert abs(4*m['protein_g']+9*m['fat_g']+4*m['carbs_g']-p['target_calories']) < 1
    assert p['bmi'] == 23.63
    # The arbitrary chart period does not change today's plan.
    other = (await api.get('/api/health/dashboard?start_date=2026-11-10&end_date=2026-11-11',headers=auth(1))).json()
    assert other['plan']['target_calories'] == p['target_calories']


@pytest.mark.asyncio
async def test_fallback_no_data_and_adult_guard(api,store,monkeypatch):
    monkeypatch.setattr(dashboard_module,'local_today',lambda:date(2026,11,16))
    unknown = (await api.get('/api/health/dashboard',headers=auth(2))).json()['plan']
    assert unknown['macros'] is None and unknown['forecast'] == []
    user = store.session.get(User,2)
    user.gender,user.height_cm,user.birth_date='female',165,date(1996,1,1)
    user.health_goal='lose'
    store.session.add(WeightLog(user_id=2,weight_kg=65,timestamp=datetime(2026,11,10,12,tzinfo=timezone.utc)))
    store.session.commit()
    p=(await api.get('/api/health/dashboard',headers=auth(2))).json()['plan']
    assert p['maintenance_source']=='bmr_activity_factor' and p['macros'] is not None
    user.birth_date=date(2016,1,1)
    store.session.commit()
    p=(await api.get('/api/health/dashboard',headers=auth(2))).json()['plan']
    assert p['target_calories'] is None and p['waist_ratio_band'] is None


@pytest.mark.asyncio
async def test_bad_goal_underweight_and_real_measurements(api,store,monkeypatch):
    monkeypatch.setattr(dashboard_module,'local_today',lambda:date(2026,11,16))
    populate(store)
    await api.patch('/api/health/profile',headers=auth(1),json={'health_goal':'lose','goal_weight_kg':40})
    p=(await api.get('/api/health/dashboard',headers=auth(1))).json()['plan']
    assert p['macros'] is None and p['forecast']==[]
    assert p['weight_trend_kg_week'] is None  # one measurement carried for many days is not many measurements


@pytest.mark.parametrize('body',[
    {'date':'2099-01-01','waist_cm':80}, {'date':'2026-11-10'},
    {'date':'2026-11-10','waist_cm':0}, {'date':'2026-11-10','height_cm':300},
    {'date':'2026-11-10','weight_kg':-2}, {'date':'2026-11-10','waist_cm':'NaN'},
])
@pytest.mark.asyncio
async def test_bad_measurements_no_write(api,store,body):
    response=await api.post('/api/health/measurements',headers=auth(2),json=body)
    assert response.status_code==422
    assert list(store.session.scalars(select(BodyMeasurement)))==[]


@pytest.mark.asyncio
@pytest.mark.parametrize('body',[{'deficit_percent':50},{'activity_factor':0},{'health_goal':'invalid'}, {'goal_weight_kg':10}])
async def test_bad_planning_params(api,body):
    assert (await api.patch('/api/health/profile',headers=auth(1),json=body)).status_code==422


def test_real_measurement_regression():
    day=date(2026,11,1)
    assert measurement_trend([(day,90),(day+timedelta(days=7),89),(day+timedelta(days=14),88)])==-1
    assert measurement_trend([(day,90),(day+timedelta(days=1),89),(day+timedelta(days=2),88)]) is None
    assert measurement_trend([(day,90),(day,89),(day,88)]) is None


def test_low_calorie_floor_and_ratio_boundary():
    user=type('User',(),dict(activity_factor=Decimal('1.2'),health_goal='lose',deficit_percent=20,goal_weight_kg=None,gender='female'))()
    summary={'days':[{'weight_kg':50,'height_cm':160,'waist_cm':80,'age':30,'bmr':1000}],
             'complete_days':0,'averages':{'tdee':None}}
    plan=build_plan(user,summary,[],[],date(2026,11,16))
    assert plan['target_calories'] is None
    assert plan['waist_ratio_band'].startswith('0,5')


def test_waist_forecast_needs_real_history_and_fresh_measurement():
    today=date(2026,11,16)
    user=SimpleNamespace(activity_factor=Decimal('1.2'),health_goal='maintain',deficit_percent=15,goal_weight_kg=None,gender='male')
    summary={'days':[{'weight_kg':80,'height_cm':184,'waist_cm':88,'age':30,'bmr':1805}],
             'complete_days':0,'averages':{'tdee':None}}
    rows=[SimpleNamespace(date=today-timedelta(days=14-7*i),waist_cm=Decimal(90-i)) for i in range(3)]
    p=build_plan(user,summary,[],rows,today)
    assert p['waist_trend_cm_week']==-1
    assert p['waist_forecast'][-1]['waist_cm']==84
    assert p['planned_loss_kg_week']==0
    old=build_plan(user,summary,[],rows,today+timedelta(days=15))
    assert old['waist_forecast']==[]
