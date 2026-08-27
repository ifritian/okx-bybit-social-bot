#!/usr/bin/env python3
"""
Тесты кросспостинга в Bluesky:
- post_format.build_bluesky_post: хэштег + ссылки + обрезка под лимит
  300 символов + список link_facets, ссылки/тег никогда не обрезаются
  первыми.
- bluesky_publisher: is_configured(), сессия -> (опционально картинка) ->
  публикация, байтовые facets, проброс ошибок наружу без исключений
  сверх BlueskyPublishError. requests.post замокан - реальных запросов
  не идёт.
"""
import types

import config
import post_format
import bluesky_publisher
import validator
from signal_parser import RsiSignal


def _make_signal(**overrides) -> RsiSignal:
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


class _FakeResponse:
    def __init__(self, json_data, ok=True):
        self._json = json_data
        self.ok = ok

    def json(self):
        return self._json


def test_build_bluesky_post_includes_hashtag_and_links(monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_PUBLISH_CHANNEL", "@my_channel")
    text = "Короткий хук.\n\nВход: 1-2\nСтоп: 0.9\nТейк: 3\n\nИнформационный пост, не финансовая рекомендация."

    result, facets = post_format.build_bluesky_post(text, ticker="BTC")

    assert "#BTC" in result
    assert post_format.REFERRAL_LINK in result
    assert "https://t.me/my_channel" in result
    assert len(result) <= post_format.BLUESKY_CHAR_LIMIT
    assert (post_format.REFERRAL_LINK, post_format.REFERRAL_LINK) in facets
    assert ("https://t.me/my_channel", "https://t.me/my_channel") in facets


def test_build_bluesky_post_without_telegram_channel_still_has_binance_link(monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_PUBLISH_CHANNEL", "")
    result, facets = post_format.build_bluesky_post("Хук без канала.", ticker=None)

    assert post_format.REFERRAL_LINK in result
    assert "t.me" not in result
    assert len(facets) == 1


def test_build_bluesky_post_truncates_long_body_but_keeps_links(monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_PUBLISH_CHANNEL", "@my_channel")
    long_text = "А" * 2000

    result, facets = post_format.build_bluesky_post(long_text, ticker="DOGE")

    assert len(result) <= post_format.BLUESKY_CHAR_LIMIT
    assert post_format.REFERRAL_LINK in result
    assert "https://t.me/my_channel" in result
    assert "#DOGE" in result
    assert result.count("…") >= 1


def test_is_configured_false_without_credentials(monkeypatch):
    monkeypatch.setattr(config, "BLUESKY_HANDLE", "")
    monkeypatch.setattr(config, "BLUESKY_APP_PASSWORD", "")
    assert bluesky_publisher.is_configured() is False


def test_is_configured_true_with_both_values(monkeypatch):
    monkeypatch.setattr(config, "BLUESKY_HANDLE", "alexei.bsky.social")
    monkeypatch.setattr(config, "BLUESKY_APP_PASSWORD", "app-pass")
    assert bluesky_publisher.is_configured() is True


def test_byte_facets_finds_substring_offsets():
    text = "Хук про $BTC.\n\nБинанс: https://www.binance.com/register?ref=X"
    facets = bluesky_publisher._byte_facets(
        text, [("https://www.binance.com/register?ref=X", "https://www.binance.com/register?ref=X")]
    )
    assert len(facets) == 1
    start = facets[0]["index"]["byteStart"]
    end = facets[0]["index"]["byteEnd"]
    recovered = text.encode("utf-8")[start:end].decode("utf-8")
    assert recovered == "https://www.binance.com/register?ref=X"


def test_byte_facets_skips_missing_substring():
    facets = bluesky_publisher._byte_facets("нет ссылки тут", [("https://example.com", "https://example.com")])
    assert facets == []


def test_publish_post_text_only_calls_session_then_create_record(monkeypatch):
    monkeypatch.setattr(config, "BLUESKY_HANDLE", "alexei.bsky.social")
    monkeypatch.setattr(config, "BLUESKY_APP_PASSWORD", "app-pass")

    calls = []

    def _fake_post(url, **kwargs):
        calls.append((url, kwargs))
        if url.endswith("/com.atproto.server.createSession"):
            return _FakeResponse({"accessJwt": "jwt-1", "did": "did:plc:abc"})
        if url.endswith("/com.atproto.repo.createRecord"):
            return _FakeResponse({"uri": "at://did:plc:abc/app.bsky.feed.post/1"})
        raise AssertionError(f"неожиданный вызов: {url}")

    monkeypatch.setattr(bluesky_publisher.requests, "post", _fake_post)

    result = bluesky_publisher.publish_post("текст без картинки")

    assert result["uri"].startswith("at://")
    assert len(calls) == 2
    assert calls[0][0].endswith("createSession")
    assert calls[1][0].endswith("createRecord")
    assert calls[1][1]["headers"]["Authorization"] == "Bearer jwt-1"


def test_publish_post_with_image_uploads_blob_first(monkeypatch):
    monkeypatch.setattr(config, "BLUESKY_HANDLE", "alexei.bsky.social")
    monkeypatch.setattr(config, "BLUESKY_APP_PASSWORD", "app-pass")

    calls = []

    def _fake_post(url, **kwargs):
        calls.append(url)
        if url.endswith("/com.atproto.server.createSession"):
            return _FakeResponse({"accessJwt": "jwt-1", "did": "did:plc:abc"})
        if url.endswith("/com.atproto.repo.uploadBlob"):
            return _FakeResponse({"blob": {"ref": "fake-blob-ref"}})
        if url.endswith("/com.atproto.repo.createRecord"):
            return _FakeResponse({"uri": "at://did:plc:abc/app.bsky.feed.post/2"})
        raise AssertionError(f"неожиданный вызов: {url}")

    monkeypatch.setattr(bluesky_publisher.requests, "post", _fake_post)

    result = bluesky_publisher.publish_post("текст с картинкой", image_bytes=b"\x89PNG...", image_content_type="image/png")

    assert result["uri"].endswith("/2")
    assert any(u.endswith("uploadBlob") for u in calls)


def test_publish_post_raises_on_login_error(monkeypatch):
    monkeypatch.setattr(config, "BLUESKY_HANDLE", "alexei.bsky.social")
    monkeypatch.setattr(config, "BLUESKY_APP_PASSWORD", "wrong-pass")

    def _fake_post(url, **kwargs):
        return _FakeResponse({"message": "Invalid identifier or password"}, ok=False)

    monkeypatch.setattr(bluesky_publisher.requests, "post", _fake_post)

    try:
        bluesky_publisher.publish_post("текст")
        raise AssertionError("ожидалось BlueskyPublishError")
    except bluesky_publisher.BlueskyPublishError:
        pass


def _make_closed_record(**overrides) -> dict:
    base = dict(
        ticker="BEAT", direction="short", strategy="RSI + Bollinger Touch",
        entry=2.21, stop=2.2371, target=2.1729, result="win",
        exit_price=2.1729, pnl_pct=1.72, mfe_pct=1.9,
        bluesky_ref={"uri": "at://did:plc:abc/app.bsky.feed.post/1", "cid": "bafy1"},
    )
    base.update(overrides)
    return base


def test_build_bluesky_outcome_reply_win(monkeypatch):
    record = _make_closed_record(result="win", pnl_pct=1.72)
    text, facets = post_format.build_bluesky_outcome_reply(record)
    assert "Цель достигнута" in text
    assert "$BEAT" in text
    assert "+1.72%" in text
    assert facets == []


def test_build_bluesky_outcome_reply_loss():
    record = _make_closed_record(result="loss", pnl_pct=-1.15, exit_price=2.2371)
    text, _ = post_format.build_bluesky_outcome_reply(record)
    assert "Сработал стоп" in text
    assert "-1.15%" in text


def test_build_bluesky_outcome_reply_timeout():
    record = _make_closed_record(result="timeout", pnl_pct=0.2)
    text, _ = post_format.build_bluesky_outcome_reply(record)
    assert "Тайм-аут" in text


def test_build_bluesky_win_reveal_has_links_and_positive_pnl(monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_PUBLISH_CHANNEL", "@my_channel")
    record = _make_closed_record(result="win", pnl_pct=1.72)

    text, facets = post_format.build_bluesky_win_reveal(record)

    assert "$BEAT" in text
    assert "+1.72%" in text
    assert post_format.REFERRAL_LINK in text
    assert "https://t.me/my_channel" in text
    assert len(facets) == 2


def test_build_bluesky_teaser_with_ticker_and_telegram(monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_PUBLISH_CHANNEL", "@my_channel")
    text, facets = post_format.build_bluesky_teaser(ticker="BTC")

    assert "$BTC" in text
    assert "👀" in text
    assert "https://t.me/my_channel" in text
    assert facets == [("https://t.me/my_channel", "https://t.me/my_channel")]


def test_build_bluesky_teaser_without_ticker():
    text, _ = post_format.build_bluesky_teaser(ticker=None)
    assert "👀" in text
    assert "$" not in text.split("\n")[0]


def test_build_bluesky_teaser_falls_back_to_binance_link_without_telegram(monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_PUBLISH_CHANNEL", "")
    text, facets = post_format.build_bluesky_teaser(ticker="ETH")

    assert post_format.REFERRAL_LINK in text
    assert facets == [(post_format.REFERRAL_LINK, post_format.REFERRAL_LINK)]


def test_is_strong_setup_true_above_threshold():
    signal = _make_signal(score="89")
    assert post_format.is_strong_setup(signal) is True


def test_is_strong_setup_false_below_threshold():
    signal = _make_signal(score="70")
    assert post_format.is_strong_setup(signal) is False


def test_is_strong_setup_false_on_garbage_score():
    signal = _make_signal(score="не число")
    assert post_format.is_strong_setup(signal) is False


def test_build_bluesky_thread_signal_has_three_posts_with_links_on_last(monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_PUBLISH_CHANNEL", "@my_channel")
    signal = _make_signal()

    posts = post_format.build_bluesky_thread_signal("Хук без цифр про сетап.", signal)

    assert len(posts) == 3
    assert posts[0] == "Хук без цифр про сетап."
    assert signal.entry_low in posts[1]
    assert signal.target in posts[1]
    post3_text, facets = posts[2]
    assert post_format.DISCLAIMER in post3_text
    assert post_format.REFERRAL_LINK in post3_text
    assert "https://t.me/my_channel" in post3_text
    assert len(facets) == 2


def test_thread_ref_extracts_uri_and_cid():
    ref = bluesky_publisher.thread_ref({"uri": "at://did:plc:x/app.bsky.feed.post/1", "cid": "bafy1"})
    assert ref == {"uri": "at://did:plc:x/app.bsky.feed.post/1", "cid": "bafy1"}


def test_thread_ref_raises_without_cid():
    try:
        bluesky_publisher.thread_ref({"uri": "at://did:plc:x/app.bsky.feed.post/1"})
        raise AssertionError("ожидалось BlueskyPublishError")
    except bluesky_publisher.BlueskyPublishError:
        pass


def test_reply_refs_second_post_root_equals_parent():
    root = {"uri": "at://root", "cid": "c-root"}
    refs = bluesky_publisher.reply_refs(root)
    assert refs["root"] == refs["parent"] == root


def test_reply_refs_third_post_root_differs_from_parent():
    root = {"uri": "at://root", "cid": "c-root"}
    parent = {"uri": "at://post2", "cid": "c-post2"}
    refs = bluesky_publisher.reply_refs(root, parent)
    assert refs["root"] == root
    assert refs["parent"] == parent


def test_publish_thread_chains_three_posts_with_correct_reply_refs(monkeypatch):
    monkeypatch.setattr(config, "BLUESKY_HANDLE", "alexei.bsky.social")
    monkeypatch.setattr(config, "BLUESKY_APP_PASSWORD", "app-pass")

    session_calls = []
    record_calls = []

    def _fake_post(url, **kwargs):
        if url.endswith("/com.atproto.server.createSession"):
            session_calls.append(1)
            return _FakeResponse({"accessJwt": "jwt-1", "did": "did:plc:abc"})
        if url.endswith("/com.atproto.repo.createRecord"):
            record = kwargs["json"]["record"]
            record_calls.append(record)
            idx = len(record_calls)
            return _FakeResponse({"uri": f"at://did:plc:abc/app.bsky.feed.post/{idx}", "cid": f"cid-{idx}"})
        raise AssertionError(f"неожиданный вызов: {url}")

    monkeypatch.setattr(bluesky_publisher.requests, "post", _fake_post)

    results = bluesky_publisher.publish_thread(["Пост 1 - интрига", "Пост 2 - сетап", ("Пост 3 - вывод", [])])

    assert len(results) == 3
    assert "reply" not in record_calls[0]
    assert record_calls[1]["reply"]["root"]["uri"] == results[0]["uri"]
    assert record_calls[1]["reply"]["parent"]["uri"] == results[0]["uri"]
    assert record_calls[2]["reply"]["root"]["uri"] == results[0]["uri"]
    assert record_calls[2]["reply"]["parent"]["uri"] == results[1]["uri"]
    # Логинимся заново на КАЖДЫЙ пост (см. docstring publish_post) - три
    # поста треда -> три отдельных createSession.
    assert len(session_calls) == 3


def test_publish_thread_attaches_image_only_to_first_post(monkeypatch):
    monkeypatch.setattr(config, "BLUESKY_HANDLE", "alexei.bsky.social")
    monkeypatch.setattr(config, "BLUESKY_APP_PASSWORD", "app-pass")

    calls = []

    def _fake_post(url, **kwargs):
        calls.append(url)
        if url.endswith("/com.atproto.server.createSession"):
            return _FakeResponse({"accessJwt": "jwt-1", "did": "did:plc:abc"})
        if url.endswith("/com.atproto.repo.uploadBlob"):
            return _FakeResponse({"blob": {"ref": "fake-blob"}})
        if url.endswith("/com.atproto.repo.createRecord"):
            idx = sum(1 for c in calls if c.endswith("createRecord"))
            return _FakeResponse({"uri": f"at://did:plc:abc/app.bsky.feed.post/{idx}", "cid": f"cid-{idx}"})
        raise AssertionError(f"неожиданный вызов: {url}")

    monkeypatch.setattr(bluesky_publisher.requests, "post", _fake_post)

    bluesky_publisher.publish_thread(["Пост 1", "Пост 2"], image_bytes=b"\x89PNG", image_content_type="image/png")

    upload_calls = [c for c in calls if c.endswith("uploadBlob")]
    assert len(upload_calls) == 1


def test_signal_risk_reward_line_long():
    # long: entry mid = 100, stop = 90 (риск 10), тейк = 130 (профит 30) -> 1:3.0
    signal = _make_signal(direction="Лонг", entry_low="98", entry_high="102",
                           invalidation="90", target="130")
    assert post_format.signal_risk_reward_line(signal) == "R:R 1:3.0"


def test_signal_risk_reward_line_short():
    # short: entry mid = 100, стоп ВЫШЕ входа (110, риск 10), тейк НИЖЕ (70, профит 30) -> 1:3.0
    signal = _make_signal(direction="Шорт", entry_low="98", entry_high="102",
                           invalidation="110", target="70")
    assert post_format.signal_risk_reward_line(signal) == "R:R 1:3.0"


def test_signal_risk_reward_line_none_when_stop_equals_entry():
    signal = _make_signal(entry_low="100", entry_high="100", invalidation="100", target="130")
    assert post_format.signal_risk_reward_line(signal) is None


def test_signal_risk_reward_line_none_on_garbage_numbers():
    signal = _make_signal(entry_low="не число", entry_high="", invalidation="90", target="130")
    assert post_format.signal_risk_reward_line(signal) is None


def test_signal_setup_lines_includes_rr_line():
    signal = _make_signal(entry_low="98", entry_high="102", invalidation="90", target="130")
    lines = post_format.signal_setup_lines(signal)
    assert lines[-1] == "R:R 1:3.0"
    assert len(lines) == 6  # направление/стратегия, вход, стоп, тейк, RSI/score, R:R


def test_assemble_signal_post_includes_rr_line():
    signal = _make_signal(entry_low="98", entry_high="102", invalidation="90", target="130")
    text = post_format.assemble_signal_post("Хук.", signal)
    assert "R:R 1:3.0" in text


def test_validate_post_text_passes_with_rr_line(monkeypatch):
    """R:R 1:X.X не должен ловиться как 'смешение языков' (одна буква
    R, не 3+ подряд латинских букв - см. validator._LATIN_WORD_RE) и не
    должен мешать проверке обязательных полей."""
    monkeypatch.setattr(config, "TELEGRAM_PUBLISH_CHANNEL", None)
    signal = _make_signal(entry_low="98", entry_high="102", invalidation="90", target="130")
    text = post_format.assemble_signal_post("Обычный хук без чисел про сетап.", signal)
    ok, reason = validator.validate_post_text(text, signal)
    assert ok, reason


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
        needs_mp = fn.__code__.co_argcount > 0
        mp = _MiniMonkeypatch()
        try:
            if needs_mp:
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