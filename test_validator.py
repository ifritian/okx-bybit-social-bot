#!/usr/bin/env python3
"""
Тесты validator.py - защита от искажения чисел LLM-ом. Чистая логика,
без сети и без обращения к LLM.
"""
from post_format import DISCLAIMER
from signal_parser import RsiSignal
import validator


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


def test_valid_post_passes():
    signal = _make_signal()
    text = (
        f"BEAT выглядит перегретым. Вход 2.205 - 2.2178, стоп 2.2371, "
        f"тейк 2.1729, RSI 81.74, score 89.\n\n{DISCLAIMER}"
    )
    ok, reason = validator.validate_post_text(text, signal)
    assert ok is True, reason


def test_missing_target_fails():
    signal = _make_signal()
    text = f"Вход 2.205 - 2.2178, стоп 2.2371, RSI 81.74, score 89.\n\n{DISCLAIMER}"
    ok, reason = validator.validate_post_text(text, signal)
    assert ok is False
    assert "тейк" in reason


def test_altered_number_fails():
    signal = _make_signal()
    # LLM исказил стоп (2.2371 -> 2.24) - должно быть отклонено
    text = f"Вход 2.205 - 2.2178, стоп 2.24, тейк 2.1729, RSI 81.74, score 89.\n\n{DISCLAIMER}"
    ok, reason = validator.validate_post_text(text, signal)
    assert ok is False


def test_missing_disclaimer_fails():
    signal = _make_signal()
    text = "Вход 2.205 - 2.2178, стоп 2.2371, тейк 2.1729, RSI 81.74, score 89."
    ok, reason = validator.validate_post_text(text, signal)
    assert ok is False
    assert "дисклеймер" in reason.lower()


def test_image_post_without_numbers_passes():
    text = f"Похоже на разворот, но подтверждения пока не видно.\n\n{DISCLAIMER}"
    ok, reason = validator.validate_image_post_text(text)
    assert ok is True, reason


def test_image_post_with_invented_number_fails():
    text = f"RSI около 75, разворот вероятен.\n\n{DISCLAIMER}"
    ok, reason = validator.validate_image_post_text(text)
    assert ok is False
    assert "числа" in reason or "чисел" in reason


def test_language_mixing_fails():
    signal = _make_signal()
    # Ровно тот баг, что был в проде: русский текст с затесавшимися
    # английскими словами (не тикер, не акроним).
    text = (
        f"BEAT в a крутом sprint вниз, волatility like on American ropges. "
        f"Вход 2.205 - 2.2178, стоп 2.2371, тейк 2.1729, RSI 81.74, score 89.\n\n{DISCLAIMER}"
    )
    ok, reason = validator.validate_post_text(text, signal)
    assert ok is False
    assert "английск" in reason.lower()


def test_uppercase_tickers_do_not_trigger_language_check():
    signal = _make_signal()
    text = (
        f"BEAT перегрет против USDT, возможен откат. "
        f"Вход 2.205 - 2.2178, стоп 2.2371, тейк 2.1729, RSI 81.74, score 89.\n\n{DISCLAIMER}"
    )
    ok, reason = validator.validate_post_text(text, signal)
    assert ok is True, reason


def test_strategy_name_words_do_not_trigger_language_check():
    """Регресс на реальный сбой в проде: сигналы со стратегией
    'RSI + Bollinger Touch' / '+ Divergence' (см. scanner.py
    strategy_parts) отклонялись валидатором как 'смешение языков',
    хотя это служебные слова из кода, а не текст LLM. Полностью
    блокировало публикацию всех сигналов с этими стратегиями."""
    signal = _make_signal(
        ticker="TRX", strategy="RSI + Bollinger Touch", direction="Шорт (перекупленность)",
        entry_low="0.38471", entry_high="0.39458", invalidation="0.39987", target="0.3574",
        rsi_now="89.97", score="100",
    )
    text = (
        f"$TRX в зоне перекупленности, RSI зашкаливает, а цена только что "
        f"коснулась верхней полосы Боллинджера - время для шорта 🤔\n\n"
        f"🔴 Шорт (перекупленность) | RSI + Bollinger Touch\n"
        f"Вход: 0.38471 - 0.39458\n"
        f"Стоп: 0.39987\n"
        f"Тейк: 0.3574\n"
        f"RSI: 89.97 | Score: 100/100\n\n{DISCLAIMER}"
    )
    ok, reason = validator.validate_post_text(text, signal)
    assert ok is True, reason


def test_macd_and_breakout_strategy_names_do_not_trigger_language_check():
    """Регресс на реальный баг в проде (найден по логу с постом DODO):
    та же история, что с 'RSI + Bollinger Touch' выше, но для двух
    других стратегий из strategies.py - 'MACD Crossover' и 'Donchian
    Breakout'. Подтверждено по outcome_tracker.get_accuracy_stats: с
    момента появления этих стратегий в коде ОПУБЛИКОВАНО ноль сигналов
    с ними, хотя сканер находит такие сигналы регулярно - валидатор
    отклонял КАЖДЫЙ такой пост как 'смешение языков' из-за слов
    Crossover/Donchian/Breakout (смешанный регистр, не заглавные - MACD
    сам по себе не страдал, он весь заглавными и проходит по отдельному
    правилу)."""
    signal = _make_signal(
        ticker="DODO", strategy="Donchian Breakout", direction="Лонг (пробой диапазона вверх)",
        entry_low="0.0200299", entry_high="0.0201091", invalidation="0.0192831", target="0.0208",
        rsi_now="н/д", score="92",
    )
    text = (
        f"$DODO пробил 20-свечный диапазон вверх, объём вдвое выше обычного 🚀\n"
        f"Подтверждено на старших таймфреймах, держу в зоне входа.\n\n"
        f"🟢 Лонг (пробой диапазона вверх) | Donchian Breakout\n"
        f"Вход: 0.0200299 - 0.0201091\n"
        f"Стоп: 0.0192831\n"
        f"Тейк: 0.0208\n"
        f"RSI: н/д | Score: 92/100\n\n{DISCLAIMER}"
    )
    ok, reason = validator.validate_post_text(text, signal)
    assert ok is True, reason

    signal2 = _make_signal(
        ticker="CFX", strategy="MACD Crossover", direction="Шорт (медвежье пересечение MACD)",
        entry_low="0.108", entry_high="0.1087", invalidation="0.1097", target="0.1054",
        rsi_now="н/д", score="54",
    )
    text2 = (
        f"$CFX дал медвежье пересечение MACD на 15м, импульс разворачивается вниз 📉\n\n"
        f"🔴 Шорт (медвежье пересечение MACD) | MACD Crossover\n"
        f"Вход: 0.108 - 0.1087\n"
        f"Стоп: 0.1097\n"
        f"Тейк: 0.1054\n"
        f"RSI: н/д | Score: 54/100\n\n{DISCLAIMER}"
    )
    ok2, reason2 = validator.validate_post_text(text2, signal2)
    assert ok2 is True, reason2


if __name__ == "__main__":
    import sys
    import types

    passed, failed = 0, 0
    module = sys.modules[__name__]
    for name in dir(module):
        if not name.startswith("test_"):
            continue
        fn = getattr(module, name)
        if not isinstance(fn, types.FunctionType):
            continue
        try:
            fn()
            print(f"OK   {name}")
            passed += 1
        except AssertionError as e:
            print(f"FAIL {name}: {e}")
            failed += 1

    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
