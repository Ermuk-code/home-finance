from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Literal, Optional
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import get_async_session
from app.core.security import DataScope, get_current_user, get_scope
from app.health.activity import local_today
from app.health.analytics import HealthAnalytics
from app.health.planning import build_plan
from app.models import BodyMeasurement, User, WeightLog
from app.users.router import PhysicalParamsSchema

router = APIRouter(prefix='/api/health', tags=['Health dashboard'])


class HealthProfile(PhysicalParamsSchema):
    target_body_fat: Decimal = Field(default=Decimal('12'), gt=0, lt=100, decimal_places=1)
    avg_daily_deficit: int = Field(default=500, strict=True, ge=0, le=2000)
    health_goal: Literal['maintain', 'lose'] = 'maintain'
    deficit_percent: int = Field(default=15, strict=True, ge=0, le=20)
    goal_weight_kg: Optional[Decimal] = Field(default=None, ge=30, le=300)
    activity_factor: Decimal = Field(default=Decimal('1.2'), ge=Decimal('1.2'), le=Decimal('2.4'))


class MeasurementPayload(BaseModel):
    date: date
    height_cm: Optional[Decimal] = Field(default=None, ge=50, le=260, decimal_places=1)
    waist_cm: Optional[Decimal] = Field(default=None, ge=30, le=250, decimal_places=1)
    weight_kg: Optional[Decimal] = Field(default=None, gt=20, le=400, decimal_places=2)
    body_fat: Optional[Decimal] = Field(default=None, gt=0, lt=100, decimal_places=1)

    @model_validator(mode='after')
    def valid_measurement(self):
        if self.date > local_today():
            raise ValueError('Нельзя записать замер в будущем')
        if self.body_fat is not None and self.weight_kg is None:
            raise ValueError('Процент жира нужно передавать вместе с весом того же замера')
        if all(getattr(self, field) is None for field in ('height_cm', 'waist_cm', 'weight_kg')):
            raise ValueError('Введите хотя бы один замер')
        return self


def serialize_profile(user):
    return {'id': user.id, 'name': user.name, 'gender': user.gender, 'height_cm': user.height_cm,
            'birth_date': user.birth_date.isoformat() if user.birth_date else None,
            'health_goal': user.health_goal, 'deficit_percent': user.deficit_percent,
            'goal_weight_kg': float(user.goal_weight_kg) if user.goal_weight_kg else None,
            'activity_factor': float(user.activity_factor), 'target_body_fat': float(user.target_body_fat),
            'avg_daily_deficit': user.avg_daily_deficit}


def single_profile(scope):
    if scope.filter_id is None:
        raise HTTPException(400, 'Выберите один профиль для изменения и расчета')


@router.get('/profiles')
async def profiles(user: User = Depends(get_current_user), session: AsyncSession = Depends(get_async_session)):
    stmt = select(User).order_by(User.id)
    if user.role != 'admin':
        stmt = stmt.where(User.id == user.id)
    return [serialize_profile(u) for u in (await session.execute(stmt)).scalars().all()]


@router.patch('/profile')
async def save_profile(payload: HealthProfile, scope: DataScope = Depends(get_scope),
                       session: AsyncSession = Depends(get_async_session)):
    single_profile(scope)
    user = await session.get(User, scope.owner_id)
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(user, key, value)
    await session.commit()
    return serialize_profile(user)


@router.post('/measurements', status_code=201)
async def save_measurement(payload: MeasurementPayload, scope: DataScope = Depends(get_scope),
                           session: AsyncSession = Depends(get_async_session)):
    single_profile(scope)
    values = {key: getattr(payload, key) for key in ('height_cm', 'waist_cm') if getattr(payload, key) is not None}
    if values:
        stmt = insert(BodyMeasurement).values(user_id=scope.owner_id, date=payload.date, **values)
        stmt = stmt.on_conflict_do_update(index_elements=['user_id', 'date'], set_=values)
        await session.execute(stmt)
    if payload.weight_kg is not None:
        ts = datetime.now(ZoneInfo(settings.ACTIVITY_TZ)) if payload.date == local_today() else datetime.combine(
            payload.date, time(12), tzinfo=ZoneInfo(settings.ACTIVITY_TZ))
        session.add(WeightLog(user_id=scope.owner_id, weight_kg=payload.weight_kg, body_fat=payload.body_fat, timestamp=ts))
    await session.commit()
    return {'status': 'ok', 'user_id': scope.owner_id, 'date': payload.date.isoformat()}


@router.get('/dashboard')
async def dashboard(start_date: Optional[date] = Query(default=None), end_date: Optional[date] = Query(default=None),
                    scope: DataScope = Depends(get_scope), session: AsyncSession = Depends(get_async_session)):
    single_profile(scope)
    today = local_today()
    end = end_date or today
    start = start_date or end - timedelta(days=27)
    service = HealthAnalytics(session)
    try:
        summary = await service.get_summary(scope.owner_id, start, end)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    planning_summary = summary if (start, end) == (today - timedelta(days=27), today) else await service.get_summary(
        scope.owner_id, today - timedelta(days=27), today)
    user = await session.get(User, scope.owner_id)
    tz = ZoneInfo(settings.ACTIVITY_TZ)
    end_ts = datetime.combine(today + timedelta(days=1), time.min, tzinfo=tz)
    weights = list((await session.execute(select(WeightLog).where(
        WeightLog.user_id == user.id, WeightLog.timestamp < end_ts).order_by(WeightLog.timestamp, WeightLog.id)
    )).scalars().all())
    measurements = list((await session.execute(select(BodyMeasurement).where(
        BodyMeasurement.user_id == user.id, BodyMeasurement.date <= today).order_by(BodyMeasurement.date)
    )).scalars().all())
    plan = build_plan(user, planning_summary, weights, measurements, today)
    def local_day(ts):
        return (ts.replace(tzinfo=timezone.utc) if ts.tzinfo is None else ts).astimezone(tz).date()
    return {'profile': serialize_profile(user), 'summary': summary, 'plan': plan,
            'measurements': [{'date': m.date.isoformat(), 'height_cm': float(m.height_cm) if m.height_cm else None,
                              'waist_cm': float(m.waist_cm) if m.waist_cm else None}
                             for m in measurements if start <= m.date <= end],
            'weights': [{'date': local_day(w.timestamp).isoformat(), 'timestamp': w.timestamp.isoformat(),
                         'weight_kg': float(w.weight_kg), 'body_fat': float(w.body_fat) if w.body_fat is not None else None}
                        for w in weights if start <= local_day(w.timestamp) <= end]}
