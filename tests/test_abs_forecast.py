from datetime import date, datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy import select

from app.health import abs_forecast as forecast
from app.health.schemas import WeightWebhookPayload
from app.health.dashboard import HealthProfile
from app.models import HealthMetric, User, WeightLog
from tests import test_health_analytics as fixtures
from tests.test_activity import auth

api = fixtures.api
store = fixtures.store
base_store = fixtures.base_store


def user(**kwargs):
    return SimpleNamespace(target_body_fat=12, avg_daily_deficit=500, gender='male', **kwargs)


def metric(weight=81.55, fat=18):
    return HealthMetric(user_id=1, weight=weight, body_fat=fat,
                        date=datetime(2026,11,10,12,tzinfo=timezone.utc))


def test_requested_example_math_and_calendar():
    result=forecast.calculate_abs_date(user(), metric(), date(2026,11,16))
    assert result.status=='in_progress'
    assert result.target_weight==75.99
    assert result.fat_mass==14.68 and result.lean_body_mass==66.87
    assert result.days_left==86 and result.target_date==date(2027,2,10)
    assert '86' in forecast.format_abs_forecast(result)


@pytest.mark.parametrize('target',[18,20])
def test_goal_already_reached_even_without_deficit(target):
    u=user();u.target_body_fat=target;u.avg_daily_deficit=0
    result=forecast.calculate_abs_date(u,metric(),date(2026,11,16))
    assert result.status=='achieved' and result.days_left==0
    assert result.fat_to_lose==0 and result.total_deficit_needed==0
    assert 'Цель уже достигнута' in forecast.format_abs_forecast(result)


@pytest.mark.parametrize('deficit',[0,-500])
def test_no_positive_deficit(deficit):
    u=user();u.avg_daily_deficit=deficit
    result=forecast.calculate_abs_date(u,metric())
    assert result.status=='no_deficit' and result.target_date is None and result.days_left is None


def test_missing_metrics_and_missing_fat():
    for m in (None,metric(fat=None)):
        result=forecast.calculate_abs_date(user(),m)
        assert result.status=='missing_metrics' and result.days_left is None
        assert 'Нужен замер' in forecast.format_abs_forecast(result)


def test_ceil_fractional_day_and_huge_horizon():
    u=user();u.avg_daily_deficit=2000
    result=forecast.calculate_abs_date(u,metric(),date(2026,11,16))
    assert result.days_left==22
    u.avg_daily_deficit=1
    result=forecast.calculate_abs_date(u,metric(weight=400,fat=99.9),date(9999,1,1))
    assert result.status=='horizon_exceeded' and result.target_date is None


@pytest.mark.parametrize('fat',[0,100,-1,float('nan')])
def test_invalid_metric_rejected(fat):
    with pytest.raises(ValueError):
        forecast.calculate_abs_date(user(),metric(fat=fat))


def test_warning_is_not_a_visible_abs_promise():
    u=user();u.gender='female'
    result=forecast.calculate_abs_date(u,metric())
    assert result.warnings and 'женщин' in result.warnings[0]
    assert 'не гарантирует' in forecast.format_abs_forecast(result)


@pytest.mark.parametrize('body',[{'target_body_fat':0},{'target_body_fat':100},{'target_body_fat':'NaN'},
                                 {'avg_daily_deficit':-1},{'avg_daily_deficit':2001},{'avg_daily_deficit':True}])
def test_forecast_profile_validation(body):
    with pytest.raises(ValueError):
        HealthProfile(**body)


@pytest.mark.asyncio
async def test_api_auth_ownership_missing_and_future_exclusion(api,store,monkeypatch):
    monkeypatch.setattr(forecast,'local_today',lambda:date(2026,11,16))
    store.session.add_all([
        metric(),
        HealthMetric(user_id=2,weight=65,body_fat=25,date=datetime(2026,11,10,12,tzinfo=timezone.utc)),
        HealthMetric(user_id=1,weight=90,body_fat=30,date=datetime(2099,1,1,tzinfo=timezone.utc)),
        HealthMetric(user_id=1,weight=70,body_fat=None,date=datetime(2026,11,11,12,tzinfo=timezone.utc)),
    ])
    store.session.commit()
    path='/api/v1/health/forecast-abs'
    assert (await api.get(path)).status_code==401
    assert (await api.get(path+'?user_id=1',headers=auth(2))).status_code==403
    assert (await api.get(path+'?user_id=999',headers=auth(1))).status_code==404
    assert (await api.get(path+'?user_id=0',headers=auth(1))).status_code==422
    own=(await api.get(path,headers=auth(1))).json()
    assert own['current_weight']==81.55 and own['current_body_fat']==18 and own['days_left']==86
    partner=(await api.get(path,headers=auth(2))).json()
    assert partner['current_weight']==65
    assert (await api.get(path+'?user_id=2',headers=auth(1))).json()['current_body_fat']==25
    assert (await api.get(path,headers=auth(3))).json()['status']=='missing_metrics'


@pytest.mark.asyncio
async def test_manual_pair_and_profile_config(api,store):
    response=await api.post('/api/health/measurements',headers=auth(2),json={
        'date':'2026-11-10','weight_kg':65,'body_fat':25})
    assert response.status_code==201
    m=store.session.scalar(select(WeightLog))
    assert m.user_id==2 and m.body_fat==Decimal('25')
    assert (await api.post('/api/health/measurements',headers=auth(2),json={
        'date':'2026-11-10','body_fat':24})).status_code==422
    changed=await api.patch('/api/health/profile',headers=auth(2),json={'target_body_fat':22,'avg_daily_deficit':300})
    assert changed.status_code==200
    assert store.session.get(User,2).target_body_fat==22
    assert store.session.get(User,1).target_body_fat==12
    result=(await api.get('/api/v1/health/forecast-abs',headers=auth(2))).json()
    assert result['target_body_fat']==22 and result['avg_daily_deficit']==300


@pytest.mark.asyncio
async def test_webhook_stores_fat_with_weight_and_legacy_still_works(api,store,monkeypatch):
    from app.health import router
    monkeypatch.setattr(router,'send_telegram_message',AsyncMock(return_value=False))
    headers={'X-Webhook-Token':'activity-test'}
    response=await api.post('/api/health/webhook/weight',headers=headers,
                            json={'profile_alias':'admin','weight_kg':81.55,'body_fat':18})
    assert response.status_code==200 and response.json()['body_fat']==18
    old=await api.post('/api/health/webhook/weight',headers=headers,
                       json={'profile_alias':'admin','weight_kg':81.5})
    assert old.status_code==200 and old.json()['body_fat'] is None
    assert len(store.session.scalars(select(WeightLog)).all())==2


@pytest.mark.parametrize('bad',[0,100,-5,'NaN'])
def test_schema_body_fat_validation(bad):
    with pytest.raises(ValueError):
        WeightWebhookPayload(profile_alias='admin',weight_kg=80,body_fat=bad)


@pytest.mark.asyncio
async def test_bot_calls_protected_endpoint_for_own_user(api,store,monkeypatch):
    from app.bot.handlers import health
    calls=[]
    class Client:
        def __init__(self,**kwargs):pass
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
        async def get(self,url,**kwargs):
            calls.append((url,kwargs))
            result=forecast.calculate_abs_date(user(),metric(),date(2026,11,16))
            return httpx.Response(200,json=result.model_dump(mode='json'),request=httpx.Request('GET',url))
    monkeypatch.setattr(health.httpx,'AsyncClient',Client)
    message=SimpleNamespace(from_user=SimpleNamespace(id=222),answer=AsyncMock())
    await health.cmd_abs_forecast(message,store)
    assert calls[0][0]=='http://127.0.0.1:8000/api/v1/health/forecast-abs'
    from app.core.security import decode_token_user_id, TOKEN_TYPE_ACCESS
    token=calls[0][1]['headers']['Authorization'].split(' ',1)[1]
    assert decode_token_user_id(token,TOKEN_TYPE_ACCESS)==2
    assert '86 дней' in message.answer.call_args.args[0]
    assert '10.02.2027' in message.answer.call_args.args[0]


@pytest.mark.asyncio
async def test_bot_network_error_is_readable(api,store,monkeypatch):
    from app.bot.handlers import health
    class Client:
        def __init__(self,**kwargs):pass
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
        async def get(self,*args,**kwargs):raise httpx.ConnectError('offline')
    monkeypatch.setattr(health.httpx,'AsyncClient',Client)
    message=SimpleNamespace(from_user=SimpleNamespace(id=222),answer=AsyncMock())
    await health.cmd_abs_forecast(message,store)
    assert 'Не удалось' in message.answer.call_args.args[0]
