#!/usr/bin/env python3
"""
Тесты telegram_glossary.py: последовательная ротация тем, генерация
(LLM замокан) и валидация чисел/дисклеймера.
"""
import post_format
import telegram_glossary


def test_get_topic_sequential_with_wraparound():
    n = len(telegram_glossary.TOPICS)
    assert telegram_glossary.get_topic(0) == telegram_glossary.TOPICS[0]
    assert telegram_glossary.get_topic(n - 1) == telegram_glossary.TOPICS[n - 1]
    # После последней темы - снова первая (по модулю), не IndexError.
    assert telegram_glossary.get_topic(n) == telegram_glossary.TOPICS[0]
    assert telegram_glossary.get_topic(n + 3) == telegram_glossary.TOPICS[3]


def test_all_topics_have_unique_keys():
    keys = [t["key"] for t in telegram_glossary.TOPICS]
    assert len(keys) == len(set(keys))


def test_generate_glossary_post_includes_title_and_disclaimer(monkeypatch):
    monkeypatch.setattr(
        telegram_glossary, "call_groq",
        lambda *a, **k: "RSI считается на 14 периодах, а пороги - 70 и 30."
    )
    topic = telegram_glossary.get_topic(0)  # rsi_basics
    text = telegram_glossary.generate_glossary_post(topic)

    assert text is not None
    assert topic["title"] in text
    assert post_format.DISCLAIMER in text
    assert "14 периодах" in text


def test_generate_glossary_post_returns_none_on_empty_response(monkeypatch):
    monkeypatch.setattr(telegram_glossary, "call_groq", lambda *a, **k: "   ")
    topic = telegram_glossary.get_topic(0)
    assert telegram_glossary.generate_glossary_post(topic) is None


def test_validate_glossary_post_accepts_topic_constants():
    topic = telegram_glossary.get_topic(0)  # rsi_basics: {14, 70, 30}
    text = f"RSI считается на 14 периодах, пороги 70 и 30.\n\n{post_format.DISCLAIMER}"
    ok, reason = telegram_glossary.validate_glossary_post(text, topic)
    assert ok is True, reason


def test_validate_glossary_post_accepts_small_conversational_numbers():
    topic = telegram_glossary.get_topic(4)  # risk_reward
    text = f"Представь сделку с соотношением 1 к 3 - это совсем другая математика.\n\n{post_format.DISCLAIMER}"
    ok, reason = telegram_glossary.validate_glossary_post(text, topic)
    assert ok is True, reason


def test_validate_glossary_post_rejects_wrong_constant():
    topic = telegram_glossary.get_topic(0)  # rsi_basics: реальный период - 14
    text = f"RSI считается на 21 периоде, что в корне неверно.\n\n{post_format.DISCLAIMER}"
    ok, reason = telegram_glossary.validate_glossary_post(text, topic)
    assert ok is False
    assert "21" in reason


def test_validate_glossary_post_rejects_missing_disclaimer():
    topic = telegram_glossary.get_topic(0)
    text = "RSI считается на 14 периодах."
    ok, reason = telegram_glossary.validate_glossary_post(text, topic)
    assert ok is False
    assert "дисклеймер" in reason


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
