"""HTTP, реальные записи в тестовой SQLite, права, бот и вечернее расписание.

SQLite исполняет тот же ON CONFLICT; greatest зарегистрирован как аналог PostgreSQL.
Это не заменяет приемку на целевом PostgreSQL и реальных устройствах.
"""
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Session

from app.core.database import Base, get_async_session
from app.core.security import create_access_token
from app.health import activity
from app.health import router as health
from app.models import ActivityLog, User


DAY = date(2026, 11, 16)


class AsyncSessionBridge:
    """Асинхронный интерфейс над изолированной синхронной тестовой БД."""

    def __init__(self, session):
        self.session = session
        self.statements = []

    async def execute(self, stmt, **kwargs):
        self.statements.append(stmt)
        return self.session.execute(stmt, **kwargs)

    async def commit(self):
        self.session.commit()

    async def get(self, model, key):
        return self.session.get(model, key)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        pass


@pytest.fixture
def store(monkeypatch):
    engine = create_engine("sqlite://")

    @event.listens_for(engine, "connect")
    def configure(connection, _record):
        connection.create_function("greatest", 2, max)
        connection.create_function("lower", 1, lambda value: value.lower() if value is not None else None)
        connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine, tables=[User.__table__, ActivityLog.__table__])
    with Session(engine, expire_on_commit=False) as session:
        session.add_all([
            User(id=1, telegram_id=111, role="admin", name="Влад"),
            User(id=2, telegram_id=222, role="user", name="Вика"),
            User(id=3, telegram_id=333, role="user", name="Нет данных"),
        ])
        session.commit()
        monkeypatch.setattr(health.settings, "HEALTH_WEBHOOK_TOKEN", "activity-test")
        monkeypatch.setattr(health, "local_today", lambda: DAY)
        monkeypatch.setattr(activity, "local_today", lambda: DAY)
        yield AsyncSessionBridge(session)
    engine.dispose()


@pytest_asyncio.fixture
async def client(store):
    app = FastAPI()
    app.include_router(health.router)

    async def get_session():
        yield store

    app.dependency_overrides[get_async_session] = get_session
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        yield client


async def post(client, alias="admin", steps=3200, calories=100):
    return await client.post(
        "/api/health/webhook/activity", headers={"X-Webhook-Token": "activity-test"},
        json={"profile_alias": alias, "steps": steps, "active_calories": calories},
    )


def auth(user_id):
    return {"Authorization": f"Bearer {create_access_token(user_id)}"}


@pytest.mark.asyncio
async def test_upsert_retries_lower_samples_profiles_and_next_day(client, store, monkeypatch):
    first = await post(client)
    assert first.status_code == 200
    row_id = first.json()["id"]
    assert (await post(client, steps=4500, calories=150)).json()["id"] == row_id
    assert (await post(client, steps=4500, calories=150)).json()["id"] == row_id
    old = (await post(client, steps=2000, calories=0)).json()
    assert old["steps"] == 4500 and old["active_calories"] == 150
    assert (await post(client, alias="Вика", steps=1200, calories=0)).json()["user_id"] == 2
    monkeypatch.setattr(health, "local_today", lambda: DAY + timedelta(days=1))
    next_day = (await post(client, steps=100, calories=0)).json()
    assert next_day["id"] != row_id and next_day["steps"] == 100
    assert store.session.scalar(select(func.count(ActivityLog.id))) == 3
    sql = str(store.statements[1].compile(dialect=postgresql.dialect()))
    assert "ON CONFLICT (user_id, date) DO UPDATE" in sql
    assert "greatest(activity_logs.steps, excluded.steps)" in sql


@pytest.mark.asyncio
@pytest.mark.parametrize("token,expected", [(None, 401), ("wrong", 401), ("activity-test", 200)])
async def test_webhook_authentication(client, token, expected):
    headers = {"X-Webhook-Token": token} if token else {}
    response = await client.post("/api/health/webhook/activity", headers=headers,
                                 json={"profile_alias": "admin", "steps": 0})
    assert response.status_code == expected
    if expected == 200:
        assert response.json()["active_calories"] == 0


@pytest.mark.asyncio
async def test_total_energy_precision_retries_and_legacy_sender(client, store):
    async def total(value):
        return await client.post('/api/health/webhook/activity', headers={'X-Webhook-Token': 'activity-test'},
                                 json={'profile_alias': 'admin', 'steps': 6202, 'total_calories': value})
    first = await total(1850.72)
    assert first.status_code == 200 and first.json()['total_calories'] == 1850.72
    assert first.json()['active_calories'] == 0
    assert (await total(1800)).json()['total_calories'] == 1850.72
    assert (await total(1850.72)).json()['id'] == first.json()['id']
    # An old sender without total energy must not erase the new source.
    assert (await post(client, steps=7000, calories=0)).json()['total_calories'] == 1850.72
    assert store.session.scalar(select(ActivityLog)).total_calories == Decimal('1850.72')
    assert store.session.scalar(select(func.count(ActivityLog.id))) == 1


@pytest.mark.asyncio
async def test_webhook_disabled_and_profiles(client, store, monkeypatch):
    monkeypatch.setattr(health.settings, "HEALTH_WEBHOOK_TOKEN", None)
    assert (await post(client)).status_code == 503
    monkeypatch.setattr(health.settings, "HEALTH_WEBHOOK_TOKEN", "activity-test")
    assert (await post(client, alias="ghost")).status_code == 404
    assert (await post(client, alias="user")).status_code == 409
    assert (await post(client, alias=" Вика ")).json()["user_id"] == 2
    assert store.session.scalar(select(func.count(ActivityLog.id))) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [
    {"steps": -1}, {"steps": 3.5}, {"steps": True}, {"steps": "unknown"},
    {"steps": 2147483648}, {"active_calories": -1}, {"active_calories": None},
    {"total_calories": -1}, {"total_calories": "NaN"}, {"total_calories": 1850.721},
    {"total_calories": True}, {"total_calories": 2147483648},
    {"profile_alias": "  "}, {"profile_alias": "x" * 101},
])
async def test_invalid_payload_does_not_write(client, store, change):
    body = {"profile_alias": "admin", "steps": 10, **change}
    response = await client.post("/api/health/webhook/activity", headers={"X-Webhook-Token": "activity-test"}, json=body)
    assert response.status_code == 422
    assert store.session.scalar(select(func.count(ActivityLog.id))) == 0


@pytest.mark.asyncio
async def test_read_activity_is_private_and_today_has_no_stale_data(client, store):
    await post(client, alias="admin", steps=6000)
    await post(client, alias="Вика", steps=0, calories=0)
    store.session.add(ActivityLog(user_id=2, date=DAY - timedelta(days=1), steps=9000))
    store.session.commit()
    assert (await client.get("/api/health/activity/today")).status_code == 401
    partner = await client.get("/api/health/activity/today", headers=auth(2))
    assert partner.json() == {"date": DAY.isoformat(), "profiles_count": 1, "steps": 0, "active_calories": 0}
    no_data = (await client.get("/api/health/activity/today", headers=auth(3))).json()
    assert no_data["profiles_count"] == 0 and no_data["steps"] is None
    for path in ("/api/health/activity", "/api/health/activity/today"):
        assert (await client.get(path + "?target_user_id=1", headers=auth(2))).status_code == 403
        assert (await client.get(path + "?all_users=true", headers=auth(2))).status_code == 403
    history = (await client.get("/api/health/activity", headers=auth(2))).json()
    assert len(history) == 2 and all(row["user_id"] == 2 for row in history)
    assert (await client.get("/api/health/activity/today?target_user_id=2", headers=auth(1))).json()["steps"] == 0
    assert (await client.get("/api/health/activity/today?all_users=true", headers=auth(1))).json()["steps"] == 6000


@pytest.mark.asyncio
async def test_nudge_only_today_below_threshold_and_private(store, monkeypatch):
    store.session.add_all([
        ActivityLog(user_id=1, date=DAY, steps=5000),
        ActivityLog(user_id=2, date=DAY, steps=3200),
        ActivityLog(user_id=3, date=DAY - timedelta(days=1), steps=100),
    ])
    store.session.commit()
    send = AsyncMock(return_value=True)
    monkeypatch.setattr(activity, "async_session_maker", lambda: store)
    monkeypatch.setattr(activity, "send_telegram_message", send)
    monkeypatch.setattr(activity.settings, "ACTIVITY_NUDGE_ENABLED", False)
    await activity.send_activity_nudges()
    send.assert_not_called()
    monkeypatch.setattr(activity.settings, "ACTIVITY_NUDGE_ENABLED", True)
    monkeypatch.setattr(activity.settings, "ACTIVITY_NUDGE_STEPS", 5000)
    await activity.send_activity_nudges()
    assert send.call_count == 1 and send.call_args.args[0] == 222
    assert "3200" in send.call_args.args[1]
    send.reset_mock()
    monkeypatch.setattr(activity.settings, "ACTIVITY_NUDGE_STEPS", 3000)
    await activity.send_activity_nudges()
    send.assert_not_called()


@pytest.mark.asyncio
async def test_bot_only_own_today_and_missing_data(store, monkeypatch):
    from app.bot.handlers import health as handler
    monkeypatch.setattr(handler, "local_today", lambda: DAY)
    store.session.add_all([
        ActivityLog(user_id=1, date=DAY, steps=9999),
        ActivityLog(user_id=2, date=DAY, steps=3200, active_calories=100),
    ])
    store.session.commit()
    message = SimpleNamespace(from_user=SimpleNamespace(id=222), answer=AsyncMock())
    await handler.cmd_activity(message, store)
    text = message.answer.call_args.args[0]
    assert "3200" in text and "100 ккал" in text and "9999" not in text
    message.from_user.id = 333
    await handler.cmd_activity(message, store)
    assert "пока нет" in message.answer.call_args.args[0]


@pytest.mark.asyncio
async def test_scheduler_opt_in_20_local_time(monkeypatch):
    from app.finance.services import scheduler as jobs
    monkeypatch.setattr(jobs, "fatsecret_configured", lambda: False)
    monkeypatch.setattr(jobs.settings, "ACTIVITY_TZ", "Europe/Moscow")
    for enabled in (False, True):
        monkeypatch.setattr(jobs.settings, "ACTIVITY_NUDGE_ENABLED", enabled)
        scheduler = jobs.setup_scheduler(SimpleNamespace())
        try:
            job = scheduler.get_job("activity_nudge_job")
            if not enabled:
                assert job is None
            else:
                now = datetime(2026, 11, 16, 16, 59, tzinfo=timezone.utc)
                fire = job.trigger.get_next_fire_time(None, now)
                assert fire.hour == 20 and fire.minute == 0
                assert fire.astimezone(timezone.utc).hour == 17
        finally:
            scheduler.shutdown(wait=False)


def test_local_day_uses_activity_timezone(monkeypatch):
    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz):
            return datetime(2026, 11, 16, 22, 30, tzinfo=timezone.utc).astimezone(tz)

    monkeypatch.setattr(activity, "datetime", FixedDatetime)
    monkeypatch.setattr(activity.settings, "ACTIVITY_TZ", "Europe/Moscow")
    assert activity.local_today() == date(2026, 11, 17)
