#!/usr/bin/env python3
"""
Тесты portfolio_rebalancer.py - анализ отклонения от целевых весов и
планирование ордеров (analyze_portfolio/plan_orders/check_and_rebalance)
на ПОДДЕЛЬНОМ SpotClient, без реальной сети и без обращения к
config.PORTFOLIO_REBALANCE_DRIFT_THRESHOLD_PCT напрямую (monkeypatch).
"""
import config
import portfolio_rebalancer as pr
import treasury_index


def _all_basket_symbols():
    symbols = set()
    for tier in treasury_index.BASKET.values():
        for c in tier:
            symbols.add(c["ticker"] + "USDT")
            if c.get("fallback"):
                symbols.add(c["fallback"] + "USDT")
    return symbols


class _FakeClient:
    base_url = "https://testnet.binance.vision"

    def __init__(self, balances, prices, tradable, step_size=0.0001, min_notional=5.0):
        self._balances = balances
        self._prices = prices
        self._tradable = tradable
        self._step_size = step_size
        self._min_notional = min_notional
        self.orders_placed = []

    def get_exchange_info(self):
        return {"symbols": [{"symbol": s, "status": "TRADING"} for s in self._tradable]}

    def get_balances(self):
        return dict(self._balances)

    def get_price(self, symbol):
        return self._prices[symbol]

    def get_symbol_filters(self, symbol, exchange_info=None):
        return {"step_size": self._step_size, "min_notional": self._min_notional}

    def place_market_order(self, symbol, side, quantity=None, quote_order_qty=None):
        self.orders_placed.append((symbol, side, quantity, quote_order_qty))
        return {"orderId": len(self.orders_placed), "symbol": symbol, "side": side}


def _balanced_balances(total=10000.0, price=10.0):
    balances = {"USDT": 0.0}
    for tier in treasury_index.BASKET.values():
        for c in tier:
            balances[c["ticker"]] = (c["weight"] / 100 * total) / price
    return balances


def test_balanced_portfolio_has_zero_drift_and_no_orders():
    symbols = _all_basket_symbols()
    prices = {s: 10.0 for s in symbols}
    client = _FakeClient(_balanced_balances(), prices, symbols)

    analysis = pr.analyze_portfolio(client)
    assert analysis is not None
    assert round(analysis.max_drift_pct, 6) == 0.0

    summary = pr.check_and_rebalance(client, dry_run=True)
    assert summary["needs_rebalance"] is False
    assert summary["orders_planned"] == []


def test_drifted_portfolio_triggers_rebalance_and_orders_are_funded(monkeypatch):
    monkeypatch.setattr(config, "PORTFOLIO_REBALANCE_DRIFT_THRESHOLD_PCT", 5.0)
    symbols = _all_basket_symbols()
    prices = {s: 10.0 for s in symbols}
    balances = _balanced_balances()
    balances["SOL"] *= 2  # SOL (вес 20%, крупнейший) вдвое больше целевого
    client = _FakeClient(balances, prices, symbols)

    summary = pr.check_and_rebalance(client, dry_run=True)
    assert summary["needs_rebalance"] is True
    assert summary["max_drift_pct"] > 5.0

    orders = summary["orders_planned"]
    sells = [o for o in orders if o.side == "SELL"]
    buys = [o for o in orders if o.side == "BUY"]
    assert len(sells) == 1 and sells[0].symbol == "SOLUSDT"

    # Бюджет на покупки не должен превышать выручку с продаж (никаких
    # "лишних" денег из воздуха) - с точностью до округления по step_size.
    sell_proceeds = sells[0].quantity * 10.0
    buy_total = sum(o.quote_order_qty for o in buys)
    assert buy_total <= sell_proceeds + 1e-6

    # dry-run не должен реально слать ордера.
    assert client.orders_placed == []


def test_live_mode_actually_places_orders(monkeypatch):
    monkeypatch.setattr(config, "PORTFOLIO_REBALANCE_DRIFT_THRESHOLD_PCT", 5.0)
    monkeypatch.setattr(pr, "_notify_owner", lambda *a, **k: None)  # не дёргаем Telegram в тесте
    symbols = _all_basket_symbols()
    prices = {s: 10.0 for s in symbols}
    balances = _balanced_balances()
    balances["SOL"] *= 2
    client = _FakeClient(balances, prices, symbols)

    summary = pr.check_and_rebalance(client, dry_run=False)
    assert summary["needs_rebalance"] is True
    assert len(client.orders_placed) == len(summary["orders_planned"])
    assert any(s == "SOLUSDT" and side == "SELL" for s, side, _, _ in client.orders_placed)


def test_unresolvable_symbol_is_skipped_and_weights_renormalized(monkeypatch):
    monkeypatch.setattr(config, "PORTFOLIO_REBALANCE_DRIFT_THRESHOLD_PCT", 5.0)
    symbols = _all_basket_symbols() - {"SOLUSDT"}  # SOL недоступен на testnet
    prices = {s: 10.0 for s in symbols}
    balances = _balanced_balances()
    del balances["SOL"]
    client = _FakeClient(balances, prices, symbols)

    analysis = pr.analyze_portfolio(client)
    assert analysis is not None
    assert analysis.skipped_tickers == ["SOL"]
    assert all(p.ticker != "SOL" for p in analysis.positions)
    # Остальные веса перенормировались до суммы 100 между собой.
    assert round(sum(p.target_weight_pct for p in analysis.positions), 4) == 100.0


def test_analyze_portfolio_returns_none_when_nothing_resolves():
    client = _FakeClient({"USDT": 100.0}, {}, tradable=set())
    assert pr.analyze_portfolio(client) is None


def test_orders_below_min_notional_are_skipped():
    symbols = _all_basket_symbols()
    prices = {s: 10.0 for s in symbols}
    balances = _balanced_balances(total=10000.0)
    # Крошечный перекос PENDLE (вес 1.5%, самый маленький) - сумма
    # сделки должна выйти меньше типичного min_notional (5 USDT).
    balances["PENDLE"] *= 1.02
    client = _FakeClient(balances, prices, symbols, min_notional=5.0)

    summary = pr.check_and_rebalance(client, dry_run=True)
    # Либо вообще не считается отклонением, либо считается, но ордер
    # по PENDLE отфильтрован как слишком мелкий - в любом случае PENDLE
    # не должен попасть в orders_planned.
    assert all(o.ticker != "PENDLE" for o in summary["orders_planned"])
