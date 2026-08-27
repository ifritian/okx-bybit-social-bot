#!/usr/bin/env python3
"""
Тесты rebalance_advisor.py: расчёт хронического отставания монеты в
тире (по замоканной истории, без сети), детект нездоровых монет (по
замоканным streaks), и генерация отчёта (LLM замокан).
"""
import post_format
import queue_manager
import rebalance_advisor
from treasury_index import BASKET


def _tier1_tickers():
    return [c["ticker"] for c in BASKET["tier1"]]


def test_underperformance_rate_none_with_too_few_periods():
    tier_tickers = _tier1_tickers()
    ticker = tier_tickers[0]
    # Всего 5 валидных периодов - меньше MIN_PERIODS_FOR_REVIEW (20).
    history = {t: [1.0, -1.0, 2.0, -2.0, 0.5] for t in tier_tickers}
    history[ticker] = [-5.0, -5.0, -5.0, -5.0, -5.0]  # хуже всех, но выборка мала

    result = rebalance_advisor._underperformance_rate(ticker, tier_tickers, history)

    assert result is None


def test_underperformance_rate_detects_chronic_underperformer():
    tier_tickers = _tier1_tickers()
    ticker = tier_tickers[0]
    peers = tier_tickers[1:]

    n = 25
    history = {ticker: [-5.0] * n}  # всегда хуже всех
    for p in peers:
        history[p] = [1.0] * n

    rate, valid_periods = rebalance_advisor._underperformance_rate(ticker, tier_tickers, history)

    assert rate == 1.0
    assert valid_periods == n


def test_underperformance_rate_not_flagged_when_average():
    tier_tickers = _tier1_tickers()
    ticker = tier_tickers[0]
    peers = tier_tickers[1:]

    n = 25
    # Чередуется выше/ниже медианы - примерно 50%, ниже порога 0.65.
    history = {ticker: [1.0 if i % 2 == 0 else -1.0 for i in range(n)]}
    for p in peers:
        history[p] = [0.0] * n

    result = rebalance_advisor._underperformance_rate(ticker, tier_tickers, history)

    assert result is not None
    rate, _ = result
    assert rate < rebalance_advisor.REBALANCE_UNDERPERFORM_THRESHOLD


def test_find_rebalance_candidates_flags_unhealthy_coin(monkeypatch):
    ticker = _tier1_tickers()[0]
    monkeypatch.setattr(queue_manager, "get_coin_pct_history", lambda: {})
    monkeypatch.setattr(queue_manager, "get_coin_miss_streaks", lambda: {ticker: 5})

    candidates = rebalance_advisor.find_rebalance_candidates()

    matching = [c for c in candidates if c["ticker"] == ticker]
    assert len(matching) == 1
    assert matching[0]["reason"] == "unhealthy"


def test_find_rebalance_candidates_flags_chronic_underperformer(monkeypatch):
    tier_tickers = _tier1_tickers()
    ticker = tier_tickers[0]
    peers = tier_tickers[1:]

    n = 25
    history = {ticker: [-5.0] * n}
    for p in peers:
        history[p] = [1.0] * n

    monkeypatch.setattr(queue_manager, "get_coin_pct_history", lambda: history)
    monkeypatch.setattr(queue_manager, "get_coin_miss_streaks", lambda: {})

    candidates = rebalance_advisor.find_rebalance_candidates()

    matching = [c for c in candidates if c["ticker"] == ticker]
    assert len(matching) == 1
    assert matching[0]["reason"] == "underperform"


def test_find_rebalance_candidates_empty_when_all_healthy(monkeypatch):
    monkeypatch.setattr(queue_manager, "get_coin_pct_history", lambda: {})
    monkeypatch.setattr(queue_manager, "get_coin_miss_streaks", lambda: {})

    assert rebalance_advisor.find_rebalance_candidates() == []


def test_build_rebalance_report_includes_tickers_and_disclaimer(monkeypatch):
    monkeypatch.setattr(
        rebalance_advisor, "call_groq",
        lambda *a, **k: "Стоит присмотреться к этой монете - данные показывают стабильное отставание."
    )
    candidates = [{"ticker": "OP", "tier": "tier1", "reason": "underperform", "detail": "в нижней половине тира в 70% периодов (из 25)"}]

    text = rebalance_advisor.build_rebalance_report(candidates)

    assert text is not None
    assert "$OP" in text
    assert post_format.DISCLAIMER in text
    assert "Стоит присмотреться" in text


def test_build_rebalance_report_returns_none_on_empty_response(monkeypatch):
    monkeypatch.setattr(rebalance_advisor, "call_groq", lambda *a, **k: "   ")
    candidates = [{"ticker": "OP", "tier": "tier1", "reason": "underperform", "detail": "..."}]

    assert rebalance_advisor.build_rebalance_report(candidates) is None


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
