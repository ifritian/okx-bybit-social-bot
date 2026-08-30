#!/usr/bin/env python3
"""
Тесты bybit_byx_generator.py: ротация формата (market_take/trading_
insight), генерация (LLM замокан), валидация текста.
"""
import bybit_byx_generator
import post_format


def _fake_single_stats(pct=5.5):
    return {"single": {"pct": pct, "amplitude_pct": 7.2, "current_price": 65000.0}}


def test_pick_format_avoids_last_used():
    for _ in range(30):
        chosen = bybit_byx_generator.pick_format("market_take")
        assert chosen != "market_take"


def test_pick_format_handles_unknown_last():
    chosen = bybit_byx_generator.pick_format(None)
    assert chosen in bybit_byx_generator.FORMATS


def test_generate_bybit_byx_post_returns_none_when_no_data(monkeypatch):
    monkeypatch.setattr(bybit_byx_generator, "calc_theme_stats", lambda theme: None)
    assert bybit_byx_generator.generate_bybit_byx_post("BTC", "market_take") is None


def test_generate_bybit_byx_post_includes_disclaimer(monkeypatch):
    monkeypatch.setattr(bybit_byx_generator, "calc_theme_stats", lambda theme: _fake_single_stats())
    monkeypatch.setattr(bybit_byx_generator, "call_groq", lambda *a, **k: "Рынок сегодня ведёт себя интересно.")

    result = bybit_byx_generator.generate_bybit_byx_post("BTC", "market_take")

    assert result is not None
    text, allowed_numbers, headline_pct, format_type = result
    assert post_format.DISCLAIMER in text
    assert 5.5 in allowed_numbers
    assert headline_pct == 5.5
    assert format_type == "market_take"


def test_generate_bybit_byx_post_allows_negative_pct_written_without_sign(monkeypatch):
    """Регрессия: та же логика, что и в okx_orbit_generator - см.
    комментарий там. LLM пишет "ETH снизился на 2.12%" без минуса,
    валидатор не должен это бpaковать."""
    monkeypatch.setattr(bybit_byx_generator, "calc_theme_stats", lambda theme: _fake_single_stats(pct=-2.12))
    monkeypatch.setattr(
        bybit_byx_generator, "call_groq",
        lambda *a, **k: "ETH снизился на 2.12% за последние 48 часов - текущая цена $65000.00 ближе к нижней границе.",
    )

    result = bybit_byx_generator.generate_bybit_byx_post("ETH", "trading_insight")

    assert result is not None
    text, allowed_numbers, headline_pct, format_type = result
    assert 2.12 in allowed_numbers
    assert -2.12 in allowed_numbers

    ok, reason = bybit_byx_generator.validate_bybit_byx_post_text(text, allowed_numbers)
    assert ok is True, reason


def test_generate_bybit_byx_post_trading_insight_uses_dedicated_prompt(monkeypatch):
    monkeypatch.setattr(bybit_byx_generator, "calc_theme_stats", lambda theme: _fake_single_stats())
    captured_system_prompts = []
    monkeypatch.setattr(
        bybit_byx_generator, "call_groq",
        lambda system, user, **k: captured_system_prompts.append(system) or "Короткий инсайт про волатильность.",
    )

    bybit_byx_generator.generate_bybit_byx_post("BTC", "trading_insight")

    assert "трейдинг-инсайт" in captured_system_prompts[0].lower()


def test_generate_bybit_byx_post_falls_back_on_empty_llm_response(monkeypatch):
    monkeypatch.setattr(bybit_byx_generator, "calc_theme_stats", lambda theme: _fake_single_stats())
    monkeypatch.setattr(bybit_byx_generator, "call_groq", lambda *a, **k: "")

    result = bybit_byx_generator.generate_bybit_byx_post("BTC", "market_take")

    assert result is None


def test_generated_post_never_mentions_binance(monkeypatch):
    # Черновик уходит на другую площадку - упоминание Binance тут
    # неуместно и может сбить с толку при ручной публикации.
    monkeypatch.setattr(bybit_byx_generator, "calc_theme_stats", lambda theme: _fake_single_stats())
    monkeypatch.setattr(bybit_byx_generator, "call_groq", lambda *a, **k: "Рынок сегодня ведёт себя интересно.")

    text, *_ = bybit_byx_generator.generate_bybit_byx_post("BTC", "market_take")

    assert "binance" not in text.lower()


def test_validate_bybit_byx_post_text_rejects_unknown_numbers():
    text = f"$BTC вырос на 999% - невероятно.\n\n{post_format.DISCLAIMER}"
    ok, reason = bybit_byx_generator.validate_bybit_byx_post_text(text, {5.5})
    assert ok is False
    assert "999" in reason


def test_validate_bybit_byx_post_text_rejects_cliche():
    text = f"$BTC вырос на 5.5%. Кроме того, это важно.\n\n{post_format.DISCLAIMER}"
    ok, reason = bybit_byx_generator.validate_bybit_byx_post_text(text, {5.5})
    assert ok is False
    assert "ИИ-фраз" in reason


def test_validate_bybit_byx_post_text_passes_valid_text():
    text = f"$BTC вырос на 5.5% - и рынок явно не ожидал такого разворота.\n\n{post_format.DISCLAIMER}"
    ok, reason = bybit_byx_generator.validate_bybit_byx_post_text(text, {5.5})
    assert ok is True, reason


def test_validate_bybit_byx_post_text_allows_48_hours_and_2_days_mentions():
    """Регрессия: system-промпт (_build_user_prompt) сам явно просит модель
    писать период времени как "48 часов" или "2 дня" - и raньше эти
    числа не были в allowed_numbers, из-за чего валидатор бpaковал
    честно выполнивший инструкцию ответ. См. _build_user_prompt."""
    text = (
        f"$BTC вырос на 5.5% за последние 48 часов - и рынок явно не "
        f"ожидал такого разворота.\n\n{post_format.DISCLAIMER}"
    )
    ok, reason = bybit_byx_generator.validate_bybit_byx_post_text(text, {5.5, 48.0, 2.0})
    assert ok is True, reason

    text_days = (
        f"$BTC вырос на 5.5% за последние 2 дня - и рынок явно не "
        f"ожидал такого разворота.\n\n{post_format.DISCLAIMER}"
    )
    ok, reason = bybit_byx_generator.validate_bybit_byx_post_text(text_days, {5.5, 48.0, 2.0})
    assert ok is True, reason


def test_generate_chart_for_post_uses_first_ticker_and_no_watermark(monkeypatch):
    calls = []

    def _fake_generate_chart_image(ticker, days=2, expected_price=None, watermark_text="BINANCE", filename_suffix=""):
        calls.append((ticker, watermark_text, filename_suffix))
        return "/tmp/fake_chart.png"

    monkeypatch.setattr(bybit_byx_generator, "generate_chart_image", _fake_generate_chart_image)

    result = bybit_byx_generator.generate_chart_for_post("market")

    assert result == "/tmp/fake_chart.png"
    assert len(calls) == 1
    ticker, watermark_text, filename_suffix = calls[0]
    assert ticker == bybit_byx_generator.THEMES["market"]["tickers"][0]
    assert watermark_text is None
    assert filename_suffix == "_bybit"


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
