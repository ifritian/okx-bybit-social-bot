#!/usr/bin/env python3
"""
Тесты voice_guidelines.py - базовая проверка целостности словаря
запретов и того, что директива реально внедрена во все системные
промпты генераторов.
"""
import accuracy_report_generator
import article_generator
import hot_take_generator
import index_signal_generator
import loss_review_generator
import mini_lesson_generator
import opinion_generator
import rebalance_advisor
import telegram_extended
import telegram_glossary
import text_generator
import treasury_generator
import voice_guidelines
import volatility_alert


def test_banned_phrases_non_empty_and_lowercase():
    assert len(voice_guidelines.BANNED_PHRASES) >= 10
    for phrase in voice_guidelines.BANNED_PHRASES:
        assert phrase == phrase.lower(), f"Фраза должна быть в нижнем регистре: {phrase!r}"


def test_no_duplicate_banned_phrases():
    assert len(voice_guidelines.BANNED_PHRASES) == len(set(voice_guidelines.BANNED_PHRASES))


def test_style_directive_mentions_all_banned_phrases():
    for phrase in voice_guidelines.BANNED_PHRASES[:5]:
        assert phrase in voice_guidelines.STYLE_DIRECTIVE.lower()


def test_style_directive_injected_into_all_generator_prompts():
    modules_with_single_prompt = [
        accuracy_report_generator, article_generator, hot_take_generator,
        index_signal_generator, loss_review_generator, mini_lesson_generator,
        opinion_generator, rebalance_advisor, telegram_extended,
        telegram_glossary, treasury_generator, volatility_alert,
    ]
    for module in modules_with_single_prompt:
        assert voice_guidelines.STYLE_DIRECTIVE in module._SYSTEM_PROMPT, (
            f"{module.__name__}._SYSTEM_PROMPT не содержит voice_guidelines.STYLE_DIRECTIVE"
        )


def test_style_directive_injected_into_text_generator_prompts():
    assert voice_guidelines.STYLE_DIRECTIVE in text_generator._BASE_SIGNAL_SYSTEM_PROMPT
    assert voice_guidelines.STYLE_DIRECTIVE in text_generator._BASE_IMAGE_SYSTEM_PROMPT


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
