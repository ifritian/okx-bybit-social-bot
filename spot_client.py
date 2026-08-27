"""
spot_client.py - низкоуровневый клиент Binance Spot API (балансы,
цены, рыночные ордера). Пара к futures_client.py, но для спота - нужен
portfolio_rebalancer.py (ребалансировка Treasury Index реальными
спот-ордерами, в отличие от фьючерсов - БЕЗ плеча и БЕЗ риска
ликвидации, см. docstring portfolio_rebalancer.py).

По умолчанию ВСЕГДА указывает на Binance Spot TESTNET (testnet.binance.
vision - учебный счёт с фейковыми монетами, НЕ имеет отношения к
реальному аккаунту Binance) - см. config.BINANCE_SPOT_USE_TESTNET. Тот
же принцип, что и у FuturesClient: переключение на реальный счёт
требует ЯВНО выставить BINANCE_SPOT_USE_TESTNET=false.

Как получить testnet-ключи:
1. Зайти на https://testnet.binance.vision
2. Залогиниться через GitHub-аккаунт (отдельная тестовая сеть, ключ не
   имеет отношения к реальному аккаунту Binance - и это ДРУГАЯ тестовая
   сеть, чем testnet.binancefuture.com для фьючерсов, ключи не совпадают).
3. Сгенерировать HMAC-ключ, выдать себе тестовый баланс.
4. Положить в переменные окружения (никогда не в git!):
   BINANCE_SPOT_API_KEY=...
   BINANCE_SPOT_API_SECRET=...

Подпись запросов - тот же механизм, что у FuturesClient (HMAC-SHA256 по
query-строке).
"""
import hashlib
import hmac
import logging
import time
from typing import Optional
from urllib.parse import urlencode

import requests

logger = logging.getLogger(__name__)

TESTNET_BASE_URL = "https://testnet.binance.vision"
MAINNET_BASE_URL = "https://api.binance.com"

_REQUEST_TIMEOUT = 15


class SpotApiError(Exception):
    """Binance вернул ошибку (4xx/5xx) - сообщение включает код и текст
    ответа биржи."""


class SpotClient:
    """Один инстанс - один набор ключей + один base_url (testnet или
    mainnet). Как и FuturesClient - не читает config напрямую внутри
    методов, все параметры передаются в конструктор явно."""

    def __init__(self, api_key: str, api_secret: str, base_url: str = TESTNET_BASE_URL,
                 recv_window_ms: int = 5000):
        if not api_key or not api_secret:
            raise ValueError("SpotClient: api_key/api_secret не заданы - проверь переменные окружения")
        self.api_key = api_key
        self._api_secret = api_secret
        self.base_url = base_url.rstrip("/")
        self.recv_window_ms = recv_window_ms
        self.is_testnet = self.base_url == TESTNET_BASE_URL

    # --- низкий уровень ---

    def _sign(self, query_string: str) -> str:
        return hmac.new(
            self._api_secret.encode("utf-8"), query_string.encode("utf-8"), hashlib.sha256
        ).hexdigest()

    def _public_request(self, method: str, path: str, params: Optional[dict] = None) -> dict:
        url = f"{self.base_url}{path}"
        resp = requests.request(method, url, params=params or {}, timeout=_REQUEST_TIMEOUT)
        return self._handle_response(resp)

    def _signed_request(self, method: str, path: str, params: Optional[dict] = None) -> dict:
        params = dict(params or {})
        params["timestamp"] = int(time.time() * 1000)
        params["recvWindow"] = self.recv_window_ms
        query_string = urlencode(params, safe=",")
        signature = self._sign(query_string)
        url = f"{self.base_url}{path}?{query_string}&signature={signature}"
        headers = {"X-MBX-APIKEY": self.api_key}
        resp = requests.request(method, url, headers=headers, timeout=_REQUEST_TIMEOUT)
        return self._handle_response(resp)

    @staticmethod
    def _handle_response(resp: requests.Response) -> dict:
        try:
            data = resp.json()
        except ValueError:
            resp.raise_for_status()
            raise SpotApiError(f"Не удалось разобрать ответ Binance: {resp.text[:300]}")
        if resp.status_code >= 400:
            raise SpotApiError(f"Binance Spot API error {resp.status_code}: {data}")
        return data

    # --- публичные данные (без подписи) ---

    def get_exchange_info(self) -> dict:
        return self._public_request("GET", "/api/v3/exchangeInfo")

    def symbol_exists(self, symbol: str, exchange_info: Optional[dict] = None) -> bool:
        """True, если symbol сейчас торгуется (status TRADING) на споте.
        Принимает уже полученный exchange_info (для резолва нескольких
        символов подряд без повторных запросов) - если не передан,
        запрашивает сам."""
        info = exchange_info or self.get_exchange_info()
        for s in info.get("symbols", []):
            if s.get("symbol") == symbol:
                return s.get("status") == "TRADING"
        return False

    def get_symbol_filters(self, symbol: str, exchange_info: Optional[dict] = None) -> dict:
        """{'step_size': ..., 'min_notional': ...} - округление
        количества и минимальный объём ордера. Современный Binance
        Spot использует фильтр 'NOTIONAL' (поле minNotional) вместо
        устаревшего 'MIN_NOTIONAL' - проверяем оба имени."""
        info = exchange_info or self.get_exchange_info()
        for s in info.get("symbols", []):
            if s.get("symbol") == symbol:
                step_size = min_notional = None
                for f in s.get("filters", []):
                    if f.get("filterType") == "LOT_SIZE":
                        step_size = float(f["stepSize"])
                    elif f.get("filterType") in ("NOTIONAL", "MIN_NOTIONAL"):
                        min_notional = float(f.get("minNotional", 0))
                return {"step_size": step_size, "min_notional": min_notional}
        raise SpotApiError(f"Символ {symbol} не найден в exchangeInfo")

    def get_price(self, symbol: str) -> float:
        data = self._public_request("GET", "/api/v3/ticker/price", {"symbol": symbol})
        return float(data["price"])

    # --- аккаунт (с подписью) ---

    def get_balances(self) -> dict:
        """{asset: total_qty} - free + locked (locked - то, что уже
        стоит в открытых ордерах). Только активы с ненулевым остатком."""
        data = self._signed_request("GET", "/api/v3/account")
        out = {}
        for row in data.get("balances", []):
            total = float(row.get("free", 0)) + float(row.get("locked", 0))
            if total > 0:
                out[row["asset"]] = total
        return out

    # --- ордера (с подписью) ---

    def place_market_order(self, symbol: str, side: str, quantity: Optional[float] = None,
                            quote_order_qty: Optional[float] = None) -> dict:
        """side - 'BUY' или 'SELL'. Ровно один из quantity (в базовом
        активе, например BTC для BTCUSDT) / quote_order_qty (в
        котируемом активе, например USDT) должен быть задан - Binance
        сам считает количество по текущей рыночной цене в случае
        quote_order_qty. quote_order_qty удобен для покупок "на X
        долларов" (см. portfolio_rebalancer.py) - не нужно самому
        считать quantity по цене на момент отправки, которая может
        успеть чуть измениться."""
        if (quantity is None) == (quote_order_qty is None):
            raise ValueError("place_market_order: нужно задать ровно одно из quantity/quote_order_qty")
        params = {"symbol": symbol, "side": side, "type": "MARKET"}
        if quantity is not None:
            params["quantity"] = quantity
        else:
            params["quoteOrderQty"] = quote_order_qty
        return self._signed_request("POST", "/api/v3/order", params)


def client_from_config(config) -> SpotClient:
    """Собирает SpotClient из config.py (BINANCE_SPOT_*) - единая точка
    входа, аналог futures_client.client_from_config."""
    base_url = TESTNET_BASE_URL if config.BINANCE_SPOT_USE_TESTNET else MAINNET_BASE_URL
    if not config.BINANCE_SPOT_USE_TESTNET:
        logger.warning(
            "SpotClient создаётся для РЕАЛЬНОГО счёта (BINANCE_SPOT_USE_TESTNET=false) - "
            "используются настоящие средства."
        )
    return SpotClient(
        api_key=config.BINANCE_SPOT_API_KEY,
        api_secret=config.BINANCE_SPOT_API_SECRET,
        base_url=base_url,
        recv_window_ms=config.BINANCE_SPOT_RECV_WINDOW_MS,
    )
