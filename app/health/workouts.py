"""Async workout queries, formatting and the morning reminder."""
from html import escape

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.database import async_session_maker
from app.core.security import DataScope
from app.health.activity import local_today
from app.models import User, Workout
from app.notifications.sender import send_telegram_message


async def find_workout(session: AsyncSession, workout_id: int, scope: DataScope) -> Workout | None:
    return (await session.execute(scope.apply(
        select(Workout).where(Workout.id == workout_id).options(selectinload(Workout.exercises)), Workout
    ))).scalar_one_or_none()


async def complete_workout(session: AsyncSession, workout_id: int, scope: DataScope) -> tuple[Workout | None, bool]:
    workout = await find_workout(session, workout_id, scope)
    if workout is None:
        return None, False
    changed = not workout.is_completed
    workout.is_completed = True
    await session.commit()
    return workout, changed


async def today_workouts(session: AsyncSession, user_id: int) -> list[Workout]:
    return list((await session.execute(select(Workout).where(
        Workout.user_id == user_id, Workout.date == local_today()
    ).options(selectinload(Workout.exercises)).order_by(Workout.id))).scalars().all())


def format_workout(workout: Workout, morning: bool = False) -> str:
    heading = '💪 Доброе утро! Сегодня по плану' if morning else '💪 Тренировка на сегодня'
    lines = [f'{heading}: <b>{escape(workout.title)}</b>',
             f'📅 {workout.date:%d.%m.%Y}',
             '✅ Выполнено' if workout.is_completed else 'Упражнения:']
    for exercise in workout.exercises:
        weight = f' · {exercise.weight_kg:g} кг' if exercise.weight_kg is not None else ''
        lines.append(f'▫️ {escape(exercise.name)} — {exercise.sets} × {escape(exercise.reps)}{weight}')
    return '\n'.join(lines)


async def send_morning_workout_plan() -> None:
    today = local_today()
    async with async_session_maker() as session:
        rows = (await session.execute(select(Workout, User.telegram_id).join(User, Workout.user_id == User.id)
            .where(Workout.date == today, Workout.is_completed.is_(False),
                   or_(Workout.morning_notified_on.is_(None), Workout.morning_notified_on != today))
            .options(selectinload(Workout.exercises)).order_by(Workout.user_id, Workout.id)
            .with_for_update(of=Workout, skip_locked=True))).all()
        for workout, telegram_id in rows:
            if await send_telegram_message(telegram_id, format_workout(workout, morning=True)):
                workout.morning_notified_on = today
        await session.commit()
