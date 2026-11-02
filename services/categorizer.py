import re
from typing import Any, Dict, List, Optional, Tuple
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import Category, Transaction
from services.parser import ParsedTransaction

# Базовый набор категорий, учитывающий карты и накопительные счета Сбера, Альфы и Т-Банка
DEFAULT_CATEGORIES: List[Dict[str, Any]] = [
    {
        "name": "Супермаркеты и продукты",
        "type": "expense",
        "keywords": [
            "pyaterochka", "perekrestok", "magnit", "мagnit", "fixprice",
            "krasnoe", "ermolino", "kurskkhleb", "pekarnya", "sdobno",
            "slavyanka", "evropa", "samokat", "супермаркеты"
        ],
    },
    {
        "name": "Кафе, фастфуд и столовые",
        "type": "expense",
        "keywords": [
            "rostics", "makfud", "мakfud", "stolovaya", "трапеза", "papa lepit",
            "раpa lepit", "kafe", "kofejnya", "bakery", "pyshki", "shalash",
            "lion tomato", "пивасиком"
        ],
    },
    {
        "name": "Аптеки и медицина",
        "type": "expense",
        "keywords": [
            "apteka", "артека", "αρτεκα", "mdt+", "лечение", "здоровье и красота"
        ],
    },
    {
        "name": "ЖКХ, Связь и Интернет",
        "type": "expense",
        "keywords": [
            "zhku", "жкх", "ibank.mts", "mts", "мтс", "ростелеком", "дом.ру", "мегафон", "билайн", "tele2"
        ],
    },
    {
        "name": "Транспорт и Подписки (Яндекс / Сервисы)",
        "type": "expense",
        "keywords": [
            "yandex", "сервисы яндекса", "vpn", "firstbyte", "afisha"
        ],
    },
    {
        "name": "Канцтовары и Дом",
        "type": "expense",
        "keywords": ["officemag", "одежда и аксессуары"],
    },
    {
        "name": "Переводы и Сбережения (Исходящие)",
        "type": "expense",
        "keywords": [
            "внешний перевод", "внутренний перевод", "внешний банковский перевод",
            "перевод сбп", "karta-vklad", "копилка для зарплаты", "перевод с42"
        ],
    },
    {
        "name": "Зарплата и Стипендия",
        "type": "income",
        "keywords": [
            "заработная плата", "зарплата", "стипендия"
        ],
    },
    {
        "name": "Проценты по вкладам и Кэшбэк",
        "type": "income",
        "keywords": [
            "проценты на остаток", "капитализация", "кэшбэк", "возврат средств"
        ],
    },
    {
        "name": "Переводы и Пополнения (Входящие)",
        "type": "income",
        "keywords": [
            "перевод себе", "пополнение. система быстрых платежей",
            "перевод на карту", "vklad-karta", "внутрибанковский перевод",
            "перевод с17", "перевод с19"
        ],
    },
]


async def seed_default_categories(session: AsyncSession) -> None:
    """Создает стартовые категории или дополняет список новыми дефолтными категориями."""
    result = await session.execute(select(Category))
    existing = {cat.name: cat for cat in result.scalars().all()}

    changed = False
    for item in DEFAULT_CATEGORIES:
        if item["name"] not in existing:
            session.add(
                Category(
                    name=item["name"],
                    type=item["type"],
                    keywords=item["keywords"],
                )
            )
            changed = True

    if changed:
        await session.commit()


def match_category_id(
    description: str, amount_is_positive: bool, categories: List[Category]
) -> Optional[int]:
    """
    Приводит назначение платежа (description) к нижнему регистру и ищет
    совпадение с массивом keywords подходящего типа (income / expense).
    """
    if not description:
        return None

    desc_lower = description.lower()
    expected_type = "income" if amount_is_positive else "expense"

    # 1. Приоритетный поиск среди категорий совпадающего типа (доход или расход)
    for category in categories:
        if category.type != expected_type or not category.keywords:
            continue
        for keyword in category.keywords:
            if keyword.lower() in desc_lower:
                return category.id

    # 2. Резервный поиск по всем категориям
    for category in categories:
        if not category.keywords:
            continue
        for keyword in category.keywords:
            if keyword.lower() in desc_lower:
                return category.id

    return None


def extract_smart_keyword(description: str) -> str:
    """
    Автоматически выделяет из сырого банковского описания ключевое слово (название магазина/сервиса)
    для сохранения в базу знаний (keywords) при ручной корректировке пользователем.
    """
    text = description.lower()

    # 1. Если это выписка Альфа-Банка с путем вида \ru\moskva\pyaterochka mcc5411
    alfa_place = re.search(r"место совершения операции:\s*[^\s\\]+\\ru\\[^\\]+\\([^\n]+?)(?:\s+mcc\d+|\s*$)", text)
    if alfa_place:
        return alfa_place.group(1).strip()

    # 2. Убираем типовой банковский шум (префиксы Т-Банка, Сбера, Альфы, города, номера карт и телефонов)
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

    # Берем первые 1-2 значимых слова (например, "magnit", "slavyanka", "ibank.mts")
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
    """
    Обучает парсер на основе ручной корректировки пользователя:
    1. Меняет категорию у указанной транзакции.
    2. Извлекает ключевое слово из описания (или берет переданное custom_keyword)
       и добавляет его в массив keywords новой категории.
    3. Удаляет это ключевое слово из старой категории (если оно приводило к ложному срабатыванию).
    4. Автоматически обновляет категорию у всех остальных аналогичных транзакций в БД.
    """
    tx = await session.get(Transaction, transaction_id)
    if not tx:
        raise ValueError(f"Транзакция с ID {transaction_id} не найдена.")

    new_cat = await session.get(Category, new_category_id)
    if not new_cat:
        raise ValueError(f"Категория с ID {new_category_id} не найдена.")

    old_category_id = tx.category_id
    keyword = (custom_keyword or extract_smart_keyword(tx.description)).strip().lower()

    # Если в старой категории было слово, которое сработало на это описание — убираем его
    if old_category_id and old_category_id != new_category_id:
        old_cat = await session.get(Category, old_category_id)
        if old_cat and old_cat.keywords:
            desc_lower = tx.description.lower()
            # Оставляем только те слова, которые не являются виновниками ложного срабатывания,
            # либо удаляем конкретный keyword
            updated_old_kw = [
                k for k in old_cat.keywords
                if k.lower() != keyword and (k.lower() not in desc_lower or len(k) < 4)
            ]
            old_cat.keywords = updated_old_kw

    # Добавляем новое ключевое слово в целевую категорию
    current_keywords = list(new_cat.keywords or [])
    if keyword and all(k.lower() != keyword for k in current_keywords):
        current_keywords.append(keyword)
        new_cat.keywords = current_keywords

    # Обновляем саму транзакцию
    tx.category_id = new_category_id
    tx.is_parsed_automatically = False  # Помечаем, что скорректировано человеком

    # Перекатегоризируем все похожие транзакции пользователя в БД с этим же ключевым словом
    auto_updated_count = 0
    if keyword and len(keyword) >= 3:
        stmt = select(Transaction).where(
            Transaction.user_id == tx.user_id,
            Transaction.id != tx.id,
        )
        res = await session.execute(stmt)
        for other_tx in res.scalars().all():
            # Проверяем совпадение знака операции (доход/расход) и вхождение выученного слова
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
    Категоризирует список распознанных транзакций и сохраняет их в БД (пропуская дубликаты).
    Возвращает кортеж: (добавлено_новых, категоризировано, пропущено_дубликатов).
    """
    await seed_default_categories(session)

    cat_result = await session.execute(select(Category))
    categories = list(cat_result.scalars().all())

    # Загружаем существующие транзакции пользователя для отсева дубликатов при пересечении выписок
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
        category_id = match_category_id(item.description, is_income, categories)

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