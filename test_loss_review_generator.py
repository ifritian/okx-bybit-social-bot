#!/usr/bin/env python3
"""
Тесты loss_review_generator.py - форматирование блока промахов и
валидация хука + generate_loss_review_post с замоканными
queue_manager.get_closed_outcomes и call_groq (без сети).
"""
import time

import loss_review_generator as lrg


def _loss(ticker, pnl_pct, days_ago=1, strategy="RSI + Bollinger Touch", direction="long",
          mfe_pct=1.0, hours_to_close=3.0):
    return {
        "ticker": ticker,
        "direction": direction,
        "strategy": strategy,
        "quality": "Moderate",
        "entry": 1.0,
        "stop": 0.9 if direction == "long" else 1.1,
        "target": 1.2 if direction == "long" else 0.8,
        "result": "loss",
        "exit_price": 0.9 if direction == "long" else 1.1,
        "pnl_pct": pnl_pct,
        "mfe_pct": mfe_pct,
        "hours_to_close": hours_to_close,
        "closed_at": time.time() - days_ago * 86400,
    }


def _win(ticker, days_ago=1):
    return {
        "ticker": ticker, "direction": "long", "strategy": "RSI", "quality": "Moderate",
        "entry": 1.0, "stop": 0.9, "target": 1.2, "result": "win", "exit_price": 1.2,
        "pnl_pct": 20.0, "closed_at": time.time() - days_ago * 86400,
    }


def test_classify_miss_close_call():
    # entry=1.0, target=1.2 (long) -> target_distance=20%; mfe=0.14 (14%) -> ratio=0.7 >= 0.6
    loss = _loss("AAA", -5.0, mfe_pct=0.14 * 100, hours_to_close=2.0)
    label = lrg.classify_miss(loss)
    assert "близкий промах" in label


def test_classify_miss_went_against_immediately():
    # mfe почти нулевой/отрицательный относительно 20% дистанции -> ratio <= 0.15
    loss = _loss("AAA", -5.0, mfe_pct=1.0, hours_to_close=1.0)
    label = lrg.classify_miss(loss)
    assert "сразу пошёл против" in label


def test_classify_miss_normal_volatility():
    # ratio между 0.15 и 0.6: mfe=8% при distance=20% -> ratio=0.4
    loss = _loss("AAA", -5.0, mfe_pct=8.0, hours_to_close=5.0)
    label = lrg.classify_miss(loss)
    assert "обычной волатильности" in label


def test_classify_miss_missing_data_is_graceful():
    loss = _loss("AAA", -5.0, mfe_pct=None)
    label = lrg.classify_miss(loss)
    assert "недостаточно данных" in label


def test_extract_numbers():
    nums = lrg._extract_numbers("вход 1.0, стоп 0.9, результат -10.0%")
    assert 1.0 in nums and 0.9 in nums and -10.0 in nums


def test_format_losses_block_contains_tickers_and_results():
    losses = [_loss("AAA", -5.0), _loss("BBB", -12.5)]
    block = lrg._format_losses_block(losses, total_closed=10, days=14)
    assert "AAA" in block and "BBB" in block
    assert "-12.50%" in block or "-12.5%" in block.replace("-12.50%", "-12.5%")
    assert "2 из 10" in block


def test_format_losses_block_caps_shown_cases():
    losses = [_loss(f"T{i}", -1.0 * i) for i in range(1, 8)]  # 7 losses
    block = lrg._format_losses_block(losses, total_closed=20, days=14, max_shown=5)
    assert "...и ещё 2" in block


def test_format_losses_block_sorts_worst_first():
    losses = [_loss("SMALL", -1.0), _loss("BIG", -20.0)]
    block = lrg._format_losses_block(losses, total_closed=5, days=14)
    # худший результат (BIG, -20%) должен идти раньше SMALL в блоке
    assert block.index("BIG") < block.index("SMALL")


def test_format_losses_block_does_not_use_dollar_cashtags():
    """Регресс: Binance Square сам парсит $ТИКЕР в тексте и превращает в
    кэштег/coin-pair виджет - у него есть лимит на количество таких
    кэштегов в одном посте ('Coin pair count exceeds the allowed
    limit'). При нескольких убытках в одном посте (до 5 тикеров) это
    реально приводило к ошибке публикации - тикеры в этом блоке НЕ
    должны иметь префикс $."""
    losses = [_loss("AAA", -1.0), _loss("BBB", -2.0), _loss("CCC", -3.0)]
    block = lrg._format_losses_block(losses, total_closed=5, days=14)
    assert "$AAA" not in block and "$BBB" not in block and "$CCC" not in block
    assert "AAA" in block and "BBB" in block and "CCC" in block


def test_validate_loss_review_hook_ok():
    ok, reason = lrg.validate_loss_review_hook("Не все сигналы отрабатывают, это нормально", {14})
    assert ok is True, reason


def test_validate_loss_review_hook_rejects_unknown_number():
    ok, reason = lrg.validate_loss_review_hook("Потеряли ровно 42.0% в этот раз", {14})
    assert ok is False


def test_validate_loss_review_hook_rejects_english_words():
    ok, reason = lrg.validate_loss_review_hook("This period was tough, но идём дальше", {14})
    assert ok is False


def test_generate_returns_none_when_not_enough_losses(monkeypatch):
    monkeypatch.setattr(lrg.queue_manager, "get_closed_outcomes", lambda: [_loss("AAA", -5.0), _win("BBB")])
    monkeypatch.setattr(lrg, "call_groq", lambda *a, **k: "не должно вызываться")
    import config
    monkeypatch.setattr(config, "LOSS_REVIEW_MIN_LOSSES", 3)

    result = lrg.generate_loss_review_post()
    assert result is None


def test_generate_returns_text_when_enough_losses(monkeypatch):
    data = [_loss("AAA", -5.0), _loss("BBB", -8.0), _loss("CCC", -3.0), _win("DDD")]
    monkeypatch.setattr(lrg.queue_manager, "get_closed_outcomes", lambda: data)
    monkeypatch.setattr(lrg, "call_groq", lambda *a, **k: "Не всё идеально, но это часть игры")
    import config
    monkeypatch.setattr(config, "LOSS_REVIEW_MIN_LOSSES", 3)

    result = lrg.generate_loss_review_post()
    assert result is not None
    binance_text, telegram_text = result
    assert "Не всё идеально" in binance_text
    assert "AAA" in binance_text and "BBB" in binance_text and "CCC" in binance_text
    assert binance_text == telegram_text


def test_generate_ignores_losses_outside_period(monkeypatch):
    # 2 убыточных сигнала внутри периода, 1 - давно (за пределами 14 дней) -
    # должен считаться только 2, что меньше LOSS_REVIEW_MIN_LOSSES=3.
    data = [_loss("AAA", -5.0, days_ago=1), _loss("BBB", -8.0, days_ago=2), _loss("OLD", -9.0, days_ago=30)]
    monkeypatch.setattr(lrg.queue_manager, "get_closed_outcomes", lambda: data)
    monkeypatch.setattr(lrg, "call_groq", lambda *a, **k: "не должно вызываться")
    import config
    monkeypatch.setattr(config, "LOSS_REVIEW_MIN_LOSSES", 3)

    result = lrg.generate_loss_review_post(days=14.0)
    assert result is None


def test_generate_falls_back_to_neutral_hook_on_bad_llm_output(monkeypatch):
    data = [_loss("AAA", -5.0), _loss("BBB", -8.0), _loss("CCC", -3.0)]
    monkeypatch.setattr(lrg.queue_manager, "get_closed_outcomes", lambda: data)
    monkeypatch.setattr(lrg, "call_groq", lambda *a, **k: "This was a rough patch, потеряли 42.0%")
    import config
    monkeypatch.setattr(config, "LOSS_REVIEW_MIN_LOSSES", 3)

    result = lrg.generate_loss_review_post()
    assert result is not None
    binance_text, _ = result
    assert "This was" not in binance_text
    assert "Честный разбор" in binance_text


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
