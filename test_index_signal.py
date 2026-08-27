#!/usr/bin/env python3
"""
Тесты index_signal_scanner.py / index_signal_generator.py - чистая
логика, без сети (сетевые функции подменяются monkeypatch).
"""
import index_signal_generator as isg
import index_signal_scanner as iss
import post_format
from signal_parser import RsiSignal
import treasury_index


def _make_signal(direction="Лонг (перепроданность)", ticker="SOL"):
    return RsiSignal(
        ticker=ticker, timeframe="15m", strategy="RSI", direction=direction,
        current_price="100", rsi_now="25.0", score="80", quality="Moderate",
        entry_low="99.8", entry_high="100.1", invalidation="97.0", target="105.0",
        change_24h="+3.0%", volume="10.00M", rsi_live="25.0",
        created_at="2026-01-01 00:00:00 UTC",
        description="RSI ниже 30 [Индекс: 🔵 Фундамент, вес 20% корзины]",
        raw_text="(сгенерировано сканером)",
    )


def test_flatten_basket_includes_all_tiers():
    coins = iss._flatten_basket()
    tiers = {c["tier"] for c in coins}
    assert tiers == {"tier1", "tier2", "tier3"}
    tickers = {c["ticker"] for c in coins}
    assert "SOL" in tickers and "AAVE" in tickers and "SUI" in tickers


def test_resolve_symbol_and_candles_uses_primary(monkeypatch):
    calls = []

    def fake_fetch(symbol, limit=100):
        calls.append(symbol)
        return [{"open": 1, "high": 1, "low": 1, "close": 1}] if symbol == "SOLUSDT" else []

    monkeypatch.setattr(iss.scanner, "_fetch_klines", fake_fetch)
    result = iss._resolve_symbol_and_candles({"ticker": "SOL", "weight": 20.0})
    assert result is not None
    symbol, candles = result
    assert symbol == "SOLUSDT"
    assert calls == ["SOLUSDT"]


def test_resolve_symbol_and_candles_falls_back(monkeypatch):
    def fake_fetch(symbol, limit=100):
        return [{"open": 1, "high": 1, "low": 1, "close": 1}] if symbol == "MATICUSDT" else []

    monkeypatch.setattr(iss.scanner, "_fetch_klines", fake_fetch)
    result = iss._resolve_symbol_and_candles({"ticker": "POL", "weight": 7.0, "fallback": "MATIC"})
    assert result is not None
    symbol, _ = result
    assert symbol == "MATICUSDT"


def test_resolve_symbol_and_candles_none_when_unavailable(monkeypatch):
    monkeypatch.setattr(iss.scanner, "_fetch_klines", lambda symbol, limit=100: [])
    result = iss._resolve_symbol_and_candles({"ticker": "SOL", "weight": 20.0})
    assert result is None


def test_validate_hook_rejects_numbers():
    ok, reason = isg._validate_hook("SOL сейчас в зоне перепроданности, RSI 25.0")
    assert ok is False
    assert "числа" in reason


def test_validate_hook_rejects_english_words():
    ok, reason = isg._validate_hook("SOL в крутом sprint вниз, стоит присмотреться")
    assert ok is False
    assert "английск" in reason.lower()


def test_validate_hook_ok_for_clean_text():
    ok, reason = isg._validate_hook("SOL сейчас перепродан - неплохой момент присмотреться в рамках индекса")
    assert ok is True, reason


def test_fallback_hook_long_for_oversold():
    signal = _make_signal(direction="Лонг (перепроданность)")
    hook = isg._fallback_hook(signal)
    assert "докупки" in hook.lower()


def test_fallback_hook_short_for_overbought():
    signal = _make_signal(direction="Шорт (перекупленность)")
    hook = isg._fallback_hook(signal)
    assert "фиксац" in hook.lower()


def test_generate_index_signal_post_uses_fallback_on_bad_llm_output(monkeypatch):
    signal = _make_signal()
    monkeypatch.setattr(isg, "call_groq", lambda *a, **k: "плохой хук с числом 42.0")
    text = isg.generate_index_signal_post(signal)
    assert "42.0" not in text
    assert "$SOL" in text or "SOL" in text
    assert "Действие:" in text  # блок собран кодом, в терминах управления долей
    assert "Диапазон для докупки" in text
    assert "Вход:" not in text and "Стоп:" not in text and "Тейк:" not in text  # НЕ формат разовой сделки


def test_find_coin_by_ticker_primary():
    found = treasury_index.find_coin_by_ticker("sol")
    assert found is not None
    tier_key, coin = found
    assert tier_key == "tier1"
    assert coin["ticker"] == "SOL"


def test_find_coin_by_ticker_fallback():
    found = treasury_index.find_coin_by_ticker("MATIC")
    assert found is not None
    _, coin = found
    assert coin["ticker"] == "POL"


def test_find_coin_by_ticker_not_in_basket():
    assert treasury_index.find_coin_by_ticker("DOGE") is None


def test_assemble_index_management_post_buy_side():
    signal = _make_signal(direction="Лонг (перепроданность)")
    text = post_format.assemble_index_management_post("хук", signal, "🔵 Фундамент", 20.0)
    assert "Докупка доли" in text
    assert "Диапазон для докупки" in text
    assert "Вход:" not in text and "Стоп:" not in text and "Тейк:" not in text
    assert post_format.DISCLAIMER in text


def test_assemble_index_management_post_sell_side():
    signal = _make_signal(direction="Шорт (перекупленность)")
    text = post_format.assemble_index_management_post("хук", signal, "🟡 Рост", 8.0)
    assert "Частичная фиксация доли" in text
    assert "Диапазон для фиксации" in text


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
