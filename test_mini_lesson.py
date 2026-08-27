#!/usr/bin/env python3
"""
Тесты mini_lesson_generator.py: генерация (LLM замокан через
mini_lesson_generator.call_groq) и валидация (дисклеймер/лимит длины/
запрет на утверждения о конкретной цене реального актива).
"""
import mini_lesson_generator
import post_format


def test_pick_topic_avoids_last_used():
    topics = list(mini_lesson_generator.TOPICS.keys())
    for _ in range(20):
        chosen = mini_lesson_generator.pick_topic(topics[0])
        assert chosen != topics[0]


def test_pick_topic_handles_unknown_last():
    # last_topic не из TOPICS (например, первый запуск, None) - не должно падать.
    chosen = mini_lesson_generator.pick_topic(None)
    assert chosen in mini_lesson_generator.TOPICS


def test_generate_mini_lesson_unknown_topic_returns_none():
    result = mini_lesson_generator.generate_mini_lesson("несуществующая_тема")
    assert result is None


def test_generate_mini_lesson_includes_disclaimer(monkeypatch):
    monkeypatch.setattr(
        mini_lesson_generator, "call_groq",
        lambda *a, **k: "RSI показывает, насколько резко актив рос или падал в последнее время."
    )
    text = mini_lesson_generator.generate_mini_lesson("rsi")
    assert text is not None
    assert post_format.DISCLAIMER in text
    assert "RSI показывает" in text


def test_generate_mini_lesson_returns_none_on_empty_response(monkeypatch):
    monkeypatch.setattr(mini_lesson_generator, "call_groq", lambda *a, **k: "   ")
    result = mini_lesson_generator.generate_mini_lesson("rsi")
    assert result is None


def test_generate_mini_lesson_truncates_overly_long_response(monkeypatch):
    monkeypatch.setattr(mini_lesson_generator, "call_groq", lambda *a, **k: "Б" * 500)
    text = mini_lesson_generator.generate_mini_lesson("divergence")
    assert text is not None
    assert len(text) <= post_format.BLUESKY_CHAR_LIMIT
    assert "…" in text


def test_validate_mini_lesson_rejects_missing_disclaimer():
    ok, reason = mini_lesson_generator.validate_mini_lesson("Просто текст без дисклеймера про RSI.")
    assert ok is False
    assert "дисклеймер" in reason


def test_validate_mini_lesson_rejects_over_length():
    text = "А" * 350 + f"\n\n{post_format.DISCLAIMER}"
    ok, reason = mini_lesson_generator.validate_mini_lesson(text)
    assert ok is False
    assert "лимита" in reason


def test_validate_mini_lesson_rejects_specific_price_claim():
    text = f"$BTC сейчас на уровне 65000, и вот почему это важно.\n\n{post_format.DISCLAIMER}"
    ok, reason = mini_lesson_generator.validate_mini_lesson(text)
    assert ok is False
    assert "цене" in reason


def test_validate_mini_lesson_allows_bare_cashtag_without_price():
    text = f"$BTC часто используют как пример при объяснении RSI.\n\n{post_format.DISCLAIMER}"
    ok, reason = mini_lesson_generator.validate_mini_lesson(text)
    assert ok is True, reason


def test_validate_mini_lesson_passes_valid_text():
    text = (
        f"Дивергенция - когда цена растёт, а индикатор уже нет. "
        f"Это часто сигнал, что импульс выдыхается.\n\n{post_format.DISCLAIMER}"
    )
    ok, reason = mini_lesson_generator.validate_mini_lesson(text)
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
