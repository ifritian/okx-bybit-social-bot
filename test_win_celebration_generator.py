#!/usr/bin/env python3
"""
Тесты win_celebration_generator.py: генерация хука (LLM замокана через
groq_client.call_groq, как и в остальных генераторах - см. test_hot_take.py)
и его валидация (хук не должен содержать ни одного числа - весь
фактический результат сделки добавляется отдельно кодом, см.
post_format.assemble_win_celebration_post), плюс сборка финального поста.
"""
import post_format
import win_celebration_generator


def _make_win_record(**overrides):
    base = {
        "ticker": "SOL",
        "direction": "long",
        "strategy": "RSI + Bollinger Touch",
        "entry": 150.0,
        "exit_price": 160.5,
        "pnl_pct": 7.0,
        "hours_to_close": 5.25,
        "result": "win",
    }
    base.update(overrides)
    return base


def test_pick_angle_avoids_repeating_last():
    keys = list(win_celebration_generator._ANGLES.keys())
    for last in keys:
        chosen = win_celebration_generator.pick_angle(last)
        assert chosen != last
        assert chosen in keys


def test_pick_angle_handles_unknown_last_angle():
    # last_angle=None (первый пост вообще) - не должно падать, просто
    # случайный выбор из всех.
    chosen = win_celebration_generator.pick_angle(None)
    assert chosen in win_celebration_generator._ANGLES


def test_generate_win_celebration_hook_returns_text(monkeypatch):
    monkeypatch.setattr(win_celebration_generator, "call_groq", lambda *a, **k: "Невероятно, снова получилось!")

    hook = win_celebration_generator.generate_win_celebration_hook("pure_joy")

    assert hook == "Невероятно, снова получилось!"


def test_generate_win_celebration_hook_returns_none_when_too_short(monkeypatch):
    monkeypatch.setattr(win_celebration_generator, "call_groq", lambda *a, **k: "Ура")
    assert win_celebration_generator.generate_win_celebration_hook("pure_joy") is None


def test_validate_win_celebration_hook_rejects_numbers():
    ok, reason = win_celebration_generator.validate_win_celebration_hook("Заработали 7% на этой сделке!")
    assert not ok
    assert "цифры" in reason


def test_validate_win_celebration_hook_rejects_mixed_language():
    ok, reason = win_celebration_generator.validate_win_celebration_hook("Такой amazing feeling прямо сейчас!")
    assert not ok
    assert "английск" in reason


def test_validate_win_celebration_hook_rejects_ai_cliches():
    ok, reason = win_celebration_generator.validate_win_celebration_hook("Стоит отметить, что сделка закрылась удачно.")
    assert not ok
    assert "канцелярит" in reason


def test_validate_win_celebration_hook_accepts_clean_hype_text():
    ok, reason = win_celebration_generator.validate_win_celebration_hook(
        "Не могу поверить, но снова получилось! Обожаю такие дни."
    )
    assert ok
    assert reason == ""


def test_assemble_win_celebration_post_contains_hook_and_facts():
    record = _make_win_record()
    text = post_format.assemble_win_celebration_post("Невероятно, снова получилось!", record)

    assert "Невероятно, снова получилось!" in text
    assert "$SOL" in text
    assert "150" in text and "160.5" in text
    assert "+7.00%" in text
    assert post_format.DISCLAIMER in text


def test_win_celebration_lines_use_correct_emoji_and_direction_for_short():
    record = _make_win_record(direction="short", ticker="AVAX")
    lines = post_format.win_celebration_lines(record)

    assert any("🔴" in line and "$AVAX" in line and "шорт" in line for line in lines)


def test_win_celebration_lines_use_correct_emoji_and_direction_for_long():
    record = _make_win_record(direction="long", ticker="SOL")
    lines = post_format.win_celebration_lines(record)

    assert any("🟢" in line and "$SOL" in line and "лонг" in line for line in lines)
