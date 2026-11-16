"""Conditional body-fat goal date, preserving lean mass (the model requested in the spec)."""
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_CEILING
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_async_session
from app.core.security import build_scope, get_current_user
from app.health.activity import local_today
from app.models import HealthMetric, User

router = APIRouter(prefix='/api/v1/health', tags=['Body fat forecast'])


class AbsForecastRequest(BaseModel):
    user_id: Optional[int] = Field(default=None, gt=0)


class AbsForecastResponse(BaseModel):
    current_weight: Optional[float] = None
    current_body_fat: Optional[float] = None
    target_body_fat: float
    avg_daily_deficit: int
    deficit_source: Literal['configured'] = 'configured'
    target_weight: Optional[float] = None
    fat_mass: Optional[float] = None
    lean_body_mass: Optional[float] = None
    fat_to_lose: Optional[float] = None
    total_deficit_needed: Optional[float] = None
    days_left: Optional[int] = Field(default=None, ge=0)
    target_date: Optional[date] = None
    metric_at: Optional[datetime] = None
    status: Literal['in_progress', 'achieved', 'missing_metrics', 'no_deficit', 'horizon_exceeded']
    warnings: list[str] = Field(default_factory=list)
    model_note: str = 'Сухая масса постоянна; 7700 ккал/кг; дефицит постоянен. Видимость пресса не гарантируется.'


def calculate_abs_date(user: User, latest_metrics: Optional[HealthMetric], today: Optional[date] = None) -> AbsForecastResponse:
    today = today or local_today()
    target = Decimal(str(user.target_body_fat))
    deficit = user.avg_daily_deficit
    if not target.is_finite() or not 0 < target < 100:
        raise ValueError('Целевой процент жира должен быть между 0 и 100')
    result = AbsForecastResponse(target_body_fat=float(target), avg_daily_deficit=deficit, status='missing_metrics')
    if user.gender == 'female' and target <= 12:
        result.warnings.append('10–12% не является универсальной целью для женщин; согласуйте индивидуальную цель со специалистом.')
    if latest_metrics is None or latest_metrics.body_fat is None:
        return result
    weight, body_fat = Decimal(str(latest_metrics.weight_kg)), Decimal(str(latest_metrics.body_fat))
    if not weight.is_finite() or weight <= 0 or not body_fat.is_finite() or not 0 < body_fat < 100:
        raise ValueError('Некорректные вес или процент жира')
    result.current_weight, result.current_body_fat = float(weight), float(body_fat)
    result.metric_at = latest_metrics.timestamp
    fat_mass = weight * body_fat / 100
    lean_mass = weight - fat_mass
    target_weight = lean_mass / (1 - target / 100)
    fat_to_lose = max(Decimal(0), weight - target_weight)
    total_deficit = fat_to_lose * 7700
    result.fat_mass, result.lean_body_mass = round(float(fat_mass), 2), round(float(lean_mass), 2)
    result.target_weight = round(float(target_weight), 2)
    result.fat_to_lose, result.total_deficit_needed = round(float(fat_to_lose), 2), round(float(total_deficit), 2)
    if target >= body_fat:
        result.status, result.days_left, result.target_date = 'achieved', 0, today
        return result
    if deficit <= 0:
        result.status = 'no_deficit'
        return result
    # Round up: a partial day must not claim an earlier completion date.
    days = int((total_deficit / Decimal(deficit)).to_integral_value(rounding=ROUND_CEILING))
    result.days_left = days
    if days > (date.max - today).days:
        result.status = 'horizon_exceeded'
        return result
    result.status, result.target_date = 'in_progress', today + timedelta(days=days)
    if days > 365:
        result.warnings.append('Горизонт больше года: линейная оценка особенно неопределенна.')
    return result


async def get_abs_forecast(session: AsyncSession, user: User) -> AbsForecastResponse:
    from datetime import timezone
    from zoneinfo import ZoneInfo
    from app.core.config import settings
    now = datetime.now(timezone.utc)
    # Weight and fat must come from the SAME measurement. Never mix two dates.
    metric = (await session.execute(select(HealthMetric).where(
        HealthMetric.user_id == user.id, HealthMetric.body_fat.is_not(None), HealthMetric.timestamp <= now
    ).order_by(HealthMetric.timestamp.desc(), HealthMetric.id.desc()).limit(1))).scalar_one_or_none()
    result = calculate_abs_date(user, metric)
    if metric:
        ts = metric.timestamp.replace(tzinfo=timezone.utc) if metric.timestamp.tzinfo is None else metric.timestamp
        result.metric_at = ts.astimezone(ZoneInfo(settings.ACTIVITY_TZ))
        day = ts.astimezone(ZoneInfo(settings.ACTIVITY_TZ)).date()
        if (local_today() - day).days > 14:
            result.warnings.append('Замер состава тела старше 14 дней; обновите вес и процент жира.')
    return result


@router.get('/forecast-abs', response_model=AbsForecastResponse)
async def forecast_abs(user_id: Optional[int] = Query(default=None, gt=0),
                       current_user: User = Depends(get_current_user),
                       session: AsyncSession = Depends(get_async_session)) -> AbsForecastResponse:
    scope = build_scope(current_user, target_user_id=user_id)
    user = await session.get(User, scope.owner_id)
    if user is None:
        raise HTTPException(404, 'Пользователь не найден')
    return await get_abs_forecast(session, user)


def format_abs_forecast(result: AbsForecastResponse) -> str:
    lines = ['💪 <b>Твой фитнес-прогноз (Life OS)</b>']
    if result.status == 'missing_metrics':
        lines.append('Нужен замер веса вместе с процентом жира. Добавьте его на странице «Здоровье» или передайте с весов через Home Assistant.')
    else:
        lines += [f'Вес в замере: <b>{result.current_weight:.2f} кг</b> · Жир: {result.current_body_fat:g}%',
                  f'Цель: {result.target_body_fat:g}% жира · Расчетный вес: <b>{result.target_weight:.2f} кг</b>']
        if result.metric_at:
            lines.append(f'Дата исходного замера: {result.metric_at:%d.%m.%Y}')
        if result.status == 'achieved':
            lines.append('✅ Цель уже достигнута по указанному проценту жира.')
        elif result.status == 'no_deficit':
            lines.append('Дата не рассчитывается: заданный дефицит не положителен.')
        elif result.status == 'horizon_exceeded':
            lines.append('Срок выходит за поддерживаемый календарный диапазон.')
        else:
            lines += [f'Заданный дефицит: {result.avg_daily_deficit} ккал/день.',
                      f'При сохранении этих условий: <b>{result.days_left} дней</b>.',
                      f'📅 Условная дата: <b>{result.target_date:%d.%m.%Y}</b>']
    lines.extend('⚠️ ' + warning for warning in result.warnings)
    lines.append('Модель предполагает сохранение сухой массы. Погрешность весов и изменение расхода влияют на срок. Процент жира не гарантирует видимый пресс.')
    return '\n'.join(lines)
