#!/usr/bin/env python3
"""
Тесты telegram_extended.py: генерация блока "Контекст" (LLM замокан) и
валидация чисел. Историческая точность стратегии - через замоканный
outcome_tracker.get_accuracy_stats, без реальной БД.
"""
import outcome_tracker
import telegram_extended
from signal_parser import RsiSignal


def _make_signal(**overrides) -> RsiSignal:
    base = dict(
        ticker="BEAT", timeframe="15m", strategy="RSI + Bollinger Touch",
        direction="Шорт", current_price="2.225", rsi_now="81.74", score="89",
        quality="Conservative", entry_low="2.205", entry_high="2.2178",
        invalidation="2.2371", target="2.1729", change_24h="+35.67%",
        volume="57.67M", rsi_live="82.64", created_at="2026-06-23 22:44:59 EEST",
        description="desc", raw_text="raw",
    )
    base.update(overrides)
    return RsiSignal(**base)


def _empty_stats():
    return {"overall": {"count": 0, "win_rate": None, "avg_pnl_pct": None}, "by_strategy": {}, "by_quality": {}}


def _stats_with_strategy(strategy: str, count: int, win_rate: float):
    return {
        "overall": {"count": count, "win_rate": win_rate, "avg_pnl_pct": 0.5},
        "by_strategy": {strategy: {"count": count, "win_rate": win_rate, "avg_pnl_pct": 0.5}},
        "by_quality": {},
    }


def test_generate_extended_context_without_strategy_history(monkeypatch):
    monkeypatch.setattr(outcome_tracker, "get_accuracy_stats", lambda days=None: _empty_stats())
    monkeypatch.setattr(
        telegram_extended, "call_groq",
        lambda *a, **k: "RSI на 81.74 говорит о сильной перекупленности - редкий случай для этой стратегии."
    )

    signal = _make_signal()
    result = telegram_extended.generate_extended_context(signal, "Хук без цифр.")

    assert result is not None
    text, allowed_numbers = result
    assert 81.74 in allowed_numbers
    assert 89.0 in allowed_numbers
    assert 100.0 in allowed_numbers
    assert "RSI на 81.74" in text


def test_generate_extended_context_includes_strategy_stats_when_enough_samples(monkeypatch):
    monkeypatch.setattr(
        outcome_tracker, "get_accuracy_stats",
        lambda days=None: _stats_with_strategy("RSI + Bollinger Touch", 12, 66.7),
    )
    captured_prompts = []
    monkeypatch.setattr(
        telegram_extended, "call_groq",
        lambda system, user, **k: captured_prompts.append(user) or "Эта стратегия исторически показывала 66.7% побед."
    )

    signal = _make_signal()
    result = telegram_extended.generate_extended_context(signal, "Хук без цифр.")

    assert result is not None
    _, allowed_numbers = result
    assert 66.7 in allowed_numbers
    assert 12.0 in allowed_numbers
    assert "66.7" in captured_prompts[0]


def test_generate_extended_context_skips_strategy_stats_below_min_samples(monkeypatch):
    monkeypatch.setattr(
        outcome_tracker, "get_accuracy_stats",
        lambda days=None: _stats_with_strategy("RSI + Bollinger Touch", 2, 100.0),  # ниже MIN_STRATEGY_SAMPLES
    )
    captured_prompts = []
    monkeypatch.setattr(
        telegram_extended, "call_groq",
        lambda system, user, **k: captured_prompts.append(user) or "Разберём, что тут происходит с RSI."
    )

    signal = _make_signal()
    result = telegram_extended.generate_extended_context(signal, "Хук без цифр.")

    assert result is not None
    assert "Историческая точность" not in captured_prompts[0]


def test_generate_extended_context_returns_none_on_empty_response(monkeypatch):
    monkeypatch.setattr(outcome_tracker, "get_accuracy_stats", lambda days=None: _empty_stats())
    monkeypatch.setattr(telegram_extended, "call_groq", lambda *a, **k: "   ")

    signal = _make_signal()
    result = telegram_extended.generate_extended_context(signal, "Хук без цифр.")

    assert result is None


def test_validate_extended_context_accepts_allowed_numbers():
    text = "RSI на 81.74 сейчас в зоне перекупленности, score сетапа 89 из 100."
    ok, reason = telegram_extended.validate_extended_context(text, {81.74, 89.0, 100.0})
    assert ok is True, reason


def test_validate_extended_context_rejects_unknown_numbers():
    text = "Обычно такие движения дают потом ещё 200% - на моей памяти."
    ok, reason = telegram_extended.validate_extended_context(text, {81.74, 89.0})
    assert ok is False
    assert "200" in reason


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
