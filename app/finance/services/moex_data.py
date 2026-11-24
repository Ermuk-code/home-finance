"""Данные MOEX ISS для расчёта пассивного дохода: купоны, погашение, доходность, дивиденды.

Разбор JSON вынесен в чистые функции (parse_*), чтобы их можно было тестировать без сети.
Все сетевые функции «мягкие»: при любой ошибке возвращают пустой результат, а не роняют отчёт.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

BASE_URL = "https://iss.moex.com/iss"
_BOND_BOARDS = ("TQOB", "TQCB", "TQIR", "TQOD")


@dataclass
class Coupon:
    pay_date: date
    amount: Optional[Decimal]  # руб. на 1 облигацию; None — купон ещё не определён (плавающий)


@dataclass
class BondInfo:
    mat_date: Optional[date] = None
    next_coupon_date: Optional[date] = None
    coupon_value: Optional[Decimal] = None   # руб. на облигацию, ближайший купон
    coupon_period_days: Optional[int] = None
    face_value: Decimal = Decimal("1000")
    yield_pct: Optional[Decimal] = None      # доходность к погашению по данным биржи, % годовых
    coupons: List[Coupon] = field(default_factory=list)


@dataclass
class DividendPayment:
    record_date: date
    amount: Decimal   # руб. на 1 бумагу


@dataclass
class InstrumentData:
    bond: Optional[BondInfo] = None
    dividends: List[DividendPayment] = field(default_factory=list)


# ---------------------------------------------------------------- чистые парсеры

def _rows(data: Dict[str, Any], block: str) -> List[Dict[str, Any]]:
    b = data.get(block) or {}
    cols = b.get("columns") or []
    return [dict(zip(cols, row)) for row in (b.get("data") or [])]


def _dec(v: Any) -> Optional[Decimal]:
    if v is None or v == "":
        return None
    try:
        return Decimal(str(v))
    except InvalidOperation:
        return None


def _date(v: Any) -> Optional[date]:
    if not v or str(v).startswith("0000"):
        return None
    try:
        return datetime.strptime(str(v)[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def parse_bond_info(data: Dict[str, Any]) -> BondInfo:
    """Ответ engines/stock/markets/bonds/securities/{secid}.json -> BondInfo (без списка купонов)."""
    info = BondInfo()
    sec = {r.get("BOARDID"): r for r in _rows(data, "securities")}
    row = next((sec[b] for b in _BOND_BOARDS if b in sec), None) or next(iter(sec.values()), None)
    if row:
        info.mat_date = _date(row.get("MATDATE"))
        info.next_coupon_date = _date(row.get("NEXTCOUPON"))
        info.coupon_value = _dec(row.get("COUPONVALUE"))
        period = _dec(row.get("COUPONPERIOD"))
        info.coupon_period_days = int(period) if period is not None else None
        face = _dec(row.get("FACEVALUE"))
        if face:
            info.face_value = face

    yields = {r.get("BOARDID"): r for r in _rows(data, "marketdata_yields")}
    for board in _BOND_BOARDS:
        y = yields.get(board)
        if y:
            val = _dec(y.get("EFFECTIVEYIELD")) or _dec(y.get("YIELDATWAP"))
            if val is not None:
                info.yield_pct = val
                break
    return info


def parse_coupons(data: Dict[str, Any]) -> List[Coupon]:
    """Ответ statistics/.../bondization/{secid}.json -> график купонов."""
    coupons: List[Coupon] = []
    for r in _rows(data, "coupons"):
        d = _date(r.get("coupondate"))
        if not d:
            continue
        amount = _dec(r.get("value_rub")) or _dec(r.get("value"))
        coupons.append(Coupon(pay_date=d, amount=amount))
    coupons.sort(key=lambda c: c.pay_date)
    return coupons


def parse_dividends(data: Dict[str, Any]) -> List[DividendPayment]:
    """Ответ securities/{secid}/dividends.json -> выплаты на 1 бумагу (в рублях)."""
    out: List[DividendPayment] = []
    for r in _rows(data, "dividends"):
        d = _date(r.get("registryclosedate"))
        amount = _dec(r.get("value"))
        currency = (r.get("currencyid") or "SUR").upper()
        if d and amount and currency in ("SUR", "RUB", "RUR"):
            out.append(DividendPayment(record_date=d, amount=amount))
    out.sort(key=lambda p: p.record_date)
    return out


# ---------------------------------------------------------------- сеть

async def _get_json(client, url: str, params: Dict[str, str]) -> Optional[Dict[str, Any]]:
    try:
        resp = await client.get(url, params=params)
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:  # сеть/формат — отчёт всё равно должен собраться
        logger.warning("MOEX ISS: %s не ответил: %s", url, exc)
        return None


async def fetch_bond(client, secid: str) -> Optional[BondInfo]:
    main = await _get_json(
        client,
        f"{BASE_URL}/engines/stock/markets/bonds/securities/{secid}.json",
        {"iss.meta": "off", "iss.only": "securities,marketdata_yields"},
    )
    if not main:
        return None
    info = parse_bond_info(main)
    sched = await _get_json(
        client,
        f"{BASE_URL}/statistics/engines/stock/markets/bonds/bondization/{secid}.json",
        {"iss.meta": "off", "iss.only": "coupons", "limit": "100"},
    )
    if sched:
        info.coupons = parse_coupons(sched)
    return info


async def fetch_dividends(client, secid: str) -> List[DividendPayment]:
    data = await _get_json(
        client,
        f"{BASE_URL}/securities/{secid}/dividends.json",
        {"iss.meta": "off"},
    )
    return parse_dividends(data) if data else []


async def fetch_instruments(items: List[Dict[str, str]]) -> Dict[str, InstrumentData]:
    """items: [{"ticker": ..., "asset_type": "bond|stock|fund"}] -> {TICKER: InstrumentData}."""
    import httpx  # локальные импорты: чистые парсеры выше не требуют httpx

    from app.finance.services.moex_http import create_moex_client

    result: Dict[str, InstrumentData] = {}
    if not items:
        return result
    async with create_moex_client(httpx.Timeout(10.0, connect=5.0)) as client:

        async def one(item: Dict[str, str]) -> Tuple[str, InstrumentData]:
            ticker = item["ticker"].strip().upper()
            data = InstrumentData()
            if item.get("asset_type") == "bond":
                data.bond = await fetch_bond(client, ticker)
            else:
                data.dividends = await fetch_dividends(client, ticker)
            return ticker, data

        pairs = await asyncio.gather(*(one(i) for i in items if i.get("ticker")), return_exceptions=True)
        for p in pairs:
            if isinstance(p, Exception):
                logger.warning("MOEX ISS: сбой обогащения: %s", p)
                continue
            result[p[0]] = p[1]
    return result
