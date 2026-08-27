#!/usr/bin/env python3
"""
Тесты signal_parser.py - парсинг постов канала @resultrsi (реальный
формат, см. пример в docstring signal_parser.py). Чистая логика,
без сети.
"""
import signal_parser
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


# --- calc_risk_reward_ratio (см. post_format.signal_risk_reward_line -
# использует ЭТУ ЖЕ функцию, там же есть эквивалентные тесты через
# отформатированную строку "R:R 1:X.X") ---

def test_calc_risk_reward_ratio_long():
    signal = _make_signal(direction="Лонг", entry_low="98", entry_high="102",
                           invalidation="90", target="130")
    assert signal_parser.calc_risk_reward_ratio(signal) == 3.0


def test_calc_risk_reward_ratio_short():
    signal = _make_signal(direction="Шорт", entry_low="98", entry_high="102",
                           invalidation="110", target="70")
    assert signal_parser.calc_risk_reward_ratio(signal) == 3.0


def test_calc_risk_reward_ratio_poor_ratio_below_one():
    # риск 20 (100->80), прибыль 10 (100->110) -> R:R 1:0.5
    signal = _make_signal(entry_low="100", entry_high="100",
                           invalidation="80", target="110")
    assert signal_parser.calc_risk_reward_ratio(signal) == 0.5


def test_calc_risk_reward_ratio_none_when_risk_is_zero():
    signal = _make_signal(entry_low="100", entry_high="100", invalidation="100", target="130")
    assert signal_parser.calc_risk_reward_ratio(signal) is None


def test_calc_risk_reward_ratio_none_on_garbage_numbers():
    signal = _make_signal(entry_low="не число", entry_high="", invalidation="90", target="130")
    assert signal_parser.calc_risk_reward_ratio(signal) is None


_SAMPLE_SINGLE = """BEATUSDT • 15m
[Свежий] [RSI + Bollinger Touch] [Шорт]
RSI stayed above 70 while price tagged the upper Bollinger Band.
Сетап
Стратегия: RSI + Bollinger Touch
Сейчас: 2.225
Направление: Шорт
RSI / Score сейчас: 81.74 / 89/100
RSI / Score на сигнале: 81.74 / 89/100
Качество: Conservative
Фоллоу-ап: Включён
Уровни
Вход: 2.205 - 2.2178
Инвалидация: 2.2371
Цель: 2.1729
Окно RSI: 30.00 / 70.00
Контекст
24h: +35.67%
Объем: 57.67M
Режим: Directional
RSI live: 82.64
Создан: 2026-06-23 22:44:59 EEST
"""

_SAMPLE_BATCH = _SAMPLE_SINGLE + "\n" + _SAMPLE_SINGLE.replace("BEATUSDT", "PHBUSDT").replace("Шорт", "Лонг")


def test_is_signal_message_true_for_signal():
    assert signal_parser.is_signal_message(_SAMPLE_SINGLE) is True


def test_is_signal_message_false_for_plain_text():
    assert signal_parser.is_signal_message("привет, как дела?") is False


def test_parse_single_signal_fields():
    signals = signal_parser.parse_signals(_SAMPLE_SINGLE)
    assert len(signals) == 1
    s = signals[0]
    assert s.ticker == "BEAT"
    assert s.timeframe == "15m"
    assert s.strategy == "RSI + Bollinger Touch"
    assert s.current_price == "2.225"
    assert s.rsi_now == "81.74"
    assert s.score == "89"
    assert s.entry_low == "2.205"
    assert s.entry_high == "2.2178"
    assert s.invalidation == "2.2371"
    assert s.target == "2.1729"
    assert s.change_24h == "+35.67%"


def test_parse_batch_returns_all_blocks():
    signals = signal_parser.parse_signals(_SAMPLE_BATCH)
    assert len(signals) == 2
    tickers = {s.ticker for s in signals}
    assert tickers == {"BEAT", "PHB"}


def test_parse_signals_empty_for_no_headers():
    assert signal_parser.parse_signals("просто какой-то текст без сигналов") == []


def test_parse_signals_ignores_incomplete_block():
    # Заголовок есть, но нет обязательных полей (Вход/Инвалидация/Цель) -
    # блок должен быть пропущен, а не упасть с ошибкой.
    broken = "ABCUSDT • 15m\n[Шорт]\nСтратегия: RSI\nСейчас: 1.0\n"
    assert signal_parser.parse_signals(broken) == []


def test_pick_entry_avoids_recent_tickers():
    signals = signal_parser.parse_signals(_SAMPLE_BATCH)
    # оба тикера присутствуют (BEAT, PHB) - просим избежать BEAT
    chosen = signal_parser.pick_entry(signals, recent_tickers=["BEAT"])
    assert chosen.ticker == "PHB"


def test_pick_entry_falls_back_to_first_if_all_recent():
    signals = signal_parser.parse_signals(_SAMPLE_BATCH)
    chosen = signal_parser.pick_entry(signals, recent_tickers=["BEAT", "PHB"])
    assert chosen.ticker == signals[0].ticker


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
