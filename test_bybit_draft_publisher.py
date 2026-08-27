#!/usr/bin/env python3
"""
Тесты bybit_draft_publisher.py: is_configured(), формирование
sendMessage/sendPhoto запросов (requests.post замокан, реальных
запросов не идёт).
"""
import config
import bybit_draft_publisher


class _FakeResponse:
    def __init__(self, json_data):
        self._json = json_data

    def json(self):
        return self._json


def test_is_configured_false_without_chat_id(monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setattr(config, "BYBIT_BYX_DRAFT_CHAT_ID", "")
    assert bybit_draft_publisher.is_configured() is False


def test_is_configured_true_with_token_and_chat_id(monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setattr(config, "BYBIT_BYX_DRAFT_CHAT_ID", "123456")
    assert bybit_draft_publisher.is_configured() is True


def test_send_draft_without_image_sends_text_message(monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setattr(config, "BYBIT_BYX_DRAFT_CHAT_ID", "123456")

    calls = []

    def _fake_post(url, timeout=30, **kwargs):
        calls.append((url, kwargs))
        return _FakeResponse({"ok": True, "result": {"message_id": 7}})

    monkeypatch.setattr(bybit_draft_publisher.requests, "post", _fake_post)

    result = bybit_draft_publisher.send_draft("Текст черновика поста.", "market_take")

    assert result["message_id"] == 7
    assert len(calls) == 1
    url, kwargs = calls[0]
    assert url.endswith("/sendMessage")
    assert kwargs["data"]["chat_id"] == "123456"
    assert "Текст черновика поста." in kwargs["data"]["text"]
    assert "Разбор рынка" in kwargs["data"]["text"]


def test_send_draft_raises_on_api_error(monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setattr(config, "BYBIT_BYX_DRAFT_CHAT_ID", "123456")

    def _fake_post(url, timeout=30, **kwargs):
        return _FakeResponse({"ok": False, "description": "Bad Request: chat not found"})

    monkeypatch.setattr(bybit_draft_publisher.requests, "post", _fake_post)

    try:
        bybit_draft_publisher.send_draft("Текст.", "trading_insight")
        raise AssertionError("ожидалось DraftDeliveryError")
    except bybit_draft_publisher.DraftDeliveryError:
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
