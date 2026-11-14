"""Анализ доходов: откуда приходят деньги и как это меняется по месяцам.

Чистые функции без БД (на вход — список IncomeTx), поэтому всё легко тестируется.

Источники дохода:
  salary      — зарплата, аванс, отпускные
  scholarship — стипендия
  parents     — родители и родные (определяется ПРАВИЛАМИ пользователя: по имени отправителя в описании)
  transfers   — обычные переводы от людей
  own         — перевод между своими счетами и пополнение наличными (это не доход, по умолчанию скрыто)
  interest    — проценты по вкладам и кэшбэк
  other       — всё остальное положительное без категории

Почему родители — по правилам: в выписке видно только описание операции («Перевод от Иван Иванович И.»),
банк не знает, что это папа. Поэтому пользователь один раз добавляет правило «Иван Иванович → родители».
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from app.finance.services.recurrence import LOCAL_TZ

SOURCES: List[Dict[str, str]] = [
    {"key": "salary", "label": "Зарплата", "color": "#10b981"},
    {"key": "scholarship", "label": "Стипендия", "color": "#6366f1"},
    {"key": "parents", "label": "От родителей и родных", "color": "#f59e0b"},
    {"key": "transfers", "label": "Переводы от людей", "color": "#0ea5e9"},
    {"key": "interest", "label": "Проценты и кэшбэк", "color": "#a855f7"},
    {"key": "other", "label": "Прочее", "color": "#94a3b8"},
    {"key": "own", "label": "Между своими счетами", "color": "#cbd5e1"},
]
SOURCE_KEYS = {s["key"] for s in SOURCES}
_LABELS = {s["key"]: s["label"] for s in SOURCES}

_SALARY_WORDS = ("заработная плата", "3apаботная плата", "зарплата", "3aрплата", "3aарплата",
                 "аванс", "abванс", "отпускные", "расчет при увольнении", "расчёт при увольнении")
_SCHOLARSHIP_WORDS = ("стипенд",)
_OWN_WORDS = ("перевод себе", "внутрибанковский перевод", "перевод с договора", "vklad-karta",
              "внесение наличных", "между своими")

CAT_SALARY = "Зарплата и Стипендия"
CAT_INTEREST = "Проценты по вкладам и Кэшбэк"
CAT_TRANSFER_IN = "Переводы и Пополнения (Входящие)"

_Q2 = Decimal("0.01")
_ZERO = Decimal("0")


@dataclass(frozen=True)
class IncomeTx:
    id: int
    date: datetime
    amount: Decimal
    description: str
    category: Optional[str]
    category_type: Optional[str]   # 'income' | 'expense' | None
    bank: str = ""


Rule = Tuple[str, str]   # (ключевая фраза в нижнем регистре, источник)


def label_of(source: str) -> str:
    return _LABELS.get(source, source)


def normalize_rule(keyword: str, source: str) -> Rule:
    kw = " ".join(str(keyword).lower().split())
    if len(kw) < 3:
        raise ValueError("Ключевая фраза должна быть не короче 3 символов.")
    if source not in SOURCE_KEYS:
        raise ValueError(f"Неизвестный источник: {source!r}.")
    return kw, source


def classify_income(description: str, category: Optional[str], rules: Sequence[Rule] = ()) -> str:
    """Источник дохода по описанию операции. Правила пользователя главнее встроенных."""
    desc = " ".join((description or "").lower().split())
    for keyword, source in rules:
        if keyword and keyword in desc:
            return source
    if any(w in desc for w in _SCHOLARSHIP_WORDS):
        return "scholarship"
    if any(w in desc for w in _SALARY_WORDS) or category == CAT_SALARY:
        return "salary"
    if category == CAT_INTEREST:
        return "interest"
    if category == CAT_TRANSFER_IN or "перевод" in desc or "пополнение" in desc:
        return "own" if any(w in desc for w in _OWN_WORDS) else "transfers"
    return "other"


def _local(dt: datetime) -> datetime:
    return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).astimezone(LOCAL_TZ)


def _month_key(dt: datetime) -> str:
    return _local(dt).strftime("%Y-%m")


def _is_income(tx: IncomeTx) -> bool:
    # Плюс по расходной категории — это возврат покупки, а не доход
    return tx.amount > 0 and tx.category_type in ("income", None)


def _in_period(tx: IncomeTx, start: Optional[date], end: Optional[date]) -> bool:
    d = _local(tx.date).date()
    return (start is None or d >= start) and (end is None or d <= end)


def _months_between(first: str, last: str) -> List[str]:
    y, m = int(first[:4]), int(first[5:])
    ly, lm = int(last[:4]), int(last[5:])
    out: List[str] = []
    while (y, m) <= (ly, lm):
        out.append(f"{y:04d}-{m:02d}")
        m += 1
        if m == 13:
            y, m = y + 1, 1
    return out


def _selected(txs: Iterable[IncomeTx], rules: Sequence[Rule], start: Optional[date], end: Optional[date]):
    return [(t, classify_income(t.description, t.category, rules))
            for t in txs if _is_income(t) and _in_period(t, start, end)]


def _month_range(rows, start: Optional[date], end: Optional[date]) -> List[str]:
    if not rows and not (start and end):
        return []
    first = start.strftime("%Y-%m") if start else min(_month_key(t.date) for t, _ in rows)
    last = end.strftime("%Y-%m") if end else max(_month_key(t.date) for t, _ in rows)
    return _months_between(first, last) if first <= last else []


def _f(v: Decimal) -> float:
    return float(v.quantize(_Q2))


def build_analysis(
    txs: Iterable[IncomeTx],
    rules: Sequence[Rule] = (),
    start: Optional[date] = None,
    end: Optional[date] = None,
    include_own: bool = False,
) -> Dict[str, Any]:
    """Сводка по источникам и по месяцам. Переводы между своими счетами по умолчанию в итог не входят."""
    rows = _selected(txs, rules, start, end)
    months = _month_range(rows, start, end)
    index = {m: i for i, m in enumerate(months)}

    totals: Dict[str, Decimal] = {s["key"]: _ZERO for s in SOURCES}
    counts: Dict[str, int] = {s["key"]: 0 for s in SOURCES}
    series: Dict[str, List[Decimal]] = {s["key"]: [_ZERO] * len(months) for s in SOURCES}
    for tx, src in rows:
        totals[src] += tx.amount
        counts[src] += 1
        series[src][index[_month_key(tx.date)]] += tx.amount

    shown = [s for s in SOURCES if counts[s["key"]] and (include_own or s["key"] != "own")]
    grand = sum((totals[s["key"]] for s in shown), _ZERO)
    n_months = max(len(months), 1)

    return {
        "period": {"start": start.isoformat() if start else None, "end": end.isoformat() if end else None},
        "months": months,
        "include_own": include_own,
        "total": _f(grand),
        "monthly_avg": _f(grand / n_months),
        "own_total": _f(totals["own"]),            # показываем отдельно, чтобы было видно, что исключено
        "sources": [
            {
                "key": s["key"], "label": s["label"], "color": s["color"],
                "total": _f(totals[s["key"]]),
                "share_pct": _f(totals[s["key"]] / grand * 100) if grand > 0 else 0.0,
                "monthly_avg": _f(totals[s["key"]] / n_months),
                "count": counts[s["key"]],
                "series": [_f(v) for v in series[s["key"]]],
            }
            for s in shown
        ],
    }


def build_source_detail(
    txs: Iterable[IncomeTx],
    source: str,
    rules: Sequence[Rule] = (),
    start: Optional[date] = None,
    end: Optional[date] = None,
    limit: int = 500,
) -> Dict[str, Any]:
    """Расшифровка одного источника: помесячный график, статистика и список операций (новые сверху)."""
    if source not in SOURCE_KEYS:
        raise ValueError(f"Неизвестный источник: {source!r}.")
    rows = _selected(txs, rules, start, end)
    months = _month_range(rows, start, end)
    mine = sorted((t for t, s in rows if s == source), key=lambda t: t.date, reverse=True)

    by_month = {m: _ZERO for m in months}
    for t in mine:
        by_month[_month_key(t.date)] += t.amount
    total = sum((t.amount for t in mine), _ZERO)
    n_months = max(len(months), 1)
    active = [v for v in by_month.values() if v > 0]

    return {
        "source": source,
        "label": label_of(source),
        "months": months,
        "series": [_f(by_month[m]) for m in months],
        "total": _f(total),
        "monthly_avg": _f(total / n_months),
        "count": len(mine),
        "avg_payment": _f(total / len(mine)) if mine else 0.0,
        "max_payment": _f(max((t.amount for t in mine), default=_ZERO)),
        "active_months": len(active),
        "transactions": [
            {"id": t.id, "date": _local(t.date).strftime("%d.%m.%Y"), "amount": _f(t.amount),
             "description": t.description, "bank": t.bank}
            for t in mine[:limit]
        ],
        "truncated": len(mine) > limit,
    }
