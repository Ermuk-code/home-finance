"""Сводка доходов и расходов для Telegram-отчёта.

Правила совпадают с дашбордом: «чистые» доходы и расходы — это операции без категорий-переводов
(переводы между своими счетами и на накопительные счета/вклады не считаются ни доходом, ни тратой).
"""
from decimal import Decimal
from html import escape
from typing import Any, Dict, Iterable, Optional, Tuple, Union

from app.finance.services.categorizer import TRANSFER_CATEGORY_NAMES

UNCATEGORIZED_LABEL = "❓ Неразобранное"
_ZERO = Decimal("0.00")


def fmt_money(value: Union[Decimal, float, int, str]) -> str:
    """12345.5 -> '12 345,50'."""
    return f"{Decimal(str(value)):,.2f}".replace(",", " ").replace(".", ",")


def _is_refund(amount: Decimal, cat_type: Optional[str], is_transfer: bool) -> bool:
    """Плюс по расходной категории (возврат покупки) — это не доход, а уменьшение расхода."""
    return amount > 0 and cat_type == "expense" and not is_transfer


def summarize_transactions(rows: Iterable[Tuple]) -> Dict[str, Any]:
    """rows — кортежи (название категории или None, сумма со знаком[, тип категории 'income'/'expense'])."""
    income_all = expense_all = clean_income = clean_expense = _ZERO
    by_category: Dict[str, Decimal] = {}

    for row in rows:
        cat_name, amount = row[0], row[1]
        cat_type = row[2] if len(row) > 2 else None
        is_transfer = cat_name in TRANSFER_CATEGORY_NAMES
        if _is_refund(amount, cat_type, is_transfer):
            refund = amount
            expense_all -= refund
            clean_expense -= refund
            label = cat_name or UNCATEGORIZED_LABEL
            by_category[label] = by_category.get(label, _ZERO) - refund
            continue
        if amount > 0:
            income_all += amount
            if not is_transfer:
                clean_income += amount
        else:
            value = abs(amount)
            expense_all += value
            if not is_transfer:
                clean_expense += value
                label = cat_name or UNCATEGORIZED_LABEL
                by_category[label] = by_category.get(label, _ZERO) + value

    return {
        "income_all": income_all,
        "expense_all": expense_all,
        "clean_income": clean_income,
        "clean_expense": clean_expense,
        "net": clean_income - clean_expense,
        "expenses_by_category": sorted(
            ((k, v) for k, v in by_category.items() if v > 0), key=lambda x: x[1], reverse=True
        ),
    }


def summarize_dashboard(rows: Iterable[Tuple], include_transfers: bool = False) -> Dict[str, Any]:
    """Итоги для дашборда. rows — (категория|None, тип категории, обязательная?, сумма со знаком, банк).

    Свободный денежный поток (cash_flow) = чистые доходы − чистые расходы: любая трата, даже на такси,
    уменьшает его. Переводы между своими счетами не считаются ни доходом, ни расходом. Возврат покупки
    (плюс по расходной категории) уменьшает расход, а не добавляется к доходу.
    """
    clean_income = clean_expense = total_income_all = total_expense_all = mandatory_expense = _ZERO
    by_category: Dict[str, Decimal] = {}
    by_bank: Dict[str, Decimal] = {}

    for cat_name, cat_type, is_mandatory, amount, bank_name in rows:
        label = cat_name or "Неразобранное"
        is_transfer = label in TRANSFER_CATEGORY_NAMES

        if _is_refund(amount, cat_type, is_transfer):
            total_expense_all -= amount
            clean_expense -= amount
            if is_mandatory:
                mandatory_expense -= amount
            by_category[label] = by_category.get(label, _ZERO) - amount
            by_bank[bank_name] = by_bank.get(bank_name, _ZERO) - amount
            continue

        if amount > 0:
            total_income_all += amount
            if not is_transfer:
                clean_income += amount
        else:
            abs_amt = abs(amount)
            total_expense_all += abs_amt
            if not is_transfer:
                clean_expense += abs_amt
            if is_mandatory:
                mandatory_expense += abs_amt
            if include_transfers or not is_transfer:
                by_category[label] = by_category.get(label, _ZERO) + abs_amt
                by_bank[bank_name] = by_bank.get(bank_name, _ZERO) + abs_amt

    return {
        "clean_income": clean_income,
        "clean_expense": clean_expense,
        "total_income_all": total_income_all,
        "total_expense_all": total_expense_all,
        "mandatory_expense": mandatory_expense,
        "cash_flow": clean_income - clean_expense,
        "by_category": {k: v for k, v in by_category.items() if v > 0},
        "by_bank": {k: v for k, v in by_bank.items() if v > 0},
    }


def format_report(summary: Dict[str, Any], days: int = 30) -> str:
    """HTML-текст для Telegram."""
    net = summary["net"]
    net_sign = "+" if net >= 0 else "-"
    net_icon = "🟢" if net >= 0 else "🔴"
    clean_expense = summary["clean_expense"]

    lines = [
        f"📊 <b>Финансовая сводка за последние {days} дней:</b>\n",
        f"💎 Чистый доход: <b>+{fmt_money(summary['clean_income'])} ₽</b>",
        f"🔻 Чистые расходы: <b>-{fmt_money(clean_expense)} ₽</b>",
        f"{net_icon} Итог (доход − расходы): <b>{net_sign}{fmt_money(abs(net))} ₽</b>",
        "<i>Без переводов между своими счетами и пополнений накопительных счетов и вкладов.</i>\n",
        f"🔁 С переводами: доходы <b>+{fmt_money(summary['income_all'])} ₽</b>, "
        f"расходы <b>-{fmt_money(summary['expense_all'])} ₽</b>\n",
    ]

    if summary["expenses_by_category"]:
        lines.append("<b>Структура расходов по категориям:</b>")
        for label, total in summary["expenses_by_category"]:
            share = (total / clean_expense * Decimal("100")).quantize(Decimal("0.1")) if clean_expense > 0 else Decimal("0")
            lines.append(f"▫️ {escape(str(label))}: <b>{fmt_money(total)} ₽</b> ({share}%)")

    return "\n".join(lines)



def _fmt_rate(rate: float) -> str:
    return f"{Decimal(str(rate)).normalize():f}".replace(".", ",")


def format_deposit_summary(info: Dict[str, Any]) -> str:
    """Итоговое сообщение после добавления вклада / накопительного счёта (HTML для Telegram).

    ``info`` — результат services.deposits.describe_deposit.
    """
    bank = escape(info["bank_name"])
    title = escape(info["title"])
    rate = _fmt_rate(info["interest_rate"])

    if info["kind"] == "savings":
        return "\n".join(
            [
                "✅ <b>Накопительный счёт добавлен</b>",
                f"🏦 {bank} — «{title}»",
                f"💰 Остаток: <b>{fmt_money(info['amount'])} ₽</b> · ставка <b>{rate}%</b>",
                "📆 Проценты считаются ежедневно, зачисляются 12-го числа каждого месяца\n",
                f"📈 Доход: ≈ <b>+{fmt_money(info['daily_income'])} ₽</b>/день, "
                f"≈ <b>+{fmt_money(info['expected_yearly_income'])} ₽</b> за год",
                f"⏭ Ближайшее зачисление {info['next_credit_date']}: "
                f"≈ <b>+{fmt_money(info['next_credit_amount'])} ₽</b>",
                "<i>Оценка без учёта будущих пополнений и снятий.</i>",
            ]
        )

    cap_note = " · с капитализацией" if info.get("capitalization") else ""
    return "\n".join(
        [
            "✅ <b>Вклад добавлен</b>",
            f"🏦 {bank} — «{title}»",
            f"💰 Сумма: <b>{fmt_money(info['amount'])} ₽</b> · ставка <b>{rate}%</b>",
            f"📅 Срок: {info['opened_at']} → <b>{info['end_date']}</b> ({info['term_months']} мес.){cap_note}\n",
            f"📈 Доход за весь срок: <b>+{fmt_money(info['interest_total'])} ₽</b>",
            f"🎯 К получению в конце срока: <b>{fmt_money(info['final_amount'])} ₽</b>",
        ]
    )
