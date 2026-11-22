import re
import unicodedata
from datetime import datetime
from functools import lru_cache
from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Category, Transaction, Vacation

OUTGOING_TRANSFER = "Переводы и Сбережения (Исходящие)"
INCOMING_TRANSFER = "Переводы и Пополнения (Входящие)"
TRANSFER_CATEGORY_NAMES = {OUTGOING_TRANSFER, INCOMING_TRANSFER}

# Расширенный список из 15 базовых категорий
DEFAULT_CATEGORIES: List[Dict[str, Any]] = [
    {
        "name": "Зарплата и Стипендия",
        "type": "income",
        "is_mandatory": False,
        "keywords": [
            "заработная плата", "3apаботная плата", "зарплата за первую половину",
            "3aрплата", "3aарплата", "аванс по заработной плате", "abванс",
            "зарплата", "отпускные", "стипендия учащимся", "стипендия"
        ],
    },
    {
        "name": "Проценты по вкладам и Кэшбэк",
        "type": "income",
        "is_mandatory": False,
        "keywords": [
            "проценты на остаток", "капитализация", "кэшбэк за обычные",
            "кэшбэк за покупки", "кэшбэк", "возврат средств по оплате",
            "возврат покупки", "возврат оплаты", "проценты по вкладу"
        ],
    },
    {
        "name": INCOMING_TRANSFER,
        "type": "income",
        "is_mandatory": False,
        "keywords": [
            "перевод себе", "пополнение. система быстрых платежей",
            "пополнение. сбербанк онлайн", "внутрибанковский перевод",
            "перевод с договора", "перевод на карту", "sbol", "vklad-karta",
            "внесение наличных", "банковский перевод. ао \"райффайзенбанк\"",
            "перевод денежных средств", "перевод с17", "перевод с19", "перевод с47"
        ],
    },
    {
        "name": "ЖКХ",
        "type": "expense",
        "is_mandatory": True,
        "keywords": [
            "zhku", "жкх", "ук проспект в. клыкова", "спецавтобаза по уборке",
            "иц \"регион-курск\"", "регион-курск", "40601810233000000002",
            "40702810933000000890", "квартплата", "водоканал", "энергосбыт"
        ],
    },
    {
        "name": "Связь и интернет (оплата тарифа и впн)",
        "type": "expense",
        "is_mandatory": True,
        "keywords": [
            "ibank.mts", "mbank.mts", "мтс", "mts",
            "enryvpn", "епгуvpn", "vpn", "firstbyte", "hostkey", "ростелеком",
            "дом.ру", "мегафон", "билайн", "tele2"
        ],
    },
    {
        "name": "Подписки",
        "type": "expense",
        "is_mandatory": True,
        "keywords": [
            "сервисы яндекса", "vk vk.com", "яндекс плюс", "yandex plus",
            "кинопоиск", "иви", "okko", "сберпрайм", "tinkoff pro",
            "telegram premium", "spotify", "плата за обслуживание"
        ],
    },
    {
        "name": "Такси",
        "type": "expense",
        "is_mandatory": False,
        "keywords": [
            "fasten", "yandex*4121", "yandex go", "яндекс такси",
            "uber", "maxim"
        ],
    },
    {
        "name": "Транспорт",
        "type": "expense",
        "is_mandatory": False,
        "keywords": [
            "oplata_proezda", "oplata proezda", "sbertroika", "sbertrojka",
            "bilet.nspk.ru", "karty zhitelya", "podorozhnik", "cppk",
            "informseti.ru", "beltrans1.ru"
        ],
    },
    {
        "name": "Путешествия",
        "type": "expense",
        "is_mandatory": False,
        "keywords": [
            "ostrovok.ru", "оплата ж/д перевозок", "cosmos smart", "артбокс отель",
            "avokzaly", "buskassaa", "peterhof-express", "neva reka", "gorbilet",
            "ticketnet", "mazapark", "history of the museum", "orudiya pytok",
            "petrovskij ostrov", "prichal mt", "mayakovskaya", "m.pos.4aposadskaya",
            "vending moscow"
        ],
    },
    {
        "name": "Аптеки и медицина",
        "type": "expense",
        "is_mandatory": False,
        "keywords": [
            "apteka", "артека", "αρτεκα", "aprel", "garant", "mdt+",
            "счастливый взгляд", "на лечение", "ип максимова ирина евгеньевна",
            "ip savchenko", "здоровье и красота"
        ],
    },
    {
        "name": "Канцтовары",
        "type": "expense",
        "is_mandatory": False,
        "keywords": [
            "officemag", "chitay gorod", "читай-город", "killerantiplagiat",
            "канцтовары", "комус", "killerantiplagiatr"
        ],
    },
    {
        "name": "Дом",
        "type": "expense",
        "is_mandatory": False,
        "keywords": [
            "lemanapro", "lemanaprogpb073", "лемана про", "dns-shop", "dns цифровая", "novyy dom",
            "poryadok", "domof", "semeynyy univermag", "semejnyj univermag",
            "inflowers", "infl0wers", "cvety dost", "o'stin", "спортмастер", "gold apple",
            "goldapple", "goldapplevoron", "avito", "авито", "afisha_kassa", "gameattack",
            "gamepark", "bouling", "vk store", "plati.market", "onlipay",
            "aidapp", "prtm", "lgs", "gosuslugi", "tvoya_polka", "geek trip",
            "b125", "в125", "au1068", "t3 sankt", "т3 sankt", "все для дома"
        ],
    },
    {
        "name": "Кафе фастфуд и столовые",
        "type": "expense",
        "is_mandatory": False,
        "keywords": [
            "rostics", "makfud", "мakfud", "mak food", "mak food2", "мak food", "stolovaya",
            "trapeza", "трапеза", "papa lepit", "раpa lepit", "папа лепит",
            "dodo pizza", "додо пицца", "burger king", "бургер рус", "mybox",
            "мybox", "мүbox", "туbox", "tomato-pizza", "lion tomato", "tomato",
            "shalash lavash", "ishalash lavash", "zhar-picca", "zharpi",
            "pyshki", "pyshechnaya", "kofejnya", "coffee", "zerna kofe",
            "cofix", "onepricecoffee", "zerna kofe1", "bakery", "cheburechnaya", "сheburechnaya", "снheburechnaya",
            "rest central park", "evo_grillroom", "evo grillroom", "morkoff",
            "мorkoff", "starik khinkalych", "chikenkhauz", "чикен хауз", "чикенхауз",
            "kafe", "кafe", "bubble plug", "heybubble", "pims", "ріms",
            "eatandsplit", "midijnoe mesto", "zavtraktebe", "пэпэ", "good bear",
            "goodbear", "badbar", "kanelo", "kanelo2", "каnelo", "mandarinovyj gus",
            "zakusochnaya", "zakusochnaya2", "konditerskaya", "коnditerskaya", "svit ket",
            "vycota", "el kursk", "koktejl bar", "люблю pizza", "tseh46",
            "сh46", "сн46", "сh31", "снh31", "снh46", "26701 mop", "26702 mop",
            "ip koval", "ір koval", "ір кkoval", "syvovarnya", "teremok",
            "luna bar", "prichal picca", "stosha", "ѕtosha", "vlavashe",
            "котельная камчатка", "krang pizza", "pancake house", "morozh",
            "brynza", "chicko", "khulio", "barmuzej", "lavash36", "stardogs",
            "blinchik", "phobo", "рново", "кофе", "кафе", "столовая",
            "fresh kafe2", "kafe1", "midijnoe mesto1", "zharpizza", "sdobno udobno"
        ],
    },
    {
        "name": "Супермаркеты и продукты",
        "type": "expense",
        "is_mandatory": False,
        "keywords": [
            "pyaterochka", "пятерочка", "perekrestok", "перекресток", "magnit", "магнит", "fixprice",
            "krasnoe", "ermolin", "kurskkhleb", "pekarnya", "реkarnya",
            "sdobno", "slavyanka", "evropa", "samokat", "lenta", "auchan",
            "metro store", "мetro store", "liniya", "dary oboyani", "bristol",
            "dixy", "za pivasikom", "пивасиком", "khlebnyj_dvorik", "blizhnie gorki",
            "slaviya", "kapitaltorg", "каpitaltorg", "urozhaj", "pselckij",
            "produkty", "магазиin", "мagazin", "маgazin", "магазин 20326",
            "tdreal", "tdread", "tdreadmposadskaya13", "tdrealbmorskaya28", "mirk_внж", "harvest around", "ivo grupp",
            "ip gasanov", "ip inozemtseva", "ip larina", "ip sryvkova",
            "ip lyfar", "pe lashkareva", "ip romanovskiy", "ip moskalev",
            "ajsberg", "kiosk", "кіosk", "кiosk", "lavka miloty"
        ],
    },
    {
        "name": OUTGOING_TRANSFER,
        "type": "expense",
        "is_mandatory": False,
        "keywords": [
            "внешний перевод по номеру телефона", "внутренний перевод на договор",
            "внешний банковский перевод", "пополнение брокерского счета",
            "перевод в рамках услуги копилка для зарплаты", "копилка для зарплаты",
            "перевод с42", "перевод с16", "перевод сбп", "перевод с карты",
            "karta-vklad", "выдача наличных", "mapp sberbank_onl@in_pay",
            "yandexbank_c2a"
        ],
    },
]

# Карта переименования старых категорий в новые (чтобы не дублировать записи в БД)
LEGACY_RENAME_MAP = {
    "Кафе, фастфуд и столовые": "Кафе фастфуд и столовые",
    "ЖКХ, Связь и Интернет": "ЖКХ",
    "Транспорт и Подписки (Яндекс / Сервисы)": "Подписки",
    "Канцтовары и Дом": "Канцтовары",
    "Кэшбэк и Возвраты": "Проценты по вкладам и Кэшбэк",
}


_LOOKALIKES = str.maketrans("авекмнорстухіѕ", "abekmhopctyxis")
_ALFA_MERCHANT = re.compile(
    r"место совершения операции:\s*[^\\]*\\[A-Z]{2}\\[^\\]*\\([^\\]+)", re.IGNORECASE
)
_MCC = re.compile(r"\bmcc\s*[:=]?\s*(\d{4})\b", re.IGNORECASE)
_CARD_FOOTER = re.compile(r"\bоперация по карте\b", re.IGNORECASE)
_MERCHANT_PREFIX = re.compile(r"^(?:оплата в|оплата услуг)\s+")
_TRANSFER = re.compile(
    r"^(?:внешний (?:банковский )?перевод|внутрибанковский перевод|"
    r"внутренний перевод|пополнение (?:брокерского счета|счета|система быстрых платежей|сбербанк онлайн)|"
    r"выдача наличных|внесение наличных|перевод (?:[cс]\d{14,}|сбп|себе|на карту|"
    r"с карты|с договора|денежных средств|в рамках услуги копилка для зарплаты))\b"
)
_TRANSFER_MARKERS = ("sberbank onl in karta vklad", "vklad karta", "mapp sberbank onl in pay")
_BANK_HEADINGS = {
    "супермаркеты": "Супермаркеты и продукты",
    "рестораны и кафе": "Кафе фастфуд и столовые",
    "транспорт": "Транспорт",
    "прочие расходы": None,
    "прочие операции": None,
    "здоровье и красота": None,
    "одежда и аксессуары": None,
    "оплата по qr коду сбп": None,
}
_MCC_CATEGORIES = {
    "4111": "Транспорт",
    "4121": "Такси",
    "5200": "Дом",
    "5411": "Супермаркеты и продукты",
    "5499": "Супермаркеты и продукты",
    "5812": "Кафе фастфуд и столовые",
    "5814": "Кафе фастфуд и столовые",
    "5912": "Аптеки и медицина",
    "5943": "Канцтовары",
}
_IGNORED_KEYWORDS = {
    *_BANK_HEADINGS,
    "магазин", "магазин продуктов", "magazin", "magaziin", "операция по карте",
    "agnit", "yaterochk",
}


def _normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).casefold().replace("ё", "е")
    words = re.findall(r"[^\W_]+", text)
    # Эквайринг иногда смешивает алфавиты внутри одного названия (например, МYBOX).
    return " ".join(
        word.translate(_LOOKALIKES) if re.search("[a-z]", word) and re.search("[а-яіѕ]", word) else word
        for word in words
    )


_normalize_keyword = lru_cache(maxsize=2048)(_normalize)


_DEFAULT_KEYWORDS = {
    item["name"]: {_normalize_keyword(keyword) for keyword in item["keywords"]}
    for item in DEFAULT_CATEGORIES
}


def _has_phrase(text: str, phrase: str) -> bool:
    return bool(phrase) and f" {phrase} " in f" {text} "


def _merchant_text(description: str) -> tuple[str, Optional[str]]:
    alfa = _ALFA_MERCHANT.search(description)
    if alfa:
        return _normalize(_MCC.split(alfa[1], maxsplit=1)[0].split(" (")[0]), None

    text = _normalize(_CARD_FOOTER.split(description, maxsplit=1)[0])
    for heading, category_name in _BANK_HEADINGS.items():
        if text == heading or text.startswith(f"{heading} "):
            return text[len(heading):].strip(), category_name
    return _MERCHANT_PREFIX.sub("", text), None


async def seed_default_categories(session: AsyncSession, *, commit: bool = True) -> None:
    """Синхронизирует таблицу categories с эталонным списком из 15 категорий."""
    result = await session.execute(select(Category))
    existing_cats = list(result.scalars().all())
    existing_by_name = {cat.name: cat for cat in existing_cats}

    changed = False

    # 1. Переименовываем старые категории, если новых еще нет
    for old_name, new_name in LEGACY_RENAME_MAP.items():
        if old_name in existing_by_name and new_name not in existing_by_name:
            cat_obj = existing_by_name.pop(old_name)
            cat_obj.name = new_name
            existing_by_name[new_name] = cat_obj
            changed = True

    # 2. Создаем или обновляем 15 эталонных категорий
    for item in DEFAULT_CATEGORIES:
        name = item["name"]
        if name not in existing_by_name:
            session.add(
                Category(
                    name=name,
                    type=item["type"],
                    is_mandatory=item["is_mandatory"],
                    keywords=item["keywords"],
                )
            )
            changed = True
        else:
            cat_obj = existing_by_name[name]
            if cat_obj.is_mandatory != item["is_mandatory"]:
                cat_obj.is_mandatory = item["is_mandatory"]
                changed = True
            # Дополняем ключевые слова новыми, сохраняя те, которым парсер уже обучился
            current_kw = list(cat_obj.keywords or [])
            current_kw_lower = {k.lower() for k in current_kw}
            for kw in item["keywords"]:
                if kw.lower() not in current_kw_lower:
                    current_kw.append(kw)
                    changed = True
            cat_obj.keywords = current_kw

    if changed and commit:
        await session.commit()


def match_category(
    description: str, amount_is_positive: bool, categories: List[Category], *, bank_name: str = ""
) -> Optional[Category]:
    """Выбирает категорию по направлению, типу операции, продавцу и данным банка."""
    if not description:
        return None

    expected_type = "income" if amount_is_positive else "expense"
    candidates = {category.name: category for category in categories if category.type == expected_type}
    merchant, bank_category = _merchant_text(description)

    # Проверяем префикс исходной операции. В названии продавца тоже может быть «Пополнение».
    operation = _normalize(_CARD_FOOTER.split(description, maxsplit=1)[0])
    if _TRANSFER.match(operation) or any(_has_phrase(operation, marker) for marker in _TRANSFER_MARKERS):
        # Коммунальные платежи по реквизитам Т-Банк описывает как банковские переводы.
        utilities = candidates.get("ЖКХ")
        if utilities and operation.startswith("внешний банковский перевод "):
            if any(_has_phrase(operation, _normalize_keyword(word)) for word in utilities.keywords or ()):
                return utilities
        transfer_name = INCOMING_TRANSFER if amount_is_positive else OUTGOING_TRANSFER
        return candidates.get(transfer_name)

    best_score = None
    best_category = None
    for category in candidates.values():
        default_keywords = _DEFAULT_KEYWORDS.get(category.name, set())
        for raw_keyword in category.keywords or ():
            keyword = _normalize_keyword(raw_keyword)
            if keyword in _IGNORED_KEYWORDS or keyword.isdecimal() or not _has_phrase(merchant, keyword):
                continue
            score = (keyword not in default_keywords, len(keyword.split()), len(keyword))
            if best_score is None or score > best_score:
                best_score, best_category = score, category
            elif score == best_score and category != best_category:
                best_category = None

    if best_score is not None:
        return best_category

    if amount_is_positive:
        return None
    if not bank_name or bank_name.casefold() == "альфа-банк":
        mcc = _MCC.search(description) if _ALFA_MERCHANT.search(description) else None
        if mcc and mcc[1] in _MCC_CATEGORIES:
            return candidates.get(_MCC_CATEGORIES[mcc[1]])
    if bank_name.casefold() == "сбербанк" or (not bank_name and _CARD_FOOTER.search(description)):
        return candidates.get(bank_category)
    return None


def match_category_id(
    description: str, amount_is_positive: bool, categories: List[Category]
) -> Optional[int]:
    cat = match_category(description, amount_is_positive, categories)
    return cat.id if cat else None


def _is_date_in_vacations(tx_date: datetime, vacations: List[Vacation]) -> bool:
    """Проверяет, попадает ли дата транзакции в любой из периодов отпуска пользователя."""
    tx_naive = tx_date.replace(tzinfo=None)
    for vac in vacations:
        v_start = vac.start_date.replace(tzinfo=None)
        v_end = vac.end_date.replace(tzinfo=None)
        if v_start <= tx_naive <= v_end:
            return True
    return False


def extract_smart_keyword(description: str) -> str:
    """Предлагает правило по продавцу, если в описании есть устойчивое название."""
    merchant, _ = _merchant_text(description)
    if _TRANSFER.match(merchant) or any(_has_phrase(merchant, marker) for marker in _TRANSFER_MARKERS):
        return ""

    # Удаляем терминал и географию, сохраняя связную фразу из исходного описания.
    merchant = re.split(
        r"\b(?:\d+|kursk|moskva|moscow|sankt|rus|ru|mcc\d+)\b", merchant, maxsplit=1
    )[0].strip()
    keyword = " ".join(merchant.split()[:3])
    return _learnable_keyword(keyword)


def _learnable_keyword(keyword: str) -> str:
    if keyword in _IGNORED_KEYWORDS or keyword in {"оплата", "покупка", "перевод", "сбп"}:
        return ""
    if sum(char.isalpha() for char in keyword) < 3:
        return ""
    return keyword


async def learn_from_correction(
    session: AsyncSession,
    transaction_id: int,
    new_category_id: int,
    custom_keyword: Optional[str] = None,
    user_id: Optional[int] = None,
) -> Dict[str, Any]:
    """Обучает категоризатор на основе ручной правки пользователя.

    ``user_id`` — владелец, от имени которого идёт правка: чужая транзакция считается ненайденной.
    """
    tx = await session.get(Transaction, transaction_id)
    if not tx or (user_id is not None and tx.user_id != user_id):
        raise ValueError(f"Транзакция с ID {transaction_id} не найдена.")

    new_cat = await session.get(Category, new_category_id)
    if not new_cat:
        raise ValueError(f"Категория с ID {new_category_id} не найдена.")

    keyword = _learnable_keyword(_normalize(custom_keyword)) if custom_keyword else extract_smart_keyword(tx.description)
    expected_type = "income" if tx.amount > 0 else "expense"
    if new_cat.type != expected_type:
        keyword = ""

    tx.category_id = new_category_id
    tx.is_parsed_automatically = False

    auto_updated_count = 0
    if keyword:
        categories = list((await session.execute(select(Category).where(Category.type == expected_type))).scalars())
        for category in categories:
            if category.id == new_category_id or keyword in _DEFAULT_KEYWORDS.get(category.name, ()):
                continue
            keywords = [word for word in category.keywords or () if _normalize_keyword(word) != keyword]
            if keywords != list(category.keywords or ()):
                category.keywords = keywords
        if all(_normalize_keyword(word) != keyword for word in new_cat.keywords or ()):
            new_cat.keywords = [*(new_cat.keywords or ()), keyword]

        direction = Transaction.amount > 0 if tx.amount > 0 else Transaction.amount <= 0
        stmt = select(Transaction).where(
            Transaction.user_id == tx.user_id,
            Transaction.id != tx.id,
            direction,
            Transaction.is_parsed_automatically.is_(True) | Transaction.category_id.is_(None),
        )
        res = await session.execute(stmt)
        for other_tx in res.scalars().all():
            merchant, _ = _merchant_text(other_tx.description)
            if other_tx.category_id == new_category_id or not _has_phrase(merchant, keyword):
                continue
            matched = match_category(other_tx.description, tx.amount > 0, categories, bank_name=other_tx.bank_name)
            if matched == new_cat:
                other_tx.category_id = new_category_id
                other_tx.is_parsed_automatically = True
                auto_updated_count += 1

    await session.commit()

    return {
        "transaction_id": tx.id,
        "new_category_id": new_cat.id,
        "new_category_name": new_cat.name,
        "learned_keyword": keyword,
        "similar_transactions_updated": auto_updated_count,
    }
