#!/usr/bin/env python3
"""
Тесты spot_client.py - подпись запросов (тот же механизм и тестовый
вектор, что и у futures_client.py - Binance использует одинаковую
HMAC-SHA256 схему на споте и фьючерсах) и разбор ответов, без реальной
сети.
"""
import spot_client as sc


def _client():
    return sc.SpotClient(api_key="test-key", api_secret="test-secret")


def test_sign_matches_official_binance_example():
    client = sc.SpotClient(
        api_key="x",
        api_secret="NhqPtmdSJYdKjVHjA7PZj4Mge3R5YNiP1e3UZjInClVN65XAbvqqM6A7H5fATj0j",
    )
    query = ("symbol=LTCBTC&side=BUY&type=LIMIT&timeInForce=GTC&quantity=1"
             "&price=0.1&recvWindow=5000&timestamp=1499827319559")
    signature = client._sign(query)
    assert signature == "c8db56825ae71d6d79447849e617115f4a920fa2acdcab2b053c4b2838bd6b71"
    assert len(signature) == 64


def test_client_requires_api_key_and_secret():
    try:
        sc.SpotClient(api_key="", api_secret="secret")
        assert False, "должно было упасть без api_key"
    except ValueError:
        pass
    try:
        sc.SpotClient(api_key="key", api_secret="")
        assert False, "должно было упасть без api_secret"
    except ValueError:
        pass


def test_defaults_to_testnet():
    client = _client()
    assert client.base_url == sc.TESTNET_BASE_URL
    assert client.is_testnet is True


def test_mainnet_flag():
    client = sc.SpotClient(api_key="k", api_secret="s", base_url=sc.MAINNET_BASE_URL)
    assert client.is_testnet is False


def test_symbol_exists_true_false():
    client = _client()
    info = {"symbols": [
        {"symbol": "SOLUSDT", "status": "TRADING"},
        {"symbol": "PENDLEUSDT", "status": "BREAK"},
    ]}
    assert client.symbol_exists("SOLUSDT", info) is True
    assert client.symbol_exists("PENDLEUSDT", info) is False  # не TRADING
    assert client.symbol_exists("NOPEUSDT", info) is False    # отсутствует вовсе


def test_get_symbol_filters_parses_lot_size_and_notional():
    client = _client()
    info = {"symbols": [{
        "symbol": "SOLUSDT",
        "filters": [
            {"filterType": "LOT_SIZE", "stepSize": "0.001"},
            {"filterType": "NOTIONAL", "minNotional": "5.0"},
        ],
    }]}
    filters = client.get_symbol_filters("SOLUSDT", info)
    assert filters == {"step_size": 0.001, "min_notional": 5.0}


def test_get_symbol_filters_missing_symbol_raises():
    client = _client()
    try:
        client.get_symbol_filters("NOPEUSDT", {"symbols": []})
        assert False, "должно было упасть SpotApiError"
    except sc.SpotApiError:
        pass


def test_place_market_order_requires_exactly_one_amount_kind(monkeypatch):
    client = _client()
    calls = []
    monkeypatch.setattr(client, "_signed_request", lambda method, path, params=None: calls.append(params) or {"ok": True})

    try:
        client.place_market_order("SOLUSDT", "BUY")
        assert False, "должно было упасть без quantity и quote_order_qty"
    except ValueError:
        pass

    try:
        client.place_market_order("SOLUSDT", "BUY", quantity=1, quote_order_qty=10)
        assert False, "должно было упасть с обоими сразу"
    except ValueError:
        pass

    client.place_market_order("SOLUSDT", "SELL", quantity=1.5)
    assert calls[-1] == {"symbol": "SOLUSDT", "side": "SELL", "type": "MARKET", "quantity": 1.5}

    client.place_market_order("SOLUSDT", "BUY", quote_order_qty=50.0)
    assert calls[-1] == {"symbol": "SOLUSDT", "side": "BUY", "type": "MARKET", "quoteOrderQty": 50.0}
