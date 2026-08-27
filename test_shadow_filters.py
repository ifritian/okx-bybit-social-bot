#!/usr/bin/env python3
"""
Тесты shadow_filters.py (P3.9) - теневые проверки (btc_macro_trend,
time_of_day_weekend), evaluate_and_log, get_shadow_stats. Никакой
реальной сети - все внешние точки (multi_timeframe._fetch_closes,
queue_manager) замоканы.
"""
from datetime import datetime, timezone

import config
import shadow_filters


def _signal(ticker="SOL", direction="Лонг (перепроданность)", strategy="RSI + Bollinger Touch", score="80"):
    from signal_parser import RsiSignal
    return RsiSignal(
        ticker=ticker, timeframe="15m", strategy=strategy, direction=direction,
        current_price="100", rsi_now="25", score=score, quality="Moderate",
        entry_low="99", entry_high="101", invalidation="95", target="110",
        change_24h="+1%", volume="10M", rsi_live="25", created_at="2026-07-29 00:00:00 UTC",
        description="тест", raw_text="тест",
    )


# --- _btc_macro_trend_shadow_check ---

def test_btc_macro_trend_shadow_check_skips_btc_itself():
    would_block, reason = shadow_filters._btc_macro_trend_shadow_check(_signal(), "BTCUSDT")
    assert would_block is False
    assert "сам" in reason.lower() or "btc" in reason.lower()


def test_btc_macro_trend_shadow_check_blocks_long_against_downtrend(monkeypatch):
    # 4h и 1d оба в нисходящем тренде (classify_trend вернёт "down") -
    # лонг против обоих должен получить would_block=True.
    monkeypatch.setattr(shadow_filters.multi_timeframe, "_fetch_closes", lambda symbol, interval, **k: [100.0] * 60)
    monkeypatch.setattr(shadow_filters.multi_timeframe, "classify_trend", lambda closes, **k: "down")

    would_block, reason = shadow_filters._btc_macro_trend_shadow_check(
        _signal(direction="Лонг (перепроданность)"), "SOLUSDT",
    )
    assert would_block is True
    assert "4h" in reason and "1d" in reason


def test_btc_macro_trend_shadow_check_allows_long_with_uptrend(monkeypatch):
    monkeypatch.setattr(shadow_filters.multi_timeframe, "_fetch_closes", lambda symbol, interval, **k: [100.0] * 60)
    monkeypatch.setattr(shadow_filters.multi_timeframe, "classify_trend", lambda closes, **k: "up")

    would_block, reason = shadow_filters._btc_macro_trend_shadow_check(
        _signal(direction="Лонг (перепроданность)"), "SOLUSDT",
    )
    assert would_block is False


def test_btc_macro_trend_shadow_check_soft_fails_without_data(monkeypatch):
    # Нет свечей - classify_trend вернёт None - не блокируем из-за
    # собственной невозможности посчитать (та же логика, что и у
    # остальных "мягких" проверок в проекте).
    monkeypatch.setattr(shadow_filters.multi_timeframe, "_fetch_closes", lambda symbol, interval, **k: [])

    would_block, reason = shadow_filters._btc_macro_trend_shadow_check(_signal(), "SOLUSDT")
    assert would_block is False


# --- _time_of_day_shadow_check ---

def test_time_of_day_shadow_check_blocks_thin_hour(monkeypatch):
    monkeypatch.setattr(config, "THIN_HOURS_UTC", frozenset({2, 3}))
    monkeypatch.setattr(config, "THIN_LIQUIDITY_WEEKEND_ENABLED", False)

    class _FakeDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 7, 27, 3, 0, tzinfo=timezone.utc)  # понедельник 03:00 UTC

    monkeypatch.setattr(shadow_filters, "datetime", _FakeDatetime)
    would_block, reason = shadow_filters._time_of_day_shadow_check(_signal(), "SOLUSDT")
    assert would_block is True
    assert "тонкий час" in reason


def test_time_of_day_shadow_check_blocks_weekend(monkeypatch):
    monkeypatch.setattr(config, "THIN_HOURS_UTC", frozenset())
    monkeypatch.setattr(config, "THIN_LIQUIDITY_WEEKEND_ENABLED", True)

    class _FakeDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 8, 1, 12, 0, tzinfo=timezone.utc)  # суббота

    monkeypatch.setattr(shadow_filters, "datetime", _FakeDatetime)
    would_block, reason = shadow_filters._time_of_day_shadow_check(_signal(), "SOLUSDT")
    assert would_block is True
    assert "выходной" in reason


def test_time_of_day_shadow_check_allows_normal_weekday_hour(monkeypatch):
    monkeypatch.setattr(config, "THIN_HOURS_UTC", frozenset({2, 3}))
    monkeypatch.setattr(config, "THIN_LIQUIDITY_WEEKEND_ENABLED", True)

    class _FakeDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 7, 28, 14, 0, tzinfo=timezone.utc)  # вторник, 14:00 UTC

    monkeypatch.setattr(shadow_filters, "datetime", _FakeDatetime)
    would_block, reason = shadow_filters._time_of_day_shadow_check(_signal(), "SOLUSDT")
    assert would_block is False


# --- evaluate_and_log ---

def test_evaluate_and_log_records_one_verdict_per_registered_filter(monkeypatch):
    logged = []
    monkeypatch.setattr(shadow_filters.queue_manager, "add_shadow_verdict", lambda record: logged.append(record))
    monkeypatch.setattr(shadow_filters, "SHADOW_FILTERS", [
        ("fake_a", lambda signal, symbol: (True, "причина A")),
        ("fake_b", lambda signal, symbol: (False, "причина B")),
    ])

    shadow_filters.evaluate_and_log(_signal(ticker="SOL"), "SOLUSDT")

    assert len(logged) == 2
    assert logged[0]["filter_name"] == "fake_a"
    assert logged[0]["would_block"] is True
    assert logged[0]["ticker"] == "SOL"
    assert logged[0]["direction"] == "long"
    assert logged[1]["filter_name"] == "fake_b"
    assert logged[1]["would_block"] is False


def test_evaluate_and_log_continues_after_one_filter_raises(monkeypatch):
    # Одна упавшая теневая проверка не должна мешать ни остальным
    # проверкам, ни (тем более) публикации сигнала - см. docstring
    # evaluate_and_log.
    logged = []
    monkeypatch.setattr(shadow_filters.queue_manager, "add_shadow_verdict", lambda record: logged.append(record))

    def _boom(signal, symbol):
        raise RuntimeError("симулированный сбой теневой проверки")

    monkeypatch.setattr(shadow_filters, "SHADOW_FILTERS", [
        ("broken", _boom),
        ("fine", lambda signal, symbol: (False, "ок")),
    ])

    shadow_filters.evaluate_and_log(_signal(), "SOLUSDT")

    assert len(logged) == 1
    assert logged[0]["filter_name"] == "fine"


# --- get_shadow_stats ---

def _outcome(ticker, direction, strategy, published_at, result, pnl_pct):
    return {
        "ticker": ticker, "direction": direction, "strategy": strategy,
        "published_at": published_at, "result": result, "pnl_pct": pnl_pct,
    }


def test_get_shadow_stats_splits_blocked_and_allowed_by_real_outcome(monkeypatch):
    verdicts = [
        {"filter_name": "btc_macro_trend", "ticker": "SOL", "direction": "long",
         "strategy": "RSI", "would_block": True, "reason": "", "logged_at": 1000.0},
        {"filter_name": "btc_macro_trend", "ticker": "ETH", "direction": "long",
         "strategy": "RSI", "would_block": False, "reason": "", "logged_at": 2000.0},
    ]
    closed = [
        _outcome("SOL", "long", "RSI", published_at=1005.0, result="loss", pnl_pct=-2.0),
        _outcome("ETH", "long", "RSI", published_at=2010.0, result="win", pnl_pct=3.0),
    ]
    monkeypatch.setattr(shadow_filters.queue_manager, "get_shadow_verdicts", lambda: verdicts)
    monkeypatch.setattr(shadow_filters.queue_manager, "get_closed_outcomes", lambda: closed)

    stats = shadow_filters.get_shadow_stats("btc_macro_trend")

    assert stats["blocked"]["count"] == 1
    assert stats["blocked"]["win_rate"] == 0.0
    assert stats["allowed"]["count"] == 1
    assert stats["allowed"]["win_rate"] == 100.0
    assert stats["verdicts_matched"] == 2


def test_get_shadow_stats_ignores_verdicts_from_other_filters(monkeypatch):
    verdicts = [
        {"filter_name": "time_of_day_weekend", "ticker": "SOL", "direction": "long",
         "strategy": "RSI", "would_block": True, "reason": "", "logged_at": 1000.0},
    ]
    closed = [_outcome("SOL", "long", "RSI", published_at=1005.0, result="win", pnl_pct=3.0)]
    monkeypatch.setattr(shadow_filters.queue_manager, "get_shadow_verdicts", lambda: verdicts)
    monkeypatch.setattr(shadow_filters.queue_manager, "get_closed_outcomes", lambda: closed)

    stats = shadow_filters.get_shadow_stats("btc_macro_trend")  # другой фильтр
    assert stats["blocked"]["count"] == 0
    assert stats["allowed"]["count"] == 0
    assert stats["verdicts_total"] == 0


def test_get_shadow_stats_unmatched_verdict_not_counted(monkeypatch):
    # Вердикт есть, а закрытого исхода с тем же ticker/direction/strategy
    # в разумном временном окне нет (сигнал ещё не закрылся, или
    # вообще не был опубликован) - не должен попасть ни в blocked, ни
    # в allowed.
    verdicts = [
        {"filter_name": "btc_macro_trend", "ticker": "SOL", "direction": "long",
         "strategy": "RSI", "would_block": True, "reason": "", "logged_at": 1000.0},
    ]
    monkeypatch.setattr(shadow_filters.queue_manager, "get_shadow_verdicts", lambda: verdicts)
    monkeypatch.setattr(shadow_filters.queue_manager, "get_closed_outcomes", lambda: [])

    stats = shadow_filters.get_shadow_stats("btc_macro_trend")
    assert stats["blocked"]["count"] == 0
    assert stats["verdicts_total"] == 1
    assert stats["verdicts_matched"] == 0


def test_get_shadow_stats_respects_max_correlation_window(monkeypatch):
    # Совпадение по ticker/direction/strategy есть, но published_at
    # слишком далеко от logged_at (за пределами max_correlation_seconds) -
    # это, скорее всего, СОВСЕМ ДРУГАЯ сделка по тому же тикеру, а не
    # тот же сигнал - не должно засчитываться.
    verdicts = [
        {"filter_name": "btc_macro_trend", "ticker": "SOL", "direction": "long",
         "strategy": "RSI", "would_block": True, "reason": "", "logged_at": 1000.0},
    ]
    closed = [_outcome("SOL", "long", "RSI", published_at=1000.0 + 3600, result="win", pnl_pct=3.0)]
    monkeypatch.setattr(shadow_filters.queue_manager, "get_shadow_verdicts", lambda: verdicts)
    monkeypatch.setattr(shadow_filters.queue_manager, "get_closed_outcomes", lambda: closed)

    stats = shadow_filters.get_shadow_stats("btc_macro_trend", max_correlation_seconds=900)
    assert stats["blocked"]["count"] == 0
    assert stats["verdicts_matched"] == 0


def test_get_shadow_stats_filters_by_days(monkeypatch):
    verdicts = [
        {"filter_name": "btc_macro_trend", "ticker": "SOL", "direction": "long",
         "strategy": "RSI", "would_block": True, "reason": "", "logged_at": 0.0},  # очень старый
    ]
    monkeypatch.setattr(shadow_filters.queue_manager, "get_shadow_verdicts", lambda: verdicts)
    monkeypatch.setattr(shadow_filters.queue_manager, "get_closed_outcomes", lambda: [])
    monkeypatch.setattr(shadow_filters.time, "time", lambda: 100 * 24 * 3600)  # "сейчас" далеко впереди

    stats = shadow_filters.get_shadow_stats("btc_macro_trend", days=7)
    assert stats["verdicts_total"] == 0


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
            fn(mp)
            print(f"OK   {name}")
            passed += 1
        except AssertionError as e:
            print(f"FAIL {name}: {e}")
            failed += 1
        finally:
            mp.undo()

    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
