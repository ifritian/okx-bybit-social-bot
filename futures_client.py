"""
futures_client.py - низкоуровневый клиент Binance USD-M Futures API
(подписанные запросы: открытие/закрытие позиций, стоп-лосс/тейк-профит,
плечо, баланс).

ЭТО НЕ ТЕСТОВЫЙ КОД ДЛЯ ГЕНЕРАЦИИ ПОСТОВ - это реальное управление
позициями через API. По умолчанию ВСЕГДА указывает на Binance Futures
TESTNET (учебный счёт, не настоящие деньги) - см.
config.BINANCE_FUTURES_USE_TESTNET. Переключение на реальный счёт
требует ЯВНО выставить BINANCE_FUTURES_USE_TESTNET=false - это
осознанное решение пользователя, а не побочный эффект забытой
переменной окружения.

Как получить testnet-ключи:
1. Зайти на https://testnet.binancefuture.com
2. Залогиниться через GitHub-аккаунт (тестовая сеть отдельная от
   основного аккаунта Binance, ключ от неё не имеет отношения к
   реальному аккаунту).
3. Внизу страницы - раздел "API Key" - сгенерировать HMAC-ключ.
4. Положить в переменные окружения (никогда не в git!):
   BINANCE_FUTURES_API_KEY=...
   BINANCE_FUTURES_API_SECRET=...

Подпись запросов - HMAC-SHA256 по query-строке с secretKey (см.
_sign). Тестовый вектор из официальной документации Binance
зафиксирован в test_futures_client.py - если реализация подписи
когда-нибудь случайно сломается при рефакторинге, этот тест сразу
это покажет.
"""
import hashlib
import hmac
import logging
import time
from typing import Optional
from urllib.parse import urlencode

import requests

logger = logging.getLogger(__name__)

TESTNET_BASE_URL = "https://testnet.binancefuture.com"
MAINNET_BASE_URL = "https://fapi.binance.com"

_REQUEST_TIMEOUT = 15


class FuturesApiError(Exception):
    """Binance вернул ошибку (4xx/5xx) - сообщение включает код и текст
    ответа биржи, чтобы не приходилось лезть в логи requests отдельно."""


class FuturesClient:
    """Один инстанс - один набор ключей + один base_url (testnet или
    mainnet). НЕ читает config напрямую внутри методов - все параметры
    передаются в конструктор явно, чтобы клиент было легко подменить
    в тестах (см. test_futures_client.py) без monkeypatch модуля config."""

    def __init__(self, api_key: str, api_secret: str, base_url: str = TESTNET_BASE_URL,
                 recv_window_ms: int = 5000):
        if not api_key or not api_secret:
            raise ValueError("FuturesClient: api_key/api_secret не заданы - проверь переменные окружения")
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
        """Запрос без подписи (публичные данные - exchangeInfo, свечи и т.п.)."""
        url = f"{self.base_url}{path}"
        resp = requests.request(method, url, params=params or {}, timeout=_REQUEST_TIMEOUT)
        return self._handle_response(resp)

    def _signed_request(self, method: str, path: str, params: Optional[dict] = None) -> dict:
        """Запрос С подписью (аккаунт/ордера) - добавляет timestamp/
        recvWindow, подписывает ТОЧНО ТУ строку, что будет отправлена,
        секрет никогда не уходит в заголовки/URL - только сама подпись."""
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
            raise FuturesApiError(f"Не удалось разобрать ответ Binance: {resp.text[:300]}")
        if resp.status_code >= 400:
            raise FuturesApiError(f"Binance Futures API error {resp.status_code}: {data}")
        return data

    # --- публичные данные (без подписи) ---

    def get_exchange_info(self) -> dict:
        return self._public_request("GET", "/fapi/v1/exchangeInfo")

    def get_symbol_filters(self, symbol: str) -> dict:
        """{'step_size': ..., 'tick_size': ..., 'min_notional': ...} -
        нужно, чтобы округлять количество/цену ровно так, как этого
        требует биржа (иначе ордер будет отклонён с ошибкой точности)."""
        info = self.get_exchange_info()
        for s in info.get("symbols", []):
            if s.get("symbol") == symbol:
                step_size = tick_size = min_notional = None
                for f in s.get("filters", []):
                    if f.get("filterType") == "LOT_SIZE":
                        step_size = float(f["stepSize"])
                    elif f.get("filterType") == "PRICE_FILTER":
                        tick_size = float(f["tickSize"])
                    elif f.get("filterType") == "MIN_NOTIONAL":
                        min_notional = float(f.get("notional", f.get("minNotional", 0)))
                return {"step_size": step_size, "tick_size": tick_size, "min_notional": min_notional}
        raise FuturesApiError(f"Символ {symbol} не найден в exchangeInfo")

    def get_mark_price(self, symbol: str) -> float:
        data = self._public_request("GET", "/fapi/v1/premiumIndex", {"symbol": symbol})
        return float(data["markPrice"])

    def get_funding_rate(self, symbol: str) -> float:
        """Текущая (последняя начисленная) ставка фандинга по символу -
        та же /fapi/v1/premiumIndex, что и get_mark_price (поле
        lastFundingRate), просто отдельным методом ради читаемости
        вызывающего кода (см. futures_signal_bridge - там important
        именно фандинг, а не markPrice). Положительная ставка = лонги
        платят шортам (риск для новых лонгов), отрицательная = шорты
        платят лонгам (риск для новых шортов). Значение - доля (0.0001
        = 0.01%), НЕ проценты."""
        data = self._public_request("GET", "/fapi/v1/premiumIndex", {"symbol": symbol})
        return float(data["lastFundingRate"])

    # --- аккаунт (с подписью) ---

    def get_available_balance(self, asset: str = "USDT") -> float:
        rows = self._signed_request("GET", "/fapi/v2/balance")
        for row in rows:
            if row.get("asset") == asset:
                return float(row["availableBalance"])
        return 0.0

    def get_position(self, symbol: str) -> Optional[dict]:
        rows = self._signed_request("GET", "/fapi/v2/positionRisk", {"symbol": symbol})
        for row in rows:
            if row.get("symbol") == symbol and float(row.get("positionAmt", 0)) != 0:
                return row
        return None

    def get_all_positions(self) -> list:
        """Все ОТКРЫТЫЕ позиции across ВСЕХ символов - в отличие от
        get_position (один конкретный символ), нужен предохранителям
        риска (см. risk_guard.py), чтобы посчитать "сколько сейчас
        всего открыто позиций одновременно", не зная заранее, какие
        символы вообще торгуются."""
        rows = self._signed_request("GET", "/fapi/v2/positionRisk")
        return [row for row in rows if float(row.get("positionAmt", 0)) != 0]

    def get_wallet_balance(self, asset: str = "USDT") -> float:
        """Полный баланс кошелька (см. поле 'balance' - включает
        нереализованный PnL по cross-марже, но НЕ включает то, что
        зарезервировано под открытые ордера) - в отличие от
        get_available_balance (свободная маржа под НОВУЮ позицию), это
        число - "как будто закрыли всё прямо сейчас", основа для
        дневного лимита убытка (см. risk_guard._daily_loss_pct)."""
        rows = self._signed_request("GET", "/fapi/v2/balance")
        for row in rows:
            if row.get("asset") == asset:
                return float(row["balance"])
        return 0.0

    def get_income_history(self, income_type: str = "REALIZED_PNL",
                            start_time_ms: Optional[int] = None, limit: int = 1000) -> list:
        """История начислений аккаунта (реализованный PnL, комиссии,
        фандинг и т.п. - см. incomeType в документации Binance). Нужна
        risk_guard.py, чтобы считать серию убыточных сделок ПОДРЯД по
        факту закрытия позиций на бирже, а не по локальному логу бота
        (тот не увидит сделку, закрытую вручную на сайте биржи)."""
        params = {"incomeType": income_type, "limit": limit}
        if start_time_ms is not None:
            params["startTime"] = start_time_ms
        return self._signed_request("GET", "/fapi/v1/income", params)

    def set_leverage(self, symbol: str, leverage: int) -> dict:
        return self._signed_request("POST", "/fapi/v1/leverage", {"symbol": symbol, "leverage": leverage})

    def set_margin_type(self, symbol: str, margin_type: str = "ISOLATED") -> Optional[dict]:
        """Binance возвращает ошибку -4046, если margin type уже такой,
        какой запрашивается - это не сбой, а норма (значит, всё уже
        настроено верно), поэтому глотаем именно эту ошибку и продолжаем."""
        try:
            return self._signed_request("POST", "/fapi/v1/marginType",
                                         {"symbol": symbol, "marginType": margin_type})
        except FuturesApiError as e:
            if "-4046" in str(e):
                logger.info("%s: margin type уже %s", symbol, margin_type)
                return None
            raise

    # --- ордера (с подписью) ---

    def place_market_order(self, symbol: str, side: str, quantity: float) -> dict:
        return self._signed_request("POST", "/fapi/v1/order", {
            "symbol": symbol, "side": side, "type": "MARKET", "quantity": quantity,
        })

    def place_stop_market(self, symbol: str, side: str, stop_price: float,
                           close_position: bool = True, quantity: Optional[float] = None) -> dict:
        """side - сторона ЗАКРЫВАЮЩЕГО ордера (противоположная стороне
        входа: для лонга - SELL, для шорта - BUY). close_position=True
        закрывает всю позицию целиком при срабатывании - не нужно
        отдельно передавать quantity (и они взаимоисключающие для
        Binance - именно поэтому quantity здесь Optional).

        С 2026 года STOP_MARKET/TAKE_PROFIT_MARKET на USD-M фьючерсах
        ставятся ТОЛЬКО через Algo Order API (/fapi/v1/algoOrder,
        algoType=CONDITIONAL) - обычный /fapi/v1/order их больше не
        принимает и возвращает ошибку -4120 ("Order type not supported
        for this endpoint"). Обнаружено вживую на testnet при первом
        реальном прогоне - см. тестовый вектор в test_futures_client.py."""
        params = {
            "algoType": "CONDITIONAL", "symbol": symbol, "side": side,
            "type": "STOP_MARKET", "triggerPrice": stop_price,
        }
        if close_position:
            params["closePosition"] = "true"
        else:
            params["quantity"] = quantity
            params["reduceOnly"] = "true"
        return self._signed_request("POST", "/fapi/v1/algoOrder", params)

    def place_take_profit_market(self, symbol: str, side: str, stop_price: float,
                                  close_position: bool = True, quantity: Optional[float] = None) -> dict:
        """См. docstring place_stop_market - та же Algo Order API."""
        params = {
            "algoType": "CONDITIONAL", "symbol": symbol, "side": side,
            "type": "TAKE_PROFIT_MARKET", "triggerPrice": stop_price,
        }
        if close_position:
            params["closePosition"] = "true"
        else:
            params["quantity"] = quantity
            params["reduceOnly"] = "true"
        return self._signed_request("POST", "/fapi/v1/algoOrder", params)

    def place_trailing_stop_market(self, symbol: str, side: str, callback_rate: float,
                                    close_position: bool = True, quantity: Optional[float] = None,
                                    activation_price: Optional[float] = None) -> dict:
        """TRAILING_STOP_MARKET - стоп, который сам подтягивается вслед за
        ценой на callback_rate% от лучшего достигнутого уровня, вместо
        фиксированной цены (см. futures_position_monitor._manage_partial_profit -
        используется на ОСТАТКЕ позиции после частичного профита, чтобы
        поймать более крупное движение, если оно продолжится, а не просто
        зафиксировать исходный тейк целиком).

        activation_price - с какой цены начинать отслеживать лучший
        уровень. Стоит передавать текущую рыночную цену явно (а не
        полагаться на дефолт биржи) - иначе при активации "задним числом"
        от цены входа стоп может тут же посчитать текущую цену уже
        достаточным откатом и сработать почти сразу после постановки.

        См. docstring place_stop_market про то, почему это тоже Algo
        Order API, а не обычный /fapi/v1/order."""
        params = {
            "algoType": "CONDITIONAL", "symbol": symbol, "side": side,
            "type": "TRAILING_STOP_MARKET", "callbackRate": callback_rate,
        }
        if activation_price is not None:
            params["activationPrice"] = activation_price
        if close_position:
            params["closePosition"] = "true"
        else:
            params["quantity"] = quantity
            params["reduceOnly"] = "true"
        return self._signed_request("POST", "/fapi/v1/algoOrder", params)

    def place_reduce_only_market_order(self, symbol: str, side: str, quantity: float) -> dict:
        """MARKET-ордер с reduceOnly=true - для ЧАСТИЧНОГО закрытия уже
        открытой позиции (в отличие от place_market_order, который
        используется и для входа тоже, и сам по себе ничем не мешает
        случайно нарастить позицию вместо того, чтобы её уменьшить).
        reduceOnly биржа отклонит, если по факту получилось бы увеличение,
        а не уменьшение - дополнительная защита от бага в вызывающем коде."""
        return self._signed_request("POST", "/fapi/v1/order", {
            "symbol": symbol, "side": side, "type": "MARKET",
            "quantity": quantity, "reduceOnly": "true",
        })

    def cancel_order(self, symbol: str, order_id) -> dict:
        """Отменяет ОДИН конкретный algo-ордер (стоп-лосс/тейк-профит/
        трейлинг-стоп) по его orderId - в отличие от cancel_all_algo_orders
        (сразу все ордера по символу), нужен частичному профиту (см.
        futures_position_monitor._manage_partial_profit), где нужно снять
        ИМЕННО старый стоп или тейк по отдельности, не трогая другой
        условный ордер, который мог быть выставлен позже."""
        return self._signed_request("DELETE", "/fapi/v1/algoOrder", {"symbol": symbol, "algoId": order_id})

    def get_open_orders(self, symbol: str) -> list:
        """Обычные (LIMIT/MARKET) И algo-ордера (наши STOP_MARKET/
        TAKE_PROFIT_MARKET, см. place_stop_market) - это ДВЕ отдельные
        системы у Binance с 2026 года, поэтому опрашиваем обе и
        возвращаем объединённый список."""
        regular = self._signed_request("GET", "/fapi/v1/openOrders", {"symbol": symbol})
        algo = self._signed_request("GET", "/fapi/v1/openAlgoOrders", {"symbol": symbol})
        algo_orders = algo.get("data", algo) if isinstance(algo, dict) else algo
        return list(regular) + list(algo_orders)

    def cancel_all_open_orders(self, symbol: str) -> dict:
        """Отменяет ОБЫЧНЫЕ ордера. Стоп-лосс/тейк-профит (algo-ордера) -
        отдельная система, их отменяет cancel_all_algo_orders (см. ниже) -
        оба вызова нужны для полной очистки (см. futures_executor.
        emergency_close_all, который вызывает оба)."""
        return self._signed_request("DELETE", "/fapi/v1/allOpenOrders", {"symbol": symbol})

    def cancel_all_algo_orders(self, symbol: str) -> dict:
        """Отменяет ВСЕ открытые algo-ордера (наши STOP_MARKET/
        TAKE_PROFIT_MARKET) по символу - см. place_stop_market про то,
        почему они теперь отдельная от обычных ордеров система. Без
        этого вызова аварийное закрытие позиции (emergency_close_all)
        могло бы оставить старый стоп/тейк висеть на бирже и случайно
        сработать на уже закрытой/новой позиции."""
        return self._signed_request("DELETE", "/fapi/v1/algoOpenOrders", {"symbol": symbol})


def client_from_config(config) -> FuturesClient:
    """Собирает FuturesClient из config.py (BINANCE_FUTURES_*) - единая
    точка входа для main.py/скриптов, чтобы не дублировать логику
    выбора testnet/mainnet URL по всему проекту."""
    base_url = TESTNET_BASE_URL if config.BINANCE_FUTURES_USE_TESTNET else MAINNET_BASE_URL
    if not config.BINANCE_FUTURES_USE_TESTNET:
        logger.warning(
            "FuturesClient создаётся для РЕАЛЬНОГО счёта (BINANCE_FUTURES_USE_TESTNET=false) - "
            "используются настоящие средства."
        )
    return FuturesClient(
        api_key=config.BINANCE_FUTURES_API_KEY,
        api_secret=config.BINANCE_FUTURES_API_SECRET,
        base_url=base_url,
        recv_window_ms=config.BINANCE_FUTURES_RECV_WINDOW_MS,
    )
