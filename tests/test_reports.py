from decimal import Decimal as D

from app.finance.services.reports import fmt_money, format_deposit_summary, format_report, summarize_transactions

TRANSFER_IN = "Переводы и Пополнения (Входящие)"
TRANSFER_OUT = "Переводы и Сбережения (Исходящие)"


def _rows():
    return [
        ("Зарплата и Стипендия", D("3586.80")),
        (TRANSFER_IN, D("2000.00")),        # перевод от родственника/между счетами — не доход
        (TRANSFER_OUT, D("-2000.00")),      # перевод на свой вклад — не трата
        ("Продукты", D("-500.00")),
        (None, D("-100.00")),               # неразобранное — считается расходом
    ]


def test_clean_income_excludes_transfers():
    s = summarize_transactions(_rows())
    assert s["clean_income"] == D("3586.80")
    assert s["income_all"] == D("5586.80")


def test_clean_expense_and_net():
    s = summarize_transactions(_rows())
    assert s["clean_expense"] == D("600.00")
    assert s["expense_all"] == D("2600.00")
    assert s["net"] == D("2986.80")


def test_expense_breakdown_has_no_transfers_and_keeps_uncategorized():
    s = summarize_transactions(_rows())
    labels = [label for label, _ in s["expenses_by_category"]]
    assert TRANSFER_OUT not in labels
    assert labels == ["Продукты", "❓ Неразобранное"]


def test_report_text_contains_clean_income():
    text = format_report(summarize_transactions(_rows()))
    assert "Чистый доход: <b>+3 586,80 ₽</b>" in text
    assert "С переводами" in text


def test_report_negative_net():
    s = summarize_transactions([("Продукты", D("-100.00"))])
    assert "-100,00 ₽" in format_report(s)
    assert s["net"] == D("-100.00")


def test_fmt_money():
    assert fmt_money(D("1234567.5")) == "1 234 567,50"
    assert fmt_money(16.5) == "16,50"


def test_deposit_summary_escapes_html_and_shows_total():
    info = {
        "kind": "deposit", "bank_name": "Сбер", "title": "<b>Весна</b>", "amount": 300000.0,
        "interest_rate": 16.5, "capitalization": False, "opened_at": "01.09.2026",
        "end_date": "01.09.2027", "term_months": 12, "interest_total": 49500.0, "final_amount": 349500.0,
    }
    text = format_deposit_summary(info)
    assert "&lt;b&gt;Весна" in text and "<b>Весна</b>" not in text
    assert "349 500,00 ₽" in text and "16,5%" in text


# --- свободный денежный поток = доход − расход
from app.finance.services.reports import summarize_dashboard  # noqa: E402


def _dash(rows, **kw):
    return summarize_dashboard(rows, **kw)


def test_cash_flow_is_income_minus_all_expenses_including_taxi():
    rows = [
        ("Зарплата и Стипендия", "income", False, D("50000"), "Т-Банк"),
        ("ЖКХ", "expense", True, D("-5000"), "Т-Банк"),
        ("Такси", "expense", False, D("-1500"), "Т-Банк"),
        ("Продукты", "expense", False, D("-8000"), "Т-Банк"),
    ]
    r = _dash(rows)
    assert r["clean_income"] == D("50000")
    assert r["clean_expense"] == D("14500")
    assert r["cash_flow"] == D("35500")
    assert r["mandatory_expense"] == D("5000")      # обязательные — лишь часть расходов, на поток влияют все


def test_cash_flow_goes_negative_when_spending_exceeds_income():
    rows = [("Зарплата и Стипендия", "income", False, D("1000"), "Сбер"), ("Такси", "expense", False, D("-1800"), "Сбер")]
    assert _dash(rows)["cash_flow"] == D("-800")


def test_transfers_between_own_accounts_do_not_affect_cash_flow():
    rows = [
        ("Зарплата и Стипендия", "income", False, D("1000"), "Сбер"),
        (TRANSFER_IN, "income", False, D("5000"), "Сбер"),
        (TRANSFER_OUT, "expense", False, D("-5000"), "Сбер"),
    ]
    r = _dash(rows)
    assert r["cash_flow"] == D("1000")
    assert r["total_income_all"] == D("6000")        # «с переводами» остаётся для справки


def test_refund_reduces_expense_instead_of_inflating_income():
    rows = [
        ("Продукты", "expense", False, D("-1000"), "Т-Банк"),
        ("Продукты", "expense", False, D("300"), "Т-Банк"),   # возврат
        ("Зарплата и Стипендия", "income", False, D("2000"), "Т-Банк"),
    ]
    r = _dash(rows)
    assert r["clean_income"] == D("2000")
    assert r["clean_expense"] == D("700")
    assert r["cash_flow"] == D("1300")
    assert r["by_category"] == {"Продукты": D("700")}


def test_uncategorized_expense_counts_and_include_transfers_only_affects_charts():
    rows = [(None, None, False, D("-200"), "Сбер"), (TRANSFER_OUT, "expense", False, D("-900"), "Сбер")]
    base = _dash(rows)
    with_tr = _dash(rows, include_transfers=True)
    assert base["cash_flow"] == with_tr["cash_flow"] == D("-200")
    assert TRANSFER_OUT not in base["by_category"] and TRANSFER_OUT in with_tr["by_category"]


def test_telegram_summary_also_nets_refunds():
    s = summarize_transactions([("Продукты", D("-1000"), "expense"), ("Продукты", D("300"), "expense")])
    assert s["clean_income"] == D("0.00") and s["clean_expense"] == D("700")
