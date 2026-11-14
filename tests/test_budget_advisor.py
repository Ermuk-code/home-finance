from datetime import date
from decimal import Decimal as D

from app.finance.services.advisor import (
    CategoryTrend,
    calc_trends,
    extrapolate,
    format_advice,
    shift_month,
)
from app.finance.services.budget import build_limit_warning


# ---------------------------- бюджетные лимиты ----------------------------
def test_limit_below_80_percent_has_no_warning():
    assert build_limit_warning("Кафе", D("7999"), D("10000")) is None


def test_yellow_zone_message():
    text = build_limit_warning("Супермаркеты", D("8500"), D("10000"))
    assert text.startswith("⚠️ Внимание: Вы исчерпали 85% лимита на категорию Супермаркеты в этом месяце")


def test_yellow_zone_starts_exactly_at_80():
    assert "80%" in build_limit_warning("Кафе", D("8000"), D("10000"))


def test_red_zone_at_and_above_100():
    at_limit = build_limit_warning("Кафе", D("10000"), D("10000"))
    over = build_limit_warning("Кафе", D("12000"), D("10000"))
    assert at_limit.startswith("🔴") and "100%" in at_limit
    assert over.startswith("🔴") and "120%" in over


def test_percent_is_rounded_down():
    assert "99%" in build_limit_warning("Кафе", D("9999"), D("10000"))


def test_html_in_category_name_is_escaped():
    assert "&lt;b&gt;" in build_limit_warning("<b>x", D("9000"), D("10000"))


def test_zero_limit_is_ignored():
    assert build_limit_warning("Кафе", D("100"), D("0")) is None


# ------------------------------ советы ------------------------------
def test_extrapolation_formula():
    # 15 000 за 10 дней в ноябре (30 дней) -> 45 000
    assert extrapolate(D("15000"), date(2026, 11, 10)) == D("45000")
    # високосный февраль: 29 дней
    assert extrapolate(D("1000"), date(2028, 2, 10)) == D("1000") / 10 * 29


def test_shift_month_crosses_year():
    assert shift_month(2026, 2, -3) == (2025, 11)
    assert shift_month(2026, 11, -3) == (2026, 8)


def test_growth_advice_matches_spec_wording():
    # среднее 20 000, текущие 10 000 за 15 дней ноября -> прогноз 20 000 * ... = 20 000; берём 12 500 -> 25 000 (+25%)
    trends = calc_trends({"Супермаркеты": D("60000")}, 3, {"Супермаркеты": D("12500")}, date(2026, 11, 15))
    assert len(trends) == 1 and trends[0].change_pct == D("25")
    text = format_advice(trends[0])
    assert "выросли на 25% по сравнению со средним за 3 мес." in text
    assert "Рекомендуем сократить эту категорию." in text


def test_average_uses_only_months_with_data():
    # история только за 1 месяц: 30 000 -> среднее 30 000, а не 10 000
    trends = calc_trends({"Кафе": D("30000")}, 1, {"Кафе": D("15000")}, date(2026, 11, 15))
    assert trends[0].average == D("30000")
    assert trends[0].change_pct == D("0")
    assert format_advice(trends[0]) is None


def test_no_history_means_no_trends():
    assert calc_trends({}, 0, {"Кафе": D("5000")}, date(2026, 11, 15)) == []
    assert calc_trends({}, 3, {"Кафе": D("5000")}, date(2026, 11, 15)) == []


def test_small_absolute_difference_is_not_advised():
    # +50%, но всего +300 ₽
    t = CategoryTrend("Кино", D("600"), D("450"), D("900"), D("50"))
    assert format_advice(t) is None


def test_drop_gives_positive_note():
    t = CategoryTrend("Такси", D("10000"), D("3000"), D("6000"), D("-40"))
    assert "ниже среднего" in format_advice(t)
