#!/usr/bin/env python3
"""
Тесты voice_memory.py: анти-повтор зачинов (record_post/anti_repeat_block)
и честная преемственность по теме (continuity_block) - особое внимание
тому, что continuity_block НИКОГДА не придумывает историю, которой нет
в queue_manager (только реально сохранённые факты, с ограничением по
давности).
"""
import time

import queue_manager
import voice_memory


def test_record_post_adds_opener_to_recent_openers(monkeypatch):
    monkeypatch.setattr(queue_manager, "get_recent_openers", lambda: [])
    saved = {}
    monkeypatch.setattr(queue_manager, "set_recent_openers", lambda openers: saved.__setitem__("openers", openers))
    monkeypatch.setattr(queue_manager, "get_theme_post_history", lambda: {})
    monkeypatch.setattr(queue_manager, "set_theme_post_history", lambda h: None)

    voice_memory.record_post("Рынок сегодня разошёлся не на шутку. Вот детали.")

    assert saved["openers"] == ["Рынок сегодня разошёлся не на шутку."]


def test_record_post_caps_recent_openers_length(monkeypatch):
    existing = [f"Зачин {i}." for i in range(voice_memory._RECENT_OPENERS_MAX)]
    monkeypatch.setattr(queue_manager, "get_recent_openers", lambda: list(existing))
    saved = {}
    monkeypatch.setattr(queue_manager, "set_recent_openers", lambda openers: saved.__setitem__("openers", openers))
    monkeypatch.setattr(queue_manager, "get_theme_post_history", lambda: {})
    monkeypatch.setattr(queue_manager, "set_theme_post_history", lambda h: None)

    voice_memory.record_post("Новый зачин.")

    assert len(saved["openers"]) == voice_memory._RECENT_OPENERS_MAX
    assert saved["openers"][-1] == "Новый зачин."


def test_record_post_without_theme_does_not_touch_theme_history(monkeypatch):
    monkeypatch.setattr(queue_manager, "get_recent_openers", lambda: [])
    monkeypatch.setattr(queue_manager, "set_recent_openers", lambda openers: None)
    theme_history_calls = []
    monkeypatch.setattr(queue_manager, "set_theme_post_history", lambda h: theme_history_calls.append(h))

    voice_memory.record_post("Просто текст без темы.")

    assert theme_history_calls == []


def test_record_post_with_theme_updates_theme_history(monkeypatch):
    monkeypatch.setattr(queue_manager, "get_recent_openers", lambda: [])
    monkeypatch.setattr(queue_manager, "set_recent_openers", lambda openers: None)
    monkeypatch.setattr(queue_manager, "get_theme_post_history", lambda: {})
    saved = {}
    monkeypatch.setattr(queue_manager, "set_theme_post_history", lambda h: saved.update(h))

    voice_memory.record_post("BTC выглядит перегретым.", theme="BTC", pct=5.5)

    assert "BTC" in saved
    assert saved["BTC"]["pct"] == 5.5
    assert saved["BTC"]["stance_summary"] == "BTC выглядит перегретым."


def test_anti_repeat_block_empty_without_history(monkeypatch):
    monkeypatch.setattr(queue_manager, "get_recent_openers", lambda: [])
    assert voice_memory.anti_repeat_block() == ""


def test_anti_repeat_block_lists_recent_openers(monkeypatch):
    monkeypatch.setattr(queue_manager, "get_recent_openers", lambda: ["Зачин раз.", "Зачин два."])
    block = voice_memory.anti_repeat_block()
    assert "Зачин раз." in block
    assert "Зачин два." in block
    assert "НЕ начинай" in block


def test_anti_repeat_block_limits_to_recent_n(monkeypatch):
    openers = [f"Зачин {i}." for i in range(20)]
    monkeypatch.setattr(queue_manager, "get_recent_openers", lambda: openers)
    block = voice_memory.anti_repeat_block()
    # Только последние _RECENT_OPENERS_IN_PROMPT штук, не всю историю целиком.
    assert "Зачин 0." not in block
    assert f"Зачин {len(openers) - 1}." in block


def test_continuity_block_empty_when_no_history(monkeypatch):
    monkeypatch.setattr(queue_manager, "get_theme_post_history", lambda: {})
    assert voice_memory.continuity_block("BTC", "$BTC") == ""


def test_continuity_block_empty_when_too_old(monkeypatch):
    old_timestamp = time.time() - (voice_memory._CONTINUITY_MAX_AGE_DAYS + 1) * 86400
    monkeypatch.setattr(
        queue_manager, "get_theme_post_history",
        lambda: {"BTC": {"pct": 5.5, "stance_summary": "Старое мнение.", "timestamp": old_timestamp}},
    )
    assert voice_memory.continuity_block("BTC", "$BTC") == ""


def test_continuity_block_includes_real_stored_facts_only(monkeypatch):
    recent_timestamp = time.time() - 2 * 86400
    monkeypatch.setattr(
        queue_manager, "get_theme_post_history",
        lambda: {"BTC": {"pct": 5.5, "stance_summary": "Рынок выглядит перегретым.", "timestamp": recent_timestamp}},
    )

    block = voice_memory.continuity_block("BTC", "$BTC")

    assert "Рынок выглядит перегретым." in block
    assert "5.5" in block
    assert "2 дн." in block
    # Явный запрет придумывать другую историю - страховка от галлюцинации.
    assert "Не выдумывай" in block


def test_continuity_block_only_returns_data_for_requested_theme(monkeypatch):
    recent_timestamp = time.time() - 1 * 86400
    monkeypatch.setattr(
        queue_manager, "get_theme_post_history",
        lambda: {"ETH": {"pct": 3.0, "stance_summary": "Про ETH было такое.", "timestamp": recent_timestamp}},
    )

    # Спрашиваем про BTC, а в истории только ETH - не должно подсунуть чужую тему.
    block = voice_memory.continuity_block("BTC", "$BTC")

    assert block == ""


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
