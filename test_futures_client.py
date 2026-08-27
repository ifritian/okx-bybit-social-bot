#!/usr/bin/env python3
"""
Тесты futures_client.py - подпись запросов (официальный тестовый вектор
Binance) и обработка ответов, без реальной сети (requests подменяется
monkeypatch).
"""
import futures_client as fc


class _FakeResponse:
    def __init__(self, json_data, status_code=200):
        self._json = json_data
        self.status_code = status_code
        self.text = str(json_data)

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise Exception(f"HTTP {self.status_code}")


def _client():
    return fc.FuturesClient(api_key="test-key", api_secret="test-secret")


# --- Подпись (официальный тестовый вектор из документации Binance) ---

def test_sign_matches_official_binance_example():
    client = fc.FuturesClient(
        api_key="x",
        api_secret="NhqPtmdSJYdKjVHjA7PZj4Mge3R5YNiP1e3UZjInClVN65XAbvqqM6A7H5fATj0j",
    )
    query = ("symbol=LTCBTC&side=BUY&type=LIMIT&timeInForce=GTC&quantity=1"
             "&price=0.1&recvWindow=5000&timestamp=1499827319559")
    signature = client._sign(query)
    assert signature == "c8db56825ae71d6d79447849e617115f4a920fa2acdcab2b053c4b2838bd6b71"
    assert len(signature) == 64  # HMAC-SHA256 в hex - всегда 64 символа


def test_client_requires_api_key_and_secret():
    try:
        fc.FuturesClient(api_key="", api_secret="secret")
        assert False, "должно было упасть без api_key"
    except ValueError:
        pass
    try:
        fc.FuturesClient(api_key="key", api_secret="")
        assert False, "должно было упасть без api_secret"
    except ValueError:
        pass


def test_default_base_url_is_testnet():
    client = _client()
    assert client.base_url == fc.TESTNET_BASE_URL
    assert client.is_testnet is True


def test_mainnet_client_is_not_testnet():
    client = fc.FuturesClient(api_key="k", api_secret="s", base_url=fc.MAINNET_BASE_URL)
    assert client.is_testnet is False


# --- Обработка ответов/ошибок ---

def test_handle_response_raises_on_error_status(monkeypatch):
    client = _client()
    monkeypatch.setattr(fc.requests, "request",
                         lambda *a, **k: _FakeResponse({"code": -1121, "msg": "Invalid symbol."}, 400))
    try:
        client._signed_request("GET", "/fapi/v2/balance")
        assert False, "должно было бросить FuturesApiError"
    except fc.FuturesApiError as e:
        assert "-1121" in str(e) or "Invalid symbol" in str(e)


def test_signed_request_includes_timestamp_and_signature(monkeypatch):
    captured = {}

    def fake_request(method, url, headers=None, **kwargs):
        captured["url"] = url
        captured["headers"] = headers
        return _FakeResponse({"ok": True})

    monkeypatch.setattr(fc.requests, "request", fake_request)
    client = _client()
    client._signed_request("GET", "/fapi/v2/balance", {"symbol": "BTCUSDT"})

    assert "timestamp=" in captured["url"]
    assert "signature=" in captured["url"]
    assert captured["headers"]["X-MBX-APIKEY"] == "test-key"
    # Секрет никогда не должен светиться в URL/заголовках.
    assert client._api_secret not in captured["url"]


# --- Публичные данные ---

def test_get_funding_rate_reads_last_funding_rate_field(monkeypatch):
    def fake_request(method, url, params=None, **kwargs):
        assert params == {"symbol": "BTCUSDT"}
        return _FakeResponse({"symbol": "BTCUSDT", "markPrice": "50000.0", "lastFundingRate": "0.00012"})

    monkeypatch.setattr(fc.requests, "request", fake_request)
    client = _client()
    assert client.get_funding_rate("BTCUSDT") == 0.00012


def test_get_funding_rate_handles_negative_rate(monkeypatch):
    def fake_request(method, url, params=None, **kwargs):
        return _FakeResponse({"markPrice": "50000.0", "lastFundingRate": "-0.0034"})

    monkeypatch.setattr(fc.requests, "request", fake_request)
    client = _client()
    assert client.get_funding_rate("BTCUSDT") == -0.0034


# --- Ордера (форма запроса) ---

def test_place_stop_market_uses_algo_order_endpoint(monkeypatch):
    captured = {}
    monkeypatch.setattr(fc.requests, "request",
                         lambda method, url, **k: (captured.setdefault("method", method),
                                                     captured.setdefault("url", url), _FakeResponse({"algoId": 1}))[2])
    client = _client()
    client.place_stop_market("BTCUSDT", "SELL", stop_price=95000.0, close_position=True)
    # С 2026 года стоп/тейк идут через отдельный Algo Order API, не
    # обычный /fapi/v1/order (см. docstring place_stop_market) - это
    # подтвердилось вживую на testnet ошибкой -4120 на старом пути.
    assert "/fapi/v1/algoOrder" in captured["url"]
    assert captured["method"] == "POST"
    assert "algoType=CONDITIONAL" in captured["url"]
    assert "triggerPrice=95000" in captured["url"]
    assert "stopPrice=" not in captured["url"]  # старое имя параметра больше не используется


def test_place_take_profit_market_uses_algo_order_endpoint(monkeypatch):
    captured = {}
    monkeypatch.setattr(fc.requests, "request",
                         lambda method, url, **k: (captured.setdefault("url", url), _FakeResponse({"algoId": 2}))[1])
    client = _client()
    client.place_take_profit_market("BTCUSDT", "SELL", stop_price=110000.0, close_position=True)
    assert "/fapi/v1/algoOrder" in captured["url"]
    assert "type=TAKE_PROFIT_MARKET" in captured["url"]
    assert "triggerPrice=110000" in captured["url"]


def test_cancel_all_algo_orders_uses_correct_endpoint(monkeypatch):
    captured = {}
    monkeypatch.setattr(fc.requests, "request",
                         lambda method, url, **k: (captured.setdefault("method", method),
                                                     captured.setdefault("url", url), _FakeResponse({}))[2])
    client = _client()
    client.cancel_all_algo_orders("BTCUSDT")
    assert captured["method"] == "DELETE"
    assert "/fapi/v1/algoOpenOrders" in captured["url"]


def test_get_open_orders_merges_regular_and_algo(monkeypatch):
    def fake_request(method, url, **k):
        if "openAlgoOrders" in url:
            return _FakeResponse([{"algoId": 1, "type": "STOP_MARKET"}])
        return _FakeResponse([{"orderId": 2, "type": "LIMIT"}])

    monkeypatch.setattr(fc.requests, "request", fake_request)
    client = _client()
    orders = client.get_open_orders("BTCUSDT")
    assert len(orders) == 2
    assert any("orderId" in o for o in orders)
    assert any("algoId" in o for o in orders)


def test_place_stop_market_close_position_omits_quantity(monkeypatch):
    captured = {}
    monkeypatch.setattr(fc.requests, "request",
                         lambda method, url, **k: (captured.setdefault("url", url), _FakeResponse({"orderId": 1}))[1])
    client = _client()
    client.place_stop_market("BTCUSDT", "SELL", stop_price=95000.0, close_position=True)
    assert "closePosition=true" in captured["url"]
    assert "quantity=" not in captured["url"]


def test_place_stop_market_partial_requires_quantity_and_reduce_only(monkeypatch):
    captured = {}
    monkeypatch.setattr(fc.requests, "request",
                         lambda method, url, **k: (captured.setdefault("url", url), _FakeResponse({"orderId": 1}))[1])
    client = _client()
    client.place_stop_market("BTCUSDT", "SELL", stop_price=95000.0, close_position=False, quantity=0.01)
    assert "reduceOnly=true" in captured["url"]
    assert "quantity=0.01" in captured["url"]


def test_place_trailing_stop_market_uses_algo_order_endpoint(monkeypatch):
    captured = {}
    monkeypatch.setattr(fc.requests, "request",
                         lambda method, url, **k: (captured.setdefault("method", method),
                                                     captured.setdefault("url", url), _FakeResponse({"orderId": 4}))[2])
    client = _client()
    client.place_trailing_stop_market("BTCUSDT", "SELL", callback_rate=1.0, close_position=True, activation_price=101.5)
    assert captured["method"] == "POST"
    assert "/fapi/v1/algoOrder" in captured["url"]
    assert "algoType=CONDITIONAL" in captured["url"]
    assert "type=TRAILING_STOP_MARKET" in captured["url"]
    assert "callbackRate=1.0" in captured["url"]
    assert "activationPrice=101.5" in captured["url"]
    assert "closePosition=true" in captured["url"]
    assert "quantity=" not in captured["url"]


def test_place_trailing_stop_market_partial_requires_quantity_and_reduce_only(monkeypatch):
    captured = {}
    monkeypatch.setattr(fc.requests, "request",
                         lambda method, url, **k: (captured.setdefault("url", url), _FakeResponse({"orderId": 4}))[1])
    client = _client()
    client.place_trailing_stop_market("BTCUSDT", "SELL", callback_rate=2.0, close_position=False, quantity=0.5)
    assert "reduceOnly=true" in captured["url"]
    assert "quantity=0.5" in captured["url"]


def test_place_reduce_only_market_order_sets_reduce_only(monkeypatch):
    captured = {}
    monkeypatch.setattr(fc.requests, "request",
                         lambda method, url, **k: (captured.setdefault("method", method),
                                                     captured.setdefault("url", url), _FakeResponse({"orderId": 5}))[2])
    client = _client()
    client.place_reduce_only_market_order("BTCUSDT", "SELL", 0.25)
    assert captured["method"] == "POST"
    assert "/fapi/v1/order" in captured["url"]
    assert "reduceOnly=true" in captured["url"]
    assert "quantity=0.25" in captured["url"]
    assert "type=MARKET" in captured["url"]


def test_cancel_order_uses_algo_order_endpoint_with_algo_id(monkeypatch):
    captured = {}
    monkeypatch.setattr(fc.requests, "request",
                         lambda method, url, **k: (captured.setdefault("method", method),
                                                     captured.setdefault("url", url), _FakeResponse({}))[2])
    client = _client()
    client.cancel_order("BTCUSDT", 12345)
    assert captured["method"] == "DELETE"
    assert "/fapi/v1/algoOrder" in captured["url"]
    assert "algoId=12345" in captured["url"]


def test_set_margin_type_swallows_already_set_error(monkeypatch):
    monkeypatch.setattr(fc.requests, "request",
                         lambda *a, **k: _FakeResponse({"code": -4046, "msg": "No need to change margin type."}, 400))
    client = _client()
    result = client.set_margin_type("BTCUSDT", "ISOLATED")
    assert result is None  # не бросает исключение - это ожидаемая "ошибка"


def test_set_margin_type_reraises_other_errors(monkeypatch):
    monkeypatch.setattr(fc.requests, "request",
                         lambda *a, **k: _FakeResponse({"code": -1121, "msg": "Invalid symbol."}, 400))
    client = _client()
    try:
        client.set_margin_type("BTCUSDT", "ISOLATED")
        assert False, "должно было пробросить ошибку дальше"
    except fc.FuturesApiError:
        pass


if __name__ == "__main__":
    import sys
    import types

    class _MiniMonkeypatch:
        def __init__(self):
            self._restore = []

        def setattr(self, obj, name, value):
            self._restore.append((obj, name, getattr(obj, name)))
            setattr(obj, name, value)

        def undo(self):
            for obj, name, old in reversed(self._restore):
                setattr(obj, name, old)

    passed, failed = 0, 0
    module = sys.modules[__name__]
    for name in dir(module):
        if not name.startswith("test_"):
            continue
        fn = getattr(module, name)
        if not isinstance(fn, types.FunctionType):
            continue
        mp = _MiniMonkeypatch()
        try:
            if "monkeypatch" in fn.__code__.co_varnames[: fn.__code__.co_argcount]:
                fn(mp)
            else:
                fn()
            print(f"OK   {name}")
            passed += 1
        except AssertionError as e:
            print(f"FAIL {name}: {e}")
            failed += 1
        finally:
            mp.undo()

    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
