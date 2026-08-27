#!/usr/bin/env python3
"""
Тесты strategy_tuner.py - чистая логика, без сети (outcome_tracker и
queue_manager подменяются monkeypatch).
"""
import strategy_tuner as st


def _fake_stats(by_strategy):
    return {"overall": {}, "by_strategy": by_strategy, "by_quality": {}}


def test_ignores_strategy_with_too_few_samples(monkeypatch):
    stats = _fake_stats({"RSI": {"count": 5, "win_rate": 0.0, "avg_pnl_pct": -3.0}})
    monkeypatch.setattr(st.outcome_tracker, "get_accuracy_stats", lambda days=30: stats)
    saved = {}
    monkeypatch.setattr(st.queue_manager, "get_strategy_adjustments", lambda: {})
    monkeypatch.setattr(st.queue_manager, "set_strategy_adjustments", lambda a: saved.setdefault("v", a))

    result = st.recompute_adjustments()
    assert result == {}
    assert saved["v"] == {}


def test_penalizes_weak_strategy_with_enough_samples(monkeypatch):
    stats = _fake_stats({"RSI + Bollinger Touch": {"count": 20, "win_rate": 20.0, "avg_pnl_pct": -2.0}})
    monkeypatch.setattr(st.outcome_tracker, "get_accuracy_stats", lambda days=30: stats)
    monkeypatch.setattr(st.queue_manager, "get_strategy_adjustments", lambda: {})
    saved = {}
    monkeypatch.setattr(st.queue_manager, "set_strategy_adjustments", lambda a: saved.setdefault("v", a))

    result = st.recompute_adjustments()
    assert "RSI + Bollinger Touch" in result
    assert result["RSI + Bollinger Touch"] > 0
    assert saved["v"] == result


def test_penalty_capped_at_max():
    stats = _fake_stats({"RSI": {"count": 50, "win_rate": 0.0, "avg_pnl_pct": -5.0}})
    import strategy_tuner
    strategy_tuner.outcome_tracker.get_accuracy_stats = lambda days=30: stats
    strategy_tuner.queue_manager.get_strategy_adjustments = lambda: {}
    strategy_tuner.queue_manager.set_strategy_adjustments = lambda a: None

    result = strategy_tuner.recompute_adjustments()
    assert result["RSI"] <= strategy_tuner.MAX_PENALTY


def test_does_not_penalize_healthy_strategy(monkeypatch):
    stats = _fake_stats({"RSI": {"count": 30, "win_rate": 65.0, "avg_pnl_pct": 1.5}})
    monkeypatch.setattr(st.outcome_tracker, "get_accuracy_stats", lambda days=30: stats)
    monkeypatch.setattr(st.queue_manager, "get_strategy_adjustments", lambda: {})
    monkeypatch.setattr(st.queue_manager, "set_strategy_adjustments", lambda a: None)

    result = st.recompute_adjustments()
    assert result == {}


def test_get_effective_min_score_applies_penalty(monkeypatch):
    monkeypatch.setattr(st.queue_manager, "get_strategy_adjustments", lambda: {"RSI": 8})
    assert st.get_effective_min_score("RSI", 70) == 78
    assert st.get_effective_min_score("RSI + Bollinger Touch", 70) == 70  # без штрафа


def test_describe_active_adjustments_empty(monkeypatch):
    monkeypatch.setattr(st.queue_manager, "get_strategy_adjustments", lambda: {})
    assert "нет активных" in st.describe_active_adjustments()


def test_describe_active_adjustments_lists_strategies(monkeypatch):
    monkeypatch.setattr(st.queue_manager, "get_strategy_adjustments", lambda: {"RSI": 5})
    desc = st.describe_active_adjustments()
    assert "RSI" in desc and "+5" in desc


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
