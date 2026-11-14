import asyncio
import io
import logging
import re
from decimal import Decimal, InvalidOperation
from typing import List, Optional, Tuple

import pandas as pd
import pdfplumber
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Asset, User
from app.finance.services.tbank_report import TBankReportError, is_tbank_report, parse_tbank_pdf

logger = logging.getLogger(__name__)


class ParsedAsset(BaseModel):
    """Стандартизированная схема инвестиционного актива."""
    ticker: str
    asset_type: str  # 'stock', 'bond' или 'fund'
    amount: Decimal
    # None — цена покупки неизвестна (в отчёте ТБанка её нет)
    average_buy_price: Optional[Decimal] = None
    name: Optional[str] = None
    isin: Optional[str] = None


class ParsedAssets(list):
    """Список позиций + то, что относится ко всему отчёту целиком.

    Наследуем list, чтобы существующий код (`if parsed_assets:`, `len(...)`, итерация) работал как раньше.
    """

    cash_rub: Optional[Decimal] = None          # свободные рубли на счёте
    closed_tickers: List[str] = []              # бумаги с нулевым остатком (проданы полностью)
    warnings: List[str] = []                    # что показать пользователю
    from_snapshot: bool = False                 # True — отчёт ТБанка (полный срез портфеля)


class BrokerParserError(Exception):
    """Исключение при ошибках разбора брокерского отчета."""
    pass


# Признаки банковской выписки: такие документы не должны попадать в брокерский парсер.
_BANK_STATEMENT_MARKERS = (
    "ВЫПИСКА ПО СЧЁТУ", "ВЫПИСКА ПО СЧЕТУ", "СПРАВКА О ДВИЖЕНИИ СРЕДСТВ",
    "РАСШИФРОВКА ОПЕРАЦИЙ", "ОПЕРАЦИЯ ПО КАРТЕ", "ОПЕРАЦИИ ПО СЧЕТУ", "ДАТА ПРОВОДКИ",
)
# Признаки брокерского отчёта / отчёта о портфеле ценных бумаг.
_BROKER_REPORT_MARKERS = (
    "БРОКЕР", "ПОРТФЕЛ", "ЦЕННЫХ БУМАГ", "ЦЕННЫЕ БУМАГИ", "ТИКЕР", "ISIN",
    "ДЕПО", "ИНВЕСТИЦИОН", "СДЕЛКИ С ЦЕННЫМИ",
)


class BrokerReportParser:
    """Парсер брокерских отчетов в форматах CSV, XLSX и PDF."""

    @staticmethod
    def detect_asset_type(ticker: str, raw_type: str = "") -> str:
        t = ticker.strip().upper()
        rt = raw_type.strip().lower()
        if any(w in rt for w in ("облиг", "bond", "офз")):
            return "bond"
        if any(w in rt for w in ("акц", "stock", "share", "ао", "ап")):
            return "stock"
        # Коды ОФЗ (SU...) и корпоративных облигаций (RU000...)
        if t.startswith("SU") or t.startswith("RU000") or len(t) >= 10:
            return "bond"
        return "stock"

    @staticmethod
    def clean_decimal(raw_val: object) -> Optional[Decimal]:
        if raw_val is None:
            return None
        s = str(raw_val).strip()
        if not s or s.lower() == "nan":
            return None
        s = re.sub(r"[RURPРрруб₽%\s\+−–—]", "", s, flags=re.IGNORECASE)
        if "." in s and "," in s:
            if s.rfind(",") > s.rfind("."):
                s = s.replace(".", "").replace(",", ".")
            else:
                s = s.replace(",", "")
        elif "," in s:
            s = s.replace(",", ".")

        try:
            return Decimal(s)
        except InvalidOperation:
            return None

    @classmethod
    async def parse_file_async(cls, file_bytes: bytes, filename: str) -> List[ParsedAsset]:
        """Асинхронная обертка для выноса чтения таблиц/PDF в отдельный поток."""
        return await asyncio.to_thread(cls.parse_file, file_bytes, filename)

    @classmethod
    def parse_file(cls, file_bytes: bytes, filename: str) -> List[ParsedAsset]:
        if not file_bytes:
            raise BrokerParserError("Передан пустой файл брокерского отчета.")

        ext = filename.lower().split(".")[-1]
        try:
            if ext in ("xlsx", "xls"):
                df = pd.read_excel(io.BytesIO(file_bytes))
                return cls._parse_dataframe(df)
            elif ext == "csv":
                return cls._parse_csv(file_bytes)
            elif ext == "pdf":
                return cls._parse_pdf_any(file_bytes)
            else:
                raise BrokerParserError(
                    f"Неподдерживаемый формат файла: .{ext}. Ожидается CSV, XLSX или PDF."
                )
        except BrokerParserError:
            raise
        except Exception as e:
            logger.exception("Ошибка при разборе брокерского отчета %s", filename)
            raise BrokerParserError(f"Не удалось обработать брокерский отчет: {str(e)}") from e

    @classmethod
    def _parse_csv(cls, file_bytes: bytes) -> List[ParsedAsset]:
        for encoding in ("utf-8-sig", "utf-8", "cp1251", "windows-1251"):
            for sep in (";", ",", "\t"):
                try:
                    df = pd.read_csv(io.BytesIO(file_bytes), encoding=encoding, sep=sep)
                    if df.shape[1] >= 3:
                        return cls._parse_dataframe(df)
                except Exception:
                    continue
        raise BrokerParserError("Не удалось прочитать CSV-таблицу брокерского отчета.")

    @classmethod
    def _parse_dataframe(cls, df: pd.DataFrame) -> List[ParsedAsset]:
        if df is None or df.empty:
            raise BrokerParserError("Таблица в файле брокерского отчета пуста.")

        cols_lower = {str(col).strip().lower(): col for col in df.columns}

        ticker_col = next(
            (
                orig for low, orig in cols_lower.items()
                if any(k in low for k in ("тикер", "ticker", "код", "isin", "инструмент", "бумага"))
            ),
            None,
        )
        amount_col = next(
            (
                orig for low, orig in cols_lower.items()
                if any(k in low for k in ("кол-во", "количество", "amount", "quantity", "остаток", "шт", "лотов"))
            ),
            None,
        )
        price_col = next(
            (
                orig for low, orig in cols_lower.items()
                if any(k in low for k in ("средняя", "цена", "price", "average", "балансовая", "приобретения"))
            ),
            None,
        )
        type_col = next(
            (
                orig for low, orig in cols_lower.items()
                if any(k in low for k in ("тип", "type", "вид", "категория"))
            ),
            None,
        )

        if not (ticker_col and amount_col and price_col):
            raise BrokerParserError(
                f"Не найдены обязательные колонки (Тикер, Количество, Средняя цена). Найдены: {list(df.columns)}"
            )

        assets: List[ParsedAsset] = []
        for _, row in df.iterrows():
            raw_ticker = str(row[ticker_col]).strip().upper()
            if not raw_ticker or raw_ticker == "NAN" or len(raw_ticker) > 20:
                continue

            qty = cls.clean_decimal(row[amount_col])
            avg_price = cls.clean_decimal(row[price_col])
            if qty is None or avg_price is None or qty <= 0 or avg_price <= 0:
                continue

            raw_type = str(row[type_col]) if type_col else ""
            asset_type = cls.detect_asset_type(raw_ticker, raw_type)

            assets.append(
                ParsedAsset(
                    ticker=raw_ticker,
                    asset_type=asset_type,
                    amount=qty,
                    average_buy_price=avg_price,
                )
            )

        if not assets:
            raise BrokerParserError("В таблице не найдено ни одной корректной позиции.")

        return assets

    @classmethod
    def _parse_pdf_any(cls, file_bytes: bytes) -> List[ParsedAsset]:
        """Сначала отчёт ТБанка (точный разбор по разделам), иначе — общий эвристический разбор."""
        try:
            with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
                head = "\n".join((page.extract_text() or "") for page in pdf.pages[:2])
        except Exception as e:
            raise BrokerParserError("Поврежденный PDF-файл брокерского отчета.") from e

        if is_tbank_report(head):
            try:
                report = parse_tbank_pdf(file_bytes)
            except TBankReportError as e:
                raise BrokerParserError(str(e)) from e
            result = ParsedAssets(
                ParsedAsset(
                    ticker=p.code.upper(),
                    asset_type=p.asset_type,
                    amount=p.qty_out,
                    average_buy_price=None,
                    name=p.name,
                    isin=p.isin,
                )
                for p in report.positions
            )
            result.cash_rub = report.cash.get("RUB")
            result.closed_tickers = [c.upper() for c in report.closed_codes]
            result.warnings = list(report.warnings)
            result.from_snapshot = True
            if not result and not result.closed_tickers:
                raise BrokerParserError("В отчёте ТБанка нет ни одной бумаги в разделе 3.1.")
            return result
        return cls._parse_pdf(file_bytes)

    @classmethod
    def _parse_pdf(cls, file_bytes: bytes) -> List[ParsedAsset]:
        """Извлечение активов из таблиц и текста PDF брокерского отчета."""
        assets_map: dict[str, ParsedAsset] = {}

        try:
            with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
                full_text = "\n".join(page.extract_text() or "" for page in pdf.pages)
        except Exception as e:
            raise BrokerParserError("Поврежденный PDF-файл брокерского отчета.") from e

        if not full_text.strip():
            raise BrokerParserError("PDF-файл не содержит текстового слоя.")

        text_upper = full_text.upper()
        if any(m in text_upper for m in _BANK_STATEMENT_MARKERS):
            raise BrokerParserError(
                "Документ похож на банковскую выписку, а не на брокерский отчет."
            )
        if not any(m in text_upper for m in _BROKER_REPORT_MARKERS):
            raise BrokerParserError(
                "В PDF нет признаков брокерского отчета (брокер, портфель, тикер, ISIN)."
            )

        # Поиск строк вида: ТИКЕР (например SBER, LKOH, SU26238RMFS4, RU000A10...) + Кол-во + Средняя цена
        pattern = re.compile(
            r"\b(?P<ticker>SU\d{5}RMFS\d|RU000[A-Z0-9]{7}|[A-Z]{4,5})\b"
            r"[^\n\d]*?"
            r"(?P<qty>\d+(?:[\s.,]\d+)?)\s*(?:шт\.?|лотов|шт)?\s+"
            r"(?P<price>\d+(?:[\s.,]\d+)?)",
            re.IGNORECASE,
        )

        stop_words = {
            "RUR", "RUB", "USD", "EUR", "HOLD", "MOSKVA", "KURSK",
            "SBERBANK", "TBANK", "ALFA", "ISIN", "FIFO", "LIFO"
        }

        for match in pattern.finditer(full_text):
            ticker = match.group("ticker").upper()
            if ticker in stop_words:
                continue

            qty = cls.clean_decimal(match.group("qty"))
            price = cls.clean_decimal(match.group("price"))
            if not qty or not price or qty <= 0 or price <= 0:
                continue

            assets_map[ticker] = ParsedAsset(
                ticker=ticker,
                asset_type=cls.detect_asset_type(ticker),
                amount=qty,
                average_buy_price=price,
            )

        if not assets_map:
            raise BrokerParserError("Не удалось обнаружить позиции ценных бумаг в PDF-документе.")

        return list(assets_map.values())


async def sync_parsed_assets(
    session: AsyncSession, user_id: int, parsed_assets: List[ParsedAsset]
) -> Tuple[int, int]:
    """
    Синхронизирует извлеченные активы с таблицей assets в БД:
    - тикер уже есть у пользователя — обновляет количество (и цену покупки, только если она передана:
      отчёт ТБанка цен не содержит, и введённую вручную цену затирать нельзя);
    - тикера нет — создает новую запись;
    - для отчёта ТБанка: бумаги с нулевым остатком удаляются, свободные рубли сохраняются.
    Возвращает кортеж: (добавлено_новых, обновлено_существующих).
    """
    result = await session.execute(select(Asset).where(Asset.user_id == user_id))
    existing_assets = {a.ticker.upper(): a for a in result.scalars().all()}

    added_count = 0
    updated_count = 0

    for item in parsed_assets:
        ticker_key = item.ticker.strip().upper()
        if ticker_key in existing_assets:
            db_asset = existing_assets[ticker_key]
            db_asset.amount = item.amount
            if item.average_buy_price is not None:
                db_asset.average_buy_price = item.average_buy_price
            db_asset.asset_type = item.asset_type
            if item.name:
                db_asset.name = item.name
            if item.isin:
                db_asset.isin = item.isin
            updated_count += 1
        else:
            new_asset = Asset(
                user_id=user_id,
                ticker=ticker_key,
                asset_type=item.asset_type,
                amount=item.amount,
                average_buy_price=item.average_buy_price,
                name=item.name,
                isin=item.isin,
            )
            session.add(new_asset)
            added_count += 1

    for closed in getattr(parsed_assets, "closed_tickers", []):
        asset = existing_assets.get(closed.upper())
        if asset is not None:
            await session.delete(asset)

    cash_rub = getattr(parsed_assets, "cash_rub", None)
    if getattr(parsed_assets, "from_snapshot", False) and cash_rub is not None:
        user = await session.get(User, user_id)
        if user is not None:
            user.broker_cash_rub = cash_rub

    await session.commit()
    return added_count, updated_count
