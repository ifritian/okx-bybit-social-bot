#!/usr/bin/env python3
"""
Тесты alerting.py: троттлинг, отсутствие исключений наружу, поведение
без конфигурации. requests.post замокан - реальных запросов в Telegram
не идёт.
"""
import types

import alerting


class _FakeResponse:
    def __init__(self, ok=True, status=200):
        self._ok = ok
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise Exception(f"HTTP {self.status_code}")

    def json(self):
        return {"ok": self._ok}


def test_not_configured_returns_false_without_network(monkeypatch):
    monkeypatch.setattr(alerting.config, "TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setattr(alerting.config, "YOUR_USER_ID", None)

    def _boom(*a, **k):
        raise AssertionError("не должно быть сетевого запроса, если алертинг не настроен")

    monkeypatch.setattr(alerting.requests, "post", _boom)
    assert alerting.send_owner_alert("k", "msg") is False


def test_sends_when_configured(monkeypatch):
    monkeypatch.setattr(alerting.config, "TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.setattr(alerting.config, "YOUR_USER_ID", 123)
    monkeypatch.setattr(alerting.queue_manager, "get_last_alert_sent", lambda k: 0)
    sent = {}
    monkeypatch.setattr(alerting.queue_manager, "set_last_alert_sent", lambda k: sent.setdefault("key", k))
    monkeypatch.setattr(alerting.requests, "post", lambda *a, **k: _FakeResponse(ok=True))

    result = alerting.send_owner_alert("test_alert", "hello")
    assert result is True
    assert sent["key"] == "test_alert"


def test_throttled_within_min_repeat_hours(monkeypatch):
    monkeypatch.setattr(alerting.config, "TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.setattr(alerting.config, "YOUR_USER_ID", 123)
    import time
    monkeypatch.setattr(alerting.queue_manager, "get_last_alert_sent", lambda k: time.time() - 60)

    def _boom(*a, **k):
        raise AssertionError("не должно быть сетевого запроса при активном троттлинге")

    monkeypatch.setattr(alerting.requests, "post", _boom)
    result = alerting.send_owner_alert("test_alert", "hello", min_repeat_hours=6)
    assert result is False


def test_network_error_does_not_raise(monkeypatch):
    monkeypatch.setattr(alerting.config, "TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.setattr(alerting.config, "YOUR_USER_ID", 123)
    monkeypatch.setattr(alerting.queue_manager, "get_last_alert_sent", lambda k: 0)

    def _raise(*a, **k):
        raise alerting.requests.RequestException("boom")

    monkeypatch.setattr(alerting.requests, "post", _raise)
    # не должно бросать исключение наружу
    result = alerting.send_owner_alert("test_alert", "hello")
    assert result is False


def test_telegram_rejects_message_returns_false(monkeypatch):
    monkeypatch.setattr(alerting.config, "TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.setattr(alerting.config, "YOUR_USER_ID", 123)
    monkeypatch.setattr(alerting.queue_manager, "get_last_alert_sent", lambda k: 0)
    monkeypatch.setattr(alerting.requests, "post", lambda *a, **k: _FakeResponse(ok=False))

    result = alerting.send_owner_alert("test_alert", "hello")
    assert result is False


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
