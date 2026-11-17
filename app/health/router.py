"""Здоровье: вес, питание, активность. Данные привязаны к users.id и фильтруются по DataScope."""
import logging
import secrets
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from html import escape
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Path, Query, status
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import settings
from app.core.database import get_async_session
from app.core.security import DataScope, get_current_user, get_scope
from app.health import fatsecret_client
from app.health.activity import local_today, upsert_activity
from app.health.analytics import HealthAnalytics
from app.health.nutrition_sync import sync_user
from app.health.schemas import ActivityWebhookPayload, WeightCreateSchema, WeightWebhookPayload
from app.health.schemas import WorkoutCreate, WorkoutRead
from app.health.workouts import complete_workout, find_workout
from app.models import ROLE_ADMIN, ROLES, ActivityLog, DailyNutrition, User, WeightLog, Workout, WorkoutExercise
from app.notifications.sender import send_telegram_message

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/health", tags=["Health"])


@router.get('/workouts', response_model=list[WorkoutRead])
async def list_workouts(start_date: Optional[date] = Query(default=None), end_date: Optional[date] = Query(default=None),
                        scope: DataScope = Depends(get_scope), session: AsyncSession = Depends(get_async_session)):
    today = local_today()
    start = start_date or (end_date - timedelta(days=6) if end_date else today - timedelta(days=today.weekday()))
    end = end_date or start + timedelta(days=6)
    if end < start or (end - start).days > 365:
        raise HTTPException(422, 'Период должен содержать от 1 до 366 дней')
    stmt = scope.apply(select(Workout).where(Workout.date.between(start, end)), Workout)
    return (await session.execute(stmt.options(selectinload(Workout.exercises)).order_by(Workout.date, Workout.id))).scalars().all()


@router.get('/workouts/next', response_model=Optional[WorkoutRead])
async def next_workout(scope: DataScope = Depends(get_scope), session: AsyncSession = Depends(get_async_session)):
    stmt = scope.apply(select(Workout).where(Workout.date >= local_today(), Workout.is_completed.is_(False)), Workout)
    return (await session.execute(stmt.options(selectinload(Workout.exercises)).order_by(Workout.date, Workout.id).limit(1))).scalar_one_or_none()


@router.post('/workouts', response_model=WorkoutRead, status_code=201)
async def create_workout(payload: WorkoutCreate, scope: DataScope = Depends(get_scope),
                         session: AsyncSession = Depends(get_async_session)):
    if scope.filter_id is None:
        raise HTTPException(400, 'Выберите один профиль для создания тренировки')
    workout = Workout(user_id=scope.owner_id, date=payload.date, title=payload.title,
                      exercises=[WorkoutExercise(**e.model_dump()) for e in payload.exercises])
    session.add(workout)
    await session.commit()
    return workout


@router.patch('/workouts/{workout_id}/complete', response_model=WorkoutRead)
async def mark_workout_complete(workout_id: int = Path(gt=0, le=2147483647), scope: DataScope = Depends(get_scope),
                                session: AsyncSession = Depends(get_async_session)):
    workout, _ = await complete_workout(session, workout_id, scope)
    if workout is None:
        raise HTTPException(404, 'Тренировка не найдена')
    return workout


@router.delete('/workouts/{workout_id}', status_code=204)
async def delete_workout(workout_id: int = Path(gt=0, le=2147483647), scope: DataScope = Depends(get_scope),
                         session: AsyncSession = Depends(get_async_session)):
    workout = await find_workout(session, workout_id, scope)
    if workout is None:
        raise HTTPException(404, 'Тренировка не найдена')
    await session.delete(workout)
    await session.commit()


@router.get("/summary")
async def health_summary(
    start_date: Optional[date] = Query(default=None),
    end_date: Optional[date] = Query(default=None),
    scope: DataScope = Depends(get_scope),
    session: AsyncSession = Depends(get_async_session),
) -> Dict[str, Any]:
    if scope.filter_id is None:
        raise HTTPException(status_code=400, detail="Выберите один профиль: расчеты индивидуальны.")
    end = end_date or local_today()
    start = start_date or end - timedelta(days=6)
    try:
        return await HealthAnalytics(session).get_summary(scope.owner_id, start, end)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


def _serialize_weight(w: WeightLog) -> Dict[str, Any]:
    return {
        "id": w.id,
        "user_id": w.user_id,
        "weight_kg": float(w.weight_kg),
        "bmi": float(w.bmi) if w.bmi is not None else None,
        "body_fat": float(w.body_fat) if w.body_fat is not None else None,
        "timestamp": w.timestamp.isoformat(),
    }


async def _save_weight(session: AsyncSession, user_id: int, payload: WeightCreateSchema) -> WeightLog:
    ts = payload.timestamp or datetime.now(timezone.utc)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    log = WeightLog(
        user_id=user_id,
        weight_kg=payload.weight_kg.quantize(Decimal("0.01")),
        bmi=payload.bmi.quantize(Decimal("0.1")) if payload.bmi is not None else None,
        body_fat=payload.body_fat,
        timestamp=ts,
    )
    session.add(log)
    await session.commit()
    await session.refresh(log)
    return log


# ---------------------------------------------------------------------------
# Веб / API для пользователей
# ---------------------------------------------------------------------------
@router.post("/weight", status_code=status.HTTP_201_CREATED, dependencies=[Depends(get_current_user)])
async def add_weight(
    payload: WeightCreateSchema,
    scope: DataScope = Depends(get_scope),
    session: AsyncSession = Depends(get_async_session),
) -> Dict[str, Any]:
    """Записать вес (себе; admin — другому пользователю через target_user_id)."""
    log = await _save_weight(session, scope.owner_id, payload)
    return {"status": "ok", **_serialize_weight(log)}


@router.get("/weight", dependencies=[Depends(get_current_user)])
async def list_weight(
    limit: int = Query(default=100, ge=1, le=1000),
    scope: DataScope = Depends(get_scope),
    session: AsyncSession = Depends(get_async_session),
) -> List[Dict[str, Any]]:
    """История веса: только свои записи (admin — свои, чужие или все)."""
    stmt = scope.apply(select(WeightLog), WeightLog).order_by(WeightLog.timestamp.desc(), WeightLog.id.desc()).limit(limit)
    return [_serialize_weight(w) for w in (await session.execute(stmt)).scalars().all()]


@router.get("/nutrition", dependencies=[Depends(get_current_user)])
async def list_nutrition(
    limit: int = Query(default=60, ge=1, le=366),
    scope: DataScope = Depends(get_scope),
    session: AsyncSession = Depends(get_async_session),
) -> List[Dict[str, Any]]:
    stmt = scope.apply(select(DailyNutrition), DailyNutrition).order_by(DailyNutrition.date.desc()).limit(limit)
    return [
        {
            "id": n.id,
            "user_id": n.user_id,
            "date": n.date.isoformat(),
            "calories": n.calories,
            "protein": float(n.protein),
            "fat": float(n.fat),
            "carbs": float(n.carbs),
        }
        for n in (await session.execute(stmt)).scalars().all()
    ]


@router.get("/activity", dependencies=[Depends(get_current_user)])
async def list_activity(
    limit: int = Query(default=60, ge=1, le=366),
    scope: DataScope = Depends(get_scope),
    session: AsyncSession = Depends(get_async_session),
) -> List[Dict[str, Any]]:
    stmt = scope.apply(select(ActivityLog), ActivityLog).order_by(ActivityLog.date.desc()).limit(limit)
    return [
        {
            "id": a.id,
            "user_id": a.user_id,
            "date": a.date.isoformat(),
            "steps": a.steps,
            "active_calories": a.active_calories,
            "total_calories": float(a.total_calories) if a.total_calories is not None else None,
        }
        for a in (await session.execute(stmt)).scalars().all()
    ]


@router.get("/activity/today", dependencies=[Depends(get_current_user)])
async def activity_today(
    scope: DataScope = Depends(get_scope),
    session: AsyncSession = Depends(get_async_session),
) -> Dict[str, Any]:
    """Итоги сегодня; отсутствие телеметрии отличается от нулевой активности."""
    day = local_today()
    stmt = scope.apply(
        select(func.count(ActivityLog.id), func.sum(ActivityLog.steps), func.sum(ActivityLog.active_calories))
        .where(ActivityLog.date == day),
        ActivityLog,
    )
    count, steps, calories = (await session.execute(stmt)).one()
    return {"date": day.isoformat(), "profiles_count": count, "steps": steps, "active_calories": calories}


# ---------------------------------------------------------------------------
# Вебхук Home Assistant
# ---------------------------------------------------------------------------
def _check_webhook_token(token: Optional[str]) -> None:
    expected = settings.HEALTH_WEBHOOK_TOKEN
    if not expected:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Вебхуки отключены: не задан HEALTH_WEBHOOK_TOKEN.")
    if not token or not secrets.compare_digest(token.encode("utf-8"), expected.encode("utf-8")):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Неверный токен вебхука.")


async def _resolve_profile(session: AsyncSession, alias: str) -> User:
    """profile_alias -> пользователь: роль (admin/user), затем имя (без учёта регистра), затем id / telegram_id."""
    alias = alias.strip()
    low = alias.lower()

    candidates: List[User] = []
    if low in ROLES:
        candidates = list((await session.execute(select(User).where(User.role == low))).scalars().all())
    if not candidates:
        candidates = list((await session.execute(select(User).where(func.lower(User.name) == low))).scalars().all())
    if len(candidates) == 1:
        return candidates[0]
    if len(candidates) > 1:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Профиль «{alias}» не уникален: укажите имя, id или telegram_id.",
        )

    if alias.isdigit():
        number = int(alias)
        user = (
            await session.execute(select(User).where((User.telegram_id == number) | (User.id == number)))
        ).scalars().first()
        if user:
            return user
    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Профиль «{alias}» не найден.")


@router.post("/webhook/activity", status_code=status.HTTP_200_OK)
async def activity_webhook(
    payload: ActivityWebhookPayload,
    x_webhook_token: Optional[str] = Header(default=None),
    session: AsyncSession = Depends(get_async_session),
) -> Dict[str, Any]:
    """Накопительные шаги, активные и/или общие ккал за текущий день; типы не смешиваются."""
    _check_webhook_token(x_webhook_token)
    user = await _resolve_profile(session, payload.profile_alias)
    log = await upsert_activity(session, user.id, local_today(), payload.steps, payload.active_calories, payload.total_calories)
    return {
        "status": "ok", "id": log.id, "user_id": log.user_id, "date": log.date.isoformat(),
        "steps": log.steps, "active_calories": log.active_calories,
        "total_calories": float(log.total_calories) if log.total_calories is not None else None,
    }


@router.post("/webhook/weight", status_code=status.HTTP_200_OK)
async def weight_webhook(
    payload: WeightWebhookPayload,
    x_webhook_token: Optional[str] = Header(default=None),
    session: AsyncSession = Depends(get_async_session),
) -> Dict[str, Any]:
    """Home Assistant присылает вес с идентификатором профиля (profile_alias=admin | user).

    Запись сохраняется за найденным пользователем (weight_logs.user_id), пуш уходит только в его личный чат.
    """
    _check_webhook_token(x_webhook_token)
    user = await _resolve_profile(session, payload.profile_alias)

    previous = (
        await session.execute(
            select(WeightLog.weight_kg)
            .where(WeightLog.user_id == user.id)
            .order_by(WeightLog.timestamp.desc(), WeightLog.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    log = await _save_weight(session, user.id, payload)
    logger.info("Вебхук веса: user_id=%s (%s) -> %s кг", user.id, payload.profile_alias, log.weight_kg)

    notified = await _notify_weight(session, user, log, previous)
    return {"status": "ok", "notified": notified, **_serialize_weight(log)}


async def _notify_weight(session: AsyncSession, user: User, log: WeightLog, previous: Optional[Decimal]) -> bool:
    """Пуш владельцу замера; опционально — дайджест админу без цифр. Ошибки Telegram вебхук не роняют."""
    text = f"⚖️ <b>Новый вес: {log.weight_kg:.1f} кг</b>"
    if previous is not None:
        delta = log.weight_kg - previous
        text += f" ({'+' if delta > 0 else ''}{delta:.1f} кг к прошлому замеру)"
    if log.bmi is not None:
        text += f"\nИМТ: {log.bmi}"
    delivered = await send_telegram_message(user.telegram_id, text)

    # Привилегия admin: знать, что партнёр взвесился (сам вес не раскрывается)
    if settings.WEIGHT_ADMIN_DIGEST and user.role != ROLE_ADMIN:
        admins = (
            await session.execute(select(User).where(User.role == ROLE_ADMIN, User.id != user.id))
        ).scalars().all()
        for admin in admins:
            await send_telegram_message(
                admin.telegram_id, f"👀 {escape(user.name)} только что взвесился(-ась), замер записан."
            )
    return delivered


# ---------------------------------------------------------------------------
# FatSecret: привязка аккаунта (OAuth 1.0a, redirect URI)
# ---------------------------------------------------------------------------
def _fatsecret_page(title: str, message: str, ok: bool) -> HTMLResponse:
    color = "#16a34a" if ok else "#dc2626"
    html = (
        "<!doctype html><html lang='ru'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"<title>{escape(title)}</title></head>"
        "<body style='font-family:system-ui,sans-serif;max-width:480px;margin:15vh auto;padding:0 16px;text-align:center'>"
        f"<h2 style='color:{color}'>{escape(title)}</h2><p>{escape(message)}</p></body></html>"
    )
    return HTMLResponse(html, status_code=200 if ok else 400)


@router.get("/fatsecret/callback", response_class=HTMLResponse)
async def fatsecret_callback(
    oauth_token: Optional[str] = Query(default=None),
    oauth_verifier: Optional[str] = Query(default=None),
    session: AsyncSession = Depends(get_async_session),
) -> HTMLResponse:
    """Redirect URI: FatSecret возвращает сюда пользователя с oauth_token и oauth_verifier.

    Пользователь определяется по oauth_token, выданному при /link_fatsecret (а не по параметру из URL),
    поэтому подставить чужой telegram_id нельзя.
    """
    if not oauth_token or not oauth_verifier:
        return _fatsecret_page("Доступ не выдан", "FatSecret не подтвердил авторизацию. Попробуйте /link_fatsecret ещё раз.", False)
    try:
        user_id, access_token, access_secret = await fatsecret_client.finish_link(oauth_token, oauth_verifier)
    except fatsecret_client.FatSecretError as e:
        logger.warning("FatSecret callback: %s", e)
        return _fatsecret_page("Не удалось привязать FatSecret", str(e), False)

    user = await session.get(User, user_id)
    if user is None:
        return _fatsecret_page("Не удалось привязать FatSecret", "Пользователь не найден.", False)
    user.fs_access_token = access_token
    user.fs_access_secret = access_secret
    await session.commit()
    logger.info("FatSecret привязан: user_id=%s", user.id)

    # Первая выгрузка сразу, чтобы увидеть результат; сбой не отменяет привязку
    try:
        await sync_user(session, user)
    except Exception as e:  # noqa: BLE001
        await session.rollback()
        logger.warning("FatSecret: первая выгрузка user_id=%s не удалась: %s", user.id, e)
    await send_telegram_message(user.telegram_id, "🍎 FatSecret привязан. Данные о питании будут подтягиваться автоматически. Команда /nutrition — итоги за сегодня.")
    return _fatsecret_page("FatSecret привязан", "Можно закрыть эту страницу и вернуться в Telegram.", True)
