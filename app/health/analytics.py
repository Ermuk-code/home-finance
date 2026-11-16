"""Read-only health estimates. All energy values are kcal, weight is kg."""
from datetime import date, datetime, time, timedelta
from html import escape
from statistics import mean
from zoneinfo import ZoneInfo
from types import SimpleNamespace

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.health.activity import local_today
from app.models import ActivityLog, BodyMeasurement, DailyNutrition, User, WeightLog


def age_on(birth: date, day: date) -> int:
    return day.year - birth.year - ((day.month, day.day) < (birth.month, birth.day))


def calculate_bmr(weight: float, height: int, age: int, gender: str) -> float:
    if gender not in ("male", "female") or weight <= 0 or height <= 0 or age < 18:
        raise ValueError("Для формулы нужны пол, положительные рост/вес и возраст от 18 лет")
    return 10 * float(weight) + 6.25 * float(height) - 5 * age + (5 if gender == "male" else -161)


def calculate_day(user, day, weight, nutrition, activity, today):
    missing = []
    for key in ("gender", "height_cm", "birth_date"):
        if getattr(user, key) is None:
            missing.append(key)
    age = age_on(user.birth_date, day) if user.birth_date else None
    if age is not None and age < 18:
        missing.append("adult_age")
    if weight is None:
        missing.append("weight")
    if nutrition is None:
        missing.append("nutrition")
    if activity is None:
        missing.append("activity")
    weight_kg = float(weight.weight_kg) if weight else None
    bmr = None
    if not any(key in missing for key in ("gender", "height_cm", "birth_date", "adult_age", "weight")):
        bmr = calculate_bmr(weight_kg, user.height_cm, age, user.gender)
    eaten = nutrition.calories if nutrition else None
    active = activity.active_calories if activity else None
    tef = eaten * 0.1 if eaten is not None else None
    tdee = bmr + active + tef if bmr is not None and active is not None and tef is not None else None
    balance = eaten - tdee if tdee is not None else None
    # Старый контракт активности использует 0 также для отсутствующего источника ккал.
    uncertain_activity = active == 0
    return {
        "date": day.isoformat(), "age": age, "weight_kg": weight_kg,
        "height_cm": float(user.height_cm) if user.height_cm else None,
        "protein": float(nutrition.protein) if nutrition else None,
        "fat": float(nutrition.fat) if nutrition else None,
        "carbs": float(nutrition.carbs) if nutrition else None,
        "weight_at": weight.timestamp.isoformat() if weight else None,
        "steps": activity.steps if activity else None, "calories": eaten, "active_calories": active,
        "bmr": round(bmr, 2) if bmr is not None else None,
        "tef": round(tef, 2) if tef is not None else None,
        "tdee": round(tdee, 2) if tdee is not None else None,
        "balance": round(balance, 2) if balance is not None else None,
        "missing": missing, "activity_calories_uncertain": uncertain_activity,
        "is_partial": day >= today,
        "complete": not missing and not uncertain_activity and day < today,
    }


class HealthAnalytics:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_daily_summary(self, user_id: int, day: date) -> dict:
        return (await self.get_summary(user_id, day, day))["days"][0]

    async def get_summary(self, user_id: int, start_date: date, end_date: date) -> dict:
        if end_date < start_date or (end_date - start_date).days > 365:
            raise ValueError("Период должен содержать от 1 до 366 дней")
        today = local_today()
        if end_date > today:
            raise ValueError("Конец периода не может быть в будущем")
        user = await self.session.get(User, user_id)
        if user is None:
            raise LookupError("Пользователь не найден")
        tz = ZoneInfo(settings.ACTIVITY_TZ)
        start = datetime.combine(start_date, time.min, tzinfo=tz)
        end = datetime.combine(end_date + timedelta(days=1), time.min, tzinfo=tz)
        # Один замер до начала периода и все замеры внутри. Будущие замеры не используются.
        baseline = (await self.session.execute(
            select(WeightLog).where(WeightLog.user_id == user_id, WeightLog.timestamp < start)
            .order_by(WeightLog.timestamp.desc(), WeightLog.id.desc()).limit(1)
        )).scalar_one_or_none()
        weights = list((await self.session.execute(
            select(WeightLog).where(WeightLog.user_id == user_id, WeightLog.timestamp >= start, WeightLog.timestamp < end)
            .order_by(WeightLog.timestamp, WeightLog.id)
        )).scalars().all())
        nutrition = {n.date: n for n in (await self.session.execute(
            select(DailyNutrition).where(DailyNutrition.user_id == user_id,
                                        DailyNutrition.date.between(start_date, end_date))
        )).scalars().all()}
        activity = {a.date: a for a in (await self.session.execute(
            select(ActivityLog).where(ActivityLog.user_id == user_id,
                                     ActivityLog.date.between(start_date, end_date))
        )).scalars().all()}
        measurements = list((await self.session.execute(
            select(BodyMeasurement).where(BodyMeasurement.user_id == user_id, BodyMeasurement.date <= end_date)
            .order_by(BodyMeasurement.date)
        )).scalars().all())
        days, index, weight = [], 0, baseline
        measure_index, height, waist = 0, user.height_cm, None
        for offset in range((end_date - start_date).days + 1):
            day = start_date + timedelta(days=offset)
            cutoff = datetime.combine(day + timedelta(days=1), time.min, tzinfo=tz)
            while index < len(weights):
                ts = weights[index].timestamp
                if ts.tzinfo is None:  # SQLite tests; PostgreSQL stores timezone-aware timestamps.
                    from datetime import timezone
                    ts = ts.replace(tzinfo=timezone.utc)
                if ts >= cutoff:
                    break
                weight = weights[index]
                index += 1
            while measure_index < len(measurements) and measurements[measure_index].date <= day:
                measure = measurements[measure_index]
                if measure.height_cm is not None:
                    height = measure.height_cm
                if measure.waist_cm is not None:
                    waist = float(measure.waist_cm)
                measure_index += 1
            physical = SimpleNamespace(gender=user.gender, birth_date=user.birth_date, height_cm=height)
            result = calculate_day(physical, day, weight, nutrition.get(day), activity.get(day), today)
            result['waist_cm'] = waist
            days.append(result)
        completed = [d for d in days if d["complete"]]
        averages, counts = {}, {}
        for key in ("steps", "calories", "protein", "fat", "carbs", "active_calories", "bmr", "tdee", "balance"):
            eligible = completed if key in ("tdee", "balance") else [d for d in days if not d["is_partial"]]
            values = [d[key] for d in eligible if d[key] is not None]
            counts[key] = len(values)
            averages[key] = round(mean(values), 2) if values else None
        known = [d for d in days if d["weight_kg"] is not None]
        first, last = (known[0], known[-1]) if known else (None, None)
        # Условная линейная модель 7700 ккал/кг; не прогноз воды/краткосрочных колебаний.
        forecast = round(averages["balance"] * 7 / 7700, 2) if len(completed) >= 3 else None
        return {
            "user_id": user.id, "name": user.name, "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(), "timezone": settings.ACTIVITY_TZ,
            "days": days, "averages": averages, "sample_days": counts, "complete_days": len(completed),
            "weight_change": {
                "start_kg": first["weight_kg"] if first else None,
                "end_kg": last["weight_kg"] if last else None,
                "delta_kg": round(last["weight_kg"] - first["weight_kg"], 2) if first else None,
                "start_measured_at": first["weight_at"] if first else None,
                "end_measured_at": last["weight_at"] if last else None,
            },
            "forecast_7d_kg": forecast,
            "forecast_model": "Средний баланс × 7 / 7700; условная оценка, не прогноз измеренного веса",
        }


def format_weekly_summary(summary: dict) -> str:
    avg, change = summary["averages"], summary["weight_change"]
    def fmt(value):
        return f"{value:,.0f}".replace(",", " ") if value is not None else "нет данных"
    lines = [f"📊 <b>Отчет здоровья за неделю — {escape(summary['name'])}</b>",
             f"{summary['start_date']} — {summary['end_date']}"]
    if change["start_kg"] is not None:
        lines.append(f"⚖️ Вес: {change['start_kg']:.1f} → {change['end_kg']:.1f} кг ({change['delta_kg']:+.1f} кг)")
        if change["start_measured_at"] == change["end_measured_at"]:
            lines.append("Один известный замер: изменение веса пока не оценить.")
    else:
        lines.append("⚖️ Вес: нет данных")
    lines += [f"🏃 Средние шаги: {fmt(avg['steps'])} в день ({summary['sample_days']['steps']} дн.)",
              f"🍎 Среднее питание: {fmt(avg['calories'])} ккал ({summary['sample_days']['calories']} дн.)",
              f"🔥 Средний расчетный расход: {fmt(avg['tdee'])} ккал",
              f"Полных дней для баланса: {summary['complete_days']} из {len(summary['days'])}."]
    balance = avg["balance"]
    if balance is not None:
        label = "дефицит" if balance < 0 else "профицит" if balance > 0 else "баланс"
        lines.append(f"Средний {label}: {fmt(abs(balance))} ккал/день.")
    else:
        lines.append("Для баланса заполните физические параметры и дождитесь питания, веса и активных ккал.")
    if summary["forecast_7d_kg"] is not None:
        lines.append(f"При сохранении баланса условное изменение за 7 дней: {summary['forecast_7d_kg']:+.2f} кг.")
    lines.append("Расход и прогноз — оценки. Вода и другие факторы влияют на вес; пропуски не считаются нулем.")
    return "\n".join(lines)
