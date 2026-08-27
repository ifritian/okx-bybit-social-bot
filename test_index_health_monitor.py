#!/usr/bin/env python3
"""
Тесты index_health_monitor.py - чистая логика, без сети (queue_manager
и alerting подменяются monkeypatch).
"""
import index_health_monitor as ihm


def test_record_check_results_increments_streak_for_missing():
    saved = {}
    import queue_manager
    orig_get = queue_manager.get_coin_miss_streaks
    orig_set = queue_manager.set_coin_miss_streaks
    queue_manager.get_coin_miss_streaks = lambda: {"SOL": 2}
    queue_manager.set_coin_miss_streaks = lambda s: saved.setdefault("v", s)
    try:
        streaks = ihm.record_check_results(["SOL"])
        assert streaks["SOL"] == 3
    finally:
        queue_manager.get_coin_miss_streaks = orig_get
        queue_manager.set_coin_miss_streaks = orig_set


def test_record_check_results_resets_streak_when_present():
    import queue_manager
    orig_get = queue_manager.get_coin_miss_streaks
    orig_set = queue_manager.set_coin_miss_streaks
    queue_manager.get_coin_miss_streaks = lambda: {"SOL": 5}
    queue_manager.set_coin_miss_streaks = lambda s: None
    try:
        streaks = ihm.record_check_results([])  # SOL больше не пропущен - резолвился нормально
        assert streaks["SOL"] == 0
    finally:
        queue_manager.get_coin_miss_streaks = orig_get
        queue_manager.set_coin_miss_streaks = orig_set


def test_record_check_results_covers_all_basket_tickers():
    import queue_manager
    orig_get = queue_manager.get_coin_miss_streaks
    orig_set = queue_manager.set_coin_miss_streaks
    queue_manager.get_coin_miss_streaks = lambda: {}
    queue_manager.set_coin_miss_streaks = lambda s: None
    try:
        streaks = ihm.record_check_results([])
        assert "SOL" in streaks and "AAVE" in streaks and "SUI" in streaks
        assert all(v == 0 for v in streaks.values())
    finally:
        queue_manager.get_coin_miss_streaks = orig_get
        queue_manager.set_coin_miss_streaks = orig_set


def test_check_and_alert_triggers_only_above_threshold(monkeypatch):
    alerts_sent = []
    monkeypatch.setattr(ihm.alerting, "send_owner_alert", lambda key, msg, min_repeat_hours=6: alerts_sent.append(key))

    streaks = {"SOL": 1, "AAVE": 2, "SUI": 3, "OP": 5}
    unhealthy = ihm.check_and_alert(streaks)

    assert set(unhealthy) == {"SUI", "OP"}
    assert len(alerts_sent) == 2


def test_check_and_alert_no_alerts_when_all_healthy(monkeypatch):
    alerts_sent = []
    monkeypatch.setattr(ihm.alerting, "send_owner_alert", lambda key, msg, min_repeat_hours=6: alerts_sent.append(key))

    unhealthy = ihm.check_and_alert({"SOL": 0, "AAVE": 1})
    assert unhealthy == []
    assert alerts_sent == []


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
