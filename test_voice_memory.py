#!/usr/bin/env python3
"""
Тесты voice_memory.py: анти-повтор зачинов, честная преемственность по
теме, и изоляция памяти между post_type (okx_orbit vs bybit_byx не
должны видеть историю друг друга).
"""
import time

import pytest

import config
import state_store
import voice_memory


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    """Каждый тест пишет в свой временный sqlite-файл, а не в реальный
    bot_state.db рядом с кодом."""
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "test_bot_state.db")


def test_anti_repeat_block_empty_when_no_history():
    assert voice_memory.anti_repeat_block("okx_orbit_test_empty") == ""


def test_record_post_then_anti_repeat_block_contains_opener():
    post_type = "okx_orbit_test_repeat"
    voice_memory.record_post(post_type, "BTC резко пробил уровень. Дальше текст.")

    block = voice_memory.anti_repeat_block(post_type)

    assert "BTC резко пробил уровень." in block


def test_recent_openers_capped_at_max(monkeypatch):
    post_type = "okx_orbit_test_cap"
    monkeypatch.setattr(voice_memory, "_RECENT_OPENERS_MAX", 3)

    for i in range(5):
        voice_memory.record_post(post_type, f"Зачин номер {i}.")

    openers = state_store.get_recent_openers(post_type)
    assert len(openers) == 3
    # Должны остаться самые последние, а не первые
    assert "Зачин номер 4." in openers
    assert "Зачин номер 0." not in openers


def test_continuity_block_empty_without_history():
    assert voice_memory.continuity_block("okx_orbit_test_cont_empty", "BTC", "$BTC") == ""
    assert voice_memory.continuity_pct("okx_orbit_test_cont_empty", "BTC") is None


def test_continuity_block_present_after_recording_with_theme():
    post_type = "okx_orbit_test_cont"
    voice_memory.record_post(post_type, "BTC откатился от максимума.", theme="BTC", pct=5.5)

    block = voice_memory.continuity_block(post_type, "BTC", "$BTC")

    assert "5.5%" in block
    assert voice_memory.continuity_pct(post_type, "BTC") == 5.5


def test_continuity_block_expires_after_max_age(monkeypatch):
    post_type = "okx_orbit_test_cont_expired"
    voice_memory.record_post(post_type, "ETH вырос.", theme="ETH", pct=3.0)

    # Подделываем время записи так, будто она была 30 дней назад
    history = state_store.get_theme_post_history(post_type)
    history["ETH"]["timestamp"] = time.time() - 30 * 86400
    state_store.set_theme_post_history(post_type, history)

    assert voice_memory.continuity_block(post_type, "ETH", "$ETH") == ""
    assert voice_memory.continuity_pct(post_type, "ETH") is None


def test_post_type_isolation_okx_vs_bybit():
    """OKX и Bybit не должны видеть историю друг друга - разные
    площадки, разная аудитория, совпадение зачинов между ними не
    проблема."""
    voice_memory.record_post("okx_orbit_test_iso", "Только для OKX.", theme="BTC", pct=1.0)

    assert "Только для OKX." in voice_memory.anti_repeat_block("okx_orbit_test_iso")
    assert voice_memory.anti_repeat_block("bybit_byx_test_iso") == ""
    assert voice_memory.continuity_pct("bybit_byx_test_iso", "BTC") is None
