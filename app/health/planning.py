"""Explicit planning assumptions, separate from observed measurements."""
from datetime import date, timedelta
from statistics import mean
from zoneinfo import ZoneInfo
from datetime import timezone
from app.core.config import settings


def measurement_trend(points):
    """Daily values only; no trend from repeated carried-forward weights."""
    daily = dict(points)
    if len(daily) < 3:
        return None
    days = sorted(daily)
    if (days[-1] - days[0]).days < 14:
        return None
    xs = [(day - days[0]).days for day in days]
    ys = [daily[day] for day in days]
    mx, my = mean(xs), mean(ys)
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sum((x - mx) ** 2 for x in xs)
    return round(slope * 7, 3)


def build_plan(user, summary, weights, measurements, today: date):
    current = summary['days'][-1]
    weight, height, waist = current['weight_kg'], current['height_cm'], current['waist_cm']
    age = current['age']
    bmi = weight / (height / 100) ** 2 if weight and height else None
    ratio = waist / height if waist and height else None
    warnings = []
    adult = age is not None and age >= 18
    if not adult or current['bmr'] is None:
        warnings.append('Для расчета питания нужны пол, рост, вес и дата рождения взрослого пользователя.')
    if ratio is not None and bmi is not None and bmi >= 35:
        warnings.append('При ИМТ от 35 отношение талии к росту здесь не классифицируется.')
    ratio_band = None
    if adult and ratio is not None and bmi is not None and bmi < 35:
        ratio_band = ('ниже 0,4: не интерпретируется автоматически' if ratio < .4 else
                      '0,4–0,49' if ratio < .5 else '0,5–0,59: повышено' if ratio < .6 else 'от 0,6: высокое')
    maintenance, source = None, None
    if adult and current['bmr'] is not None:
        if summary['complete_days'] >= 3:
            maintenance, source = summary['averages']['tdee'], 'telemetry_28d'
        else:
            maintenance = current['bmr'] * float(user.activity_factor)
            source = 'bmr_activity_factor'
            warnings.append('Мало полных дней: расход оценен по BMR и выбранной активности, а не по часам.')
    target = macros = planned_loss = goal_weeks = None
    forecast = []
    blocked = user.health_goal == 'lose' and (bmi is None or bmi < 18.5)
    if blocked:
        warnings.append('План снижения веса не строится при неизвестном ИМТ или ИМТ ниже 18,5.')
    goal = float(user.goal_weight_kg) if user.goal_weight_kg is not None else None
    if user.health_goal == 'lose' and goal and height and goal / (height / 100) ** 2 < 18.5:
        blocked = True
        warnings.append('Целевой вес ниже ИМТ 18,5 — измените цель.')
    if user.health_goal == 'lose' and goal is not None and weight is not None and goal >= weight:
        blocked = True
        warnings.append('Цель снижения уже достигнута или выше текущего веса: проверьте цель.')
    if maintenance and not blocked:
        target = maintenance * (1 - (user.deficit_percent / 100 if user.health_goal == 'lose' else 0))
        # Product's conservative planning floor, not a universal prescription.
        floor = 1500 if user.gender == 'male' else 1200
        if target < floor:
            target = None
            warnings.append('Расчет ниже диапазона автоматического планирования (1500/1200 ккал). Нужен индивидуальный план.')
        else:
            target = round(target)
            macros = {'protein_g': round(target * .25 / 4, 1), 'fat_g': round(target * .30 / 9, 1),
                      'carbs_g': round(target * .45 / 4, 1),
                      'protein_percent': 25, 'fat_percent': 30, 'carbs_percent': 45}
            # TEF falls when food intake falls: 90% maintenance + 10% planned intake.
            expected_balance = target - (.9 * maintenance + .1 * target)
            planned_loss = round(-expected_balance * 7 / 7700, 3) if user.health_goal == 'lose' else 0
            if user.health_goal == 'lose' and weight and planned_loss > 0:
                for week in range(5):
                    predicted = weight - planned_loss * week
                    if height and predicted / (height / 100) ** 2 < 18.5:
                        break
                    forecast.append({'date': (today + timedelta(weeks=week)).isoformat(),
                                     'weight_kg': round(predicted, 2)})
                if goal is not None and goal < weight:
                    goal_weeks = round((weight - goal) / planned_loss, 1)
    recent = today - timedelta(days=90)
    def local_day(ts):
        return (ts.replace(tzinfo=timezone.utc) if ts.tzinfo is None else ts).astimezone(ZoneInfo(settings.ACTIVITY_TZ)).date()
    weight_points = [(local_day(w.timestamp), float(w.weight_kg)) for w in weights if recent <= local_day(w.timestamp) <= today]
    waist_points = [(m.date, float(m.waist_cm)) for m in measurements if m.waist_cm is not None and recent <= m.date <= today]
    if weights and (today - local_day(weights[-1].timestamp)).days > 30:
        warnings.append('Последний вес старше 30 дней: обновите замер для актуального плана.')
    waist_trend = measurement_trend(waist_points)
    waist_forecast = []
    if waist_trend is not None and waist_points:
        last_date, last_waist = sorted(waist_points)[-1]
        if (today - last_date).days <= 14:
            for week in range(5):
                projected = last_waist + waist_trend * week
                if 30 <= projected <= 250:
                    waist_forecast.append({'date': (last_date + timedelta(weeks=week)).isoformat(),
                                           'waist_cm': round(projected, 1)})
    return {
        'as_of': today.isoformat(), 'weight_kg': weight, 'height_cm': height, 'waist_cm': waist,
        'bmi': round(bmi, 2) if bmi else None, 'waist_height_ratio': round(ratio, 3) if ratio else None,
        'waist_ratio_band': ratio_band,
        'maintenance_kcal': round(maintenance) if maintenance else None, 'maintenance_source': source,
        'telemetry_days': summary['complete_days'],
        'target_calories': target, 'macros': macros, 'planned_loss_kg_week': planned_loss,
        'goal_weeks': goal_weeks, 'forecast': forecast,
        'weight_trend_kg_week': measurement_trend(weight_points),
        'waist_trend_cm_week': waist_trend, 'waist_forecast': waist_forecast,
        'warnings': warnings,
        'model_note': 'БЖУ 25/30/45% энергии; прогноз — условная модель 7700 ккал/кг с поправкой TEF. '
                      'Талия показывает динамику обхвата, но не позволяет предсказать мышцы или внешний вид тела.',
    }
