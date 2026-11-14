"""Вебхук веса: валидация payload, поиск профиля, изоляция уведомлений."""
from decimal import Decimal
from types import SimpleNamespace

import asyncio

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.health import router as health_router
from app.health.schemas import WeightWebhookPayload


def test_payload_accepts_profile_alias_and_legacy_target_user():
    assert WeightWebhookPayload(profile_alias="admin", weight_kg=82.4).weight_kg == Decimal("82.4")
    assert WeightWebhookPayload(target_user=" user ", weight_kg="60").profile_alias == "user"


@pytest.mark.parametrize("bad", [{"weight_kg": 80}, {"profile_alias": "admin"},
                                 {"profile_alias": "admin", "weight_kg": "unknown"},
                                 {"profile_alias": "admin", "weight_kg": 5},
                                 {"profile_alias": "  ", "weight_kg": 80}])
def test_payload_rejects_invalid(bad):
    with pytest.raises(ValidationError):
        WeightWebhookPayload(**bad)


class _Result:
    def __init__(self, rows): self._rows = rows
    def scalars(self): return self
    def all(self): return self._rows
    def first(self): return self._rows[0] if self._rows else None


class _Session:
    def __init__(self, *batches): self._batches = list(batches)
    async def execute(self, _stmt): return _Result(self._batches.pop(0))


def test_resolve_by_role():
    admin = SimpleNamespace(id=1)
    assert asyncio.run(health_router._resolve_profile(_Session([admin]), "Admin")) is admin


def test_resolve_ambiguous_and_missing():
    with pytest.raises(HTTPException) as e:
        asyncio.run(health_router._resolve_profile(_Session([1, 2]), "user"))
    assert e.value.status_code == 409
    with pytest.raises(HTTPException) as e:
        asyncio.run(health_router._resolve_profile(_Session([], []), "ghost"))
    assert e.value.status_code == 404


def test_notify_goes_only_to_owner(monkeypatch):
    sent = []

    async def fake_send(chat_id, text, parse_mode="HTML"):
        sent.append(chat_id)
        return True

    monkeypatch.setattr(health_router, "send_telegram_message", fake_send)
    monkeypatch.setattr(health_router.settings, "WEIGHT_ADMIN_DIGEST", False)
    partner = SimpleNamespace(id=2, telegram_id=222, name="Вика", role="user")
    log = SimpleNamespace(weight_kg=Decimal("60.0"), bmi=None)
    assert asyncio.run(health_router._notify_weight(_Session(), partner, log, None)) is True
    assert sent == [222]


def test_admin_digest_has_no_weight(monkeypatch):
    sent = []

    async def fake_send(chat_id, text, parse_mode="HTML"):
        sent.append((chat_id, text))
        return True

    monkeypatch.setattr(health_router, "send_telegram_message", fake_send)
    monkeypatch.setattr(health_router.settings, "WEIGHT_ADMIN_DIGEST", True)
    partner = SimpleNamespace(id=2, telegram_id=222, name="Вика", role="user")
    admin = SimpleNamespace(id=1, telegram_id=111, name="Я", role="admin")
    log = SimpleNamespace(weight_kg=Decimal("60.0"), bmi=None)
    asyncio.run(health_router._notify_weight(_Session([admin]), partner, log, None))
    assert [c for c, _ in sent] == [222, 111]
    assert "60" not in sent[1][1]
