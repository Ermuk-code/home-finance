import asyncio
import io
import logging
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import List, Optional

import pandas as pd
import pdfplumber
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class ParsedTransaction(BaseModel):
    """Стандартизированная схема транзакции после парсинга."""
    date: datetime
    amount: Decimal
    description: str
    bank_name: str
    is_parsed_automatically: bool = Field(default=True)


class StatementParserError(Exception):
    """Пользовательское исключение для ошибок парсинга выписок."""
    pass


class BankStatementParser:
    """Модульный парсер выписок (PDF и CSV) для Сбера, Альфа-Банка и Т-Банка."""

    @staticmethod
    def clean_amount(raw_val: str, default_negative: bool = False) -> Optional[Decimal]:
        """
        Очищает строку суммы от валют (RUR, P, Р, руб), пробелов и лишних точек.
        В Сбере расходы идут без знака '-', а доходы с '+', поэтому для Сбера default_negative=True.
        """
        if not raw_val:
            return None

        val = str(raw_val).strip()
        is_positive = "+" in val
        is_negative = "-" in val or "–" in val or "—" in val

        # Убираем обозначения валют и пробелы
        val = re.sub(r"[RURPРрруб\s\+−–—-]", "", val, flags=re.IGNORECASE)

        # Если в числе есть и точка, и запятая (например, "2.000,00" в Сбере),
        # то первая является разделителем тысяч, а последняя — дробной частью.
        if "." in val and "," in val:
            if val.rfind(",") > val.rfind("."):
                val = val.replace(".", "").replace(",", ".")
            else:
                val = val.replace(",", "")
        elif "," in val:
            val = val.replace(",", ".")

        # Оставляем только цифры и последнюю точку
        parts = val.split(".")
        if len(parts) > 2:
            val = "".join(parts[:-1]) + "." + parts[-1]

        try:
            amount = Decimal(val)
        except InvalidOperation:
            return None

        if is_negative:
            return -abs(amount)
        if is_positive:
            return abs(amount)
        if default_negative:
            return -abs(amount)
        return amount

    @classmethod
    async def parse_file_async(
        cls, file_bytes: bytes, filename: str, bank_hint: Optional[str] = None
    ) -> List[ParsedTransaction]:
        """Асинхронная точка входа (выносит CPU-bound парсинг в отдельный поток)."""
        return await asyncio.to_thread(cls.parse_file, file_bytes, filename, bank_hint)

    @classmethod
    def parse_file(
        cls, file_bytes: bytes, filename: str, bank_hint: Optional[str] = None
    ) -> List[ParsedTransaction]:
        """Определяет формат файла (.pdf / .csv) и вызывает нужный обработчик."""
        if not file_bytes:
            raise StatementParserError("Загружен пустой файл.")

        ext = filename.lower().split(".")[-1]
        try:
            if ext == "pdf":
                return cls._parse_pdf(file_bytes, bank_hint)
            elif ext == "csv":
                return cls._parse_csv(file_bytes, bank_hint)
            else:
                raise StatementParserError(f"Неподдерживаемый формат файла: .{ext}. Ожидается PDF или CSV.")
        except StatementParserError:
            raise
        except Exception as e:
            logger.exception("Ошибка при разборе файла %s", filename)
            raise StatementParserError(f"Не удалось обработать файл {filename}: {str(e)}") from e

    # =========================================================================
    # РАЗБОР PDF-ВЫПИСОК (pdfplumber)
    # =========================================================================
    @classmethod
    def _parse_pdf(cls, file_bytes: bytes, bank_hint: Optional[str] = None) -> List[ParsedTransaction]:
        try:
            with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
                full_text = "\n".join(page.extract_text() or "" for page in pdf.pages)
        except Exception as e:
            raise StatementParserError("Поврежденный или защищенный паролем PDF-файл.") from e

        if not full_text.strip():
            raise StatementParserError("PDF не содержит текстового слоя (возможно, это скан).")

        # Автоопределение банка по тексту документа
        text_upper = full_text.upper()
        if bank_hint:
            bank = bank_hint.lower()
        elif "ТБАНК" in text_upper or "TBANK.RU" in text_upper or "ТИНЬКОФФ" in text_upper:
            bank = "tbank"
        elif "АЛЬФА-БАНК" in text_upper or "ALFABANK.RU" in text_upper:
            bank = "alfa"
        elif "СБЕРБАНК" in text_upper or "SBERBANK" in text_upper or "СБЕР" in text_upper:
            bank = "sber"
        else:
            raise StatementParserError("Не удалось определить банк по содержимому PDF.")

        if bank in ("tbank", "tinkoff"):
            return cls._parse_tbank_pdf_text(full_text)
        elif bank == "alfa":
            return cls._parse_alfa_pdf_text(full_text)
        elif bank == "sber":
            return cls._parse_sber_pdf_text(full_text)
        else:
            raise StatementParserError(f"Неизвестный тип банка: {bank}")

    @classmethod
    def _parse_tbank_pdf_text(cls, text: str) -> List[ParsedTransaction]:
            """
            Парсер для PDF «Справка о движении средств» от Т-Банка.
            Поддерживает как дебетовые карты (счет 40817...), так и накопительные счета (счет 42301...).
            """
            # Определяем тип счета по номеру лицевого счета (423... — накопительный счет / вклад)
            is_savings = bool(re.search(r"Номер лицевого счета:\s*423\d+", text))
            bank_label = "Т-Банк (Накопительный)" if is_savings else "Т-Банк"

            lines = [line.strip() for line in text.splitlines() if line.strip()]
            ignore_substrings = (
                "АКЦИОНЕРНОЕ ОБЩЕСТВО", "АО «ТБАНК»", "АО «ТБанк»", "БИК 044525974",
                "Справка о движении", "Дата и время", "операции списания", "Сумма в валюте",
                "валюте договора", "валюте карты", "Описание", "Номер", "карты",
                "Пополнения:", "Расходы:", "С уважением", "Руководитель",
                "Движение средств", "О продукте", "Дата заключения", "Номер договора",
                "Номер лицевого", "Адрес места", "ТЕЛ.:", "Исх. №"
            )

            cleaned_lines: List[str] = []
            in_table = False
            for line in lines:
                if "Дата и время" in line or "Сумма операции" in line:
                    in_table = True
                    continue
                if "Пополнения:" in line or "С уважением," in line:
                    break
                if not in_table:
                    continue
                if any(sub.lower() in line.lower() for sub in ignore_substrings):
                    continue
                if re.match(r"^\d{1,2}$", line):  # Номер страницы внизу листа
                    continue
                cleaned_lines.append(line)

            full_table = "\n".join(cleaned_lines)

            # Каждая операция в Т-Банке содержит дату операции и дату списания,
            # две суммы со знаком (+ или -) и символом валюты (₽, P или Р), и текст описания.
            pattern = re.compile(
                r"(?P<d1>\d{2}\.\d{2}\.\d{4})\s*(?P<t1>\d{2}:\d{2})?\s*"
                r"(?P<d2>\d{2}\.\d{2}\.\d{4})?\s*(?P<t2>\d{2}:\d{2})?\s*"
                r"(?P<amt1>[+-]\s*\d[\d\s]*\.\d{2})\s*[₽PР]?\s*"
                r"(?P<amt2>[+-]\s*\d[\d\s]*\.\d{2})\s*[₽PР]?\s*"
                r"(?P<desc>.*?)(?=(?:\d{2}\.\d{2}\.\d{4}\s+(?:\d{2}:\d{2}\s+)?(?:\d{2}\.\d{2}\.\d{4}|[+-]\s*\d))|$)",
                re.DOTALL,
            )

            transactions: List[ParsedTransaction] = []
            for match in pattern.finditer(full_table):
                d1 = match.group("d1")
                t1 = match.group("t1")
                t2 = match.group("t2")
                raw_amt = match.group("amt1")
                raw_desc = match.group("desc")

                time_str = t1 or t2 or "00:00"
                dt = datetime.strptime(f"{d1} {time_str}", "%d.%m.%Y %H:%M")

                amount = cls.clean_amount(raw_amt)
                if amount is None:
                    continue

                # Очищаем описание от времени, 4-значных кодов карты/счета (2200, 0152) и знаков ₽
                desc = re.sub(r"\b\d{2}:\d{2}\b", "", raw_desc)
                desc = re.sub(r"\b(2200|0152)\b", "", desc)
                desc = re.sub(r"[₽]", "", desc)
                desc = re.sub(r"\s+", " ", desc).strip()

                if desc:
                    transactions.append(
                        ParsedTransaction(
                            date=dt,
                            amount=amount,
                            description=desc,
                            bank_name=bank_label,
                        )
                    )

            return transactions
    @classmethod
    def _parse_alfa_pdf_text(cls, text: str) -> List[ParsedTransaction]:
        """
        Парсер для PDF «Выписка по счету» от Альфа-Банка.
        Обрабатывает стандартные проводки (ДД.ММ.ГГГГ) и операции в статусе HOLD.
        """
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        blocks: List[List[str]] = []
        current_block: List[str] = []

        row_start_re = re.compile(r"^(\d{2}\.\d{2}\.\d{4}|HOLD)\b")
        ignore_markers = (
            "Дата проводки", "в валюте счета", "АО «АЛЬФА-БАНК»", "к/с 30101810200000000593",
            "Уполномоченное лицо", "(подпись сотрудника", "alfabank.ru", "ул. Каланчёвская",
            "Москва, 107078", "+7 495", "mail@alfabank.ru", "Страница "
        )

        in_operations = False
        for line in lines:
            if "Операции по счету" in line or "Дата проводки" in line:
                in_operations = True
                continue
            if not in_operations:
                continue
            if any(marker in line for marker in ignore_markers):
                continue

            if row_start_re.match(line):
                if current_block:
                    blocks.append(current_block)
                current_block = [line]
            else:
                if current_block:
                    current_block.append(line)

        if current_block:
            blocks.append(current_block)

        transactions: List[ParsedTransaction] = []
        # Строгий паттерн суммы: исключает захват года "26" из даты "02.11.26 479.95 RUR"
        # Требует запятую (как в правой колонке "Сумма в валюте счета") либо знак минуса
        strict_amt_re = re.compile(r"(?<!\.)([+-]?\d{1,3}(?:\s\d{3})*,\d{2}|-\d{1,3}(?:\s\d{3})*\.\d{2})\s*RUR")

        for block in blocks:
            raw = " ".join(block)

            amt_matches = list(strict_amt_re.finditer(raw))
            if not amt_matches:
                # Резервный поиск с точкой, если запятой нет
                amt_matches = list(re.finditer(r"(?<!\.)([+-]?\d{1,3}(?:\s\d{3})*[.,]\d{2})\s*RUR", raw))
            if not amt_matches:
                continue

            last_amt_match = amt_matches[-1]
            amount = cls.clean_amount(last_amt_match.group(1))
            if amount is None:
                continue

            dt_match = re.match(r"^(\d{2}\.\d{2}\.\d{4})", raw)
            if dt_match:
                date_str = dt_match.group(1)
            else:
                hold_dt = re.search(r"дата операции:\s*(\d{2}\.\d{2}\.\d{4})", raw)
                date_str = hold_dt.group(1) if hold_dt else datetime.now().strftime("%d.%m.%Y")

            dt = datetime.strptime(date_str, "%d.%m.%Y")

            # Удаляем найденную сумму из текста описания
            desc = (raw[:last_amt_match.start()] + " " + raw[last_amt_match.end():]).strip()
            desc = re.sub(r"^(\d{2}\.\d{2}\.\d{4}|HOLD)\s*", "", desc)
            desc = re.sub(r"^(CRD_[A-Z0-9]+|CRD\s+[A-Z0-9]+|[A-Z0-9]{14,20})\s*", "", desc)
            desc = re.sub(r"\s+", " ", desc).strip()

            transactions.append(
                ParsedTransaction(
                    date=dt,
                    amount=amount,
                    description=desc or "Операция Альфа-Банк",
                    bank_name="Альфа-Банк",
                )
            )

        return transactions

    @classmethod
    def _parse_sber_pdf_text(cls, text: str) -> List[ParsedTransaction]:
        """
        Парсер для PDF «Выписка по счёту дебетовой карты» от СберБанка.
        Склеивает двухстрочные записи операций, учитывает отсутствие минуса у расходов.
        """
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        ignore_phrases = (
            "Выписка по счёту", "ДАТА ОПЕРАЦИИ", "Дата обработки", "код авторизации",
            "КАТЕГОРИЯ", "СУММА В ВАЛЮТЕ", "ОСТАТОК СРЕДСТВ", "Продолжение на следующей",
            "Страница ", "Дата формирования", "СБЕР Документ", "СВЕДЕНИЯ О СЕРТИФИКАТЕ",
            "ПАО Сбербанк", "Денежные средства", "В выписке отображаются", "Срок обработки",
            "Дата списания", "По курсу банка", "Согласно статье", "Скачать электронный",
            "Проверить подпись", "Для проверки подлинности", "Зайдите в приложение",
            "Нажмите кнопку", "Получите документ", "Предоставляя QR-код", "Действителен"
        )

        in_table = False
        cleaned_lines: List[str] = []
        for line in lines:
            if "Расшифровка операций" in line:
                in_table = True
                continue
            if "СВЕДЕНИЯ О СЕРТИФИКАТЕ" in line or "Дата формирования документа" in line:
                break
            if not in_table:
                continue
            if any(phrase in line for phrase in ignore_phrases):
                continue
            cleaned_lines.append(line)

        full_table_text = "\n".join(cleaned_lines)

        # Регулярное выражение ищет блок операции Сбера:
        # Дата (ДД.ММ.ГГГГ) + Время (ЧЧ:ММ) ... Сумма + Остаток + Описание с "Операция по карте"
        pattern = re.compile(
            r"(?P<date>\d{2}\.\d{2}\.\d{4})\s+(?P<time>\d{2}:\d{2})"
            r"(?P<body>.*?)"
            r"(?P<end>Операция по\s+карте\s+\*{0,4}\d{4})",
            re.DOTALL | re.IGNORECASE,
        )

        transactions: List[ParsedTransaction] = []
        for match in pattern.finditer(full_table_text):
            date_str = match.group("date")
            time_str = match.group("time")
            body = match.group("body") + " " + match.group("end")

            dt = datetime.strptime(f"{date_str} {time_str}", "%d.%m.%Y %H:%M")

            # Ищем все денежные числа формата "+2 000,00", "448,03", "2.000,00"
            amounts = re.findall(r"[+]?\d(?:[\d\s.]*\d)?,\d{2}", body)
            if not amounts:
                continue

            # Первое число в паре (Сумма в валюте счёта, Остаток) — это сумма операции
            raw_amount = amounts[0]
            # В Сбере расходы без минуса, поэтому default_negative=True
            amount = cls.clean_amount(raw_amount, default_negative=True)
            if amount is None:
                continue

            # Очищаем описание от второй даты, 6-значного кода авторизации и сумм
            desc = body
            desc = re.sub(r"\d{2}\.\d{2}\.\d{4}", "", desc)
            desc = re.sub(r"\b\d{6}\b", "", desc)
            for a in amounts:
                desc = desc.replace(a, "")
            desc = re.sub(r"\s+", " ", desc).strip()

            transactions.append(
                ParsedTransaction(
                    date=dt,
                    amount=amount,
                    description=desc,
                    bank_name="СберБанк",
                )
            )

        return transactions

    # =========================================================================
    # РАЗБОР CSV-ВЫПИСОК (pandas)
    # =========================================================================
    @classmethod
    def _parse_csv(cls, file_bytes: bytes, bank_hint: Optional[str] = None) -> List[ParsedTransaction]:
        """Универсальный парсер CSV-выписок для Т-Банка, Альфа-Банка и Сбера."""
        df = None
        for encoding in ("utf-8-sig", "utf-8", "cp1251", "windows-1251"):
            for sep in (";", ",", "\t"):
                try:
                    candidate = pd.read_csv(io.BytesIO(file_bytes), encoding=encoding, sep=sep)
                    if candidate.shape[1] >= 3:
                        df = candidate
                        break
                except Exception:
                    continue
            if df is not None:
                break

        if df is None or df.empty:
            raise StatementParserError("Не удалось прочитать CSV-файл или таблица пуста.")

        cols_lower = {str(col).strip().lower(): col for col in df.columns}

        # Определяем колонки
        date_col = next(
            (orig for low, orig in cols_lower.items() if "дата операции" in low or "дата проводки" in low or low == "дата"),
            None,
        )
        amount_col = next(
            (orig for low, orig in cols_lower.items() if "сумма операции" in low or "сумма в валюте" in low or low == "сумма"),
            None,
        )
        desc_col = next(
            (orig for low, orig in cols_lower.items() if "описание" in low or "назначение" in low or "категория" in low),
            None,
        )

        if not (date_col and amount_col and desc_col):
            raise StatementParserError(
                f"Не найдены обязательные колонки (Дата, Сумма, Описание). Найдены: {list(df.columns)}"
            )

        # Определяем имя банка
        bank_name = bank_hint or "Банк (CSV)"
        if "номер карты" in cols_lower and "кэшбэк" in cols_lower:
            bank_name = "Т-Банк"
        elif "код операции" in cols_lower or "референс проводки" in cols_lower:
            bank_name = "Альфа-Банк"

        transactions: List[ParsedTransaction] = []
        for _, row in df.iterrows():
            raw_date = str(row[date_col]).strip()
            raw_amount = str(row[amount_col]).strip()
            raw_desc = str(row[desc_col]).strip()

            if not raw_date or raw_date == "nan" or raw_date.upper() == "HOLD":
                continue

            # Пробуем несколько форматов даты
            dt = None
            for fmt in ("%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M", "%d.%m.%Y", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
                try:
                    dt = datetime.strptime(raw_date, fmt)
                    break
                except ValueError:
                    continue

            if dt is None:
                continue

            amount = cls.clean_amount(raw_amount)
            if amount is None:
                continue

            transactions.append(
                ParsedTransaction(
                    date=dt,
                    amount=amount,
                    description=raw_desc,
                    bank_name=bank_name,
                )
            )

        return transactions