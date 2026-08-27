#!/usr/bin/env python3
"""
Тесты market_stats.py: ротация темы, расчёт статистики по тикеру и по
теме (сеть замокана - fetch_klines подменяется фиктивными свечами).
"""
import market_stats


def _fake_klines(prices):
    """Строит минимальный набор свечей для calc_ticker_stats: нужны
    только open первой свечи, close последней, и high/low по всем."""
    klines = []
    for p in prices:
        klines.append({"open": p, "high": p * 1.01, "low": p * 0.99, "close": p, "volume": 100})
    return klines


def test_pick_theme_avoids_last_used():
    themes = list(market_stats.THEMES.keys())
    for _ in range(30):
        chosen = market_stats.pick_theme(themes[0])
        assert chosen != themes[0]


def test_pick_theme_handles_unknown_last():
    chosen = market_stats.pick_theme(None)
    assert chosen in market_stats.THEMES


def test_calc_ticker_stats_computes_pct_and_amplitude(monkeypatch):
    monkeypatch.setattr(market_stats, "fetch_klines", lambda ticker, days=2: _fake_klines([100, 110]))

    stats = market_stats.calc_ticker_stats("BTC")

    assert stats is not None
    assert stats["pct"] == 10.0
    assert stats["current_price"] == 110


def test_calc_ticker_stats_returns_none_on_too_few_klines(monkeypatch):
    monkeypatch.setattr(market_stats, "fetch_klines", lambda ticker, days=2: _fake_klines([100]))

    assert market_stats.calc_ticker_stats("BTC") is None


def test_calc_theme_stats_single_ticker(monkeypatch):
    monkeypatch.setattr(market_stats, "fetch_klines", lambda ticker, days=2: _fake_klines([100, 105]))

    stats = market_stats.calc_theme_stats("BTC")

    assert stats is not None
    assert "single" in stats
    assert stats["single"]["pct"] == 5.0


def test_calc_theme_stats_basket_computes_average(monkeypatch):
    prices_by_ticker = {"BTC": [100, 110], "ETH": [100, 90], "SOL": [100, 100], "BNB": [100, 105]}

    def _fake_fetch(ticker, days=2):
        return _fake_klines(prices_by_ticker[ticker])

    monkeypatch.setattr(market_stats, "fetch_klines", _fake_fetch)

    stats = market_stats.calc_theme_stats("market")

    assert stats is not None
    assert "breakdown" in stats
    assert stats["breakdown"]["BTC"] == 10.0
    assert stats["breakdown"]["ETH"] == -10.0
    # (10 - 10 + 0 + 5) / 4 = 1.25
    assert stats["avg_pct"] == 1.25


def test_calc_theme_stats_returns_none_when_all_tickers_fail(monkeypatch):
    monkeypatch.setattr(market_stats, "fetch_klines", lambda ticker, days=2: [])

    assert market_stats.calc_theme_stats("market") is None
