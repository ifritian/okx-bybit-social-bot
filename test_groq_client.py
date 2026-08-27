#!/usr/bin/env python3
"""
Тесты groq_client.call_groq: ретрай на пустой ответ, ретрай на обрыв по
finish_reason="length", проброс 429 как GroqRateLimited с корректным
retry_after. requests.post замокан - реальных запросов не идёт.
"""
import types

import config
import groq_client


class _FakeResponse:
    def __init__(self, content, finish_reason="stop", status_code=200, headers=None):
        self._content = content
        self._finish_reason = finish_reason
        self.status_code = status_code
        self.headers = headers or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise Exception(f"HTTP {self.status_code}")

    def json(self):
        return {
            "choices": [
                {"message": {"content": self._content}, "finish_reason": self._finish_reason}
            ]
        }


def test_normal_response_passes_through(monkeypatch):
    monkeypatch.setattr(config, "GROQ_API_KEY", "key")
    monkeypatch.setattr(
        groq_client.requests, "post", lambda *a, **k: _FakeResponse("Нормальный хук про $BTC 🚀")
    )
    result = groq_client.call_groq("sys", "user")
    assert result == "Нормальный хук про $BTC 🚀"


def test_empty_response_retries_once_then_succeeds(monkeypatch):
    monkeypatch.setattr(config, "GROQ_API_KEY", "key")
    calls = []

    def _fake_post(*a, **k):
        calls.append(1)
        if len(calls) == 1:
            return _FakeResponse("")
        return _FakeResponse("Хук на второй попытке")

    monkeypatch.setattr(groq_client.requests, "post", _fake_post)
    result = groq_client.call_groq("sys", "user")
    assert result == "Хук на второй попытке"
    assert len(calls) == 2


def test_empty_response_twice_raises(monkeypatch):
    monkeypatch.setattr(config, "GROQ_API_KEY", "key")
    monkeypatch.setattr(groq_client.requests, "post", lambda *a, **k: _FakeResponse(""))
    try:
        groq_client.call_groq("sys", "user")
        raise AssertionError("ожидалось GroqEmptyResponse")
    except groq_client.GroqEmptyResponse:
        pass


def test_truncated_response_retries_with_bigger_max_tokens_then_succeeds(monkeypatch):
    monkeypatch.setattr(config, "GROQ_API_KEY", "key")
    seen_max_tokens = []

    def _fake_post(url, json, headers, timeout):
        seen_max_tokens.append(json["max_tokens"])
        if len(seen_max_tokens) == 1:
            return _FakeResponse("Оборванный хук на полусл", finish_reason="length")
        return _FakeResponse("Полный законченный хук.", finish_reason="stop")

    monkeypatch.setattr(groq_client.requests, "post", _fake_post)
    result = groq_client.call_groq("sys", "user", max_tokens=300)
    assert result == "Полный законченный хук."
    assert seen_max_tokens[0] == 300
    assert seen_max_tokens[1] == 600  # x2 множитель


def test_truncated_response_twice_raises(monkeypatch):
    monkeypatch.setattr(config, "GROQ_API_KEY", "key")
    monkeypatch.setattr(
        groq_client.requests, "post",
        lambda *a, **k: _FakeResponse("Всегда обрывается", finish_reason="length"),
    )
    try:
        groq_client.call_groq("sys", "user", max_tokens=300)
        raise AssertionError("ожидалось GroqTruncatedResponse")
    except groq_client.GroqTruncatedResponse:
        pass


def test_truncated_retry_tokens_capped_at_max(monkeypatch):
    monkeypatch.setattr(config, "GROQ_API_KEY", "key")
    seen_max_tokens = []

    def _fake_post(url, json, headers, timeout):
        seen_max_tokens.append(json["max_tokens"])
        return _FakeResponse("Всегда обрывается", finish_reason="length")

    monkeypatch.setattr(groq_client.requests, "post", _fake_post)
    try:
        groq_client.call_groq("sys", "user", max_tokens=1000)
    except groq_client.GroqTruncatedResponse:
        pass
    assert seen_max_tokens[1] <= groq_client._MAX_RETRY_TOKENS


def test_rate_limit_raises_with_retry_after_header(monkeypatch):
    monkeypatch.setattr(config, "GROQ_API_KEY", "key")
    monkeypatch.setattr(
        groq_client.requests, "post",
        lambda *a, **k: _FakeResponse("", status_code=429, headers={"Retry-After": "42"}),
    )
    try:
        groq_client.call_groq("sys", "user")
        raise AssertionError("ожидалось GroqRateLimited")
    except groq_client.GroqRateLimited as e:
        assert e.retry_after_seconds == 42.0


def test_rate_limit_without_header_uses_default_backoff(monkeypatch):
    monkeypatch.setattr(config, "GROQ_API_KEY", "key")
    monkeypatch.setattr(
        groq_client.requests, "post",
        lambda *a, **k: _FakeResponse("", status_code=429),
    )
    try:
        groq_client.call_groq("sys", "user")
        raise AssertionError("ожидалось GroqRateLimited")
    except groq_client.GroqRateLimited as e:
        assert e.retry_after_seconds == groq_client.DEFAULT_RATE_LIMIT_BACKOFF_SECONDS


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
