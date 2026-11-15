import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import Category, Transaction, Vacation
from services.parser import ParsedTransaction

TRANSFER_CATEGORY_NAMES = {
    "Переводы и Сбережения (Исходящие)",
    "Переводы и Пополнения (Входящие)",
}

# Расширенный список из 15 базовых категорий
DEFAULT_CATEGORIES: List[Dict[str, Any]] = [
    {
        "name": "Зарплата и Стипендия",
        "type": "income",
        "is_mandatory": False,
        "keywords": [
            "заработная плата", "3apаботная плата", "зарплата за первую половину",
            "3aрплата", "3aарплата", "аванс по заработной плате", "abванс",
            "отпускные", "стипендия учащимся", "стипендия"
        ],
    },
    {
        "name": "Проценты по вкладам и Кэшбэк",
        "type": "income",
        "is_mandatory": False,
        "keywords": [
            "проценты на остаток", "капитализация", "кэшбэк за обычные",
            "кэшбэк за покупки", "кэшбэк", "возврат средств по оплате",
            "возврат покупки"
        ],
    },
    {
        "name": "Переводы и Пополнения (Входящие)",
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
            "ibank.mts", "mbank.mts", "оплата в мтс", "оплата в мtc", "mts",
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
            "4121", "fasten", "yandex*4121", "yandex 4121", "яндекс такси",
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
            "канцтовары", "комус"
        ],
    },
    {
        "name": "Дом",
        "type": "expense",
        "is_mandatory": False,
        "keywords": [
            "lemanapro", "лемана про", "dns-shop", "dns цифровая", "novyy dom",
            "poryadok", "domof", "semeynyy univermag", "semejnyj univermag",
            "inflowers", "cvety dost", "o'stin", "спортмастер", "gold apple",
            "goldapple", "avito", "авито", "afisha_kassa", "gameattack",
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
            "rostics", "makfud", "мakfud", "mak food", "мak food", "stolovaya",
            "trapeza", "трапеза", "papa lepit", "раpa lepit", "папа лепит",
            "dodo pizza", "додо пицца", "burger king", "бургер рус", "mybox",
            "мybox", "мүbox", "туbox", "tomato-pizza", "lion tomato", "тоmato",
            "shalash lavash", "ishalash lavash", "zhar-picca", "zharpi",
            "pyshki", "pyshechnaya", "kofejnya", "coffee", "zerna kofe",
            "cofix", "bakery", "cheburechnaya", "сheburechnaya", "снheburechnaya",
            "rest central park", "evo_grillroom", "evo grillroom", "morkoff",
            "мorkoff", "starik khinkalych", "chikenkhauz", "чикен хауз",
            "kafe", "кafe", "bubble plug", "heybubble", "pims", "ріms",
            "eatandsplit", "midijnoe mesto", "zavtraktebe", "пэпэ", "good bear",
            "goodbear", "badbar", "kanelo", "каnelo", "mandarinovyj gus",
            "zakusochnaya", "konditerskaya", "коnditerskaya", "svit ket",
            "vycota", "el kursk", "koktejl bar", "люблю pizza", "tseh46",
            "сh46", "сн46", "сh31", "снh31", "снh46", "26701 mop", "26702 mop",
            "ip koval", "ір koval", "ір кkoval", "syvovarnya", "teremok",
            "luna bar", "prichal picca", "stosha", "ѕtosha", "vlavashe",
            "котельная камчатка", "krang pizza", "pancake house", "morozh",
            "brynza", "chicko", "khulio", "barmuzej", "lavash36", "stardogs",
            "blinchik", "phobo", "рново", "рестораны и кафе", "оплата по qr-коду сбп"
        ],
    },
    {
        "name": "Супермаркеты и продукты",
        "type": "expense",
        "is_mandatory": False,
        "keywords": [
            "yaterochk", "perekrestok", "перекресток", "agnit", "fixprice",
            "krasnoe", "ermolin", "kurskkhleb", "pekarnya", "реkarnya",
            "sdobno", "slavyanka", "evropa", "samokat", "lenta", "auchan",
            "metro store", "мetro store", "liniya", "dary oboyani", "bristol",
            "dixy", "za pivasikom", "пивасиком", "khlebnyj_dvorik", "blizhnie gorki",
            "slaviya", "kapitaltorg", "каpitaltorg", "urozhaj", "pselckij",
            "produkty", "магазиin", "мagazin", "маgazin", "магазин 20326",
            "tdreal", "tdread", "mirk_внж", "harvest around", "ivo grupp",
            "ip gasanov", "ip inozemtseva", "ip larina", "ip sryvkova",
            "ip lyfar", "pe lashkareva", "ip romanovskiy", "ip moskalev",
            "ajsberg", "kiosk", "кіosk", "кiosk", "lavka miloty",
            "супермаркеты", "одежда и аксессуары", "прочие расходы"
        ],
    },
    {
        "name": "Переводы и Сбережения (Исходящие)",
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


async def seed_default_categories(session: AsyncSession) -> None:
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

    if changed:
        await session.commit()


def match_category(
    description: str, amount_is_positive: bool, categories: List[Category]
) -> Optional[Category]:
    """
    Ищет подходящий объект Category по ключевым словам с учетом направления (income / expense).
    """
    if not description:
        return None

    desc_lower = description.lower()
    expected_type = "income" if amount_is_positive else "expense"

    for category in categories:
        if category.type != expected_type or not category.keywords:
            continue
        for keyword in category.keywords:
            if keyword.lower() in desc_lower:
                return category

    for category in categories:
        if not category.keywords:
            continue
        for keyword in category.keywords:
            if keyword.lower() in desc_lower:
                return category

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
    """Выделяет из банковского описания ключевое слово для самообучения."""
    text = description.lower()

    alfa_place = re.search(
        r"место совершения операции:\s*[^\s\\]+\\ru\\[^\\]+\\([^\n]+?)(?:\s+mcc\d+|\s*$)",
        text,
    )
    if alfa_place:
        return alfa_place.group(1).strip()

    noise_patterns = [
        r"^оплата в\s+",
        r"^оплата услуг\s+",
        r"операция по карте[:\s\*0-9\+]*",
        r"на сумму:\s*[\d\.,]+\s*rur,?",
        r"дата совершения операции:\s*[\d\.]+,?",
        r"\b(kursk|moskva|moscow|sankt-peterbu|rus|ru|g\.)\b",
        r"\bmcc\d+\b",
        r"\+?\d{10,15}",
        r"\b\d{4,}\b",
        r"^(супермаркеты|прочие операции|прочие расходы|здоровье и красота|одежда и аксессуары)\s+",
    ]
    for pat in noise_patterns:
        text = re.sub(pat, " ", text, flags=re.IGNORECASE)

    text = re.sub(r"\s+", " ", text).strip(" .,;-*\\/")
    tokens = [t for t in text.split() if len(t) >= 3]
    if tokens:
        return " ".join(tokens[:2])

    return description.strip().lower()[:30]


async def learn_from_correction(
    session: AsyncSession,
    transaction_id: int,
    new_category_id: int,
    custom_keyword: Optional[str] = None,
) -> Dict[str, Any]:
    """Обучает категоризатор на основе ручной правки пользователя."""
    tx = await session.get(Transaction, transaction_id)
    if not tx:
        raise ValueError(f"Транзакция с ID {transaction_id} не найдена.")

    new_cat = await session.get(Category, new_category_id)
    if not new_cat:
        raise ValueError(f"Категория с ID {new_category_id} не найдена.")

    old_category_id = tx.category_id
    keyword = (custom_keyword or extract_smart_keyword(tx.description)).strip().lower()

    if old_category_id and old_category_id != new_category_id:
        old_cat = await session.get(Category, old_category_id)
        if old_cat and old_cat.keywords:
            desc_lower = tx.description.lower()
            old_cat.keywords = [
                k for k in old_cat.keywords
                if k.lower() != keyword and (k.lower() not in desc_lower or len(k) < 4)
            ]

    current_keywords = list(new_cat.keywords or [])
    if keyword and all(k.lower() != keyword for k in current_keywords):
        current_keywords.append(keyword)
        new_cat.keywords = current_keywords

    tx.category_id = new_category_id
    tx.is_parsed_automatically = False

    auto_updated_count = 0
    if keyword and len(keyword) >= 3:
        stmt = select(Transaction).where(
            Transaction.user_id == tx.user_id,
            Transaction.id != tx.id,
        )
        res = await session.execute(stmt)
        for other_tx in res.scalars().all():
            same_direction = (other_tx.amount > 0) == (tx.amount > 0)
            if same_direction and keyword in other_tx.description.lower():
                if other_tx.category_id != new_category_id:
                    other_tx.category_id = new_category_id
                    auto_updated_count += 1

    await session.commit()

    return {
        "transaction_id": tx.id,
        "new_category_id": new_cat.id,
        "new_category_name": new_cat.name,
        "learned_keyword": keyword,
        "similar_transactions_updated": auto_updated_count,
    }


async def save_parsed_transactions(
    session: AsyncSession, user_id: int, parsed_items: List[ParsedTransaction]
) -> Tuple[int, int, int]:
    """
    Категоризирует и сохраняет транзакции в БД.
    Включает проектный перехват отпусков: если расход совершен в даты отпуска,
    назначается категория «Путешествия» (кроме обязательных платежей и переводов).
    """
    await seed_default_categories(session)

    cat_result = await session.execute(select(Category))
    categories = list(cat_result.scalars().all())
    travel_cat = next((c for c in categories if c.name == "Путешествия"), None)

    # Загружаем все периоды отпусков пользователя
    vac_result = await session.execute(select(Vacation).where(Vacation.user_id == user_id))
    user_vacations = list(vac_result.scalars().all())

    existing_res = await session.execute(
        select(Transaction.date, Transaction.amount, Transaction.bank_name, Transaction.description)
        .where(Transaction.user_id == user_id)
    )
    existing_signatures = {
        (row.date.replace(tzinfo=None), row.amount, row.bank_name, row.description[:40])
        for row in existing_res.all()
    }

    saved_count = 0
    categorized_count = 0
    skipped_duplicates = 0

    for item in parsed_items:
        sig = (item.date.replace(tzinfo=None), item.amount, item.bank_name, item.description[:40])
        if sig in existing_signatures:
            skipped_duplicates += 1
            continue

        existing_signatures.add(sig)

        is_income = item.amount > 0
        matched_cat = match_category(item.description, is_income, categories)

        # Умный перехват отпуска (только для расходов)
        if not is_income and travel_cat and user_vacations and _is_date_in_vacations(item.date, user_vacations):
            # Исключение: если операция распознана как обязательная (ЖКХ, Связь, Подписки)
            # или это внутренний перевод — оставляем базовую категорию
            is_mandatory_match = matched_cat is not None and matched_cat.is_mandatory
            is_transfer_match = matched_cat is not None and matched_cat.name in TRANSFER_CATEGORY_NAMES
            if not (is_mandatory_match or is_transfer_match):
                matched_cat = travel_cat

        category_id = matched_cat.id if matched_cat else None
        is_auto = category_id is not None
        if is_auto:
            categorized_count += 1

        tx = Transaction(
            user_id=user_id,
            category_id=category_id,
            amount=item.amount,
            description=item.description,
            date=item.date,
            bank_name=item.bank_name,
            is_parsed_automatically=is_auto,
        )
        session.add(tx)
        saved_count += 1

    await session.commit()
    return saved_count, categorized_count, skipped_duplicates