#!/usr/bin/env python3
"""
Тесты opinion_generator.py: ротация темы, генерация (LLM замокан),
проверка валидации и внедрение "авторского голоса" (hook_mode) в
системный промпт - раньше пост-мнение всегда звучал одним нейтральным
голосом, теперь может использовать тот же словарь HOOK_MODES, что и
валютные сигналы (см. post_format.HOOK_MODES).
"""
import opinion_generator
import post_format


def _fake_single_stats(pct=5.5):
    return {"single": {"pct": pct, "amplitude_pct": 7.2, "current_price": 65000.0}}


def test_pick_theme_avoids_last_used():
    themes = list(opinion_generator.THEMES.keys())
    for _ in range(30):
        chosen = opinion_generator.pick_theme(themes[0])
        assert chosen != themes[0]


def test_pick_theme_handles_unknown_last():
    chosen = opinion_generator.pick_theme(None)
    assert chosen in opinion_generator.THEMES


def test_generate_opinion_post_returns_none_when_no_data(monkeypatch):
    monkeypatch.setattr(opinion_generator, "calc_theme_stats", lambda theme: None)
    assert opinion_generator.generate_opinion_post("BTC") is None


def test_generate_opinion_post_includes_disclaimer(monkeypatch):
    monkeypatch.setattr(opinion_generator, "calc_theme_stats", lambda theme: _fake_single_stats())
    monkeypatch.setattr(opinion_generator, "call_groq", lambda *a, **k: "Рынок сегодня ведёт себя интересно.")

    result = opinion_generator.generate_opinion_post("BTC")

    assert result is not None
    text, allowed_numbers, headline_pct = result
    assert post_format.DISCLAIMER in text
    assert 5.5 in allowed_numbers
    assert headline_pct == 5.5


def test_generate_opinion_post_injects_hook_mode_into_prompt(monkeypatch):
    monkeypatch.setattr(opinion_generator, "calc_theme_stats", lambda theme: _fake_single_stats())
    captured_system_prompts = []
    monkeypatch.setattr(
        opinion_generator, "call_groq",
        lambda system, user, **k: captured_system_prompts.append(system) or "Личное наблюдение о рынке.",
    )

    opinion_generator.generate_opinion_post("BTC", hook_mode="skeptic")

    assert opinion_generator.HOOK_MODES["skeptic"] in captured_system_prompts[0]


def test_generate_opinion_post_without_hook_mode_uses_neutral_prompt(monkeypatch):
    monkeypatch.setattr(opinion_generator, "calc_theme_stats", lambda theme: _fake_single_stats())
    captured_system_prompts = []
    monkeypatch.setattr(
        opinion_generator, "call_groq",
        lambda system, user, **k: captured_system_prompts.append(system) or "Личное наблюдение о рынке.",
    )

    opinion_generator.generate_opinion_post("BTC")

    for mode_text in opinion_generator.HOOK_MODES.values():
        assert mode_text not in captured_system_prompts[0]


def test_generate_opinion_post_falls_back_on_empty_llm_response(monkeypatch):
    monkeypatch.setattr(opinion_generator, "calc_theme_stats", lambda theme: _fake_single_stats())
    monkeypatch.setattr(opinion_generator, "call_groq", lambda *a, **k: "   ")

    result = opinion_generator.generate_opinion_post("BTC")

    # Не None - в отличие от hot_take/mini_lesson, opinion_generator
    # подставляет нейтральный хук-заглушку вместо пропуска окна целиком.
    assert result is not None
    text, _, _ = result
    assert post_format.DISCLAIMER in text


def test_validate_opinion_post_text_rejects_unknown_numbers():
    text = f"$BTC вырос на 999% - невероятно.\n\n{post_format.DISCLAIMER}"
    ok, reason = opinion_generator.validate_opinion_post_text(text, {5.5})
    assert ok is False
    assert "999" in reason


def test_validate_opinion_post_text_rejects_cliche():
    text = f"$BTC вырос на 5.5%. Кроме того, это важно.\n\n{post_format.DISCLAIMER}"
    ok, reason = opinion_generator.validate_opinion_post_text(text, {5.5})
    assert ok is False
    assert "ИИ-фраз" in reason


def test_validate_opinion_post_text_passes_valid_text():
    text = f"$BTC вырос на 5.5% - и рынок явно не ожидал такого разворота.\n\n{post_format.DISCLAIMER}"
    ok, reason = opinion_generator.validate_opinion_post_text(text, {5.5})
    assert ok is True, reason


def test_validate_opinion_post_text_allows_48_hours_and_2_days_mentions():
    """Регрессия: _build_user_prompt сам явно просит модель писать период
    времени как "48 часов" или "2 дня" - раньше эти числа не были в
    allowed_numbers, из-за чего валидатор бpaковал честно выполнивший
    инструкцию ответ."""
    text = (
        f"$BTC вырос на 5.5% за последние 48 часов - и рынок явно не "
        f"ожидал такого разворота.\n\n{post_format.DISCLAIMER}"
    )
    ok, reason = opinion_generator.validate_opinion_post_text(text, {5.5, 48.0, 2.0})
    assert ok is True, reason

    text_days = (
        f"$BTC вырос на 5.5% за последние 2 дня - и рынок явно не "
        f"ожидал такого разворота.\n\n{post_format.DISCLAIMER}"
    )
    ok, reason = opinion_generator.validate_opinion_post_text(text_days, {5.5, 48.0, 2.0})
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
