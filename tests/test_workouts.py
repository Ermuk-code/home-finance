from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message
from sqlalchemy import delete, func, select

from app.health import workouts as service
from app.health.schemas import WorkoutRead
from app.models import Workout, WorkoutExercise
from tests import test_health_analytics as fixtures
from tests.test_activity import auth

base_store = fixtures.base_store
analytics_store = fixtures.store
api = fixtures.api
DAY = date(2026, 11, 16)


@pytest.fixture
def store(request):
    base = request.getfixturevalue('analytics_store')
    for model in (Workout, WorkoutExercise):
        model.__table__.create(base.session.get_bind())
    async def remove(obj):
        base.session.delete(obj)
    base.delete = remove
    return base


def body(day=DAY, title='День ног'):
    return {'date': day.isoformat(), 'title': title, 'exercises': [
        {'name': 'Приседания', 'sets': 3, 'reps': '10-12', 'weight_kg': 50.5},
        {'name': 'Планка', 'sets': 2, 'reps': '30 секунд'},
    ]}


async def create(api, user_id=1, day=DAY, title='День ног', target=None):
    query = f'?target_user_id={target}' if target else ''
    response = await api.post('/api/health/workouts'+query, headers=auth(user_id), json=body(day,title))
    assert response.status_code == 201, response.text
    return response.json()


@pytest.mark.asyncio
async def test_nested_create_week_boundary_and_isolation(api):
    own = await create(api)
    assert own['is_completed'] is False and own['user_id'] == 1
    assert [e['name'] for e in own['exercises']] == ['Приседания','Планка']
    assert float(own['exercises'][0]['weight_kg']) == 50.5
    await create(api, day=DAY+timedelta(days=6), title='Воскресенье')
    await create(api, day=DAY-timedelta(days=1), title='Предыдущая неделя')
    await create(api, day=DAY+timedelta(days=7), title='Следующая неделя')
    partner = await create(api,user_id=2)
    rows = (await api.get('/api/health/workouts', headers=auth(1))).json()
    assert [r['title'] for r in rows] == ['День ног','Воскресенье']
    other = (await api.get('/api/health/workouts', headers=auth(2))).json()
    assert [r['id'] for r in other] == [partner['id']]
    admin_other = (await api.get('/api/health/workouts?target_user_id=2', headers=auth(1))).json()
    assert [r['id'] for r in admin_other] == [partner['id']]


@pytest.mark.asyncio
async def test_all_routes_require_auth_and_scope(api):
    workout = await create(api)
    path='/api/health/workouts'
    cases=[('get',path,{}),('get',path+'/next',{}),('post',path,{'json':body()}),
           ('patch',path+f'/{workout["id"]}/complete',{}),('delete',path+f'/{workout["id"]}',{})]
    for method,url,kwargs in cases:
        assert (await getattr(api,method)(url,**kwargs)).status_code == 401
    for method,url,kwargs in cases:
        assert (await getattr(api,method)(url+'?target_user_id=1',headers=auth(2),**kwargs)).status_code == 403
    assert (await api.post(path+'?all_users=true',headers=auth(1),json=body())).status_code==400
    assert (await api.get(path+'?all_users=true',headers=auth(2))).status_code==403
    assert (await api.patch(path+f'/{workout["id"]}/complete',headers=auth(2))).status_code==404
    assert (await api.delete(path+f'/{workout["id"]}',headers=auth(2))).status_code==404


@pytest.mark.asyncio
async def test_admin_create_complete_idempotent_next_and_cascade(api,store):
    workout=await create(api,target=2)
    assert workout['user_id']==2
    path='/api/health/workouts'
    assert (await api.get(path+'/next',headers=auth(2))).json()['id']==workout['id']
    for _ in range(2):
        response=await api.patch(path+f'/{workout["id"]}/complete',headers=auth(2))
        assert response.status_code==200 and response.json()['is_completed'] is True
    assert (await api.get(path+'/next',headers=auth(2))).json() is None
    response=await api.delete(path+f'/{workout["id"]}?target_user_id=2',headers=auth(1))
    assert response.status_code==204 and response.content==b''
    assert store.session.scalar(select(func.count(WorkoutExercise.id)))==0
    assert (await api.delete(path+f'/{workout["id"]}',headers=auth(1))).status_code==404


@pytest.mark.asyncio
async def test_database_foreign_key_cascade(api,store):
    workout=await create(api)
    store.session.execute(delete(Workout).where(Workout.id==workout['id']))
    store.session.commit()
    assert store.session.scalar(select(func.count(WorkoutExercise.id)))==0


@pytest.mark.asyncio
async def test_next_ignores_past_and_complete(api):
    await create(api,day=DAY-timedelta(days=1))
    planned=await create(api,day=DAY+timedelta(days=10))
    closer=await create(api,day=DAY+timedelta(days=1))
    assert (await api.get('/api/health/workouts/next',headers=auth(1))).json()['id']==closer['id']
    await api.patch(f'/api/health/workouts/{closer["id"]}/complete',headers=auth(1))
    assert (await api.get('/api/health/workouts/next',headers=auth(1))).json()['id']==planned['id']


@pytest.mark.parametrize('changes',[
    {'title':'   '}, {'date':'invalid'}, {'exercises':[]},
    {'exercises':[{'name':' ','sets':3,'reps':'10'}]},
    {'exercises':[{'name':'A','sets':0,'reps':'10'}]},
    {'exercises':[{'name':'A','sets':True,'reps':'10'}]},
    {'exercises':[{'name':'A','sets':3,'reps':'   '}]},
    {'exercises':[{'name':'A','sets':3,'reps':'10','weight_kg':1000}]},
    {'exercises':[{'name':'A','sets':3,'reps':'10'}]*21},
])
@pytest.mark.asyncio
async def test_validation_does_not_create_partial_plan(api,store,changes):
    assert (await api.post('/api/health/workouts',headers=auth(1),json={**body(),**changes})).status_code==422
    assert store.session.scalar(select(func.count(Workout.id)))==0
    assert store.session.scalar(select(func.count(WorkoutExercise.id)))==0


@pytest.mark.asyncio
async def test_calendar_range_validation_and_identifier_bounds(api):
    for query in ('start_date=2026-11-20&end_date=2026-11-16','start_date=2020-01-01&end_date=2026-11-16','start_date=bad'):
        assert (await api.get('/api/health/workouts?'+query,headers=auth(1))).status_code==422
    for value in ('0','-1','99999999999999999999999999'):
        assert (await api.patch('/api/health/workouts/'+value+'/complete',headers=auth(1))).status_code==422


@pytest.mark.asyncio
async def test_morning_personalized_once_skip_completed_and_failed_send(api,store,monkeypatch):
    first=await create(api,title='<Ноги & спина>')
    partner=await create(api,user_id=2)
    completed=await create(api,user_id=3)
    await api.patch(f'/api/health/workouts/{completed["id"]}/complete',headers=auth(3))
    await create(api,day=DAY-timedelta(days=1))
    sent=[]
    async def send(chat_id,text,*,reply_markup):
        sent.append((chat_id,text,reply_markup))
        return chat_id!=222
    monkeypatch.setattr(service,'local_today',lambda:DAY)
    monkeypatch.setattr(service,'async_session_maker',lambda:store)
    monkeypatch.setattr(service,'send_telegram_message',send)
    await service.send_morning_workout_plan()
    assert [chat for chat,_,_ in sent]==[111,222]
    assert '&lt;Ноги &amp; спина&gt;' in sent[0][1] and '3 × 10-12' in sent[0][1]
    assert sent[0][2].inline_keyboard[0][1].callback_data == f'workout:move:{first["id"]}:{DAY}'
    assert store.session.get(Workout,first['id']).morning_notified_on==DAY
    assert store.session.get(Workout,partner['id']).morning_notified_on is None
    sent.clear()
    await service.send_morning_workout_plan()
    assert [chat for chat,_,_ in sent]==[222]


@pytest.mark.asyncio
async def test_bot_today_plan_inline_button_and_callback_privacy(api,store,monkeypatch):
    from app.bot.handlers import workouts as bot
    own=await create(api,user_id=2)
    other=await create(api)
    monkeypatch.setattr(service,'local_today',lambda:DAY)
    message=SimpleNamespace(from_user=SimpleNamespace(id=222),answer=AsyncMock())
    await bot.cmd_workout(message,store)
    markup=message.answer.call_args.kwargs['reply_markup']
    assert markup.inline_keyboard[0][0].callback_data==f'workout:done:{own["id"]}:{DAY}'
    assert markup.inline_keyboard[0][1].callback_data==f'workout:move:{own["id"]}:{DAY}'
    callback=SimpleNamespace(from_user=SimpleNamespace(id=222),data=f'workout:done:{other["id"]}',
                             answer=AsyncMock(),message=None)
    await bot.done_callback(callback,store)
    assert callback.answer.call_args.kwargs['show_alert'] is True
    assert not store.session.get(Workout,other['id']).is_completed
    callback.data=f'workout:done:{own["id"]}'
    await bot.done_callback(callback,store)
    assert store.session.get(Workout,own['id']).is_completed
    await bot.done_callback(callback,store)
    assert 'Уже' in callback.answer.call_args.args[0]
    for data in ('workout:done:no','workout:done:0','workout:done:99999999999999999999999'):
        callback.data=data
        await bot.done_callback(callback,store)
        assert 'Некорректный' in callback.answer.call_args.args[0]
    message.from_user.id=333
    await bot.cmd_workout(message,store)
    assert 'На сегодня тренировок не запланировано' in message.answer.call_args.args[0]


@pytest.mark.asyncio
async def test_bot_reschedules_missed_workout_and_rejects_old_buttons(api, store, monkeypatch):
    from app.bot.handlers import workouts as bot

    yesterday = DAY - timedelta(days=1)
    own = await create(api, user_id=2, day=yesterday, title='<Ноги & спина>')
    other = await create(api, day=yesterday)
    completed = await create(api, user_id=2, day=yesterday)
    await api.patch(f'/api/health/workouts/{completed["id"]}/complete', headers=auth(2))
    monkeypatch.setattr(service, 'local_today', lambda: DAY)
    monkeypatch.setattr(bot, 'local_today', lambda: DAY)
    state = FSMContext(MemoryStorage(), StorageKey(bot_id=123, chat_id=222, user_id=222))
    user = SimpleNamespace(id=222)
    message = Mock(spec=Message, from_user=user, answer=AsyncMock(), edit_text=AsyncMock())

    await bot.cmd_workout(message, store, state)
    markup = message.answer.call_args.kwargs['reply_markup']
    callback = SimpleNamespace(from_user=user, data=markup.inline_keyboard[0][0].callback_data,
                               answer=AsyncMock(), message=message)
    await bot.missed_callback(callback, store)
    rows = message.edit_text.call_args.kwargs['reply_markup'].inline_keyboard
    assert len(rows) == 1
    assert rows[0][0].callback_data == f'workout:move:{own["id"]}:{yesterday}'

    for workout_id in (other['id'], completed['id']):
        callback.data = f'workout:move:{workout_id}:{yesterday}'
        await bot.start_reschedule(callback, store, state)
        assert callback.answer.call_args.kwargs['show_alert'] is True
        assert await state.get_state() is None

    callback.data = rows[0][0].callback_data
    await bot.start_reschedule(callback, store, state)
    assert '&lt;Ноги &amp; спина&gt;' in message.answer.call_args.args[0]
    date_buttons = message.answer.call_args.kwargs['reply_markup'].inline_keyboard[0]
    assert [button.text for button in date_buttons] == ['Сегодня', 'Завтра']

    for invalid in ('31.02.2027', '15.11.2026'):
        message.text = invalid
        await bot.reschedule_date_message(message, store, state)
        assert await state.get_state() == bot.WorkoutReschedule.new_date.state
        assert store.session.get(Workout, own['id']).date == yesterday

    callback.data = date_buttons[0].callback_data
    await bot.reschedule_date_callback(callback, store, state)
    assert await state.get_state() is None
    assert '16.11.2026' in message.answer.call_args.args[0]
    rows = (await api.get('/api/health/workouts', headers=auth(2))).json()
    assert [(row['id'], row['date']) for row in rows] == [(own['id'], DAY.isoformat())]
    assert WorkoutRead.model_validate(rows[0]).exercises == WorkoutRead.model_validate(own).exercises

    await bot.reschedule_date_callback(callback, store, state)
    assert callback.answer.call_args.kwargs['show_alert'] is True
    callback.data = f'workout:done:{own["id"]}:{yesterday}'
    await bot.done_callback(callback, store)
    assert callback.answer.call_args.kwargs['show_alert'] is True
    assert not store.session.get(Workout, own['id']).is_completed


@pytest.mark.asyncio
async def test_custom_reschedule_moves_reminder_and_checks_latest_status(api, store, monkeypatch):
    from app.bot.handlers import workouts as bot

    today = DAY
    monkeypatch.setattr(service, 'local_today', lambda: today)
    monkeypatch.setattr(bot, 'local_today', lambda: today)
    monkeypatch.setattr(service, 'async_session_maker', lambda: store)
    send = AsyncMock(return_value=True)
    monkeypatch.setattr(service, 'send_telegram_message', send)
    own = await create(api)
    await service.send_morning_workout_plan()
    button = send.call_args.kwargs['reply_markup'].inline_keyboard[0][1]
    user = SimpleNamespace(id=111)
    message = Mock(spec=Message, from_user=user, answer=AsyncMock(), text='19.11.2026')
    callback = SimpleNamespace(from_user=user, data=button.callback_data, answer=AsyncMock(), message=message)
    state = FSMContext(MemoryStorage(), StorageKey(bot_id=123, chat_id=111, user_id=111))

    await bot.start_reschedule(callback, store, state)
    assert [button.text for button in message.answer.call_args.kwargs['reply_markup'].inline_keyboard[0]] == ['Завтра']
    await bot.cancel_reschedule(message, state)
    assert await state.get_state() is None
    assert store.session.get(Workout, own['id']).date == DAY
    await bot.start_reschedule(callback, store, state)
    await bot.reschedule_date_message(message, store, state)
    new_date = date(2026, 11, 19)
    workout = store.session.get(Workout, own['id'])
    assert workout.date == new_date and workout.morning_notified_on == DAY

    await service.send_morning_workout_plan()
    assert send.await_count == 1
    today = new_date
    await service.send_morning_workout_plan()
    assert send.await_count == 2
    assert '19.11.2026' in send.call_args.args[1]
    callback.data = send.call_args.kwargs['reply_markup'].inline_keyboard[0][1].callback_data
    await bot.start_reschedule(callback, store, state)
    await api.patch(f'/api/health/workouts/{own["id"]}/complete', headers=auth(1))
    message.text = '20.11.2026'
    await bot.reschedule_date_message(message, store, state)
    assert 'изменилась или недоступна' in message.answer.call_args.args[0]
    assert workout.date == new_date and workout.is_completed


@pytest.mark.asyncio
async def test_morning_job_0800_health_timezone(monkeypatch):
    from app.finance.services import scheduler as jobs
    monkeypatch.setattr(jobs,'fatsecret_configured',lambda:False)
    monkeypatch.setattr(jobs.settings,'ACTIVITY_TZ','Europe/Moscow')
    scheduler=jobs.setup_scheduler(SimpleNamespace())
    try:
        job=scheduler.get_job('morning_workout_job')
        now=datetime(2026,11,16,4,59,tzinfo=timezone.utc)
        fire=job.trigger.get_next_fire_time(None,now)
        assert fire.hour==8 and fire.minute==0 and fire.astimezone(timezone.utc).hour==5
        assert job.max_instances==1 and job.coalesce
    finally:
        scheduler.shutdown(wait=False)
