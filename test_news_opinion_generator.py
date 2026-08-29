#!/usr/bin/env python3
"""
Тесты news_opinion_generator.py: гейт частоты (~раз в 3 дня), выбор
непрочитанной новости, и САМОЕ ВАЖНОЕ - детекция копипасты (если LLM
дословно скопирует кусок источника вместо пересказа своими словами).
"""
import time

import pytest

import config
import news_channel_reader
import news_opinion_generator
import post_format
import state_store


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "test_bot_state.db")


def test_is_news_window_open_true_when_never_posted():
    assert news_opinion_generator.is_news_window_open("okx_orbit_test_never") is True


def test_is_news_window_open_false_right_after_posting():
    post_type = "okx_orbit_test_justposted"
    state_store.set_last_news_take_time(post_type)

    assert news_opinion_generator.is_news_window_open(post_type) is False


def test_is_news_window_open_true_after_enough_days():
    post_type = "okx_orbit_test_old"
    old_ts = time.time() - (news_opinion_generator.MIN_DAYS_BETWEEN_NEWS_POSTS + 1) * 86400
    state_store.set_last_news_take_time(post_type, old_ts)

    assert news_opinion_generator.is_news_window_open(post_type) is True


def test_pick_unused_news_post_skips_already_used(monkeypatch):
    post_type = "okx_orbit_test_unused"
    fake_posts = [
        news_channel_reader.NewsPost(post_id=3, text="Третья новость " * 10),
        news_channel_reader.NewsPost(post_id=2, text="Вторая новость " * 10),
        news_channel_reader.NewsPost(post_id=1, text="Первая новость " * 10),
    ]
    monkeypatch.setattr(news_channel_reader, "fetch_recent_posts", lambda limit=10: fake_posts)
    state_store.set_used_news_post_ids(post_type, [3])

    picked = news_opinion_generator.pick_unused_news_post(post_type)

    assert picked.post_id == 2


def test_pick_unused_news_post_returns_none_when_all_used(monkeypatch):
    post_type = "okx_orbit_test_allused"
    fake_posts = [news_channel_reader.NewsPost(post_id=1, text="Новость " * 10)]
    monkeypatch.setattr(news_channel_reader, "fetch_recent_posts", lambda limit=10: fake_posts)
    state_store.set_used_news_post_ids(post_type, [1])

    assert news_opinion_generator.pick_unused_news_post(post_type) is None


def test_has_verbatim_overlap_detects_copied_sentence():
    source = "Base достигла первой стадии децентрализации по сценарию Бутерина сообщили разработчики проекта вчера"
    generated = f"Интересная новость: {source[:80]} - и это важное событие для индустрии."

    assert news_opinion_generator._has_verbatim_overlap(generated, source) is True


def test_has_verbatim_overlap_false_for_genuine_paraphrase():
    source = "Base достигла первой стадии децентрализации по сценарию Бутерина сообщили разработчики проекта"
    generated = "По сути, это ещё один шаг к тому, чтобы L2-решения были менее зависимы от центральной команды."

    assert news_opinion_generator._has_verbatim_overlap(generated, source) is False


def test_validate_news_take_text_rejects_copypaste():
    source = "Base достигла первой стадии децентрализации по сценарию Бутерина сообщили разработчики проекта вчера"
    text = f"{source[:90]} - вот что реально важно.\n\n{post_format.DISCLAIMER}"

    ok, reason = news_opinion_generator.validate_news_take_text(text, source)

    assert ok is False
    assert "копипаст" in reason.lower()


def test_validate_news_take_text_passes_genuine_opinion():
    source = "Base достигла первой стадии децентрализации по сценарию Бутерина сообщили разработчики проекта"
    text = (
        f"Если L2-решения и правда идут в сторону меньшей зависимости от команды разработки, "
        f"это хороший сигнал для всей экосистемы Ethereum в долгосрочной перспективе.\n\n"
        f"{post_format.DISCLAIMER}"
    )

    ok, reason = news_opinion_generator.validate_news_take_text(text, source)

    assert ok is True, reason


def test_validate_news_take_text_rejects_missing_disclaimer():
    ok, reason = news_opinion_generator.validate_news_take_text("Просто мнение без дисклеймера.", "Источник")

    assert ok is False
    assert "дисклеймер" in reason.lower()


def test_mark_news_post_used_updates_state():
    post_type = "okx_orbit_test_mark"
    news_opinion_generator.mark_news_post_used(post_type, 42)

    assert 42 in state_store.get_used_news_post_ids(post_type)
    assert news_opinion_generator.is_news_window_open(post_type) is False


def test_generate_news_take_returns_none_without_unread_posts(monkeypatch):
    post_type = "okx_orbit_test_gen_none"
    monkeypatch.setattr(news_channel_reader, "fetch_recent_posts", lambda limit=10: [])

    assert news_opinion_generator.generate_news_take(post_type) is None
