#!/usr/bin/env python3
"""
Тесты новой логики сравнения индекса с ETH/топ-4 рынка и графика
динамики (Этап 4, направления C/D):
- treasury_index.fetch_market_benchmark_pct - равновзвешенный % корзины
- queue_manager.update_treasury_history - расширение на eth/market
- queue_manager.append_treasury_snapshot - накопление снимков для графика
- treasury_generator._format_comparison_block - новый компактный блок
"""
import queue_manager
import treasury_chart
import treasury_generator
import treasury_index
from treasury_index import TreasuryIndexResult, TierResult


def test_fetch_market_benchmark_pct_averages_available_tickers(monkeypatch):
    fake_pcts = {"BTCUSDT": 2.0, "ETHUSDT": 4.0, "SOLUSDT": 6.0, "BNBUSDT": None}
    monkeypatch.setattr(treasury_index, "_fetch_symbol_change_pct", lambda symbol, hours: fake_pcts[symbol])

    result = treasury_index.fetch_market_benchmark_pct(12.0)

    # BNB недоступен - усредняем только 3 из 4: (2+4+6)/3 = 4.0
    assert result == 4.0


def test_fetch_market_benchmark_pct_none_when_all_unavailable(monkeypatch):
    monkeypatch.setattr(treasury_index, "_fetch_symbol_change_pct", lambda symbol, hours: None)
    assert treasury_index.fetch_market_benchmark_pct(12.0) is None


def test_update_treasury_history_first_call_initializes_all_four(monkeypatch):
    monkeypatch.setattr(queue_manager, "get_treasury_history", lambda: None)
    saved = {}
    monkeypatch.setattr(queue_manager, "_set", lambda key, value: saved.__setitem__(key, value))

    history = queue_manager.update_treasury_history(2.0, 1.0, eth_pct=3.0, market_pct=1.5)

    assert history["index_value"] == round(100 * 1.02, 4)
    assert history["btc_value"] == round(100 * 1.01, 4)
    assert history["eth_value"] == round(100 * 1.03, 4)
    assert history["market_value"] == round(100 * 1.015, 4)


def test_update_treasury_history_backfills_missing_keys_from_old_state(monkeypatch):
    # Старая запись (до появления eth_value/market_value) - обратная
    # совместимость должна добавить недостающие ключи с базой 100, а не упасть.
    old_history = {"launch_at": 1700000000.0, "index_value": 110.0, "btc_value": 105.0}
    monkeypatch.setattr(queue_manager, "get_treasury_history", lambda: dict(old_history))
    monkeypatch.setattr(queue_manager, "_set", lambda key, value: None)

    history = queue_manager.update_treasury_history(1.0, 1.0, eth_pct=2.0, market_pct=None)

    assert history["eth_value"] == round(100 * 1.02, 4)
    # market_pct не передан (None) - market_value остаётся на базе 100, не падает.
    assert history["market_value"] == 100.0


def test_update_treasury_history_skips_update_when_pct_is_none(monkeypatch):
    monkeypatch.setattr(queue_manager, "get_treasury_history", lambda: None)
    monkeypatch.setattr(queue_manager, "_set", lambda key, value: None)

    history = queue_manager.update_treasury_history(1.0, 1.0, eth_pct=None, market_pct=None)

    assert history["eth_value"] == 100.0
    assert history["market_value"] == 100.0


def test_append_treasury_snapshot_stores_timestamped_copy(monkeypatch):
    monkeypatch.setattr(queue_manager, "get_treasury_snapshots", lambda: [])
    saved = {}
    monkeypatch.setattr(queue_manager, "_set", lambda key, value: saved.__setitem__(key, value))

    history = {"index_value": 105.0, "btc_value": 102.0, "eth_value": 108.0, "market_value": 103.0}
    snapshots = queue_manager.append_treasury_snapshot(history)

    assert len(snapshots) == 1
    assert snapshots[0]["index_value"] == 105.0
    assert "timestamp" in snapshots[0]
    assert saved["treasury_snapshots"] == snapshots


def test_append_treasury_snapshot_caps_length(monkeypatch):
    existing = [{"timestamp": i, "index_value": 100.0, "btc_value": 100.0, "eth_value": 100.0, "market_value": 100.0}
                for i in range(queue_manager._TREASURY_SNAPSHOTS_MAX)]
    monkeypatch.setattr(queue_manager, "get_treasury_snapshots", lambda: existing)
    saved = {}
    monkeypatch.setattr(queue_manager, "_set", lambda key, value: saved.__setitem__(key, value))

    history = {"index_value": 105.0, "btc_value": 102.0, "eth_value": 108.0, "market_value": 103.0}
    snapshots = queue_manager.append_treasury_snapshot(history)

    assert len(snapshots) == queue_manager._TREASURY_SNAPSHOTS_MAX


def _fake_result(total_pct=2.0):
    return TreasuryIndexResult(total_pct=total_pct, period_hours=12.0, tiers=[], missing=[])


def test_format_comparison_block_includes_eth_and_market_lines():
    result = _fake_result(total_pct=2.0)
    block = treasury_generator._format_comparison_block(
        result, 12.0, btc_pct=1.0, eth_pct=3.0, market_pct=1.5, history=None,
    )
    assert "BTC" in block and "обогнал BTC на 1.0" in block
    assert "ETH +3.0%" in block
    assert "топ-4 рынка +1.5%" in block


def test_format_comparison_block_since_launch_includes_all_four():
    result = _fake_result(total_pct=2.0)
    history = {"launch_at": 1700000000.0, "index_value": 110.0, "btc_value": 105.0, "eth_value": 108.0, "market_value": 106.0}
    block = treasury_generator._format_comparison_block(
        result, 12.0, btc_pct=1.0, eth_pct=3.0, market_pct=1.5, history=history,
    )
    assert "С запуска" in block
    assert "Индекс +10.0%" in block
    assert "BTC +5.0%" in block
    assert "ETH +8.0%" in block
    assert "Рынок +6.0%" in block


def test_format_comparison_block_empty_when_nothing_available():
    result = _fake_result(total_pct=2.0)
    block = treasury_generator._format_comparison_block(
        result, 12.0, btc_pct=None, eth_pct=None, market_pct=None, history=None,
    )
    assert block == ""


def test_format_comparison_block_handles_partial_benchmarks():
    # BTC недоступен, но ETH и рынок - да: заголовочная BTC-строка
    # пропускается, но компактная строка сравнения всё равно появляется.
    result = _fake_result(total_pct=2.0)
    block = treasury_generator._format_comparison_block(
        result, 12.0, btc_pct=None, eth_pct=3.0, market_pct=None, history=None,
    )
    assert "BTC" not in block
    assert "ETH +3.0%" in block


def _make_snapshots(n: int, start_ts: float = 1700000000.0):
    return [
        {
            "timestamp": start_ts + i * 3600,
            "index_value": 100.0 + i,
            "btc_value": 100.0 + i * 0.5,
            "eth_value": 100.0 + i * 0.7,
            "market_value": 100.0 + i * 0.6,
        }
        for i in range(n)
    ]


def test_generate_treasury_chart_returns_none_below_min_snapshots():
    snapshots = _make_snapshots(treasury_chart.MIN_SNAPSHOTS_FOR_CHART - 1)
    assert treasury_chart.generate_treasury_chart(snapshots) is None


def test_generate_treasury_chart_produces_file():
    snapshots = _make_snapshots(treasury_chart.MIN_SNAPSHOTS_FOR_CHART + 5)
    path = treasury_chart.generate_treasury_chart(snapshots)
    assert path is not None
    assert path.exists()
    assert path.stat().st_size > 0


def test_generate_treasury_chart_handles_missing_keys_gracefully():
    # Старый снимок без eth_value/market_value (обратная совместимость) -
    # не должен ронять построение графика, .get() с дефолтом 100.0 внутри.
    snapshots = [
        {"timestamp": 1700000000.0 + i * 3600, "index_value": 100.0 + i, "btc_value": 100.0}
        for i in range(5)
    ]
    path = treasury_chart.generate_treasury_chart(snapshots)
    assert path is not None
    assert path.exists()


def test_generate_treasury_chart_returns_none_on_broken_snapshot(monkeypatch):
    # Ломаем построение искусственно (нет даже timestamp) - должно
    # аккуратно вернуть None, а не поднять исключение наружу.
    broken_snapshots = [{"index_value": 100.0}] * 5
    result = treasury_chart.generate_treasury_chart(broken_snapshots)
    assert result is None


def test_append_coin_periods_appends_one_value_per_coin(monkeypatch):
    monkeypatch.setattr(queue_manager, "get_coin_pct_history", lambda: {})
    saved = {}
    monkeypatch.setattr(queue_manager, "_set", lambda key, value: saved.__setitem__(key, value))

    tier = TierResult(key="tier1", label="Тест", pct=1.0, coins=[
        _FakeCoin(ticker="SOL", pct=2.0),
        _FakeCoin(ticker="AVAX", pct=None),
    ])
    history = queue_manager.append_coin_periods([tier])

    assert history["SOL"] == [2.0]
    assert history["AVAX"] == [None]


def test_append_coin_periods_caps_length(monkeypatch):
    existing = {"SOL": [1.0] * queue_manager._COIN_HISTORY_MAX}
    monkeypatch.setattr(queue_manager, "get_coin_pct_history", lambda: existing)
    monkeypatch.setattr(queue_manager, "_set", lambda key, value: None)

    tier = TierResult(key="tier1", label="Тест", pct=1.0, coins=[_FakeCoin(ticker="SOL", pct=3.0)])
    history = queue_manager.append_coin_periods([tier])

    assert len(history["SOL"]) == queue_manager._COIN_HISTORY_MAX
    assert history["SOL"][-1] == 3.0


class _FakeCoin:
    def __init__(self, ticker, pct):
        self.ticker = ticker
        self.pct = pct


def test_treasury_post_count_starts_at_zero(monkeypatch):
    monkeypatch.setattr(queue_manager, "_get", lambda key, default: default)
    assert queue_manager.get_treasury_post_count() == 0


def test_increment_treasury_post_count_advances_by_one(monkeypatch):
    monkeypatch.setattr(queue_manager, "get_treasury_post_count", lambda: 13)
    saved = {}
    monkeypatch.setattr(queue_manager, "_set", lambda key, value: saved.__setitem__(key, value))

    count = queue_manager.increment_treasury_post_count()

    assert count == 14
    assert saved["treasury_post_count"] == 14


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
