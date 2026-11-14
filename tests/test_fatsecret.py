"""FatSecret: подпись OAuth 1.0a, разбор food_entries.get, даты, ожидающие привязки."""
import asyncio
from datetime import date
from decimal import Decimal

import pytest

from app.health import fatsecret_client as fs
from app.health.nutrition_sync import format_day_summary
from app.health.oauth1 import sign_params, signature


def test_oauth1_signature_matches_published_vector():
    # Пример из документации Twitter «Creating a signature» (канонический тест-вектор OAuth 1.0a)
    params = {
        "status": "Hello Ladies + Gentlemen, a signed OAuth request!",
        "include_entities": "true",
        "oauth_consumer_key": "xvz1evFS4wEEPTGEFPHBog",
        "oauth_nonce": "kYjzVBB8Y0ZFabxSWbWovY3uYSQ2pTgmZeNu2VS4cg",
        "oauth_signature_method": "HMAC-SHA1",
        "oauth_timestamp": "1318622958",
        "oauth_token": "370773112-GmHxMAgYyLbNEtIKZeRNFsMKPR9EyMZeS9weJAEb",
        "oauth_version": "1.0",
    }
    sig = signature(
        "POST",
        "https://api.twitter.com/1.1/statuses/update.json",
        params,
        "kAcSOqF21Fu85e7zjz7ZN2U4ZRhfV3WpwPAoE3Z7kBw",
        "LswwdoUaIvS8ltyTt5jkRh4J50vUPVVHtR2YPi5kE",
    )
    assert sig == "hCtSmYh+iHYCEqBWrE7C7hYmtUk="


def test_sign_params_adds_oauth_fields():
    out = sign_params("POST", "https://x/y", {"a": 1}, "ck", "cs", "tok", "ts", nonce="n", timestamp=1)
    assert out["a"] == "1" and out["oauth_token"] == "tok" and out["oauth_signature"]
    assert out["oauth_signature_method"] == "HMAC-SHA1" and out["oauth_timestamp"] == "1"


def test_days_since_epoch():
    assert fs.days_since_epoch(date(1970, 1, 1)) == 0
    assert fs.days_since_epoch(date(2026, 11, 7)) == 20764


def test_parse_multiple_entries():
    data = {"food_entries": {"food_entry": [
        {"calories": "250", "protein": "10.5", "fat": "5", "carbohydrate": "30.2"},
        {"calories": "100", "protein": "1.0", "fat": "0.5", "carbohydrate": "20"},
    ]}}
    t = fs.parse_food_entries(data)
    assert (t.calories, t.protein, t.fat, t.carbs, t.entries) == (350, Decimal("11.5"), Decimal("5.5"), Decimal("50.2"), 2)


def test_parse_single_entry_is_dict():
    t = fs.parse_food_entries({"food_entries": {"food_entry": {"calories": "95", "protein": "0.5", "fat": "0.3", "carbohydrate": "25"}}})
    assert t.calories == 95 and t.entries == 1


@pytest.mark.parametrize("empty", [{}, {"food_entries": None}, {"food_entries": {}}])
def test_parse_empty_day(empty):
    t = fs.parse_food_entries(empty)
    assert t.calories == 0 and t.entries == 0


def test_pending_link_is_single_use_and_unknown_token_rejected(monkeypatch):
    async def fake_form(url, params, token=None, token_secret=""):
        if url == fs.REQUEST_TOKEN_URL:
            return {"oauth_token": "rt", "oauth_token_secret": "rs"}
        assert (token, token_secret, params["oauth_verifier"]) == ("rt", "rs", "ver")
        return {"oauth_token": "at", "oauth_token_secret": "as"}

    monkeypatch.setattr(fs, "_oauth_form", fake_form)
    url = asyncio.run(fs.start_link(42))
    assert url.endswith("oauth_token=rt")
    assert asyncio.run(fs.finish_link("rt", "ver")) == (42, "at", "as")
    with pytest.raises(fs.FatSecretError):
        asyncio.run(fs.finish_link("rt", "ver"))  # повторно нельзя
    with pytest.raises(fs.FatSecretError):
        asyncio.run(fs.finish_link("чужой", "ver"))


def test_summary_text_escapes_name():
    t = fs.DayTotals(calories=2100, protein=Decimal("120"), fat=Decimal("60"), carbs=Decimal("250"))
    text = format_day_summary("<b>Влад", t)
    assert "&lt;b&gt;Влад" in text and "2100 ккал" in text and "Белки: 120 г" in text
