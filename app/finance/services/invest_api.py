import asyncio
import logging
from decimal import Decimal
from typing import Dict, List, Optional

import httpx

from app.finance.services.moex_http import create_moex_client

logger = logging.getLogger(__name__)


class MoexClient:
    """Асинхронный клиент для получения котировок с MOEX ISS API."""

    BASE_URL = "https://iss.moex.com/iss"
    TIMEOUT = httpx.Timeout(10.0, connect=5.0)

    @classmethod
    async def get_current_price(
        cls,
        ticker: str,
        asset_type: str = "stock",
        client: Optional[httpx.AsyncClient] = None,
    ) -> Optional[Decimal]:
        """
        Возвращает актуальную рыночную цену бумаги в рублях.
        При недоступности MOEX перехватывает исключения и возвращает None.
        """
        ticker = ticker.strip().upper()
        if not ticker:
            return None

        own_client = client is None
        if own_client:
            client = create_moex_client(cls.TIMEOUT)

        try:
            is_bond = (
                asset_type.lower() == "bond"
                or ticker.startswith("SU")
                or ticker.startswith("RU000")
            )
            if is_bond:
                price = await cls._fetch_bond_price(client, ticker)
                if price is None:
                    price = await cls._fetch_stock_price(client, ticker)
                return price
            else:
                price = await cls._fetch_stock_price(client, ticker)
                if price is None:
                    price = await cls._fetch_bond_price(client, ticker)
                return price

        except (httpx.RequestError, httpx.HTTPStatusError) as net_err:
            logger.warning("Сетевая ошибка при запросе MOEX для %s: %s", ticker, net_err)
            return None
        except Exception as exc:
            logger.exception("Непредвиденная ошибка при разборе ответа MOEX для %s: %s", ticker, exc)
            return None
        finally:
            if own_client and client is not None:
                await client.aclose()

    @classmethod
    async def _fetch_stock_price(
        cls, client: httpx.AsyncClient, ticker: str
    ) -> Optional[Decimal]:
        """Получение цены акции (режим TQBR / TQTF)."""
        url = f"{cls.BASE_URL}/engines/stock/markets/shares/securities/{ticker}.json"
        params = {"iss.meta": "off", "iss.only": "marketdata,securities"}

        resp = await client.get(url, params=params)
        resp.raise_for_status()
        data = resp.json()

        # 1. Ищем живую цену в блоке marketdata (режим TQBR в приоритете)
        md_cols = data.get("marketdata", {}).get("columns", [])
        md_rows = data.get("marketdata", {}).get("data", [])

        preferred_boards = ("TQBR", "TQTF", "TQPI", "SMAL")
        for board in preferred_boards:
            for row in md_rows:
                row_dict = dict(zip(md_cols, row))
                if row_dict.get("BOARDID") == board:
                    price = (
                        row_dict.get("LAST")
                        or row_dict.get("LCURRENTPRICE")
                        or row_dict.get("MARKETPRICE")
                    )
                    if price is not None:
                        return Decimal(str(price)).quantize(Decimal("0.0001"))

        # 2. Если биржа закрыта и LAST пуст, берем PREVPRICE из блока securities
        sec_cols = data.get("securities", {}).get("columns", [])
        sec_rows = data.get("securities", {}).get("data", [])
        for board in preferred_boards:
            for row in sec_rows:
                row_dict = dict(zip(sec_cols, row))
                if row_dict.get("BOARDID") == board:
                    prev = row_dict.get("PREVPRICE") or row_dict.get("PREVWAPRICE")
                    if prev is not None:
                        return Decimal(str(prev)).quantize(Decimal("0.0001"))

        return None

    @classmethod
    async def _fetch_bond_price(
        cls, client: httpx.AsyncClient, ticker: str
    ) -> Optional[Decimal]:
        """
        Получение цены облигации (режимы TQOB / TQCB).
        Формула перевода в рубли: (Цена_в_% / 100) * FACEVALUE + ACCRUEDINT (НКД).
        """
        url = f"{cls.BASE_URL}/engines/stock/markets/bonds/securities/{ticker}.json"
        params = {"iss.meta": "off", "iss.only": "marketdata,securities"}

        resp = await client.get(url, params=params)
        resp.raise_for_status()
        data = resp.json()

        sec_cols = data.get("securities", {}).get("columns", [])
        sec_rows = data.get("securities", {}).get("data", [])

        preferred_boards = ("TQOB", "TQCB", "TQIR", "TQOD")
        face_value = Decimal("1000")
        accrued_int = Decimal("0")
        prev_pct_price: Optional[Decimal] = None
        matched_board: Optional[str] = None

        for board in preferred_boards:
            for row in sec_rows:
                row_dict = dict(zip(sec_cols, row))
                if row_dict.get("BOARDID") == board:
                    matched_board = board
                    if row_dict.get("FACEVALUE") is not None:
                        face_value = Decimal(str(row_dict["FACEVALUE"]))
                    if row_dict.get("ACCRUEDINT") is not None:
                        accrued_int = Decimal(str(row_dict["ACCRUEDINT"]))
                    if row_dict.get("PREVPRICE") is not None:
                        prev_pct_price = Decimal(str(row_dict["PREVPRICE"]))
                    break
            if matched_board:
                break

        md_cols = data.get("marketdata", {}).get("columns", [])
        md_rows = data.get("marketdata", {}).get("data", [])

        live_pct_price: Optional[Decimal] = None
        for row in md_rows:
            row_dict = dict(zip(md_cols, row))
            if row_dict.get("BOARDID") in preferred_boards:
                raw_price = (
                    row_dict.get("LAST")
                    or row_dict.get("LCURRENTPRICE")
                    or row_dict.get("MARKETPRICE")
                )
                if raw_price is not None:
                    live_pct_price = Decimal(str(raw_price))
                    break

        pct_price = live_pct_price if live_pct_price is not None else prev_pct_price
        if pct_price is None:
            return None

        # Расчет полной стоимости облигации в рублях с учетом НКД
        rub_price = (pct_price / Decimal("100")) * face_value + accrued_int
        return rub_price.quantize(Decimal("0.01"))

    @classmethod
    async def get_prices_for_tickers(
        cls, items: List[Dict[str, str]]
    ) -> Dict[str, Optional[Decimal]]:
        """
        Пакетное асинхронное получение котировок для списка бумаг.
        Принимает список вида: [{"ticker": "SBER", "asset_type": "stock"}, ...]
        Возвращает словарь: {"SBER": Decimal("285.50"), ...}
        """
        if not items:
            return {}

        results: Dict[str, Optional[Decimal]] = {}
        async with create_moex_client(cls.TIMEOUT) as client:
            tasks = {
                item["ticker"].strip().upper(): cls.get_current_price(
                    ticker=item["ticker"],
                    asset_type=item.get("asset_type", "stock"),
                    client=client,
                )
                for item in items
                if item.get("ticker")
            }
            responses = await asyncio.gather(*tasks.values(), return_exceptions=True)

            for ticker, res in zip(tasks.keys(), responses):
                if isinstance(res, Exception):
                    logger.warning("Ошибка пакетного получения цены для %s: %s", ticker, res)
                    results[ticker] = None
                else:
                    results[ticker] = res

        return results
