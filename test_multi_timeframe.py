#!/usr/bin/env python3
"""
Тесты multi_timeframe.py - чистая логика (тренд/конфлюенс/подтверждение
сигнала старшими ТФ), без сети (fetch_htf_snapshot подменяется
monkeypatch).
"""
import multi_timeframe as mtf
from signal_parser import RsiSignal


def _make_signal(direction="Лонг (перепроданность)", score="80"):
    return RsiSignal(
        ticker="SOL", timeframe="15m", strategy="RSI", direction=direction,
        current_price="100", rsi_now="25.0", score=score, quality="Moderate",
        entry_low="99.8", entry_high="100.1", invalidation="97.0", target="105.0",
        change_24h="+3.0%", volume="10.00M", rsi_live="25.0",
        created_at="2026-01-01 00:00:00 UTC",
        description="RSI ниже 30",
        raw_text="(сгенерировано сканером)",
    )


def _flat_closes(price: float, n: int) -> list[float]:
    return [price] * n


# --- classify_trend ---

def test_classify_trend_up_when_price_above_ma():
    # Последние 10 свечей заметно выше долгой средней - "up".
    closes = _flat_closes(100.0, 50) + [110.0] * 10
    assert mtf.classify_trend(closes) == "up"


def test_classify_trend_down_when_price_below_ma():
    closes = _flat_closes(100.0, 50) + [90.0] * 10
    assert mtf.classify_trend(closes) == "down"


def test_classify_trend_neutral_within_band():
    # Цена почти равна средней (в пределах TREND_NEUTRAL_BAND_PCT) - neutral.
    closes = _flat_closes(100.0, 60)
    assert mtf.classify_trend(closes) == "neutral"


def test_classify_trend_none_when_not_enough_data():
    assert mtf.classify_trend([100.0] * 10) is None


# --- evaluate_confluence ---

def test_confluence_all_confirming_long():
    snapshot = {"1h": {"trend": "up", "rsi": 55}, "4h": {"trend": "up", "rsi": 52}, "1d": {"trend": "up", "rsi": 60}}
    adjustment, veto, note = mtf.evaluate_confluence(is_long=True, htf_snapshot=snapshot)
    assert adjustment == 3 * mtf.CONFLUENCE_BONUS_PER_TF
    assert veto is False
    assert "подтверждено" in note


def test_confluence_single_conflict_no_veto():
    snapshot = {"1h": {"trend": "down", "rsi": 40}, "4h": {"trend": "neutral", "rsi": 48}, "1d": {"trend": "neutral", "rsi": 50}}
    adjustment, veto, note = mtf.evaluate_confluence(is_long=True, htf_snapshot=snapshot)
    assert adjustment == -mtf.CONFLUENCE_PENALTY_PER_TF
    assert veto is False
    assert "против" in note


def test_confluence_two_conflicts_triggers_veto():
    snapshot = {"4h": {"trend": "down", "rsi": 35}, "1d": {"trend": "down", "rsi": 38}}
    adjustment, veto, note = mtf.evaluate_confluence(is_long=True, htf_snapshot=snapshot)
    assert veto is True


def test_confluence_mixed_note():
    snapshot = {"1h": {"trend": "up", "rsi": 55}, "4h": {"trend": "down", "rsi": 40}}
    adjustment, veto, note = mtf.evaluate_confluence(is_long=True, htf_snapshot=snapshot)
    assert "смешанная" in note
    assert veto is False  # только 1 против - ниже VETO_MIN_CONFLICTING_TF


def test_confluence_short_direction_wants_downtrend():
    snapshot = {"4h": {"trend": "down", "rsi": 45}, "1d": {"trend": "down", "rsi": 40}}
    adjustment, veto, note = mtf.evaluate_confluence(is_long=False, htf_snapshot=snapshot)
    assert adjustment == 2 * mtf.CONFLUENCE_BONUS_PER_TF
    assert veto is False


def test_confluence_neutral_only_no_adjustment():
    snapshot = {"1h": {"trend": "neutral", "rsi": 50}, "4h": {"trend": "neutral", "rsi": 49}}
    adjustment, veto, note = mtf.evaluate_confluence(is_long=True, htf_snapshot=snapshot)
    assert adjustment == 0
    assert veto is False
    assert note == "старшие ТФ нейтральны"


# --- refine_signal ---

def test_refine_signal_boosts_score_on_confirmation(monkeypatch):
    monkeypatch.setattr(mtf, "fetch_htf_snapshot", lambda symbol: {
        "1h": {"trend": "up", "rsi": 55}, "4h": {"trend": "up", "rsi": 52},
    })
    signal = _make_signal(score="70")
    refined = mtf.refine_signal(signal, "SOLUSDT")
    assert refined is not None
    assert int(refined.score) == 70 + 2 * mtf.CONFLUENCE_BONUS_PER_TF
    assert refined.quality in ("Conservative", "Moderate", "Aggressive")
    assert "Старшие ТФ" in refined.description


def test_refine_signal_returns_none_on_veto(monkeypatch):
    monkeypatch.setattr(mtf, "fetch_htf_snapshot", lambda symbol: {
        "4h": {"trend": "down", "rsi": 35}, "1d": {"trend": "down", "rsi": 30},
    })
    signal = _make_signal(direction="Лонг (перепроданность)")
    refined = mtf.refine_signal(signal, "SOLUSDT")
    assert refined is None


def test_refine_signal_unchanged_when_no_htf_data(monkeypatch):
    monkeypatch.setattr(mtf, "fetch_htf_snapshot", lambda symbol: {})
    signal = _make_signal(score="70")
    refined = mtf.refine_signal(signal, "SOLUSDT")
    assert refined is signal  # без сети про старшие ТФ - публикуем как есть, без изменений


def test_refine_signal_score_never_exceeds_100(monkeypatch):
    monkeypatch.setattr(mtf, "fetch_htf_snapshot", lambda symbol: {
        "1h": {"trend": "up", "rsi": 55}, "4h": {"trend": "up", "rsi": 52}, "1d": {"trend": "up", "rsi": 60},
    })
    signal = _make_signal(score="95")
    refined = mtf.refine_signal(signal, "SOLUSDT")
    assert int(refined.score) <= 100


def test_refine_signal_score_never_below_0(monkeypatch):
    # Единичный конфликт (не veto) на очень низком стартовом score.
    monkeypatch.setattr(mtf, "fetch_htf_snapshot", lambda symbol: {
        "1h": {"trend": "down", "rsi": 40},
    })
    signal = _make_signal(score="5")
    refined = mtf.refine_signal(signal, "SOLUSDT")
    assert refined is not None
    assert int(refined.score) >= 0


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
