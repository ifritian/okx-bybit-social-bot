#!/usr/bin/env python3
"""
Тесты telegram_publisher.publish_poll - формирование запроса к sendPoll
(JSON-кодирование options, is_anonymous). requests.post замокан,
реальных запросов не идёт.
"""
import json

import config
import telegram_publisher


class _FakeResponse:
    def __init__(self, json_data):
        self._json = json_data

    def json(self):
        return self._json


def test_publish_poll_sends_json_encoded_options(monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setattr(config, "TELEGRAM_PUBLISH_CHANNEL", "@test_channel")

    calls = []

    def _fake_post(url, timeout=30, **kwargs):
        calls.append((url, kwargs))
        return _FakeResponse({"ok": True, "result": {"message_id": 42}})

    monkeypatch.setattr(telegram_publisher.requests, "post", _fake_post)

    result = telegram_publisher.publish_poll("Вопрос?", ["Вариант 1", "Вариант 2"])

    assert result["message_id"] == 42
    assert len(calls) == 1
    url, kwargs = calls[0]
    assert url.endswith("/sendPoll")
    sent_data = kwargs["data"]
    assert sent_data["question"] == "Вопрос?"
    assert json.loads(sent_data["options"]) == ["Вариант 1", "Вариант 2"]
    assert sent_data["is_anonymous"] is True


def test_publish_poll_raises_on_api_error(monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setattr(config, "TELEGRAM_PUBLISH_CHANNEL", "@test_channel")

    def _fake_post(url, timeout=30, **kwargs):
        return _FakeResponse({"ok": False, "description": "Bad Request: POLL_OPTIONS_INVALID"})

    monkeypatch.setattr(telegram_publisher.requests, "post", _fake_post)

    try:
        telegram_publisher.publish_poll("Вопрос?", ["Только один вариант"])
        raise AssertionError("ожидалось TelegramPublishError")
    except telegram_publisher.TelegramPublishError:
        pass


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
