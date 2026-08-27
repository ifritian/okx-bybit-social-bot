#!/usr/bin/env python3
"""
Тесты volatility_alert.py: детекция скачка (по замоканным свечам BTC,
без сети), генерация текста (LLM замокан) и валидация чисел/дисклеймера/
длины.
"""
import config
import post_format
import volatility_alert


def _candles(closes: list) -> list:
    return [{"open_time": i, "open": c, "high": c, "low": c, "close": c, "volume": 1.0} for i, c in enumerate(closes)]


def test_detect_spike_returns_none_with_too_few_candles(monkeypatch):
    monkeypatch.setattr(config, "VOLATILITY_ALERT_WINDOW_HOURS", 3)
    monkeypatch.setattr(volatility_alert, "fetch_klines", lambda ticker, days=2: _candles([100.0, 101.0]))
    assert volatility_alert.detect_market_volatility_spike() is None


def test_detect_spike_returns_none_below_threshold(monkeypatch):
    monkeypatch.setattr(config, "VOLATILITY_ALERT_WINDOW_HOURS", 3)
    monkeypatch.setattr(config, "VOLATILITY_ALERT_THRESHOLD_PCT", 4.0)
    # Движение всего ~1% за окно - ниже порога.
    monkeypatch.setattr(
        volatility_alert, "fetch_klines",
        lambda ticker, days=2: _candles([100.0] * 10 + [100.0, 100.3, 100.6, 101.0]),
    )
    assert volatility_alert.detect_market_volatility_spike() is None


def test_detect_spike_detects_upward_move(monkeypatch):
    monkeypatch.setattr(config, "VOLATILITY_ALERT_WINDOW_HOURS", 3)
    monkeypatch.setattr(config, "VOLATILITY_ALERT_THRESHOLD_PCT", 4.0)
    closes = [100.0] * 10 + [100.0, 102.0, 104.0, 106.0]  # +6% за последние 3 свечи от 100 к 106
    monkeypatch.setattr(volatility_alert, "fetch_klines", lambda ticker, days=2: _candles(closes))

    spike = volatility_alert.detect_market_volatility_spike()

    assert spike is not None
    assert spike["direction"] == "up"
    assert spike["pct"] == 6.0
    assert spike["window_hours"] == 3


def test_detect_spike_detects_downward_move(monkeypatch):
    monkeypatch.setattr(config, "VOLATILITY_ALERT_WINDOW_HOURS", 3)
    monkeypatch.setattr(config, "VOLATILITY_ALERT_THRESHOLD_PCT", 4.0)
    closes = [100.0] * 10 + [100.0, 97.0, 94.0, 92.0]  # -8% за последние 3 свечи
    monkeypatch.setattr(volatility_alert, "fetch_klines", lambda ticker, days=2: _candles(closes))

    spike = volatility_alert.detect_market_volatility_spike()

    assert spike is not None
    assert spike["direction"] == "down"
    assert spike["pct"] == -8.0


def test_generate_emergency_post_includes_emoji_and_disclaimer(monkeypatch):
    monkeypatch.setattr(
        volatility_alert, "call_groq",
        lambda *a, **k: "Рынок трясёт не по-детски прямо сейчас."
    )
    spike = {"pct": 6.0, "direction": "up", "window_hours": 3}
    text = volatility_alert.generate_emergency_post(spike)

    assert text is not None
    assert text.startswith("🚨")
    assert post_format.DISCLAIMER in text


def test_generate_emergency_post_returns_none_on_empty_response(monkeypatch):
    monkeypatch.setattr(volatility_alert, "call_groq", lambda *a, **k: "   ")
    spike = {"pct": 6.0, "direction": "up", "window_hours": 3}
    assert volatility_alert.generate_emergency_post(spike) is None


def test_generate_emergency_post_truncates_overly_long_response(monkeypatch):
    monkeypatch.setattr(volatility_alert, "call_groq", lambda *a, **k: "В" * 500)
    spike = {"pct": 6.0, "direction": "up", "window_hours": 3}
    text = volatility_alert.generate_emergency_post(spike)

    assert text is not None
    assert len(text) <= post_format.BLUESKY_CHAR_LIMIT
    assert "…" in text


def test_validate_emergency_post_accepts_pct_and_window_hours():
    spike = {"pct": 6.0, "direction": "up", "window_hours": 3}
    text = f"🚨 $BTC вырос на +6.0% за 3 часа.\n\n{post_format.DISCLAIMER}"
    ok, reason = volatility_alert.validate_emergency_post(text, spike)
    assert ok is True, reason


def test_validate_emergency_post_rejects_unknown_numbers():
    spike = {"pct": 6.0, "direction": "up", "window_hours": 3}
    text = f"🚨 $BTC вырос аж на 999%!\n\n{post_format.DISCLAIMER}"
    ok, reason = volatility_alert.validate_emergency_post(text, spike)
    assert ok is False
    assert "999" in reason


def test_validate_emergency_post_rejects_missing_disclaimer():
    spike = {"pct": 6.0, "direction": "up", "window_hours": 3}
    text = "🚨 $BTC вырос на 6.0% - вот это движение."
    ok, reason = volatility_alert.validate_emergency_post(text, spike)
    assert ok is False
    assert "дисклеймер" in reason


def test_validate_emergency_post_rejects_over_length():
    spike = {"pct": 6.0, "direction": "up", "window_hours": 3}
    text = "🚨 " + "А" * 350 + f"\n\n{post_format.DISCLAIMER}"
    ok, reason = volatility_alert.validate_emergency_post(text, spike)
    assert ok is False
    assert "лимита" in reason


def test_validate_emergency_post_accepts_negative_pct_written_as_positive():
    # Для падения LLM может написать "-8.0%" или просто "8.0%" без знака -
    # оба варианта должны проходить (allowed_numbers включает abs(pct)).
    spike = {"pct": -8.0, "direction": "down", "window_hours": 3}
    text = f"🚨 $BTC упал на 8.0% за 3 часа.\n\n{post_format.DISCLAIMER}"
    ok, reason = volatility_alert.validate_emergency_post(text, spike)
    assert ok is True, reason


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
