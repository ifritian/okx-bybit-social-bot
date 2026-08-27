#!/usr/bin/env python3
"""Тесты index_volatility.py - чистая математика, без сети."""
import index_volatility as iv


def test_insufficient_history_returns_none():
    stats = iv.compute_volatility_and_drawdown([5.0])
    assert stats["volatility_pct"] is None
    assert stats["periods"] == 1


def test_empty_history_returns_none():
    stats = iv.compute_volatility_and_drawdown([])
    assert stats["volatility_pct"] is None


def test_flat_returns_zero_volatility_and_drawdown():
    stats = iv.compute_volatility_and_drawdown([1.0, 1.0, 1.0, 1.0])
    assert stats["volatility_pct"] == 0.0
    assert stats["max_drawdown_pct"] == 0.0
    assert stats["current_drawdown_pct"] == 0.0


def test_drawdown_detected_after_peak():
    # Растёт, потом падает на 10% от пика, потом немного отрастает
    stats = iv.compute_volatility_and_drawdown([10.0, 10.0, -10.0, 2.0])
    assert stats["max_drawdown_pct"] < 0
    assert stats["max_drawdown_pct"] <= -9  # примерно -10% (с учётом сложного процента)


def test_current_drawdown_zero_when_at_all_time_high():
    stats = iv.compute_volatility_and_drawdown([5.0, 3.0, 2.0])  # монотонный рост - пик прямо сейчас
    assert stats["current_drawdown_pct"] == 0.0


def test_reconstruct_cumulative_series_starts_at_100():
    series = iv._reconstruct_cumulative_series([10.0, -10.0])
    assert series[0] == 100.0
    assert abs(series[1] - 110.0) < 1e-9
    assert abs(series[2] - 99.0) < 1e-9  # 110 * 0.9


def test_format_volatility_block_empty_when_insufficient_data():
    stats = {"volatility_pct": None, "max_drawdown_pct": None, "current_drawdown_pct": None, "periods": 1}
    assert iv.format_volatility_block(stats, 12) == ""


def test_format_volatility_block_contains_numbers():
    stats = iv.compute_volatility_and_drawdown([2.0, -1.0, 3.0])
    block = iv.format_volatility_block(stats, 12)
    assert "Волатильность" in block
    assert "Макс. просадка" in block
    assert str(stats["volatility_pct"]) in block


if __name__ == "__main__":
    import sys
    import types

    passed, failed = 0, 0
    module = sys.modules[__name__]
    for name in dir(module):
        if not name.startswith("test_"):
            continue
        fn = getattr(module, name)
        if not isinstance(fn, types.FunctionType):
            continue
        try:
            fn()
            print(f"OK   {name}")
            passed += 1
        except AssertionError as e:
            print(f"FAIL {name}: {e}")
            failed += 1

    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
