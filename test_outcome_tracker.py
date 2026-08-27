#!/usr/bin/env python3
"""
Тесты чистой логики outcome_tracker: _resolve_outcome и
get_accuracy_stats (последний - через monkeypatch queue_manager, без
реальной SQLite и без сети).
"""
import outcome_tracker


def _c(high, low, close=None):
    return {"high": high, "low": low, "close": close if close is not None else (high + low) / 2}


def test_resolve_long_hits_target():
    record = {"direction": "long", "target": 110, "stop": 90, "entry": 100}
    candles = [_c(105, 100), _c(112, 108)]
    result = outcome_tracker._resolve_outcome(record, candles)
    assert result == ("win", 110, 12.0), result


def test_resolve_long_hits_stop():
    record = {"direction": "long", "target": 110, "stop": 90, "entry": 100}
    candles = [_c(105, 100), _c(95, 88)]
    result = outcome_tracker._resolve_outcome(record, candles)
    assert result == ("loss", 90, 5.0), result


def test_resolve_short_hits_target():
    record = {"direction": "short", "target": 90, "stop": 110, "entry": 100}
    candles = [_c(102, 98), _c(95, 88)]
    result = outcome_tracker._resolve_outcome(record, candles)
    assert result == ("win", 90, 12.0), result


def test_resolve_both_hit_same_candle_is_conservative_loss():
    record = {"direction": "long", "target": 110, "stop": 90, "entry": 100}
    candles = [_c(115, 85)]  # свеча пробила и тейк, и стоп
    result = outcome_tracker._resolve_outcome(record, candles)
    assert result == ("loss", 90, 15.0), result


def test_resolve_none_when_nothing_hit():
    record = {"direction": "long", "target": 110, "stop": 90, "entry": 100}
    candles = [_c(105, 95), _c(103, 97)]
    result = outcome_tracker._resolve_outcome(record, candles)
    assert result is None, result


def test_mfe_pct_long_and_short():
    assert outcome_tracker._mfe_pct(entry=100, best_price=105, is_short=False) == 5.0
    assert outcome_tracker._mfe_pct(entry=100, best_price=95, is_short=True) == 5.0
    # Цена сразу пошла против сделки - MFE отрицательный (не продвинулась к цели вообще)
    assert outcome_tracker._mfe_pct(entry=100, best_price=98, is_short=False) == -2.0


def test_accuracy_stats_aggregation(monkeypatch):
    fake_closed = [
        {"result": "win", "pnl_pct": 2.0, "strategy": "RSI", "quality": "Moderate", "closed_at": 0},
        {"result": "loss", "pnl_pct": -1.5, "strategy": "RSI", "quality": "Moderate", "closed_at": 0},
        {"result": "win", "pnl_pct": 3.0, "strategy": "RSI + Bollinger Touch", "quality": "Conservative", "closed_at": 0},
        {"result": "timeout", "pnl_pct": 0.2, "strategy": "RSI", "quality": "Moderate", "closed_at": 0},
    ]
    monkeypatch.setattr(outcome_tracker.queue_manager, "get_closed_outcomes", lambda: fake_closed)

    stats = outcome_tracker.get_accuracy_stats(days=None)

    assert stats["overall"]["count"] == 4
    # win_rate считается только по win/loss (3 записи, из них 2 win -> RSI bucket 1 win/1 loss = 50%,
    # но overall: 2 win из 3 decided (win/loss) = 66.7%
    assert stats["overall"]["win_rate"] == 66.7
    assert stats["by_strategy"]["RSI"]["count"] == 3
    assert stats["by_strategy"]["RSI"]["win_rate"] == 50.0
    assert stats["by_quality"]["Conservative"]["win_rate"] == 100.0


def test_accuracy_stats_empty(monkeypatch):
    monkeypatch.setattr(outcome_tracker.queue_manager, "get_closed_outcomes", lambda: [])
    stats = outcome_tracker.get_accuracy_stats()
    assert stats["overall"] == {"count": 0, "win_rate": None, "avg_pnl_pct": None}


def test_futures_trade_stats_aggregation(monkeypatch):
    # entry_price*quantity = 1000 в обеих сделках по BTC, для простоты
    # чисел; realized_pnl - реальный PnL с биржи (не оценка по цене).
    fake_closed = [
        {"symbol": "BTCUSDT", "strategy": "RSI + Bollinger Touch", "entry_price": 100,
         "quantity": 10, "realized_pnl": 20.0, "closed_at": 0},
        {"symbol": "BTCUSDT", "strategy": "RSI + Bollinger Touch", "entry_price": 100,
         "quantity": 10, "realized_pnl": -10.0, "closed_at": 0},
        {"symbol": "ETHUSDT", "strategy": "MACD Crossover", "entry_price": 50,
         "quantity": 20, "realized_pnl": 30.0, "closed_at": 0},
    ]
    monkeypatch.setattr(outcome_tracker.queue_manager, "get_closed_futures_positions", lambda: fake_closed)

    stats = outcome_tracker.get_futures_trade_stats(days=None)

    assert stats["overall"]["count"] == 3
    # 2 win (20, 30) из 3 decided (все не 0) -> 66.7%
    assert stats["overall"]["win_rate"] == 66.7
    assert stats["overall"]["total_pnl_usdt"] == 40.0
    assert stats["by_strategy"]["RSI + Bollinger Touch"]["count"] == 2
    assert stats["by_strategy"]["RSI + Bollinger Touch"]["win_rate"] == 50.0
    assert stats["by_strategy"]["MACD Crossover"]["win_rate"] == 100.0
    # by_quality намеренно не считается для реальных futures-сделок -
    # у closed_futures_positions нет поля quality (см. docstring функции)
    assert "by_quality" not in stats


def test_futures_trade_stats_empty(monkeypatch):
    monkeypatch.setattr(outcome_tracker.queue_manager, "get_closed_futures_positions", lambda: [])
    stats = outcome_tracker.get_futures_trade_stats()
    assert stats["overall"] == {"count": 0, "win_rate": None, "avg_pnl_pct": None, "total_pnl_usdt": 0.0}


def test_slippage_stats_aggregation(monkeypatch):
    fake_open = [
        {"symbol": "BTCUSDT", "side": "BUY", "slippage_pct": 0.2, "opened_at": 0},
    ]
    fake_closed = [
        {"symbol": "ETHUSDT", "side": "SELL", "slippage_pct": 0.5, "opened_at": 0},
        {"symbol": "SOLUSDT", "side": "BUY", "slippage_pct": -0.1, "opened_at": 0},
    ]
    monkeypatch.setattr(outcome_tracker.queue_manager, "get_open_futures_positions", lambda: fake_open)
    monkeypatch.setattr(outcome_tracker.queue_manager, "get_closed_futures_positions", lambda: fake_closed)

    stats = outcome_tracker.get_slippage_stats()

    assert stats["count"] == 3
    assert stats["avg_pct"] == round((0.2 + 0.5 - 0.1) / 3, 4)
    assert stats["max_pct"] == 0.5
    # худшие первыми (по убыванию slippage_pct)
    assert stats["worst_trades"][0]["symbol"] == "ETHUSDT"


def test_slippage_stats_ignores_records_without_the_field(monkeypatch):
    # старые записи (открытые до появления slippage_pct в коде) не должны
    # притворяться нулевым проскальзыванием - просто исключаются из выборки
    fake_open = [{"symbol": "BTCUSDT", "side": "BUY", "opened_at": 0}]  # нет slippage_pct вовсе
    monkeypatch.setattr(outcome_tracker.queue_manager, "get_open_futures_positions", lambda: fake_open)
    monkeypatch.setattr(outcome_tracker.queue_manager, "get_closed_futures_positions", lambda: [])

    stats = outcome_tracker.get_slippage_stats()
    assert stats == {"count": 0, "avg_pct": None, "max_pct": None, "worst_trades": []}


def test_slippage_stats_empty(monkeypatch):
    monkeypatch.setattr(outcome_tracker.queue_manager, "get_open_futures_positions", lambda: [])
    monkeypatch.setattr(outcome_tracker.queue_manager, "get_closed_futures_positions", lambda: [])
    stats = outcome_tracker.get_slippage_stats()
    assert stats == {"count": 0, "avg_pct": None, "max_pct": None, "worst_trades": []}


def test_record_signal_outcome_stores_bluesky_ref(monkeypatch):
    saved = []
    monkeypatch.setattr(outcome_tracker.queue_manager, "add_open_outcome", lambda record: saved.append(record))

    class _FakeSignal:
        ticker = "BEAT"
        entry_low, entry_high = "2.205", "2.2178"
        invalidation, target = "2.2371", "2.1729"
        direction, strategy, quality, score = "Шорт", "RSI + Bollinger Touch", "Conservative", "89"

    ref = {"uri": "at://did:plc:abc/app.bsky.feed.post/1", "cid": "bafy1"}
    outcome_tracker.record_signal_outcome(_FakeSignal(), bluesky_ref=ref)

    assert len(saved) == 1
    assert saved[0]["bluesky_ref"] == ref


def test_record_signal_outcome_without_bluesky_ref_defaults_to_none(monkeypatch):
    saved = []
    monkeypatch.setattr(outcome_tracker.queue_manager, "add_open_outcome", lambda record: saved.append(record))

    class _FakeSignal:
        ticker = "BEAT"
        entry_low, entry_high = "2.205", "2.2178"
        invalidation, target = "2.2371", "2.1729"
        direction, strategy, quality, score = "Шорт", "RSI + Bollinger Touch", "Conservative", "89"

    outcome_tracker.record_signal_outcome(_FakeSignal())

    assert saved[0]["bluesky_ref"] is None


if __name__ == "__main__":
    import sys
    import types

    # Простой раннер без pytest - находим все test_* функции и вызываем,
    # подставляя фейковый monkeypatch-объект тем тестам, которые его просят
    # (совместимо и с pytest, и с прямым запуском `python test_outcome_tracker.py`).
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