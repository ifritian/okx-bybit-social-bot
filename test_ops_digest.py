#!/usr/bin/env python3
"""
Тесты ops_digest.py - все источники данных (queue_manager, outcome_tracker,
risk_guard/FuturesClient) замокан - без сети и без реального Telegram/Binance.
"""
import ops_digest


def _stats(count=0, win_rate=None, avg_pnl_pct=None, total_pnl_usdt=0.0):
    return {"count": count, "win_rate": win_rate, "avg_pnl_pct": avg_pnl_pct, "total_pnl_usdt": total_pnl_usdt}


def test_publishing_section_never_published():
    def fake_seconds_since_last_post(fmt):
        return float("inf")

    import queue_manager as real_qm
    orig = real_qm.seconds_since_last_post
    real_qm.seconds_since_last_post = fake_seconds_since_last_post
    try:
        text = ops_digest._publishing_section()
    finally:
        real_qm.seconds_since_last_post = orig
    assert "ещё ни одной не было" in text


def test_publishing_section_flags_when_over_threshold(monkeypatch):
    monkeypatch.setattr(ops_digest.queue_manager, "seconds_since_last_post", lambda fmt: 30 * 3600)
    monkeypatch.setattr(ops_digest.config, "DEAD_MANS_SWITCH_HOURS", 24.0)
    text = ops_digest._publishing_section()
    assert "30.0ч" in text
    assert "⚠️" in text


def test_publishing_section_no_flag_when_under_threshold(monkeypatch):
    monkeypatch.setattr(ops_digest.queue_manager, "seconds_since_last_post", lambda fmt: 2 * 3600)
    monkeypatch.setattr(ops_digest.config, "DEAD_MANS_SWITCH_HOURS", 24.0)
    text = ops_digest._publishing_section()
    assert "⚠️" not in text


def test_signal_stats_section_formats_win_rate(monkeypatch):
    def fake_accuracy_stats(days=None):
        if days == 1:
            return {"overall": _stats(count=3, win_rate=33.3)}
        return {"overall": _stats(count=40, win_rate=17.5)}

    monkeypatch.setattr(ops_digest.outcome_tracker, "get_accuracy_stats", fake_accuracy_stats)
    text = ops_digest._signal_stats_section()
    assert "n=3, win-rate=33.3%" in text
    assert "n=40, win-rate=17.5%" in text


def test_signal_stats_section_handles_no_data(monkeypatch):
    monkeypatch.setattr(ops_digest.outcome_tracker, "get_accuracy_stats",
                         lambda days=None: {"overall": _stats(count=0, win_rate=None)})
    text = ops_digest._signal_stats_section()
    assert "win-rate=н/д" in text


def test_futures_stats_section(monkeypatch):
    def fake_futures_stats(days=None):
        if days == 1:
            return {"overall": _stats(count=1, win_rate=100.0, total_pnl_usdt=5.0)}
        return {"overall": _stats(count=4, win_rate=50.0, total_pnl_usdt=12.34)}

    monkeypatch.setattr(ops_digest.outcome_tracker, "get_futures_trade_stats", fake_futures_stats)
    monkeypatch.setattr(ops_digest.queue_manager, "get_open_futures_positions", lambda: [{"symbol": "BTCUSDT"}])
    text = ops_digest._futures_stats_section()
    assert "n=1, win-rate=100.0%" in text
    assert "суммарный PnL=+12.3400 USDT" in text
    assert "сейчас открыто: 1" in text


def test_risk_section_without_keys_configured(monkeypatch):
    monkeypatch.delenv("BINANCE_FUTURES_API_KEY", raising=False)
    monkeypatch.delenv("BINANCE_FUTURES_API_SECRET", raising=False)
    text = ops_digest._risk_section()
    assert "недоступен" in text


def test_risk_section_with_keys_reports_kill_switch(monkeypatch):
    monkeypatch.setenv("BINANCE_FUTURES_API_KEY", "k")
    monkeypatch.setenv("BINANCE_FUTURES_API_SECRET", "s")
    monkeypatch.setattr(ops_digest.risk_guard, "limits_from_config", lambda cfg: object())
    monkeypatch.setattr(ops_digest.risk_guard, "status", lambda client, limits: {
        "kill_switch": True, "open_positions": 2, "open_positions_symbols": ["BTCUSDT", "ETHUSDT"],
        "max_open_positions": 3, "daily_loss_pct": -6.2, "max_daily_loss_pct": 5.0,
        "consecutive_losses": 3, "max_consecutive_losses": 3,
    })
    # FuturesClient конструктор не должен реально стучаться в сеть -
    # подменяем на заглушку прямо в модуле futures_client.
    import futures_client
    monkeypatch.setattr(futures_client, "FuturesClient", lambda **kw: object())

    text = ops_digest._risk_section()
    assert "KILL SWITCH ВЗВЕДЁН" in text
    assert "2/3" in text
    assert "BTCUSDT, ETHUSDT" in text


def test_risk_section_degrades_gracefully_on_api_error(monkeypatch):
    monkeypatch.setenv("BINANCE_FUTURES_API_KEY", "k")
    monkeypatch.setenv("BINANCE_FUTURES_API_SECRET", "s")
    import futures_client

    def _boom(**kw):
        raise RuntimeError("network down")

    monkeypatch.setattr(futures_client, "FuturesClient", _boom)
    text = ops_digest._risk_section()
    assert "ошибка" in text


def test_build_digest_combines_all_sections(monkeypatch):
    monkeypatch.setattr(ops_digest, "_publishing_section", lambda: "PUB")
    monkeypatch.setattr(ops_digest, "_signal_stats_section", lambda: "SIG")
    monkeypatch.setattr(ops_digest, "_futures_stats_section", lambda: "FUT")
    monkeypatch.setattr(ops_digest, "_risk_section", lambda: "RISK")
    digest = ops_digest.build_digest()
    assert digest == "PUB\n\nSIG\n\nFUT\n\nRISK"


def test_main_uses_neutral_prefix_not_alert(monkeypatch):
    # Регресс-тест на осознанное решение: рутинный дайджест НЕ должен
    # идти с тем же "⚠️ Bot alert" префиксом, что и настоящие тревоги
    # (см. docstring alerting.send_owner_alert про новый параметр prefix).
    captured = {}

    def fake_send_owner_alert(alert_key, message, min_repeat_hours=6, prefix="\u26a0\ufe0f Bot alert"):
        captured["prefix"] = prefix
        captured["alert_key"] = alert_key
        return True

    monkeypatch.setattr(ops_digest, "_is_end_of_day_window", lambda: True)
    monkeypatch.setattr(ops_digest, "build_digest", lambda: "digest text")
    monkeypatch.setattr(ops_digest.alerting, "send_owner_alert", fake_send_owner_alert)

    ops_digest.main()

    assert captured["alert_key"] == "daily_ops_digest"
    assert captured["prefix"] != "\u26a0\ufe0f Bot alert"


# --- Гейт "конец дня" (config.OPS_DIGEST_HOUR_UTC) ---

def test_is_end_of_day_window_true_at_and_after_threshold_hour(monkeypatch):
    monkeypatch.setattr(ops_digest.config, "OPS_DIGEST_HOUR_UTC", 23)

    class _FakeDatetime(ops_digest.datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 7, 27, 23, 0, tzinfo=ops_digest.timezone.utc)

    monkeypatch.setattr(ops_digest, "datetime", _FakeDatetime)
    assert ops_digest._is_end_of_day_window() is True


def test_is_end_of_day_window_false_before_threshold_hour(monkeypatch):
    monkeypatch.setattr(ops_digest.config, "OPS_DIGEST_HOUR_UTC", 23)

    class _FakeDatetime(ops_digest.datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 7, 27, 14, 0, tzinfo=ops_digest.timezone.utc)

    monkeypatch.setattr(ops_digest, "datetime", _FakeDatetime)
    assert ops_digest._is_end_of_day_window() is False


def test_main_skips_without_attempting_send_outside_window(monkeypatch):
    monkeypatch.setattr(ops_digest, "_is_end_of_day_window", lambda: False)

    def _boom():
        raise AssertionError("build_digest не должен вызываться вне окна конца дня")
    monkeypatch.setattr(ops_digest, "build_digest", _boom)

    called = []
    monkeypatch.setattr(ops_digest.alerting, "send_owner_alert", lambda *a, **k: called.append(1) or True)

    result = ops_digest.main()

    assert result == 0
    assert called == []
