import asyncio
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Chat, Message, User as TelegramUser
from fastapi import HTTPException
from jose import jwt
from sqlalchemy.dialects import postgresql

from app.bot.handlers import reports
from app.bot.keyboards.reply import get_main_menu, MAIN_MENU_TEXTS
from app.bot.keyboards.inline import get_period_kb
from app.core.config import settings
from app.core.security import create_access_token
from app.models import User
from app.services import reporting, screenshot
from tests import test_health_analytics as fixtures

api=fixtures.api
store=fixtures.store
base_store=fixtures.base_store
DAY=date(2026,11,16)


@pytest.fixture(autouse=True)
def fixed_today(monkeypatch):
    monkeypatch.setattr(reporting,'local_today',lambda:DAY)


@pytest_asyncio.fixture
async def state():
    storage=MemoryStorage()
    context=FSMContext(storage,StorageKey(bot_id=123,chat_id=222,user_id=222))
    yield context
    await storage.close()


def callback(data):
    message=Message(message_id=1,date=datetime.now(timezone.utc),chat=Chat(id=222,type='private'),
                    from_user=TelegramUser(id=999,is_bot=True,first_name='Bot'),text='menu')
    return SimpleNamespace(data=data,message=message,from_user=TelegramUser(id=222,is_bot=False,first_name='Вика'),answer=AsyncMock())


def report_data():
    return {'kind':'finance','name':'Вика','start_date':'2026-11-01','end_date':'2026-11-16',
            'finance':{'clean_income':500,'clean_expense':200,'net':300,'transaction_count':3}}


def test_main_menu_and_period_buttons():
    keyboard=get_main_menu()
    assert {b.text for row in keyboard.keyboard for b in row}==MAIN_MENU_TEXTS
    assert keyboard.resize_keyboard and keyboard.is_persistent
    for kind in ('finance','health'):
        callbacks={b.callback_data for row in get_period_kb(kind).inline_keyboard for b in row}
        assert {f'report:{kind}:{p}' for p in ('day','week','month','year','custom')}<=callbacks


def test_period_boundaries_leap_year_and_calendar_month():
    assert reporting.report_period('day',DAY)==(DAY,DAY)
    assert reporting.report_period('week',DAY)==(date(2026,11,9),date(2026,11,15))
    assert reporting.report_period('month',DAY)==(date(2026,11,1),DAY)
    assert reporting.report_period('year',date(2024,2,29))==(date(2024,1,1),date(2024,2,29))
    assert reporting.parse_custom_period('01.11.2026 - 16.11.2026')==(date(2026,11,1),DAY)
    assert reporting.parse_custom_period('01.11.2026–16.11.2026')==(date(2026,11,1),DAY)


@pytest.mark.parametrize('value',['2026-11-01/2026-11-16','31.02.2026-16.11.2026','17.11.2026-16.11.2026',
                                 '01.11.2026-17.11.2026','01.01.2020-16.11.2026'])
def test_custom_date_errors(value):
    with pytest.raises(ValueError):reporting.parse_custom_period(value)


@pytest.mark.asyncio
async def test_finance_boundaries_user_filter_and_transfer_refund_math():
    rows=[('Зарплата',Decimal('1000'),'income',datetime(2026,11,1,8,tzinfo=timezone.utc)),
          ('Продукты',Decimal('-200'),'expense',datetime(2026,11,1,9,tzinfo=timezone.utc)),
          ('Продукты',Decimal('50'),'expense',datetime(2026,11,1,10,tzinfo=timezone.utc))]
    class Session:
        async def execute(self,stmt):
            self.statement=stmt
            return SimpleNamespace(all=lambda:rows)
    session=Session()
    data=await reporting.build_report_data(session,SimpleNamespace(id=2,name='Вика'),'finance',date(2026,11,1),DAY)
    assert data['finance']['clean_expense']==150 and data['finance']['net']==850
    assert len(data['finance']['days'])==16 and data['finance']['days'][0]['expense']==150
    compiled=session.statement.compile(dialect=postgresql.dialect())
    assert 'transactions.user_id =' in str(compiled) and 2 in compiled.params.values()
    dates=[v for v in compiled.params.values() if isinstance(v,datetime)]
    assert dates[0].hour==0 and dates[0].utcoffset()==timedelta(hours=3)
    assert dates[1].date()==DAY+timedelta(days=1) and 'transactions.date < ' in str(compiled)


@pytest.mark.asyncio
async def test_health_data_is_personal(api,store):
    fixtures.populate(store)
    owner=await store.get(User,1)
    partner=await store.get(User,2)
    first=await reporting.build_report_data(store,owner,'health',date(2026,11,9),date(2026,11,15))
    second=await reporting.build_report_data(store,partner,'health',date(2026,11,9),date(2026,11,15))
    assert first['health']['averages']['calories']==2150
    assert second['health']['averages']['calories'] is None


def test_render_token_binding_expiry_and_token_separation():
    token=screenshot.create_render_token(2,DAY,DAY,'health')
    assert screenshot.decode_render_token(token,DAY,DAY,'health')==2
    for kind,start in [('finance',DAY),('health',DAY-timedelta(days=1))]:
        with pytest.raises(HTTPException):screenshot.decode_render_token(token,start,DAY,kind)
    with pytest.raises(HTTPException):screenshot.decode_render_token(create_access_token(2),DAY,DAY,'health')
    payload=jwt.decode(token,settings.JWT_SECRET_KEY,algorithms=[settings.JWT_ALGORITHM])
    payload['exp']=datetime.now(timezone.utc)-timedelta(seconds=5)
    expired=jwt.encode(payload,settings.JWT_SECRET_KEY,algorithm=settings.JWT_ALGORITHM)
    with pytest.raises(HTTPException):screenshot.decode_render_token(expired,DAY,DAY,'health')


@pytest.mark.asyncio
async def test_render_page_protected_scoped_and_no_session_escalation(api,store):
    token=screenshot.create_render_token(2,DAY,DAY,'health')
    params={'start_date':DAY.isoformat(),'end_date':DAY.isoformat(),'report_type':'health'}
    assert (await api.get('/dashboard-render',params=params)).status_code==401
    assert (await api.get('/dashboard-render',params=params,headers={'Authorization':'Bearer '+create_access_token(1)})).status_code==401
    response=await api.get('/dashboard-render',params={**params,'user_id':1},headers={'Authorization':'Bearer '+token})
    assert response.status_code==200 and 'Вика' in response.text and 'Admin' not in response.text
    assert 'no-store' in response.headers['cache-control'] and 'set-cookie' not in response.headers
    assert 'cdn.' not in response.text
    wrong=await api.get('/dashboard-render',params={**params,'report_type':'finance'},headers={'Authorization':'Bearer '+token})
    assert wrong.status_code==401
    assert (await api.get('/api/users/me',headers={'Authorization':'Bearer '+token})).status_code==401


@pytest.mark.asyncio
async def test_render_name_escaped(api,store):
    (await store.get(User,2)).name='<script>alert(1)</script>'
    await store.commit()
    token=screenshot.create_render_token(2,DAY,DAY,'health')
    response=await api.get('/dashboard-render',params={'start_date':str(DAY),'end_date':str(DAY),'report_type':'health','token':token})
    assert response.status_code==200 and '<script>alert(1)</script>' not in response.text
    assert '&lt;script&gt;' in response.text


@pytest.mark.asyncio
async def test_custom_state_keeps_bad_input_and_clears_good(state,store,monkeypatch):
    await state.set_state(reports.ReportState.waiting_for_custom_dates)
    await state.update_data(report_type='health')
    message=SimpleNamespace(from_user=SimpleNamespace(id=222),text='31.02.2026-16.11.2026',answer=AsyncMock())
    sender=AsyncMock();monkeypatch.setattr(reports,'send_report',sender)
    await reports.custom_dates(message,state,store)
    assert await state.get_state()==reports.ReportState.waiting_for_custom_dates.state
    sender.assert_not_called()
    message.text='01.11.2026-16.11.2026'
    await reports.custom_dates(message,state,store)
    assert await state.get_state() is None
    assert sender.call_args.args[1].id==2 and sender.call_args.args[3:] == ('health',date(2026,11,1),DAY)


@pytest.mark.asyncio
async def test_period_callback_uses_clicking_person_not_bot(state,store,monkeypatch):
    cb=callback('report:finance:day')
    sender=AsyncMock();monkeypatch.setattr(reports,'send_report',sender)
    monkeypatch.setattr(reports,'report_period',lambda p:(DAY,DAY))
    await reports.choose_period(cb,state,store)
    assert sender.call_args.args[1].id==2
    answer=AsyncMock();monkeypatch.setattr(Message,'answer',answer)
    cb.data='report:health:custom'
    await reports.choose_period(cb,state,store)
    assert (await state.get_data())['report_type']=='health'
    assert await state.get_state()==reports.ReportState.waiting_for_custom_dates.state
    await reports.cancel_report(cb,state)
    assert await state.get_state() is None


@pytest.mark.asyncio
async def test_menu_action_preserves_di_and_actual_user(state,store,monkeypatch):
    from app.bot.handlers import manual,limits
    handler=AsyncMock();monkeypatch.setattr(manual,'cmd_add',handler)
    await reports.menu_action(callback('menu:add'),state,store)
    assert len(handler.call_args.args)==2 and handler.call_args.args[0].from_user.id==222
    assert handler.call_args.args[1] is state
    handler=AsyncMock();monkeypatch.setattr(limits,'cmd_set_limit',handler)
    await reports.menu_action(callback('menu:set_limit'),state,store)
    assert handler.call_args.args[2] is store


@pytest.mark.asyncio
async def test_image_delivery_fallback_and_caption_limits(store,monkeypatch):
    data=report_data()
    monkeypatch.setattr(reports,'build_report_data',AsyncMock(return_value=data))
    generator=AsyncMock(return_value=b'PNG');monkeypatch.setattr(reports,'take_dashboard_screenshot',generator)
    message=SimpleNamespace(answer=AsyncMock(),answer_photo=AsyncMock())
    actor=await store.get(User,2)
    await reports.send_report(message,actor,store,'finance',DAY,DAY)
    assert message.answer_photo.call_count==1 and generator.call_args.args[3]=='finance'
    assert len(message.answer_photo.call_args.kwargs['caption'])<=1024
    generator.side_effect=RuntimeError('browser failed')
    message.answer.reset_mock();message.answer_photo.reset_mock()
    await reports.send_report(message,actor,store,'finance',DAY,DAY)
    assert message.answer_photo.call_count==0
    assert any('Доходы:' in c.args[0] for c in message.answer.call_args_list)
    generator.side_effect=None
    monkeypatch.setattr(reports,'format_period_summary',lambda _: 'A'*1200)
    message.answer.reset_mock()
    await reports.send_report(message,actor,store,'finance',DAY,DAY)
    assert len(message.answer_photo.call_args.kwargs['caption'])<1024
    assert any(c.args[0]=='A'*1200 for c in message.answer.call_args_list)
    message.answer_photo.side_effect=TelegramAPIError(method=SimpleNamespace(),message='failed')
    await reports.send_report(message,actor,store,'finance',DAY,DAY)
    assert actor.id not in reports._busy_users


@pytest.mark.asyncio
async def test_summary_arrives_before_render_and_photo_upload(store,monkeypatch):
    expected='Готовая текстовая сводка'
    monkeypatch.setattr(reports,'build_report_data',AsyncMock(return_value=report_data()))
    monkeypatch.setattr(reports,'format_period_summary',lambda _: expected)
    events=[]
    async def answer(text,**kwargs):
        if text==expected:
            events.append('summary')
    async def render(*args,**kwargs):
        assert events==['summary']
        events.append('render')
        return b'PNG'
    async def photo(*args,**kwargs):
        assert events==['summary','render']
        events.append('upload')
    monkeypatch.setattr(reports,'take_dashboard_screenshot',render)
    message=SimpleNamespace(answer=AsyncMock(side_effect=answer),answer_photo=AsyncMock(side_effect=photo))
    await reports.send_report(message,await store.get(User,2),store,'finance',DAY,DAY)
    assert events==['summary','render','upload']
    assert sum(c.args[0]==expected for c in message.answer.call_args_list)==1


@pytest.mark.asyncio
async def test_stalled_photo_is_bounded_and_summary_is_already_available(store,monkeypatch,caplog):
    monkeypatch.setattr(reports,'build_report_data',AsyncMock(return_value=report_data()))
    monkeypatch.setattr(reports,'format_period_summary',lambda _: 'Готовая сводка')
    monkeypatch.setattr(reports,'take_dashboard_screenshot',AsyncMock(return_value=b'PNG'))
    monkeypatch.setattr(reports,'REPORT_PHOTO_TIMEOUT_SEC',0.01)
    message=SimpleNamespace(answer=AsyncMock())
    async def stalled_photo(*args,**kwargs):
        assert any(c.args[0]=='Готовая сводка' for c in message.answer.call_args_list)
        await asyncio.Event().wait()
    message.answer_photo=AsyncMock(side_effect=stalled_photo)
    actor=await store.get(User,2)
    await asyncio.wait_for(reports.send_report(message,actor,store,'finance',DAY,DAY),timeout=1)
    assert actor.id not in reports._busy_users
    assert 'Текстовая сводка уже отправлена' in message.answer.call_args.args[0]
    assert 'error=TimeoutError' in caplog.text


@pytest.mark.asyncio
async def test_duplicate_report_is_not_generated(store,monkeypatch):
    generator=AsyncMock();monkeypatch.setattr(reports,'take_dashboard_screenshot',generator)
    actor=await store.get(User,2);message=SimpleNamespace(answer=AsyncMock())
    reports._busy_users.add(actor.id)
    try:
        await reports.send_report(message,actor,store,'finance',DAY,DAY)
        generator.assert_not_called()
        assert 'еще готовится' in message.answer.call_args.args[0]
    finally:reports._busy_users.discard(actor.id)


@pytest.mark.asyncio
@pytest.mark.parametrize('period',['day','week','month','year'])
async def test_health_period_callback_generates_same_photo_report(period,state,store,monkeypatch):
    sender=AsyncMock()
    monkeypatch.setattr(reports,'send_report',sender)
    monkeypatch.setattr(reports,'report_period',lambda p: reporting.report_period(p,DAY))
    await reports.choose_period(callback('report:health:'+period),state,store)
    assert sender.call_args.args[1].id==2
    assert sender.call_args.args[3:] == ('health',*reporting.report_period(period,DAY))


@pytest.mark.asyncio
async def test_health_report_sends_image_with_personal_summary(store,monkeypatch):
    actor=await store.get(User,2)
    data=await reporting.build_report_data(store,actor,'health',date(2026,11,9),date(2026,11,15))
    monkeypatch.setattr(reports,'build_report_data',AsyncMock(return_value=data))
    generator=AsyncMock(return_value=b'PNG')
    monkeypatch.setattr(reports,'take_dashboard_screenshot',generator)
    message=SimpleNamespace(answer=AsyncMock(),answer_photo=AsyncMock())
    await reports.send_report(message,actor,store,'health',date(2026,11,9),date(2026,11,15))
    assert generator.call_args.args[3]=='health'
    text=message.answer_photo.call_args.kwargs['caption']+'\n'+'\n'.join(c.args[0] for c in message.answer.call_args_list)
    assert 'Здоровье' in text and 'Вика' in text and 'Шаги' in text
