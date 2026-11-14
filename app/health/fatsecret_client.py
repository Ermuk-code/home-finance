"""Клиент FatSecret Platform API (OAuth 1.0a, 3-legged) для чтения дневника питания.

Почему OAuth 1.0a, а не OAuth 2.0: у FatSecret OAuth 2.0 (client_credentials) работает только для методов
без привязки к человеку (foods.search и т.п.). Дневник питания (food_entries.get) — «delegated»-метод,
доступный лишь через OAuth 1.0a. Пользовательские токены OAuth 1.0a бессрочные, поэтому refresh не нужен.

Поток привязки:
  1. start_link(user_id)  -> request token у FatSecret + ссылка авторизации для пользователя;
  2. пользователь нажимает «Разрешить», FatSecret редиректит на callback с oauth_token и oauth_verifier;
  3. finish_link(...)     -> обмен на постоянные access token + secret, которые сохраняются в users.
"""
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Dict, Optional, Tuple
from urllib.parse import parse_qsl, urlencode

import httpx

from app.core.config import settings
from app.health.oauth1 import sign_params

logger = logging.getLogger(__name__)

REQUEST_TOKEN_URL = "https://authentication.fatsecret.com/oauth/request_token"
AUTHORIZE_URL = "https://authentication.fatsecret.com/oauth/authorize"
ACCESS_TOKEN_URL = "https://authentication.fatsecret.com/oauth/access_token"
API_URL = "https://platform.fatsecret.com/rest/server.api"

HTTP_TIMEOUT = 20
PENDING_TTL = timedelta(minutes=15)
_EPOCH = date(1970, 1, 1)


class FatSecretError(Exception):
    """Любая ошибка обмена с FatSecret (сеть, подпись, отказ API)."""


class FatSecretNotConfigured(FatSecretError):
    """Не заданы FATSECRET_CONSUMER_KEY / FATSECRET_CONSUMER_SECRET."""


@dataclass
class DayTotals:
    calories: int = 0
    protein: Decimal = Decimal("0")
    fat: Decimal = Decimal("0")
    carbs: Decimal = Decimal("0")
    entries: int = 0


@dataclass
class _Pending:
    user_id: int
    token_secret: str
    expires_at: datetime


# request_token -> данные ожидающей привязки. Приложение — один процесс, поэтому хватает памяти.
_PENDING: Dict[str, _Pending] = {}


def _credentials() -> Tuple[str, str]:
    key, secret = settings.FATSECRET_CONSUMER_KEY, settings.FATSECRET_CONSUMER_SECRET
    if not key or not secret:
        raise FatSecretNotConfigured("FatSecret не настроен: задайте FATSECRET_CONSUMER_KEY и FATSECRET_CONSUMER_SECRET.")
    return key, secret


def is_configured() -> bool:
    return bool(settings.FATSECRET_CONSUMER_KEY and settings.FATSECRET_CONSUMER_SECRET)


def _purge_pending(now: datetime) -> None:
    for token in [t for t, p in _PENDING.items() if p.expires_at <= now]:
        _PENDING.pop(token, None)


async def _post(url: str, params: Dict[str, Any], token: Optional[str] = None, token_secret: str = "") -> httpx.Response:
    key, secret = _credentials()
    body = sign_params("POST", url, params, key, secret, token, token_secret)
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            return await client.post(url, data=body)
    except httpx.HTTPError as e:
        raise FatSecretError(f"Нет связи с FatSecret: {e}") from e


async def _oauth_form(url: str, params: Dict[str, Any], token: Optional[str] = None, token_secret: str = "") -> Dict[str, str]:
    resp = await _post(url, params, token, token_secret)
    if resp.status_code != 200:
        raise FatSecretError(f"FatSecret OAuth вернул {resp.status_code}: {resp.text[:200]}")
    data = dict(parse_qsl(resp.text))
    if "oauth_token" not in data or "oauth_token_secret" not in data:
        raise FatSecretError(f"Неожиданный ответ FatSecret OAuth: {resp.text[:200]}")
    return data


async def start_link(user_id: int) -> str:
    """Запрашивает request token и возвращает ссылку, по которой пользователь даёт доступ."""
    data = await _oauth_form(REQUEST_TOKEN_URL, {"oauth_callback": settings.fatsecret_callback_url})
    now = datetime.now(timezone.utc)
    _purge_pending(now)
    _PENDING[data["oauth_token"]] = _Pending(user_id, data["oauth_token_secret"], now + PENDING_TTL)
    return f"{AUTHORIZE_URL}?{urlencode({'oauth_token': data['oauth_token']})}"


async def finish_link(oauth_token: str, oauth_verifier: str) -> Tuple[int, str, str]:
    """Обменивает request token + verifier на постоянные токены. Возвращает (user_id, access_token, access_secret)."""
    now = datetime.now(timezone.utc)
    _purge_pending(now)
    pending = _PENDING.pop(oauth_token, None)
    if pending is None:
        raise FatSecretError("Ссылка устарела или уже использована. Запросите новую командой /link_fatsecret.")
    data = await _oauth_form(
        ACCESS_TOKEN_URL, {"oauth_verifier": oauth_verifier}, token=oauth_token, token_secret=pending.token_secret
    )
    return pending.user_id, data["oauth_token"], data["oauth_token_secret"]


def days_since_epoch(day: date) -> int:
    """FatSecret принимает дату как число дней с 1970-01-01."""
    return (day - _EPOCH).days


def parse_food_entries(data: Dict[str, Any]) -> DayTotals:
    """Суммирует калории и БЖУ из ответа food_entries.get (одна запись приходит объектом, не списком)."""
    container = data.get("food_entries")
    entries = container.get("food_entry") if isinstance(container, dict) else None
    if entries is None:
        entries = []
    elif isinstance(entries, dict):
        entries = [entries]

    totals = DayTotals()
    calories = Decimal("0")
    for entry in entries:
        calories += Decimal(str(entry.get("calories") or 0))
        totals.protein += Decimal(str(entry.get("protein") or 0))
        totals.fat += Decimal(str(entry.get("fat") or 0))
        totals.carbs += Decimal(str(entry.get("carbohydrate") or 0))
        totals.entries += 1
    totals.calories = int(calories.to_integral_value())
    return totals


async def fetch_day_totals(access_token: str, access_secret: str, day: date) -> DayTotals:
    """food_entries.get за указанный день -> суммарные КБЖУ."""
    params = {"method": "food_entries.get", "format": "json", "date": days_since_epoch(day)}
    resp = await _post(API_URL, params, token=access_token, token_secret=access_secret)
    if resp.status_code != 200:
        raise FatSecretError(f"FatSecret API вернул {resp.status_code}: {resp.text[:200]}")
    try:
        data = resp.json()
    except ValueError as e:
        raise FatSecretError(f"FatSecret вернул не JSON: {resp.text[:200]}") from e
    if isinstance(data, dict) and "error" in data:
        err = data["error"]
        raise FatSecretError(f"FatSecret API: [{err.get('code')}] {err.get('message')}")
    return parse_food_entries(data)
