#!/usr/bin/env python3
"""
Тесты cliche_filter.py - пост-фильтр на клише поверх voice_guidelines.
BANNED_PHRASES.
"""
import cliche_filter


def test_check_cliches_passes_clean_text():
    text = "$BTC вырос на 5% за ночь - и это ещё не предел."
    ok, found = cliche_filter.check_cliches(text)
    assert ok is True
    assert found == []


def test_check_cliches_detects_single_phrase():
    text = "Кроме того, рынок продолжает расти."
    ok, found = cliche_filter.check_cliches(text)
    assert ok is False
    assert "кроме того" in found


def test_check_cliches_case_insensitive():
    text = "СТОИТ ОТМЕТИТЬ, что ситуация меняется."
    ok, found = cliche_filter.check_cliches(text)
    assert ok is False
    assert "стоит отметить" in found


def test_check_cliches_detects_multiple_phrases():
    text = "Как известно, рынок волатилен. Таким образом, будьте осторожны."
    ok, found = cliche_filter.check_cliches(text)
    assert ok is False
    assert "как известно" in found
    assert "таким образом" in found


def test_check_cliches_all_banned_phrases_are_detectable():
    # Каждая фраза из словаря реально детектируется, когда встречается в тексте -
    # защита от опечатки при добавлении новой фразы в voice_guidelines.
    import voice_guidelines
    for phrase in voice_guidelines.BANNED_PHRASES:
        ok, found = cliche_filter.check_cliches(f"Текст с фразой '{phrase}' внутри.")
        assert ok is False, f"Фраза не задетектирована: {phrase!r}"
        assert phrase in found


def test_all_validate_functions_reject_cliches():
    """Интеграционная проверка: каждая из 12 validate-функций проекта
    реально подключена к cliche_filter (а не просто импортирует модуль,
    забыв вызвать check_cliches - или, как в реальном баге, вызывает его,
    забыв импортировать модуль). Для каждой строим МИНИМАЛЬНО валидный
    (по остальным правилам) вход с одной клишированной фразой и
    проверяем, что причина отказа - именно клише."""
    import accuracy_report_generator
    import article_generator
    import hot_take_generator
    import index_signal_generator
    import loss_review_generator
    import mini_lesson_generator
    import opinion_generator
    import telegram_extended
    import telegram_glossary
    import treasury_generator
    import validator
    import volatility_alert
    from post_format import DISCLAIMER
    from signal_parser import RsiSignal

    cliche_tail = "Кроме того, это важно."

    def _assert_rejected_for_cliche(ok, reason, label):
        assert ok is False, f"{label}: ожидался отказ из-за клише, но прошло валидацию"
        assert "ИИ-фраз" in reason or "клише" in reason, f"{label}: отказ по другой причине: {reason}"

    signal = RsiSignal(
        ticker="BEAT", timeframe="15m", strategy="RSI + Bollinger Touch",
        direction="Шорт", current_price="2.225", rsi_now="81.74", score="89",
        quality="Conservative", entry_low="2.205", entry_high="2.2178",
        invalidation="2.2371", target="2.1729", change_24h="+35.67%",
        volume="57.67M", rsi_live="82.64", created_at="2026-06-23 22:44:59 EEST",
        description="desc", raw_text="raw",
    )
    text_with_all_levels = (
        f"Хук. {cliche_tail}\n\n2.205 2.2178 2.2371 2.1729 81.74 89\n\n{DISCLAIMER}"
    )
    _assert_rejected_for_cliche(*validator.validate_post_text(text_with_all_levels, signal), "validator.validate_post_text")

    image_text = f"Хук без чисел. {cliche_tail}\n\n{DISCLAIMER}"
    _assert_rejected_for_cliche(*validator.validate_image_post_text(image_text), "validator.validate_image_post_text")

    ctx_text = f"Текст контекста. {cliche_tail}"
    _assert_rejected_for_cliche(
        *telegram_extended.validate_extended_context(ctx_text, {81.74, 89.0, 100.0}),
        "telegram_extended.validate_extended_context",
    )

    spike = {"pct": 6.0, "direction": "up", "window_hours": 3}
    emergency_text = f"🚨 $BTC вырос на 6.0% за 3 часа. {cliche_tail}\n\n{DISCLAIMER}"
    _assert_rejected_for_cliche(
        *volatility_alert.validate_emergency_post(emergency_text, spike),
        "volatility_alert.validate_emergency_post",
    )

    opinion_text = f"$BTC вырос на 5.5%. {cliche_tail}\n\n{DISCLAIMER}"
    _assert_rejected_for_cliche(
        *opinion_generator.validate_opinion_post_text(opinion_text, {5.5}),
        "opinion_generator.validate_opinion_post_text",
    )

    hot_take_text = f"$BTC вырос на 5.5%. {cliche_tail}\n\n{DISCLAIMER}"
    _assert_rejected_for_cliche(
        *hot_take_generator.validate_hot_take(hot_take_text, {5.5}),
        "hot_take_generator.validate_hot_take",
    )

    lesson_text = f"RSI показывает силу движения. {cliche_tail}\n\n{DISCLAIMER}"
    _assert_rejected_for_cliche(
        *mini_lesson_generator.validate_mini_lesson(lesson_text),
        "mini_lesson_generator.validate_mini_lesson",
    )

    topic = telegram_glossary.get_topic(0)
    glossary_text = f"RSI считается на 14 периодах. {cliche_tail}\n\n{DISCLAIMER}"
    _assert_rejected_for_cliche(
        *telegram_glossary.validate_glossary_post(glossary_text, topic),
        "telegram_glossary.validate_glossary_post",
    )

    article_history = [{"score": "89", "change_pct": "5.5%"}]
    article_body = f"Обзор недели. {cliche_tail}\n\n{DISCLAIMER}"
    _assert_rejected_for_cliche(
        *article_generator.validate_article_text("Заголовок", article_body, article_history),
        "article_generator.validate_article_text",
    )

    accuracy_hook = f"Точность за неделю впечатляет. {cliche_tail}"
    _assert_rejected_for_cliche(
        *accuracy_report_generator.validate_accuracy_hook(accuracy_hook, {5.5}),
        "accuracy_report_generator.validate_accuracy_hook",
    )

    loss_hook = f"Разберём, что пошло не так. {cliche_tail}"
    _assert_rejected_for_cliche(
        *loss_review_generator.validate_loss_review_hook(loss_hook, {5.5}),
        "loss_review_generator.validate_loss_review_hook",
    )

    index_hook = f"Отличный момент для пересмотра доли. {cliche_tail}"
    _assert_rejected_for_cliche(
        *index_signal_generator._validate_hook(index_hook),
        "index_signal_generator._validate_hook",
    )

    treasury_hook = f"Индекс сегодня выглядит интересно. {cliche_tail}"
    _assert_rejected_for_cliche(
        *treasury_generator.validate_treasury_hook(treasury_hook, {5.5}),
        "treasury_generator.validate_treasury_hook",
    )


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
