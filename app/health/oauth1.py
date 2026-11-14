"""Минимальная реализация OAuth 1.0a (HMAC-SHA1, RFC 5849) — только стандартная библиотека.

Нужна для FatSecret: пользовательские методы (food_entries.get и др.) доступны только через OAuth 1.0a,
OAuth 2.0 у FatSecret поддерживает лишь методы без привязки к пользователю.
"""
import base64
import hashlib
import hmac
import secrets
import time
from typing import Dict, Mapping, Optional


def pct(value: object) -> str:
    """Процентное кодирование по RFC 3986 (незащищённые символы: A-Z a-z 0-9 - . _ ~)."""
    from urllib.parse import quote

    return quote(str(value), safe="~")


def signature(method: str, url: str, params: Mapping[str, object], consumer_secret: str, token_secret: str = "") -> str:
    """Подпись запроса. params — все параметры запроса и oauth_* (кроме oauth_signature)."""
    pairs = sorted((pct(k), pct(v)) for k, v in params.items())
    normalized = "&".join(f"{k}={v}" for k, v in pairs)
    base = "&".join([method.upper(), pct(url), pct(normalized)])
    key = f"{pct(consumer_secret)}&{pct(token_secret)}"
    digest = hmac.new(key.encode(), base.encode(), hashlib.sha1).digest()
    return base64.b64encode(digest).decode()


def sign_params(
    method: str,
    url: str,
    params: Mapping[str, object],
    consumer_key: str,
    consumer_secret: str,
    token: Optional[str] = None,
    token_secret: str = "",
    *,
    nonce: Optional[str] = None,
    timestamp: Optional[int] = None,
) -> Dict[str, str]:
    """Возвращает params + oauth_* + oauth_signature (всё строками — готово для тела POST-запроса)."""
    oauth = {
        "oauth_consumer_key": consumer_key,
        "oauth_nonce": nonce or secrets.token_hex(16),
        "oauth_signature_method": "HMAC-SHA1",
        "oauth_timestamp": str(timestamp if timestamp is not None else int(time.time())),
        "oauth_version": "1.0",
    }
    if token:
        oauth["oauth_token"] = token
    full = {**{k: str(v) for k, v in params.items()}, **oauth}
    full["oauth_signature"] = signature(method, url, full, consumer_secret, token_secret)
    return full
