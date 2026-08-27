#!/usr/bin/env python3
"""
Тесты hot_take_generator.py: генерация хот-тейка (числа считаются кодом,
LLM замокан через groq_client.call_groq) и валидация текста (числа/
дисклеймер/лимит длины Bluesky).
"""
import config
import hot_take_generator
import opinion_generator
import post_format


def _fake_single_stats(**overrides):
    base = {"pct": 5.5, "amplitude_pct": 7.2, "current_price": 65000.0}
    base.update(overrides)
    return {"single": base}


def _fake_basket_stats():
    return {"breakdown": {"BTC": 5.5, "ETH": -2.1, "SOL": 8.0, "BNB": 1.0}, "avg_pct": 3.1}


def test_generate_hot_take_returns_none_when_no_data(monkeypatch):
    monkeypatch.setattr(hot_take_generator, "calc_theme_stats", lambda theme: None)
    result = hot_take_generator.generate_hot_take("BTC")
    assert result is None


def test_generate_hot_take_single_theme_uses_real_pct(monkeypatch):
    monkeypatch.setattr(hot_take_generator, "calc_theme_stats", lambda theme: _fake_single_stats(pct=5.5))
    monkeypatch.setattr(hot_take_generator, "call_groq", lambda *a, **k: "Все празднуют рост, а я жду разворота.")

    result = hot_take_generator.generate_hot_take("BTC")

    assert result is not None
    text, allowed_numbers, headline_pct = result
    assert allowed_numbers == {5.5}
    assert post_format.DISCLAIMER in text
    assert "Все празднуют рост" in text


def test_generate_hot_take_basket_theme_allows_each_ticker_and_avg(monkeypatch):
    monkeypatch.setattr(hot_take_generator, "calc_theme_stats", lambda theme: _fake_basket_stats())
    monkeypatch.setattr(hot_take_generator, "call_groq", lambda *a, **k: "Рынок в целом выглядит перегретым.")

    result = hot_take_generator.generate_hot_take("market")

    assert result is not None
    _, allowed_numbers, _ = result
    assert allowed_numbers == {5.5, -2.1, 8.0, 1.0, 3.1}


def test_generate_hot_take_returns_none_on_empty_llm_response(monkeypatch):
    monkeypatch.setattr(hot_take_generator, "calc_theme_stats", lambda theme: _fake_single_stats())
    monkeypatch.setattr(hot_take_generator, "call_groq", lambda *a, **k: "  ")

    result = hot_take_generator.generate_hot_take("BTC")

    assert result is None


def test_generate_hot_take_truncates_overly_long_llm_response(monkeypatch):
    monkeypatch.setattr(hot_take_generator, "calc_theme_stats", lambda theme: _fake_single_stats())
    monkeypatch.setattr(hot_take_generator, "call_groq", lambda *a, **k: "А" * 500)

    result = hot_take_generator.generate_hot_take("BTC")

    assert result is not None
    text, _, _ = result
    assert len(text) <= post_format.BLUESKY_CHAR_LIMIT
    assert "…" in text


def test_validate_hot_take_rejects_unknown_numbers():
    text = f"$BTC вырос на 999% - невероятно.\n\n{post_format.DISCLAIMER}"
    ok, reason = hot_take_generator.validate_hot_take(text, {5.5})
    assert ok is False
    assert "999" in reason


def test_validate_hot_take_rejects_missing_disclaimer():
    text = "$BTC вырос на 5.5% - вот мой тейк."
    ok, reason = hot_take_generator.validate_hot_take(text, {5.5})
    assert ok is False
    assert "дисклеймер" in reason


def test_validate_hot_take_rejects_over_length():
    text = "А" * 350 + f"\n\n{post_format.DISCLAIMER}"
    ok, reason = hot_take_generator.validate_hot_take(text, set())
    assert ok is False
    assert "лимита" in reason


def test_generate_hot_take_injects_hook_mode_into_prompt(monkeypatch):
    monkeypatch.setattr(hot_take_generator, "calc_theme_stats", lambda theme: _fake_single_stats())
    captured_system_prompts = []
    monkeypatch.setattr(
        hot_take_generator, "call_groq",
        lambda system, user, **k: captured_system_prompts.append(system) or "Тезис против консенсуса.",
    )

    hot_take_generator.generate_hot_take("BTC", hook_mode="technician")

    assert hot_take_generator.HOOK_MODES["technician"] in captured_system_prompts[0]


def test_generate_hot_take_without_hook_mode_uses_neutral_prompt(monkeypatch):
    monkeypatch.setattr(hot_take_generator, "calc_theme_stats", lambda theme: _fake_single_stats())
    captured_system_prompts = []
    monkeypatch.setattr(
        hot_take_generator, "call_groq",
        lambda system, user, **k: captured_system_prompts.append(system) or "Тезис против консенсуса.",
    )

    hot_take_generator.generate_hot_take("BTC")

    for mode_text in hot_take_generator.HOOK_MODES.values():
        assert mode_text not in captured_system_prompts[0]


def test_validate_hot_take_passes_valid_text():
    text = f"$BTC вырос на 5.5% - и это совсем не то, что кажется.\n\n{post_format.DISCLAIMER}"
    ok, reason = hot_take_generator.validate_hot_take(text, {5.5})
    assert ok is True, reason


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
