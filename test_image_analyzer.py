#!/usr/bin/env python3
"""
Тесты image_analyzer.py: повтор при 429 и 5xx (серверные ошибки Groq -
модель распознавания картинок иногда нестабильна, будучи preview-
моделью), но НЕ при 404 (не транзиентная ошибка, повтор не поможет).
requests.post замокан, реальных запросов не идёт.
"""
import requests

import config
import image_analyzer


class _FakeResponse:
    def __init__(self, status_code, json_data=None, text=""):
        self.status_code = status_code
        self._json = json_data or {}
        self.text = text or str(json_data or "")

    def raise_for_status(self):
        if self.status_code >= 400:
            error = requests.exceptions.HTTPError(f"{self.status_code} error")
            error.response = self
            raise error

    def json(self):
        return self._json


_VALID_PAYLOAD = {"choices": [{"message": {"content": '{"ticker": "BTC", "direction": "up", "note": "растёт"}'}}]}


def test_analyze_retries_on_429_then_succeeds(monkeypatch):
    monkeypatch.setattr(config, "GROQ_API_KEY", "test-key")
    monkeypatch.setattr(image_analyzer.time, "sleep", lambda *_: None)

    calls = []

    def _fake_post(url, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            return _FakeResponse(429)
        return _FakeResponse(200, _VALID_PAYLOAD)

    monkeypatch.setattr(image_analyzer.requests, "post", _fake_post)

    result = image_analyzer.analyze_chart_image("https://example.com/chart.png")

    assert result is not None
    assert result.ticker == "BTC"
    assert len(calls) == 2


def test_analyze_retries_on_5xx_then_succeeds(monkeypatch):
    monkeypatch.setattr(config, "GROQ_API_KEY", "test-key")
    monkeypatch.setattr(image_analyzer.time, "sleep", lambda *_: None)

    calls = []

    def _fake_post(url, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            return _FakeResponse(503)
        return _FakeResponse(200, _VALID_PAYLOAD)

    monkeypatch.setattr(image_analyzer.requests, "post", _fake_post)

    result = image_analyzer.analyze_chart_image("https://example.com/chart.png")

    assert result is not None
    assert len(calls) == 2


def test_analyze_does_not_retry_on_404(monkeypatch):
    monkeypatch.setattr(config, "GROQ_API_KEY", "test-key")
    monkeypatch.setattr(image_analyzer.time, "sleep", lambda *_: None)

    calls = []

    def _fake_post(url, **kwargs):
        calls.append(1)
        return _FakeResponse(404)

    monkeypatch.setattr(image_analyzer.requests, "post", _fake_post)

    result = image_analyzer.analyze_chart_image("https://example.com/chart.png")

    assert result is None
    # Ни одной повторной попытки - 404 не транзиентная ошибка.
    assert len(calls) == 1


def test_analyze_gives_up_after_max_retries_on_persistent_429(monkeypatch):
    monkeypatch.setattr(config, "GROQ_API_KEY", "test-key")
    monkeypatch.setattr(image_analyzer.time, "sleep", lambda *_: None)

    calls = []

    def _fake_post(url, **kwargs):
        calls.append(1)
        return _FakeResponse(429)

    monkeypatch.setattr(image_analyzer.requests, "post", _fake_post)

    result = image_analyzer.analyze_chart_image("https://example.com/chart.png")

    assert result is None
    assert len(calls) == 3  # max_retries=3, все попытки исчерпаны


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
