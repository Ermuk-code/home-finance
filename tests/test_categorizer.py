import pytest

from app.finance.services.categorizer import (
    DEFAULT_CATEGORIES,
    INCOMING_TRANSFER,
    OUTGOING_TRANSFER,
    extract_smart_keyword,
    match_category,
)
from app.models import Category


@pytest.fixture
def categories():
    return [Category(**row) for row in DEFAULT_CATEGORIES]


@pytest.mark.parametrize(("bank", "description", "income", "expected"), [
    ("Альфа-Банк", "Перевод C420101260000000 через Систему быстрых платежей на +70000000000", False, OUTGOING_TRANSFER),
    ("Альфа-Банк", "Перевод C170101260000000 через Систему быстрых платежей от +70000000000", True, INCOMING_TRANSFER),
    ("Альфа-Банк", r"Место совершения операции: 123\RU\Kursk\NEIZVESTNYJ MAGAZIN (MP-0000) MCC5411",
     False, "Супермаркеты и продукты"),
    ("СберБанк", "Перевод с карты АО Т-Банк. Операция по карте ****0000", True, INCOMING_TRANSFER),
    ("СберБанк", "Прочие операции MAPP_SBERBANK_ONL@IN_PAY. Операция по карте ****0000", False, OUTGOING_TRANSFER),
    ("СберБанк", "Супермаркеты НЕИЗВЕСТНЫЙ МАГАЗИН. Операция по карте ****0000", False, "Супермаркеты и продукты"),
    ("СберБанк", "Прочие расходы НЕИЗВЕСТНЫЙ МАГАЗИН. Операция по карте ****4121", False, None),
    ("СберБанк", "Оплата по QR–коду СБП Незнакомый продавец", False, None),
    ("СберБанк", "Одежда и аксессуары O'STIN. Операция по карте ****0000", False, "Дом"),
    ("Т-Банк", "Внешний перевод по +70000000000", False, OUTGOING_TRANSFER),
    ("Т-Банк", "Внешний банковский перевод на счёт, получатель Водоканал", False, "ЖКХ"),
    ("Т-Банк", "Оплата в VK*VK.COM St. Petersbur RUS", False, "Подписки"),
    ("Т-Банк", "Оплата в МYBOX Kursk RUS", False, "Кафе фастфуд и столовые"),
    ("Т-Банк", "Оплата в MAK FOOD2 Kursk RUS", False, "Кафе фастфуд и столовые"),
    ("Т-Банк", "Оплата в KHLEBNYJ__DVORIK Kursk RUS", False, "Супермаркеты и продукты"),
    ("Т-Банк", "Оплата в MAGNIT MM Kursk RUS", False, "Супермаркеты и продукты"),
    ("Т-Банк", "Оплата в YANDEX*4121*GO Moscow RUS", False, "Такси"),
    ("Т-Банк", "Оплата в YANDEX*5814*MARKET Moscow RUS", False, None),
    ("Т-Банк", "Оплата в MAXIMIZE", False, None),
    ("", "Зарплата", False, None),
])
def test_bank_category_rules(categories, bank, description, income, expected):
    matched = match_category(description, income, categories, bank_name=bank)
    assert (matched.name if matched else None) == expected


def test_legacy_keywords_do_not_override_bank_description(categories):
    groceries = next(category for category in categories if category.name == "Супермаркеты и продукты")
    groceries.keywords = [*groceries.keywords, "прочие расходы", "одежда и аксессуары"]
    cafe = next(category for category in categories if category.name == "Кафе фастфуд и столовые")
    cafe.keywords = [*cafe.keywords, "оплата по qr-коду сбп"]

    assert match_category("Прочие расходы ABC. Операция по карте ****0000", False, categories) is None
    assert match_category("Одежда и аксессуары ABC. Операция по карте ****0000", False, categories) is None
    assert match_category("Оплата по QR-коду СБП ABC", False, categories) is None


def test_custom_keyword_wins_over_seeded_rule_and_ambiguous_rules_abstain(categories):
    custom = Category(name="Особая категория", type="expense", keywords=["papa lepit"])
    categories.append(custom)
    assert match_category("Оплата в PAPA LEPIT 1234", False, categories) is custom

    categories.append(Category(name="Другая категория", type="expense", keywords=["papa lepit"]))
    assert match_category("Оплата в PAPA LEPIT 1234", False, categories) is None


@pytest.mark.parametrize(("description", "expected"), [
    (r"Место совершения операции: 123\RU\Kursk\PAPA LEPIT (MP-0000) MCC5814", "papa lepit"),
    ("Прочие расходы SBER SAMOKAT 0000. Операция по карте ****0000", "sber samokat"),
    ("Внешний перевод по +70000000000", ""),
    ("Оплата по QR–коду СБП", ""),
])
def test_extract_smart_keyword(description, expected):
    assert extract_smart_keyword(description) == expected
