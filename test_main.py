#!/usr/bin/env python3
"""
Тесты main._post_outcome_updates_to_bluesky - форматы "До/После" (реплай
в исходный тред на любой исход) и "Win-reveal" (отдельный пост только на
win). Всё, что реально ходит в сеть (bluesky_publisher.publish_post),
замокано - проверяем только логику принятия решений.
"""
import types

import config
import main


class _FakeResponse:
    def __init__(self, json_data, ok=True):
        self._json = json_data
        self.ok = ok

    def json(self):
        return self._json


def _closed_record(**overrides) -> dict:
    base = dict(
        ticker="BEAT", direction="short", strategy="RSI + Bollinger Touch",
        entry=2.21, stop=2.2371, target=2.1729, result="win",
        exit_price=2.1729, pnl_pct=1.72, mfe_pct=1.9,
        bluesky_ref={"uri": "at://did:plc:abc/app.bsky.feed.post/1", "cid": "bafy1"},
    )
    base.update(overrides)
    return base


def test_skips_entirely_when_bluesky_not_configured(monkeypatch):
    monkeypatch.setattr(config, "BLUESKY_HANDLE", "")
    monkeypatch.setattr(config, "BLUESKY_APP_PASSWORD", "")
    calls = []
    monkeypatch.setattr(main.bluesky_publisher, "publish_post", lambda *a, **k: calls.append((a, k)))

    main._post_outcome_updates_to_bluesky([_closed_record()])

    assert calls == []


def test_win_posts_both_reply_and_win_reveal(monkeypatch):
    monkeypatch.setattr(config, "BLUESKY_HANDLE", "alexei.bsky.social")
    monkeypatch.setattr(config, "BLUESKY_APP_PASSWORD", "app-pass")
    calls = []
    monkeypatch.setattr(main.bluesky_publisher, "publish_post", lambda text, **k: calls.append((text, k)))

    main._post_outcome_updates_to_bluesky([_closed_record(result="win")])

    # 2 вызова: реплай "До/После" в исходный тред + отдельный Win-reveal.
    assert len(calls) == 2
    reply_text, reply_kwargs = calls[0]
    assert "reply_to" in reply_kwargs and reply_kwargs["reply_to"] is not None
    win_text, win_kwargs = calls[1]
    assert win_kwargs.get("reply_to") is None
    assert "BEAT" in win_text


def test_loss_posts_only_reply_no_win_reveal(monkeypatch):
    monkeypatch.setattr(config, "BLUESKY_HANDLE", "alexei.bsky.social")
    monkeypatch.setattr(config, "BLUESKY_APP_PASSWORD", "app-pass")
    calls = []
    monkeypatch.setattr(main.bluesky_publisher, "publish_post", lambda text, **k: calls.append((text, k)))

    main._post_outcome_updates_to_bluesky([_closed_record(result="loss", pnl_pct=-1.15)])

    assert len(calls) == 1
    assert calls[0][1]["reply_to"] is not None


def test_no_bluesky_ref_skips_reply_but_keeps_win_reveal(monkeypatch):
    monkeypatch.setattr(config, "BLUESKY_HANDLE", "alexei.bsky.social")
    monkeypatch.setattr(config, "BLUESKY_APP_PASSWORD", "app-pass")
    calls = []
    monkeypatch.setattr(main.bluesky_publisher, "publish_post", lambda text, **k: calls.append((text, k)))

    main._post_outcome_updates_to_bluesky([_closed_record(result="win", bluesky_ref=None)])

    # Без bluesky_ref реплай "До/После" отправить некуда - только Win-reveal.
    assert len(calls) == 1
    assert calls[0][1].get("reply_to") is None


def test_one_bad_record_does_not_block_the_rest(monkeypatch):
    monkeypatch.setattr(config, "BLUESKY_HANDLE", "alexei.bsky.social")
    monkeypatch.setattr(config, "BLUESKY_APP_PASSWORD", "app-pass")
    calls = []

    def _fake_publish(text, **kwargs):
        if "OOPS" in text:
            raise main.bluesky_publisher.BlueskyPublishError("boom")
        calls.append(text)

    monkeypatch.setattr(main.bluesky_publisher, "publish_post", _fake_publish)
    monkeypatch.setattr(
        main.post_format, "build_bluesky_outcome_reply",
        lambda record: ("OOPS" if record["ticker"] == "BAD" else "ok-reply", []),
    )

    records = [_closed_record(ticker="BAD", result="loss"), _closed_record(ticker="BEAT", result="loss")]
    main._post_outcome_updates_to_bluesky(records)

    # Ошибка на первой записи не должна помешать обработать вторую.
    assert "ok-reply" in calls


# ============================================================
# main._publish_win_celebrations - формат "Забрали профит!" (Binance
# Square). LLM (win_celebration_generator.call_groq) и публикация
# (binance_publisher.publish_post) замокан - проверяем только логику
# принятия решений (кого публикуем, что пропускаем, что не роняет tick).
# ============================================================

def test_win_celebrations_publishes_only_for_win_records(monkeypatch):
    monkeypatch.setattr(main.win_celebration_generator, "generate_win_celebration_hook", lambda angle: "Невероятно!")
    calls = []
    monkeypatch.setattr(main.binance_publisher, "publish_post", lambda text, **k: calls.append(text))

    records = [
        _closed_record(ticker="LOSS1", result="loss", hours_to_close=3.0),
        _closed_record(ticker="WIN1", result="win", hours_to_close=4.5),
        _closed_record(ticker="TIMEOUT1", result="timeout", hours_to_close=48.0),
    ]
    main._publish_win_celebrations(records)

    assert len(calls) == 1
    assert "$WIN1" in calls[0]
    assert "Невероятно!" in calls[0]


def test_win_celebrations_skips_when_hook_generation_fails(monkeypatch):
    monkeypatch.setattr(main.win_celebration_generator, "generate_win_celebration_hook", lambda angle: None)
    calls = []
    monkeypatch.setattr(main.binance_publisher, "publish_post", lambda text, **k: calls.append(text))

    main._publish_win_celebrations([_closed_record(ticker="WIN1", result="win", hours_to_close=4.5)])

    assert calls == []


def test_win_celebrations_skips_when_hook_fails_validation(monkeypatch):
    monkeypatch.setattr(main.win_celebration_generator, "generate_win_celebration_hook", lambda angle: "Заработал 7%!")
    calls = []
    monkeypatch.setattr(main.binance_publisher, "publish_post", lambda text, **k: calls.append(text))

    main._publish_win_celebrations([_closed_record(ticker="WIN1", result="win", hours_to_close=4.5)])

    # Реальный validate_win_celebration_hook (не замокан) должен отбраковать
    # хук с цифрой - публикации быть не должно.
    assert calls == []


def test_win_celebrations_one_bad_record_does_not_block_the_rest(monkeypatch):
    def _fake_hook(angle):
        return "Невероятно!"

    monkeypatch.setattr(main.win_celebration_generator, "generate_win_celebration_hook", _fake_hook)

    calls = []

    def _fake_publish(text, **kwargs):
        if "BAD" in text:
            raise main.binance_publisher.PublishError("boom")
        calls.append(text)

    monkeypatch.setattr(main.binance_publisher, "publish_post", _fake_publish)

    records = [
        _closed_record(ticker="BAD", result="win", hours_to_close=1.0),
        _closed_record(ticker="GOOD", result="win", hours_to_close=2.0),
    ]
    main._publish_win_celebrations(records)

    assert len(calls) == 1
    assert "$GOOD" in calls[0]


def test_win_celebrations_includes_hashtags_and_chart_image(monkeypatch):
    """Ответ на уведомление Binance про апгрейд API постинга (картинки +
    token/topic tags) - см. _publish_win_celebrations в main.py.
    Картинку не тянем из реальной сети - chart_generator замокан."""
    monkeypatch.setattr(main.win_celebration_generator, "generate_win_celebration_hook", lambda angle: "Невероятно!")
    monkeypatch.setattr(main.chart_generator, "generate_chart_image", lambda ticker, days=2, expected_price=None: "/tmp/fake_chart.png")

    calls = []
    monkeypatch.setattr(main.binance_publisher, "publish_post", lambda text, **k: calls.append((text, k)))

    main._publish_win_celebrations([_closed_record(ticker="WIN1", result="win", hours_to_close=4.5)])

    assert len(calls) == 1
    text, kwargs = calls[0]
    assert "#WIN1" in text
    assert kwargs.get("image_paths") == ["/tmp/fake_chart.png"]


def test_win_celebrations_chart_failure_still_publishes_without_image(monkeypatch):
    """Как _publish_signal: неудачная генерация графика не должна
    блокировать сам пост, просто уходит без картинки."""
    monkeypatch.setattr(main.win_celebration_generator, "generate_win_celebration_hook", lambda angle: "Невероятно!")

    def _boom(ticker, days=2, expected_price=None):
        raise RuntimeError("нет данных с биржи")

    monkeypatch.setattr(main.chart_generator, "generate_chart_image", _boom)

    calls = []
    monkeypatch.setattr(main.binance_publisher, "publish_post", lambda text, **k: calls.append((text, k)))

    main._publish_win_celebrations([_closed_record(ticker="WIN1", result="win", hours_to_close=4.5)])

    assert len(calls) == 1
    text, kwargs = calls[0]
    assert "#WIN1" in text
    assert kwargs.get("image_paths") is None


def test_try_publish_hot_take_skips_when_bluesky_not_configured(monkeypatch):
    monkeypatch.setattr(config, "BLUESKY_HANDLE", "")
    monkeypatch.setattr(config, "BLUESKY_APP_PASSWORD", "")
    calls = []
    monkeypatch.setattr(main.hot_take_generator, "generate_hot_take", lambda theme: calls.append(theme))

    main.try_publish_hot_take()

    # Не должны даже пытаться сгенерировать текст - незачем тратить LLM-вызов,
    # если публиковать всё равно некуда.
    assert calls == []


def test_try_publish_hot_take_respects_interval(monkeypatch):
    monkeypatch.setattr(config, "BLUESKY_HANDLE", "alexei.bsky.social")
    monkeypatch.setattr(config, "BLUESKY_APP_PASSWORD", "app-pass")
    monkeypatch.setattr(main.queue_manager, "seconds_since_last_post", lambda post_type: 10)
    monkeypatch.setattr(main.queue_manager, "get_jitter_seconds", lambda post_type: 0)
    calls = []
    monkeypatch.setattr(main.hot_take_generator, "generate_hot_take", lambda theme: calls.append(theme))

    main.try_publish_hot_take()

    assert calls == []


def test_try_publish_hot_take_publishes_only_to_bluesky(monkeypatch):
    monkeypatch.setattr(config, "BLUESKY_HANDLE", "alexei.bsky.social")
    monkeypatch.setattr(config, "BLUESKY_APP_PASSWORD", "app-pass")
    monkeypatch.setattr(main.queue_manager, "seconds_since_last_post", lambda post_type: 10 ** 9)
    monkeypatch.setattr(main.queue_manager, "get_jitter_seconds", lambda post_type: 0)
    monkeypatch.setattr(main.queue_manager, "should_retry_now", lambda post_type: True)
    monkeypatch.setattr(main.queue_manager, "get_last_hot_take_theme", lambda: None)
    monkeypatch.setattr(main.hot_take_generator, "pick_theme", lambda last: "BTC")
    monkeypatch.setattr(main.hot_take_generator, "generate_hot_take", lambda theme, hook_mode=None: ("текст хот-тейка", {5.5}, 5.5))
    monkeypatch.setattr(main.hot_take_generator, "validate_hot_take", lambda text, nums: (True, ""))

    binance_calls = []
    telegram_calls = []
    bluesky_calls = []
    monkeypatch.setattr(main.binance_publisher, "publish_post", lambda *a, **k: binance_calls.append(1))
    monkeypatch.setattr(main.telegram_publisher, "publish_post", lambda *a, **k: telegram_calls.append(1))
    monkeypatch.setattr(main.bluesky_publisher, "publish_post", lambda *a, **k: bluesky_calls.append(1))

    main.try_publish_hot_take()

    assert bluesky_calls == [1]
    assert binance_calls == []
    assert telegram_calls == []


def test_try_publish_mini_lesson_skips_when_bluesky_not_configured(monkeypatch):
    monkeypatch.setattr(config, "BLUESKY_HANDLE", "")
    monkeypatch.setattr(config, "BLUESKY_APP_PASSWORD", "")
    calls = []
    monkeypatch.setattr(main.mini_lesson_generator, "generate_mini_lesson", lambda topic: calls.append(topic))

    main.try_publish_mini_lesson()

    assert calls == []


def test_try_publish_mini_lesson_publishes_only_to_bluesky(monkeypatch):
    monkeypatch.setattr(config, "BLUESKY_HANDLE", "alexei.bsky.social")
    monkeypatch.setattr(config, "BLUESKY_APP_PASSWORD", "app-pass")
    monkeypatch.setattr(main.queue_manager, "seconds_since_last_post", lambda post_type: 10 ** 9)
    monkeypatch.setattr(main.queue_manager, "get_jitter_seconds", lambda post_type: 0)
    monkeypatch.setattr(main.queue_manager, "should_retry_now", lambda post_type: True)
    monkeypatch.setattr(main.queue_manager, "get_last_mini_lesson_topic", lambda: None)
    monkeypatch.setattr(main.mini_lesson_generator, "pick_topic", lambda last: "rsi")
    monkeypatch.setattr(main.mini_lesson_generator, "generate_mini_lesson", lambda topic: "текст мини-урока")
    monkeypatch.setattr(main.mini_lesson_generator, "validate_mini_lesson", lambda text: (True, ""))

    binance_calls = []
    bluesky_calls = []
    monkeypatch.setattr(main.binance_publisher, "publish_post", lambda *a, **k: binance_calls.append(1))
    monkeypatch.setattr(main.bluesky_publisher, "publish_post", lambda *a, **k: bluesky_calls.append(1))

    main.try_publish_mini_lesson()

    assert bluesky_calls == [1]
    assert binance_calls == []


def test_try_publish_audience_question_skips_when_bluesky_not_configured(monkeypatch):
    monkeypatch.setattr(config, "BLUESKY_HANDLE", "")
    monkeypatch.setattr(config, "BLUESKY_APP_PASSWORD", "")
    calls = []
    monkeypatch.setattr(main.bluesky_publisher, "publish_post", lambda *a, **k: calls.append(1))

    main.try_publish_audience_question()

    assert calls == []


def test_try_publish_audience_question_publishes_picked_question(monkeypatch):
    monkeypatch.setattr(config, "BLUESKY_HANDLE", "alexei.bsky.social")
    monkeypatch.setattr(config, "BLUESKY_APP_PASSWORD", "app-pass")
    monkeypatch.setattr(main.queue_manager, "seconds_since_last_post", lambda post_type: 10 ** 9)
    monkeypatch.setattr(main.queue_manager, "get_jitter_seconds", lambda post_type: 0)
    monkeypatch.setattr(main.queue_manager, "should_retry_now", lambda post_type: True)
    monkeypatch.setattr(main.queue_manager, "get_last_audience_question", lambda: None)
    monkeypatch.setattr(main.audience_question_generator, "pick_question", lambda last: "Тестовый вопрос?")

    calls = []
    saved_question = []
    monkeypatch.setattr(main.bluesky_publisher, "publish_post", lambda text, **k: calls.append(text))
    monkeypatch.setattr(main.queue_manager, "set_last_audience_question", lambda q: saved_question.append(q))

    main.try_publish_audience_question()

    assert calls == ["Тестовый вопрос?"]
    assert saved_question == ["Тестовый вопрос?"]


def test_crosspost_to_bluesky_uses_teaser_when_random_below_probability(monkeypatch):
    monkeypatch.setattr(config, "BLUESKY_HANDLE", "alexei.bsky.social")
    monkeypatch.setattr(config, "BLUESKY_APP_PASSWORD", "app-pass")
    monkeypatch.setattr(config, "BLUESKY_TEASER_PROBABILITY", 1.0)  # всегда тизер в этом тесте
    monkeypatch.setattr(main.random, "random", lambda: 0.0)

    teaser_calls = []
    full_calls = []
    monkeypatch.setattr(main.post_format, "build_bluesky_teaser", lambda ticker=None: teaser_calls.append(ticker) or ("teaser-text", []))
    monkeypatch.setattr(main.post_format, "build_bluesky_post", lambda text, ticker=None: full_calls.append(ticker) or ("full-text", []))
    monkeypatch.setattr(main.bluesky_publisher, "publish_post", lambda text, **k: {"uri": "at://x", "cid": "c1"})

    main._crosspost_to_bluesky("текст поста", image_path=None, ticker="BTC")
    # Без картинки тизер не используется даже при вероятности 1.0 - нечего
    # показывать, тизер имеет смысл только с графиком.
    assert teaser_calls == []
    assert full_calls == ["BTC"]


def test_crosspost_to_bluesky_uses_full_post_when_random_above_probability(monkeypatch):
    monkeypatch.setattr(config, "BLUESKY_HANDLE", "alexei.bsky.social")
    monkeypatch.setattr(config, "BLUESKY_APP_PASSWORD", "app-pass")
    monkeypatch.setattr(config, "BLUESKY_TEASER_PROBABILITY", 0.25)
    monkeypatch.setattr(main.random, "random", lambda: 0.9)  # выше вероятности - обычный пост

    teaser_calls = []
    full_calls = []
    monkeypatch.setattr(main.post_format, "build_bluesky_teaser", lambda ticker=None: teaser_calls.append(ticker) or ("teaser-text", []))
    monkeypatch.setattr(main.post_format, "build_bluesky_post", lambda text, ticker=None: full_calls.append(ticker) or ("full-text", []))
    monkeypatch.setattr(main.bluesky_publisher, "publish_post", lambda text, **k: {"uri": "at://x", "cid": "c1"})
    monkeypatch.setattr(main.Path, "read_bytes", lambda self: b"\x89PNG")

    main._crosspost_to_bluesky("текст поста", image_path="fake_chart.png", ticker="BTC")
    assert full_calls == ["BTC"]
    assert teaser_calls == []


def test_crosspost_to_bluesky_uses_teaser_with_image_when_below_probability(monkeypatch):
    monkeypatch.setattr(config, "BLUESKY_HANDLE", "alexei.bsky.social")
    monkeypatch.setattr(config, "BLUESKY_APP_PASSWORD", "app-pass")
    monkeypatch.setattr(config, "BLUESKY_TEASER_PROBABILITY", 0.25)
    monkeypatch.setattr(main.random, "random", lambda: 0.1)  # ниже вероятности - тизер

    teaser_calls = []
    full_calls = []
    monkeypatch.setattr(main.post_format, "build_bluesky_teaser", lambda ticker=None: teaser_calls.append(ticker) or ("teaser-text", []))
    monkeypatch.setattr(main.post_format, "build_bluesky_post", lambda text, ticker=None: full_calls.append(ticker) or ("full-text", []))
    monkeypatch.setattr(main.bluesky_publisher, "publish_post", lambda text, **k: {"uri": "at://x", "cid": "c1"})
    monkeypatch.setattr(main.Path, "read_bytes", lambda self: b"\x89PNG")

    main._crosspost_to_bluesky("текст поста", image_path="fake_chart.png", ticker="BTC")
    assert teaser_calls == ["BTC"]
    assert full_calls == []


def test_try_publish_emergency_post_skips_when_bluesky_not_configured(monkeypatch):
    monkeypatch.setattr(config, "BLUESKY_HANDLE", "")
    monkeypatch.setattr(config, "BLUESKY_APP_PASSWORD", "")
    calls = []
    monkeypatch.setattr(main.volatility_alert, "detect_market_volatility_spike", lambda: calls.append(1))

    main.try_publish_emergency_post()

    assert calls == []


def test_try_publish_emergency_post_respects_cooldown(monkeypatch):
    monkeypatch.setattr(config, "BLUESKY_HANDLE", "alexei.bsky.social")
    monkeypatch.setattr(config, "BLUESKY_APP_PASSWORD", "app-pass")
    monkeypatch.setattr(main.queue_manager, "seconds_since_last_post", lambda post_type: 10)
    calls = []
    monkeypatch.setattr(main.volatility_alert, "detect_market_volatility_spike", lambda: calls.append(1))

    main.try_publish_emergency_post()

    assert calls == []


def test_try_publish_emergency_post_does_nothing_without_spike(monkeypatch):
    monkeypatch.setattr(config, "BLUESKY_HANDLE", "alexei.bsky.social")
    monkeypatch.setattr(config, "BLUESKY_APP_PASSWORD", "app-pass")
    monkeypatch.setattr(main.queue_manager, "seconds_since_last_post", lambda post_type: 10 ** 9)
    monkeypatch.setattr(main.queue_manager, "should_retry_now", lambda post_type: True)
    monkeypatch.setattr(main.volatility_alert, "detect_market_volatility_spike", lambda: None)

    calls = []
    monkeypatch.setattr(main.bluesky_publisher, "publish_post", lambda *a, **k: calls.append(1))

    main.try_publish_emergency_post()

    assert calls == []


def test_try_publish_emergency_post_publishes_on_spike(monkeypatch):
    monkeypatch.setattr(config, "BLUESKY_HANDLE", "alexei.bsky.social")
    monkeypatch.setattr(config, "BLUESKY_APP_PASSWORD", "app-pass")
    monkeypatch.setattr(main.queue_manager, "seconds_since_last_post", lambda post_type: 10 ** 9)
    monkeypatch.setattr(main.queue_manager, "should_retry_now", lambda post_type: True)

    spike = {"pct": 6.0, "direction": "up", "window_hours": 3}
    monkeypatch.setattr(main.volatility_alert, "detect_market_volatility_spike", lambda: spike)
    monkeypatch.setattr(main.volatility_alert, "generate_emergency_post", lambda s: "🚨 текст экстренного поста")
    monkeypatch.setattr(main.volatility_alert, "validate_emergency_post", lambda text, s: (True, ""))

    calls = []
    saved_time = []
    monkeypatch.setattr(main.bluesky_publisher, "publish_post", lambda text, **k: calls.append(text))
    monkeypatch.setattr(main.queue_manager, "set_last_post_time", lambda post_type: saved_time.append(post_type))

    main.try_publish_emergency_post()

    assert calls == ["🚨 текст экстренного поста"]
    assert saved_time == ["emergency"]


def _make_signal(**overrides):
    from signal_parser import RsiSignal
    base = dict(
        ticker="BEAT", timeframe="15m", strategy="RSI + Bollinger Touch",
        direction="Шорт", current_price="2.225", rsi_now="81.74", score="89",
        quality="Conservative", entry_low="2.205", entry_high="2.2178",
        invalidation="2.2371", target="2.1729", change_24h="+35.67%",
        volume="57.67M", rsi_live="82.64", created_at="2026-06-23 22:44:59 EEST",
        description="desc", raw_text="raw",
    )
    base.update(overrides)
    return RsiSignal(**base)


def test_build_extended_telegram_text_inserts_context_before_disclaimer(monkeypatch):
    signal = _make_signal()
    post_text = f"Хук.\n\nВход: 2.205 - 2.2178\n\n{main.post_format.DISCLAIMER}"

    monkeypatch.setattr(
        main.telegram_extended, "generate_extended_context",
        lambda s, hook: ("Контекст про RSI 81.74.", {81.74}),
    )
    monkeypatch.setattr(main.telegram_extended, "validate_extended_context", lambda text, nums: (True, ""))

    result = main._build_extended_telegram_text(post_text, signal, "Хук.")

    assert "Контекст про RSI 81.74." in result
    assert result.endswith(main.post_format.DISCLAIMER)
    assert result.index("Контекст про RSI 81.74.") < result.index(main.post_format.DISCLAIMER)


def test_build_extended_telegram_text_falls_back_when_generation_returns_none(monkeypatch):
    signal = _make_signal()
    post_text = f"Хук.\n\n{main.post_format.DISCLAIMER}"
    monkeypatch.setattr(main.telegram_extended, "generate_extended_context", lambda s, hook: None)

    result = main._build_extended_telegram_text(post_text, signal, "Хук.")

    assert result == post_text


def test_build_extended_telegram_text_falls_back_when_validation_fails(monkeypatch):
    signal = _make_signal()
    post_text = f"Хук.\n\n{main.post_format.DISCLAIMER}"
    monkeypatch.setattr(
        main.telegram_extended, "generate_extended_context",
        lambda s, hook: ("плохой контекст", {81.74}),
    )
    monkeypatch.setattr(main.telegram_extended, "validate_extended_context", lambda text, nums: (False, "плохо"))

    result = main._build_extended_telegram_text(post_text, signal, "Хук.")

    assert result == post_text


def test_build_extended_telegram_text_falls_back_on_exception(monkeypatch):
    signal = _make_signal()
    post_text = f"Хук.\n\n{main.post_format.DISCLAIMER}"

    def _raise(s, hook):
        raise RuntimeError("boom")

    monkeypatch.setattr(main.telegram_extended, "generate_extended_context", _raise)

    result = main._build_extended_telegram_text(post_text, signal, "Хук.")

    assert result == post_text


def test_try_publish_telegram_glossary_skips_when_telegram_not_configured(monkeypatch):
    monkeypatch.setattr(main.telegram_publisher, "is_configured", lambda: False)
    calls = []
    monkeypatch.setattr(main.telegram_glossary, "generate_glossary_post", lambda topic: calls.append(topic))

    main.try_publish_telegram_glossary()

    assert calls == []


def test_try_publish_telegram_glossary_publishes_only_to_telegram_and_advances_index(monkeypatch):
    monkeypatch.setattr(main.telegram_publisher, "is_configured", lambda: True)
    monkeypatch.setattr(main.queue_manager, "seconds_since_last_post", lambda post_type: 10 ** 9)
    monkeypatch.setattr(main.queue_manager, "get_jitter_seconds", lambda post_type: 0)
    monkeypatch.setattr(main.queue_manager, "should_retry_now", lambda post_type: True)
    monkeypatch.setattr(main.queue_manager, "get_glossary_index", lambda: 2)
    monkeypatch.setattr(main.telegram_glossary, "get_topic", lambda index: {"key": "test_topic"})
    monkeypatch.setattr(main.telegram_glossary, "generate_glossary_post", lambda topic: "текст поста глоссария")
    monkeypatch.setattr(main.telegram_glossary, "validate_glossary_post", lambda text, topic: (True, ""))

    telegram_calls = []
    binance_calls = []
    saved_index = []
    monkeypatch.setattr(main.telegram_publisher, "publish_post", lambda text, **k: telegram_calls.append(text))
    monkeypatch.setattr(main.binance_publisher, "publish_post", lambda *a, **k: binance_calls.append(1))
    monkeypatch.setattr(main.queue_manager, "set_glossary_index", lambda idx: saved_index.append(idx))

    main.try_publish_telegram_glossary()

    assert telegram_calls == ["текст поста глоссария"]
    assert binance_calls == []
    assert saved_index == [3]  # индекс продвинулся на 1 (был 2)


def test_try_publish_telegram_poll_skips_when_telegram_not_configured(monkeypatch):
    monkeypatch.setattr(main.telegram_publisher, "is_configured", lambda: False)
    calls = []
    monkeypatch.setattr(main.telegram_publisher, "publish_poll", lambda *a, **k: calls.append(1))

    main.try_publish_telegram_poll()

    assert calls == []


def test_try_publish_telegram_poll_publishes_picked_poll(monkeypatch):
    monkeypatch.setattr(main.telegram_publisher, "is_configured", lambda: True)
    monkeypatch.setattr(main.queue_manager, "seconds_since_last_post", lambda post_type: 10 ** 9)
    monkeypatch.setattr(main.queue_manager, "get_jitter_seconds", lambda post_type: 0)
    monkeypatch.setattr(main.queue_manager, "should_retry_now", lambda post_type: True)
    monkeypatch.setattr(main.queue_manager, "get_last_telegram_poll", lambda: None)
    poll = {"question": "Тестовый вопрос?", "options": ["А", "Б"]}
    monkeypatch.setattr(main.telegram_engagement, "pick_poll", lambda last: poll)

    calls = []
    saved = []
    monkeypatch.setattr(main.telegram_publisher, "publish_poll", lambda q, opts: calls.append((q, opts)))
    monkeypatch.setattr(main.queue_manager, "set_last_telegram_poll", lambda q: saved.append(q))

    main.try_publish_telegram_poll()

    assert calls == [("Тестовый вопрос?", ["А", "Б"])]
    assert saved == ["Тестовый вопрос?"]


def test_try_publish_telegram_ama_skips_when_telegram_not_configured(monkeypatch):
    monkeypatch.setattr(main.telegram_publisher, "is_configured", lambda: False)
    calls = []
    monkeypatch.setattr(main.telegram_publisher, "publish_post", lambda *a, **k: calls.append(1))

    main.try_publish_telegram_ama()

    assert calls == []


def test_try_publish_telegram_ama_publishes_picked_prompt(monkeypatch):
    monkeypatch.setattr(main.telegram_publisher, "is_configured", lambda: True)
    monkeypatch.setattr(main.queue_manager, "seconds_since_last_post", lambda post_type: 10 ** 9)
    monkeypatch.setattr(main.queue_manager, "get_jitter_seconds", lambda post_type: 0)
    monkeypatch.setattr(main.queue_manager, "should_retry_now", lambda post_type: True)
    monkeypatch.setattr(main.queue_manager, "get_last_telegram_ama_prompt", lambda: None)
    monkeypatch.setattr(main.telegram_engagement, "pick_ama_prompt", lambda last: "Тестовое приглашение на AMA")

    calls = []
    saved = []
    monkeypatch.setattr(main.telegram_publisher, "publish_post", lambda text, **k: calls.append(text))
    monkeypatch.setattr(main.queue_manager, "set_last_telegram_ama_prompt", lambda p: saved.append(p))

    main.try_publish_telegram_ama()

    assert calls == ["Тестовое приглашение на AMA"]
    assert saved == ["Тестовое приглашение на AMA"]


def test_try_publish_rebalance_report_skips_when_telegram_not_configured(monkeypatch):
    monkeypatch.setattr(main.telegram_publisher, "is_configured", lambda: False)
    calls = []
    monkeypatch.setattr(main.rebalance_advisor, "find_rebalance_candidates", lambda: calls.append(1))

    main.try_publish_rebalance_report()

    assert calls == []


def test_try_publish_rebalance_report_no_publish_when_no_candidates(monkeypatch):
    monkeypatch.setattr(main.telegram_publisher, "is_configured", lambda: True)
    monkeypatch.setattr(main.queue_manager, "seconds_since_last_post", lambda post_type: 10 ** 9)
    monkeypatch.setattr(main.queue_manager, "get_jitter_seconds", lambda post_type: 0)
    monkeypatch.setattr(main.queue_manager, "should_retry_now", lambda post_type: True)
    monkeypatch.setattr(main.rebalance_advisor, "find_rebalance_candidates", lambda: [])

    calls = []
    saved_time = []
    monkeypatch.setattr(main.telegram_publisher, "publish_post", lambda *a, **k: calls.append(1))
    monkeypatch.setattr(main.queue_manager, "set_last_post_time", lambda post_type: saved_time.append(post_type))

    main.try_publish_rebalance_report()

    assert calls == []
    # Всё равно фиксируем время проверки, чтобы не проверять каждый тик подряд.
    assert saved_time == ["rebalance_report"]


def test_try_publish_rebalance_report_publishes_when_candidates_found(monkeypatch):
    monkeypatch.setattr(main.telegram_publisher, "is_configured", lambda: True)
    monkeypatch.setattr(main.queue_manager, "seconds_since_last_post", lambda post_type: 10 ** 9)
    monkeypatch.setattr(main.queue_manager, "get_jitter_seconds", lambda post_type: 0)
    monkeypatch.setattr(main.queue_manager, "should_retry_now", lambda post_type: True)

    candidates = [{"ticker": "OP", "tier": "tier1", "reason": "underperform", "detail": "..."}]
    monkeypatch.setattr(main.rebalance_advisor, "find_rebalance_candidates", lambda: candidates)
    monkeypatch.setattr(main.rebalance_advisor, "build_rebalance_report", lambda c: "текст отчёта")

    calls = []
    monkeypatch.setattr(main.telegram_publisher, "publish_post", lambda text, **k: calls.append(text))

    main.try_publish_rebalance_report()

    assert calls == ["текст отчёта"]


def test_publish_signal_records_post_opener_in_voice_memory(monkeypatch):
    from signal_parser import RsiSignal

    signal = RsiSignal(
        ticker="BEAT", timeframe="15m", strategy="RSI + Bollinger Touch",
        direction="Шорт", current_price="2.225", rsi_now="81.74", score="89",
        quality="Conservative", entry_low="2.205", entry_high="2.2178",
        invalidation="2.2371", target="2.1729", change_24h="+35.67%",
        volume="57.67M", rsi_live="82.64", created_at="2026-06-23 22:44:59 EEST",
        description="desc", raw_text="raw",
    )

    monkeypatch.setattr(main.post_format, "pick_hook_mode", lambda last: "technician")
    monkeypatch.setattr(main.text_generator, "generate_post_text", lambda s, mode: ("текст поста", "хук"))
    monkeypatch.setattr(main.validator, "validate_post_text", lambda text, s: (True, ""))
    monkeypatch.setattr(main.chart_generator, "generate_chart_image", lambda *a, **k: "fake_chart.png")
    monkeypatch.setattr(main, "_do_publish", lambda *a, **k: (True, None))
    monkeypatch.setattr(main.outcome_tracker, "record_signal_outcome", lambda *a, **k: None)

    calls = []
    monkeypatch.setattr(main.voice_memory, "record_post", lambda text, **k: calls.append(text))

    published = main._publish_signal(signal)

    assert published is True
    assert calls == ["текст поста"]


def test_publish_signal_does_not_record_when_publish_fails(monkeypatch):
    from signal_parser import RsiSignal

    signal = RsiSignal(
        ticker="BEAT", timeframe="15m", strategy="RSI + Bollinger Touch",
        direction="Шорт", current_price="2.225", rsi_now="81.74", score="89",
        quality="Conservative", entry_low="2.205", entry_high="2.2178",
        invalidation="2.2371", target="2.1729", change_24h="+35.67%",
        volume="57.67M", rsi_live="82.64", created_at="2026-06-23 22:44:59 EEST",
        description="desc", raw_text="raw",
    )

    monkeypatch.setattr(main.post_format, "pick_hook_mode", lambda last: "technician")
    monkeypatch.setattr(main.text_generator, "generate_post_text", lambda s, mode: ("текст поста", "хук"))
    monkeypatch.setattr(main.validator, "validate_post_text", lambda text, s: (True, ""))
    monkeypatch.setattr(main.chart_generator, "generate_chart_image", lambda *a, **k: "fake_chart.png")
    monkeypatch.setattr(main, "_do_publish", lambda *a, **k: (False, None))

    calls = []
    monkeypatch.setattr(main.voice_memory, "record_post", lambda text, **k: calls.append(text))

    published = main._publish_signal(signal)

    assert published is False
    assert calls == []


# ============================================================
# Топик-хэштеги/token tags (ответ на уведомление Binance про апгрейд API
# постинга) - main._do_publish (сигнал), try_publish_binance_promo,
# try_publish_opinion_post. Всё, что ходит в сеть/LLM, замокано -
# проверяем только то, что итоговый текст, отправленный в
# binance_publisher.publish_post, содержит ожидаемые теги.
# ============================================================

def test_do_publish_appends_ticker_hashtag(monkeypatch):
    monkeypatch.setattr(main.post_format, "maybe_binance_cta", lambda: None)
    monkeypatch.setattr(main, "_schedule_crossposts", lambda *a, **k: None)

    calls = []
    monkeypatch.setattr(main.binance_publisher, "publish_post", lambda text, **k: calls.append((text, k)))

    published, _ = main._do_publish("текст сигнала", None, ticker="BEAT")

    assert published is True
    text, kwargs = calls[0]
    assert "#BEAT" in text
    assert text.startswith("текст сигнала")


def test_do_publish_no_ticker_skips_hashtag_line(monkeypatch):
    monkeypatch.setattr(main.post_format, "maybe_binance_cta", lambda: None)
    monkeypatch.setattr(main, "_schedule_crossposts", lambda *a, **k: None)

    calls = []
    monkeypatch.setattr(main.binance_publisher, "publish_post", lambda text, **k: calls.append((text, k)))

    main._do_publish("текст без тикера", None, ticker=None)

    text, kwargs = calls[0]
    assert text == "текст без тикера"


def test_try_publish_binance_promo_appends_general_hashtag(monkeypatch):
    monkeypatch.setattr(main.queue_manager, "seconds_since_last_post", lambda post_type: 10 ** 9)
    monkeypatch.setattr(main.queue_manager, "get_jitter_seconds", lambda post_type: 0)
    monkeypatch.setattr(main.queue_manager, "should_retry_now", lambda post_type: True)
    monkeypatch.setattr(main.binance_promo_generator, "generate_binance_promo", lambda theme, hook_mode=None: "промо-текст.")
    monkeypatch.setattr(main.binance_promo_generator, "validate_binance_promo", lambda text: (True, ""))
    monkeypatch.setattr(main.binance_promo_generator, "assemble_binance_promo", lambda text: text)
    monkeypatch.setattr(main.post_format, "maybe_binance_cta", lambda: None)

    calls = []
    monkeypatch.setattr(main.binance_publisher, "publish_post", lambda text, **k: calls.append(text))
    # voice_memory.record_post ДОЛЖЕН получить текст БЕЗ хэштега (см.
    # try_publish_binance_promo - post_text используется для памяти, а
    # хэштег добавляется только в binance_text для самой публикации).
    voice_calls = []
    monkeypatch.setattr(main.voice_memory, "record_post", lambda text, **k: voice_calls.append(text))

    main.try_publish_binance_promo()

    assert len(calls) == 1
    assert calls[0].startswith("промо-текст.")
    assert calls[0] != "промо-текст."  # хэштег дописан
    assert voice_calls == ["промо-текст."]


def test_try_publish_opinion_post_btc_theme_uses_cashtag_hashtag(monkeypatch):
    monkeypatch.setattr(main.queue_manager, "seconds_since_last_post", lambda post_type: 10 ** 9)
    monkeypatch.setattr(main.queue_manager, "get_jitter_seconds", lambda post_type: 0)
    monkeypatch.setattr(main.queue_manager, "should_retry_now", lambda post_type: True)
    monkeypatch.setattr(main.opinion_generator, "pick_theme", lambda last: "BTC")
    monkeypatch.setattr(main.opinion_generator, "generate_opinion_post",
                         lambda theme, hook_mode=None: ("мнение про BTC.", set(), 1.5))
    monkeypatch.setattr(main.opinion_generator, "validate_opinion_post_text", lambda text, nums: (True, ""))

    calls = []
    monkeypatch.setattr(main.binance_publisher, "publish_post", lambda text, **k: calls.append(text))

    main.try_publish_opinion_post()

    assert len(calls) == 1
    assert "#BTC" in calls[0]


def test_try_publish_opinion_post_market_theme_uses_general_hashtag(monkeypatch):
    monkeypatch.setattr(main.queue_manager, "seconds_since_last_post", lambda post_type: 10 ** 9)
    monkeypatch.setattr(main.queue_manager, "get_jitter_seconds", lambda post_type: 0)
    monkeypatch.setattr(main.queue_manager, "should_retry_now", lambda post_type: True)
    monkeypatch.setattr(main.opinion_generator, "pick_theme", lambda last: "market")
    monkeypatch.setattr(main.opinion_generator, "generate_opinion_post",
                         lambda theme, hook_mode=None: ("мнение про рынок в целом.", set(), 1.5))
    monkeypatch.setattr(main.opinion_generator, "validate_opinion_post_text", lambda text, nums: (True, ""))

    calls = []
    monkeypatch.setattr(main.binance_publisher, "publish_post", lambda text, **k: calls.append(text))

    main.try_publish_opinion_post()

    assert len(calls) == 1
    # Никакого $CASHTAG (нет одного конкретного тикера у темы "market"),
    # но общий тег из post_format._SQUARE_GENERAL_HASHTAGS всё равно есть.
    assert any(tag in calls[0] for tag in main.post_format._SQUARE_GENERAL_HASHTAGS)


if __name__ == "__main__":
    import sys

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
            fn(mp)
            print(f"OK   {name}")
            passed += 1
        except AssertionError as e:
            print(f"FAIL {name}: {e}")
            failed += 1
        finally:
            mp.undo()

    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
