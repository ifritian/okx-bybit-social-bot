#!/usr/bin/env python3
"""
Тесты strategies.py (MACD Crossover, Donchian Breakout) - чистая логика
на синтетических свечах, без сети.
"""
import math

import strategies as strat
from scanner import _Candle


def _sine_candles(n=80, amplitude=10, base=100, period=6, volume=100_000):
    candles = []
    for i in range(n):
        price = base + amplitude * math.sin(i / period)
        candles.append(_Candle(open=price, high=price + 0.1, low=price - 0.1, close=price, volume=volume))
    return candles


def _flat_candles(n=60, price=100.0, volume=10_000):
    return [_Candle(open=price, high=price + 0.05, low=price - 0.05, close=price, volume=volume) for _ in range(n)]


def _sideways_then_breakout(n_range=25, level=50.0, band=0.3, breakout_price=53.0,
                             breakout_volume=50_000, normal_volume=10_000):
    candles = []
    for _ in range(n_range):
        candles.append(_Candle(open=level, high=level + band, low=level - band, close=level, volume=normal_volume))
    candles.append(_Candle(open=level + 0.2, high=breakout_price + 0.2, low=level, close=breakout_price,
                            volume=breakout_volume))
    return candles


# --- calc_atr (A2) ---

def test_calc_atr_none_with_insufficient_candles():
    assert strat.calc_atr(_flat_candles(n=5), period=14) is None  # 5 < 14+1


def test_calc_atr_none_when_exactly_period_candles():
    # period+1 свечей - МИНИМУМ необходимый (period штук True Range) -
    # значит РОВНО period свечей (на одну меньше) должно давать None.
    assert strat.calc_atr(_flat_candles(n=3), period=3) is None


def test_calc_atr_matches_hand_computed_wilder_value():
    # Те же 5 свечей, что и в ручном расчёте по формуле Уайлдера -
    # TR: 2, 2.5, 2, 4 -> seed=avg(2,2.5,2)=2.1666... -> Уайлдер с TR4=4:
    # (2.1666...*2 + 4) / 3 = 2.7777...
    candles = [
        _Candle(open=9, high=10, low=8, close=9, volume=1),
        _Candle(open=10, high=11, low=9, close=10.5, volume=1),
        _Candle(open=9, high=10, low=8, close=8.5, volume=1),
        _Candle(open=8.5, high=9, low=7, close=8, volume=1),
        _Candle(open=8, high=12, low=8, close=11, volume=1),
    ]
    atr = strat.calc_atr(candles, period=3)
    assert atr is not None
    assert math.isclose(atr, 2.7777777777777772, rel_tol=1e-9)


def test_calc_atr_positive_for_volatile_candles():
    atr = strat.calc_atr(_sine_candles(n=80), period=14)
    assert atr is not None
    assert atr > 0


def test_calc_atr_small_for_flat_candles():
    # Плоский рынок (диапазон каждой свечи 0.1, см. _flat_candles) -
    # ATR должен сойтись примерно к этому же диапазону, не раздуваться.
    atr = strat.calc_atr(_flat_candles(n=60), period=14)
    assert atr is not None
    assert atr == pytest_approx_or_close(0.1, 0.01)


# --- calc_atr_series (P2.5) ---

def test_calc_atr_series_empty_with_insufficient_candles():
    assert strat.calc_atr_series(_flat_candles(n=5), period=14) == []


def test_calc_atr_series_last_value_matches_calc_atr():
    # calc_atr - это ровно calc_atr_series(...)[-1] (см. docstring) -
    # оба должны давать одно и то же число на одних и тех же свечах.
    candles = _sine_candles(n=80)
    series = strat.calc_atr_series(candles, period=14)
    assert series != []
    assert math.isclose(series[-1], strat.calc_atr(candles, period=14), rel_tol=1e-12)


def test_calc_atr_series_length_matches_candles_minus_period():
    # period+1 свечей дают РОВНО 1 значение (первое, "seed") - дальше
    # плюс один элемент ряда на каждую дополнительную свечу.
    candles = _flat_candles(n=20)
    series = strat.calc_atr_series(candles, period=14)
    assert len(series) == len(candles) - 14


def pytest_approx_or_close(expected, tol):
    """Мини-хелпер вместо pytest.approx - раннер этого файла не
    гарантированно имеет pytest (см. __main__ ниже, это самодостаточный
    мини-раннер, как и в остальных test_*.py проекта)."""
    class _Approx:
        def __eq__(self, other):
            return abs(other - expected) <= tol
    return _Approx()


# --- A2: ATR-стопы в MACD Crossover/Donchian Breakout (интеграция) ---

def test_macd_uses_fixed_pct_stop_by_default(monkeypatch):
    monkeypatch.setattr(strat.config, "USE_ATR_STOPS", False)
    candles = _macd_bullish_cross_candles()
    signal = strat.build_macd_signal("TESTUSDT", candles, 8_000_000)
    assert signal is not None
    recent_low = min(c.low for c in candles[-20:])
    assert math.isclose(float(signal.invalidation), recent_low * 0.997, rel_tol=1e-6)


def test_macd_uses_atr_stop_when_enabled(monkeypatch):
    monkeypatch.setattr(strat.config, "USE_ATR_STOPS", True)
    monkeypatch.setattr(strat.config, "ATR_PERIOD", 14)
    monkeypatch.setattr(strat.config, "ATR_STOP_MULTIPLIER", 1.5)
    candles = _macd_bullish_cross_candles()
    signal = strat.build_macd_signal("TESTUSDT", candles, 8_000_000)
    assert signal is not None
    recent_low = min(c.low for c in candles[-20:])
    atr = strat.calc_atr(candles, 14)
    expected = recent_low - atr * 1.5
    assert math.isclose(float(signal.invalidation), expected, rel_tol=1e-6)
    # НЕ должно совпадать со старой формулой - иначе флаг никак не подействовал
    assert not math.isclose(float(signal.invalidation), recent_low * 0.997, rel_tol=1e-6)


def test_breakout_uses_atr_stop_when_enabled(monkeypatch):
    monkeypatch.setattr(strat.config, "USE_ATR_STOPS", True)
    monkeypatch.setattr(strat.config, "ATR_PERIOD", 14)
    monkeypatch.setattr(strat.config, "ATR_STOP_MULTIPLIER", 1.5)
    candles = _sideways_then_breakout()
    signal = strat.build_breakout_signal("TESTUSDT", candles, 8_000_000)
    assert signal is not None
    channel_high = max(c.high for c in candles[-(strat.BREAKOUT_LOOKBACK + 1):-1])
    atr = strat.calc_atr(candles, 14)
    expected = channel_high - atr * 1.5
    assert math.isclose(float(signal.invalidation), expected, rel_tol=1e-6)


def test_atr_stop_falls_back_to_fixed_pct_when_atr_unavailable(monkeypatch):
    # USE_ATR_STOPS включён, но свечей МЕНЬШЕ ATR_PERIOD+1 - calc_atr
    # вернёт None, формула должна тихо откатиться на фиксированный %,
    # а не упасть/пропустить сигнал.
    monkeypatch.setattr(strat.config, "USE_ATR_STOPS", True)
    monkeypatch.setattr(strat.config, "ATR_PERIOD", 999)  # заведомо больше, чем свечей в candles
    candles = _macd_bullish_cross_candles()
    signal = strat.build_macd_signal("TESTUSDT", candles, 8_000_000)
    assert signal is not None
    recent_low = min(c.low for c in candles[-20:])
    assert math.isclose(float(signal.invalidation), recent_low * 0.997, rel_tol=1e-6)


# --- P3.8: ATR-цели вместо фиксированного измеренного движения/экстремума ---

def test_atr_target_none_when_atr_none():
    assert strat._atr_target(100.0, True, None) is None


def test_atr_target_long_adds_distance_from_entry():
    target = strat._atr_target(100.0, True, atr=2.0)
    # config.ATR_TARGET_MULTIPLIER по умолчанию 3.0 -> 100 + 2*3 = 106
    assert math.isclose(target, 106.0, rel_tol=1e-9)


def test_atr_target_short_subtracts_distance_from_entry():
    target = strat._atr_target(100.0, False, atr=2.0)
    assert math.isclose(target, 94.0, rel_tol=1e-9)


def test_macd_uses_structural_target_by_default(monkeypatch):
    monkeypatch.setattr(strat.config, "USE_ATR_TARGETS", False)
    candles = _macd_bullish_cross_candles()
    signal = strat.build_macd_signal("TESTUSDT", candles, 8_000_000)
    assert signal is not None
    recent_high = max(c.high for c in candles[-20:])
    assert math.isclose(float(signal.target), recent_high, rel_tol=1e-5)


def test_macd_uses_atr_target_when_enabled(monkeypatch):
    monkeypatch.setattr(strat.config, "USE_ATR_TARGETS", True)
    monkeypatch.setattr(strat.config, "ATR_PERIOD", 14)
    monkeypatch.setattr(strat.config, "ATR_TARGET_MULTIPLIER", 3.0)
    candles = _macd_bullish_cross_candles()
    signal = strat.build_macd_signal("TESTUSDT", candles, 8_000_000)
    assert signal is not None
    atr = strat.calc_atr(candles, 14)
    current_price = candles[-1].close
    expected = current_price + atr * 3.0
    assert math.isclose(float(signal.target), expected, rel_tol=1e-6)
    # НЕ должно совпадать со старой формулой - иначе флаг никак не подействовал
    recent_high = max(c.high for c in candles[-20:])
    assert not math.isclose(float(signal.target), recent_high, rel_tol=1e-6)


def test_breakout_uses_structural_target_by_default(monkeypatch):
    monkeypatch.setattr(strat.config, "USE_ATR_TARGETS", False)
    candles = _sideways_then_breakout()
    signal = strat.build_breakout_signal("TESTUSDT", candles, 8_000_000)
    assert signal is not None
    channel_high = max(c.high for c in candles[-(strat.BREAKOUT_LOOKBACK + 1):-1])
    channel_low = min(c.low for c in candles[-(strat.BREAKOUT_LOOKBACK + 1):-1])
    range_height = channel_high - channel_low
    expected = channel_high + range_height  # измеренное движение от уровня пробоя
    assert math.isclose(float(signal.target), expected, rel_tol=1e-6)


def test_breakout_uses_atr_target_when_enabled(monkeypatch):
    monkeypatch.setattr(strat.config, "USE_ATR_TARGETS", True)
    monkeypatch.setattr(strat.config, "ATR_PERIOD", 14)
    monkeypatch.setattr(strat.config, "ATR_TARGET_MULTIPLIER", 3.0)
    candles = _sideways_then_breakout()
    signal = strat.build_breakout_signal("TESTUSDT", candles, 8_000_000)
    assert signal is not None
    atr = strat.calc_atr(candles, 14)
    current_price = candles[-1].close
    expected = current_price + atr * 3.0
    assert math.isclose(float(signal.target), expected, rel_tol=1e-6)


def test_atr_target_falls_back_to_structural_when_atr_unavailable(monkeypatch):
    # USE_ATR_TARGETS включён, но свечей МЕНЬШЕ ATR_PERIOD+1 - calc_atr
    # вернёт None, target должен тихо откатиться на структурную формулу,
    # а не упасть/пропустить сигнал - тот же принцип, что и у ATR-стопа.
    monkeypatch.setattr(strat.config, "USE_ATR_TARGETS", True)
    monkeypatch.setattr(strat.config, "ATR_PERIOD", 999)
    candles = _macd_bullish_cross_candles()
    signal = strat.build_macd_signal("TESTUSDT", candles, 8_000_000)
    assert signal is not None
    recent_high = max(c.high for c in candles[-20:])
    assert math.isclose(float(signal.target), recent_high, rel_tol=1e-5)


def _macd_bullish_cross_candles():
    """Обрезка _sine_candles() ровно до момента бычьего пересечения MACD -
    та же техника поиска пересечений, что и в
    test_macd_detects_bullish_and_bearish_crossovers ниже, но
    возвращает ПЕРВЫЙ найденный бычий случай как готовые свечи, а не
    сам факт пересечения - нужно ATR-тестам как детерминированная
    "рабочая" заготовка сигнала."""
    candles = _sine_candles()
    closes = [c.close for c in candles]
    ema_fast = strat._ema_series(closes, strat.MACD_FAST)
    ema_slow = strat._ema_series(closes, strat.MACD_SLOW)
    offset = len(ema_fast) - len(ema_slow)
    macd_line = [f - s for f, s in zip(ema_fast[offset:], ema_slow)]
    signal_line = strat._ema_series(macd_line, strat.MACD_SIGNAL)
    macd_aligned = macd_line[-len(signal_line):]
    diffs = [m - s for m, s in zip(macd_aligned, signal_line)]
    crossovers = [i for i in range(1, len(diffs)) if diffs[i - 1] * diffs[i] < 0]

    for idx in crossovers:
        trimmed = candles[: len(candles) - (len(diffs) - 1 - idx)]
        signal = strat.build_macd_signal("TESTUSDT", trimmed, 8_000_000)
        if signal is not None and "Лонг" in signal.direction:
            return trimmed
    raise AssertionError("не нашлось бычьего пересечения MACD в _sine_candles() - тестовые данные сломаны")




def test_macd_no_signal_on_flat_market():
    assert strat.build_macd_signal("TESTUSDT", _flat_candles(), 8_000_000) is None


def test_macd_detects_bullish_and_bearish_crossovers():
    candles = _sine_candles()
    closes = [c.close for c in candles]
    ema_fast = strat._ema_series(closes, strat.MACD_FAST)
    ema_slow = strat._ema_series(closes, strat.MACD_SLOW)
    offset = len(ema_fast) - len(ema_slow)
    macd_line = [f - s for f, s in zip(ema_fast[offset:], ema_slow)]
    signal_line = strat._ema_series(macd_line, strat.MACD_SIGNAL)
    macd_aligned = macd_line[-len(signal_line):]
    diffs = [m - s for m, s in zip(macd_aligned, signal_line)]
    crossovers = [i for i in range(1, len(diffs)) if diffs[i - 1] * diffs[i] < 0]
    assert len(crossovers) >= 2  # синусоида должна дать хотя бы пару пересечений

    seen_directions = set()
    for idx in crossovers:
        trimmed = candles[: len(candles) - (len(diffs) - 1 - idx)]
        signal = strat.build_macd_signal("TESTUSDT", trimmed, 8_000_000)
        assert signal is not None
        assert signal.strategy == "MACD Crossover"
        assert signal.ticker == "TEST"
        assert 0 <= int(signal.score) <= 100
        seen_directions.add(signal.direction)

    assert any("Лонг" in d for d in seen_directions)
    assert any("Шорт" in d for d in seen_directions)


def test_macd_returns_none_with_insufficient_data():
    assert strat.build_macd_signal("TESTUSDT", _flat_candles(n=10), 8_000_000) is None


# --- Donchian Breakout ---

def test_breakout_detects_upward_breakout_with_volume():
    candles = _sideways_then_breakout()
    signal = strat.build_breakout_signal("TESTUSDT", candles, 8_000_000)
    assert signal is not None
    assert signal.strategy == "Donchian Breakout"
    assert "Лонг" in signal.direction
    # Цель - выше уровня пробоя (продолжение движения), инвалидация - чуть
    # НИЖЕ уровня пробоя (если цена вернётся обратно под него - сетап неверен).
    channel_high = 50.0 + 0.3
    assert float(signal.target) > channel_high
    assert float(signal.invalidation) < channel_high


def test_breakout_detects_downward_breakout_with_volume():
    candles = []
    for _ in range(25):
        candles.append(_Candle(open=50.0, high=50.3, low=49.7, close=50.0, volume=10_000))
    candles.append(_Candle(open=49.8, high=50.0, low=46.8, close=47.0, volume=50_000))

    signal = strat.build_breakout_signal("TESTUSDT", candles, 8_000_000)
    assert signal is not None
    assert "Шорт" in signal.direction
    channel_low = 50.0 - 0.3
    assert float(signal.target) < channel_low
    assert float(signal.invalidation) > channel_low


def test_breakout_rejected_without_volume_confirmation():
    # Пробойная свеча по цене есть, но объём обычный (не превышает
    # BREAKOUT_VOLUME_RATIO_MIN) - сигнал не должен сработать.
    candles = _sideways_then_breakout(breakout_volume=10_500, normal_volume=10_000)
    assert strat.build_breakout_signal("TESTUSDT", candles, 8_000_000) is None


def test_breakout_none_when_price_stays_inside_channel():
    assert strat.build_breakout_signal("TESTUSDT", _flat_candles(), 8_000_000) is None


def test_breakout_none_with_insufficient_lookback():
    candles = _sideways_then_breakout(n_range=5)
    assert strat.build_breakout_signal("TESTUSDT", candles, 8_000_000) is None


def test_quality_from_score_thresholds():
    assert strat._quality_from_score(95) == "Conservative"
    assert strat._quality_from_score(75) == "Moderate"
    assert strat._quality_from_score(50) == "Aggressive"


def test_additional_strategies_registry_contains_both():
    assert strat.build_macd_signal in strat.ADDITIONAL_STRATEGIES
    assert strat.build_breakout_signal in strat.ADDITIONAL_STRATEGIES


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