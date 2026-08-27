#!/usr/bin/env python3
"""
Тесты risk_guard.py - на ПОДДЕЛЬНОМ FuturesClient (никакой реальной
сети) и с monkeypatch queue_manager (никакой реальной SQLite - как в
test_strategy_tuner.py)."""
import time

import risk_guard


class _FakeClient:
    """Имитирует ровно те методы FuturesClient, которые нужны
    risk_guard - каждый настраивается напрямую полем, без сети."""

    def __init__(self, positions=None, wallet_balance=10_000.0, income_rows=None):
        self.positions = positions or []
        self.wallet_balance = wallet_balance
        self.income_rows = income_rows or []
        self.calls = []

    def get_all_positions(self):
        self.calls.append("get_all_positions")
        return self.positions

    def get_wallet_balance(self, asset="USDT"):
        self.calls.append("get_wallet_balance")
        return self.wallet_balance

    def get_income_history(self, income_type="REALIZED_PNL", start_time_ms=None, limit=1000):
        self.calls.append("get_income_history")
        return self.income_rows


def _limits(max_open=3, max_daily_loss_pct=5.0, max_consecutive_losses=3, max_same_direction=None,
            max_beta_exposure=None):
    return risk_guard.RiskLimits(
        max_open_positions=max_open,
        max_daily_loss_pct=max_daily_loss_pct,
        max_consecutive_losses=max_consecutive_losses,
        max_same_direction_positions=max_same_direction,
        max_beta_exposure=max_beta_exposure,
    )


def _income(pnls):
    """pnls - список чисел в хронологическом порядке (старые первыми) -
    сознательно возвращаем их в ОБРАТНОМ порядке от client.get_income_history,
    чтобы проверить, что risk_guard сам сортирует по времени, а не
    полагается на порядок ответа биржи."""
    rows = [{"income": str(p), "time": i} for i, p in enumerate(pnls)]
    return list(reversed(rows))


# --- kill switch: блокирует немедленно, без единого сетевого вызова ---

def test_kill_switch_blocks_before_any_client_call(monkeypatch):
    monkeypatch.setattr(risk_guard.queue_manager, "get_kill_switch",
                         lambda: {"reason": "тестовая причина", "tripped_at": 0})
    client = _FakeClient()
    reason = risk_guard.check_new_position_allowed(client, _limits())
    assert reason is not None
    assert "тестовая причина" in reason
    assert client.calls == []  # ни один метод клиента не должен был вызваться


# --- автоснятие kill switch по таймауту (см. RiskLimits.kill_switch_auto_reset_hours) ---

def test_kill_switch_not_auto_reset_when_disabled(monkeypatch):
    # взведён 100ч назад, но kill_switch_auto_reset_hours не задан (None/дефолт) -
    # старое поведение: держим заблокированным, никакого автоснятия
    monkeypatch.setattr(risk_guard.queue_manager, "get_kill_switch",
                         lambda: {"reason": "старая причина", "tripped_at": time.time() - 100 * 3600})
    cleared = []
    monkeypatch.setattr(risk_guard.queue_manager, "clear_kill_switch", lambda: cleared.append(True))
    client = _FakeClient()
    reason = risk_guard.check_new_position_allowed(client, _limits())  # auto_reset не задан
    assert reason is not None
    assert cleared == []


def test_kill_switch_not_auto_reset_before_threshold(monkeypatch):
    # взведён 1ч назад, порог 24ч - ещё рано
    monkeypatch.setattr(risk_guard.queue_manager, "get_kill_switch",
                         lambda: {"reason": "причина", "tripped_at": time.time() - 1 * 3600})
    cleared = []
    monkeypatch.setattr(risk_guard.queue_manager, "clear_kill_switch", lambda: cleared.append(True))
    client = _FakeClient()
    limits = _limits()
    limits.kill_switch_auto_reset_hours = 24
    reason = risk_guard.check_new_position_allowed(client, limits)
    assert reason is not None
    assert cleared == []


def test_kill_switch_auto_reset_after_threshold_allows_trade(monkeypatch):
    # взведён 25ч назад, порог 24ч - должен сняться сам и пропустить проверку дальше
    monkeypatch.setattr(risk_guard.queue_manager, "get_kill_switch",
                         lambda: {"reason": "старая причина", "tripped_at": time.time() - 25 * 3600})
    cleared = []
    monkeypatch.setattr(risk_guard.queue_manager, "clear_kill_switch", lambda: cleared.append(True))
    monkeypatch.setattr(risk_guard.queue_manager, "get_risk_daily_baseline", lambda day: 10_000.0)
    monkeypatch.setattr(risk_guard.queue_manager, "set_risk_daily_baseline", lambda day, bal: None)
    monkeypatch.setattr(risk_guard.queue_manager, "get_risk_streak_ignore_before", lambda: None)
    client = _FakeClient(wallet_balance=10_000.0, income_rows=[])
    limits = _limits()
    limits.kill_switch_auto_reset_hours = 24
    reason = risk_guard.check_new_position_allowed(client, limits)
    assert reason is None  # реально снялся и разрешил сделку
    assert cleared == [True]  # clear_kill_switch был вызван РОВНО один раз


def test_kill_switch_auto_reset_ignores_old_streak_via_since_ts(monkeypatch):
    # ключевой сценарий: старая серия убытков НЕ должна немедленно взвести
    # switch обратно после автоснятия - since_ts отсекает старые сделки
    monkeypatch.setattr(risk_guard.queue_manager, "get_kill_switch",
                         lambda: {"reason": "3 убытка подряд", "tripped_at": time.time() - 25 * 3600})
    monkeypatch.setattr(risk_guard.queue_manager, "clear_kill_switch", lambda: None)
    monkeypatch.setattr(risk_guard.queue_manager, "get_risk_daily_baseline", lambda day: 10_000.0)
    monkeypatch.setattr(risk_guard.queue_manager, "set_risk_daily_baseline", lambda day, bal: None)
    # ВСЯ история - старые убытки, случившиеся ДО точки отсечения:
    since_ts = time.time() - 10 * 3600  # отметка "снято 10ч назад"
    monkeypatch.setattr(risk_guard.queue_manager, "get_risk_streak_ignore_before", lambda: since_ts)
    old_loss_time_ms = int((since_ts - 3600) * 1000)  # на час РАНЬШЕ отметки - должен игнорироваться
    client = _FakeClient(
        wallet_balance=10_000.0,
        income_rows=[
            {"income": "-10.0", "time": old_loss_time_ms},
            {"income": "-10.0", "time": old_loss_time_ms + 1000},
            {"income": "-10.0", "time": old_loss_time_ms + 2000},
        ],
    )
    limits = _limits(max_consecutive_losses=3)
    limits.kill_switch_auto_reset_hours = 24
    reason = risk_guard.check_new_position_allowed(client, limits)
    assert reason is None  # старые 3 убытка отсечены since_ts - не взводят switch заново


def test_get_risk_multiplier_respects_since_ts(monkeypatch):
    since_ts = time.time() - 3600
    monkeypatch.setattr(risk_guard.queue_manager, "get_risk_streak_ignore_before", lambda: since_ts)
    old_time_ms = int((since_ts - 100) * 1000)  # до отметки - должен быть проигнорирован
    client = _FakeClient(income_rows=[{"income": "-5.0", "time": old_time_ms}])
    mult, streak = risk_guard.get_risk_multiplier(client, _limits(max_consecutive_losses=3))
    assert streak == 0
    assert mult == 1.0


# --- лимит открытых позиций ---

def test_max_open_positions_blocks(monkeypatch):
    monkeypatch.setattr(risk_guard.queue_manager, "get_kill_switch", lambda: None)
    client = _FakeClient(positions=[{"symbol": "BTCUSDT"}, {"symbol": "ETHUSDT"}, {"symbol": "SOLUSDT"}])
    reason = risk_guard.check_new_position_allowed(client, _limits(max_open=3))
    assert reason is not None
    assert "3/3" in reason
    assert "BTCUSDT" in reason and "ETHUSDT" in reason and "SOLUSDT" in reason


def test_open_positions_under_limit_does_not_block_on_that_check(monkeypatch):
    monkeypatch.setattr(risk_guard.queue_manager, "get_kill_switch", lambda: None)
    monkeypatch.setattr(risk_guard.queue_manager, "get_risk_daily_baseline", lambda day: 10_000.0)
    monkeypatch.setattr(risk_guard.queue_manager, "set_risk_daily_baseline", lambda day, bal: None)
    client = _FakeClient(positions=[{"symbol": "BTCUSDT"}], wallet_balance=10_000.0, income_rows=[])
    reason = risk_guard.check_new_position_allowed(client, _limits(max_open=3))
    assert reason is None


# --- дневной лимит убытка ---

def test_daily_loss_trips_kill_switch(monkeypatch):
    tripped = {}
    monkeypatch.setattr(risk_guard.queue_manager, "get_kill_switch", lambda: None)
    monkeypatch.setattr(risk_guard.queue_manager, "get_risk_daily_baseline", lambda day: 10_000.0)
    monkeypatch.setattr(risk_guard.queue_manager, "set_risk_daily_baseline", lambda day, bal: None)
    monkeypatch.setattr(risk_guard.queue_manager, "set_kill_switch", lambda reason: tripped.setdefault("reason", reason))

    # баланс упал с 10000 до 9400 = ровно 6% убытка, лимит 5%
    client = _FakeClient(positions=[], wallet_balance=9_400.0, income_rows=[])
    reason = risk_guard.check_new_position_allowed(client, _limits(max_daily_loss_pct=5.0))
    assert reason is not None
    assert "KILL SWITCH" in reason
    assert "reason" in tripped  # queue_manager.set_kill_switch реально был вызван


def test_daily_loss_under_limit_does_not_trip(monkeypatch):
    monkeypatch.setattr(risk_guard.queue_manager, "get_kill_switch", lambda: None)
    monkeypatch.setattr(risk_guard.queue_manager, "get_risk_daily_baseline", lambda day: 10_000.0)
    monkeypatch.setattr(risk_guard.queue_manager, "set_risk_daily_baseline", lambda day, bal: None)

    def _fail_if_called(reason):
        raise AssertionError("set_kill_switch не должен был вызываться")

    monkeypatch.setattr(risk_guard.queue_manager, "set_kill_switch", _fail_if_called)
    client = _FakeClient(positions=[], wallet_balance=9_600.0, income_rows=[])  # -4%, лимит 5%
    reason = risk_guard.check_new_position_allowed(client, _limits(max_daily_loss_pct=5.0))
    assert reason is None


def test_daily_baseline_set_once_on_first_check(monkeypatch):
    stored = {}
    monkeypatch.setattr(risk_guard.queue_manager, "get_risk_daily_baseline", lambda day: stored.get(day))
    monkeypatch.setattr(risk_guard.queue_manager, "set_risk_daily_baseline", lambda day, bal: stored.__setitem__(day, bal))

    client = _FakeClient(wallet_balance=10_000.0)
    loss_pct, baseline, current = risk_guard._daily_loss_pct(client)
    assert baseline == 10_000.0 and loss_pct == 0.0

    # баланс изменился, но baseline за сегодня уже зафиксирован - должен остаться прежним
    client.wallet_balance = 9_000.0
    loss_pct2, baseline2, current2 = risk_guard._daily_loss_pct(client)
    assert baseline2 == 10_000.0  # НЕ пересчитался
    assert round(loss_pct2, 2) == 10.0


# --- серия убытков подряд ---

def test_consecutive_losses_trips_kill_switch(monkeypatch):
    tripped = {}
    monkeypatch.setattr(risk_guard.queue_manager, "get_kill_switch", lambda: None)
    monkeypatch.setattr(risk_guard.queue_manager, "get_risk_daily_baseline", lambda day: 10_000.0)
    monkeypatch.setattr(risk_guard.queue_manager, "set_risk_daily_baseline", lambda day, bal: None)
    monkeypatch.setattr(risk_guard.queue_manager, "set_kill_switch", lambda reason: tripped.setdefault("reason", reason))

    # 3 убытка подряд, лимит 3
    client = _FakeClient(positions=[], wallet_balance=10_000.0, income_rows=_income([-10, -20, -30]))
    reason = risk_guard.check_new_position_allowed(client, _limits(max_consecutive_losses=3))
    assert reason is not None
    assert "3 убыточных" in reason
    assert "reason" in tripped


def test_streak_broken_by_a_win_does_not_trip(monkeypatch):
    monkeypatch.setattr(risk_guard.queue_manager, "get_kill_switch", lambda: None)
    monkeypatch.setattr(risk_guard.queue_manager, "get_risk_daily_baseline", lambda day: 10_000.0)
    monkeypatch.setattr(risk_guard.queue_manager, "set_risk_daily_baseline", lambda day, bal: None)

    def _fail_if_called(reason):
        raise AssertionError("set_kill_switch не должен был вызываться")

    monkeypatch.setattr(risk_guard.queue_manager, "set_kill_switch", _fail_if_called)
    # два убытка, потом выигрыш (последняя сделка) - серия оборвана, несмотря на два убытка до неё
    client = _FakeClient(positions=[], wallet_balance=10_000.0, income_rows=_income([-10, -20, 15]))
    reason = risk_guard.check_new_position_allowed(client, _limits(max_consecutive_losses=3))
    assert reason is None


def test_consecutive_losses_ignores_zero_income_rows():
    # 0.0 income (например, комиссийная запись без реального закрытия) - не считается ни выигрышем, ни проигрышем
    client = _FakeClient(income_rows=_income([-10, 0, -20]))
    streak = risk_guard._consecutive_losses(client)
    assert streak == 2


def test_consecutive_losses_sorts_by_time_itself():
    """Проверяем, что порядок ответа биржи не важен - risk_guard сам
    сортирует записи по полю 'time' перед подсчётом серии."""
    rows = [{"income": "-5", "time": 2}, {"income": "10", "time": 1}, {"income": "-7", "time": 3}]
    client = _FakeClient(income_rows=rows)
    # хронологически: +10, -5, -7 -> серия из последних двух отрицательных = 2
    streak = risk_guard._consecutive_losses(client)
    assert streak == 2


# --- мягкое снижение риска (get_risk_multiplier) ---

def test_risk_multiplier_full_when_no_losses():
    client = _FakeClient(income_rows=_income([10, -5, 10]))  # серия из 0 (последняя сделка - выигрыш)
    limits = _limits()  # soft_derisk_after_losses=2 по дефолту дата-класса
    multiplier, streak = risk_guard.get_risk_multiplier(client, limits)
    assert multiplier == 1.0
    assert streak == 0


def test_risk_multiplier_full_below_threshold():
    # 1 убыток подряд - меньше soft_derisk_after_losses=2 - риск ещё полный
    client = _FakeClient(income_rows=_income([10, -5]))
    limits = risk_guard.RiskLimits(max_open_positions=3, max_daily_loss_pct=5.0,
                                    max_consecutive_losses=3, soft_derisk_after_losses=2,
                                    soft_derisk_multiplier=0.5)
    multiplier, streak = risk_guard.get_risk_multiplier(client, limits)
    assert multiplier == 1.0
    assert streak == 1


def test_risk_multiplier_reduced_at_threshold():
    # ровно 2 убытка подряд - порог достигнут - риск снижен
    client = _FakeClient(income_rows=_income([10, -5, -3]))
    limits = risk_guard.RiskLimits(max_open_positions=3, max_daily_loss_pct=5.0,
                                    max_consecutive_losses=3, soft_derisk_after_losses=2,
                                    soft_derisk_multiplier=0.5)
    multiplier, streak = risk_guard.get_risk_multiplier(client, limits)
    assert multiplier == 0.5
    assert streak == 2


def test_risk_multiplier_stays_reduced_beyond_threshold_but_before_kill_switch():
    # 2 убытка подряд - между soft_derisk_after_losses(2) и
    # max_consecutive_losses(3) - риск снижен, но kill switch ещё не взведён
    # (это проверяет отдельно check_new_position_allowed, не эта функция).
    client = _FakeClient(income_rows=_income([10, -5, -3]))
    limits = risk_guard.RiskLimits(max_open_positions=3, max_daily_loss_pct=5.0,
                                    max_consecutive_losses=3, soft_derisk_after_losses=2,
                                    soft_derisk_multiplier=0.5)
    multiplier, streak = risk_guard.get_risk_multiplier(client, limits)
    assert multiplier == 0.5
    assert streak < limits.max_consecutive_losses  # kill switch по этому лимиту ещё не должен сработать


# --- A4: лимит на позиции в одну сторону одновременно ---

def test_same_direction_open_count_counts_by_sign():
    positions = [
        {"symbol": "BTCUSDT", "positionAmt": "1.5"},   # лонг
        {"symbol": "ETHUSDT", "positionAmt": "2.0"},   # лонг
        {"symbol": "SOLUSDT", "positionAmt": "-3.0"},  # шорт
    ]
    assert risk_guard._same_direction_open_count(positions, "BUY") == 2
    assert risk_guard._same_direction_open_count(positions, "SELL") == 1


def test_same_direction_limit_blocks_when_reached(monkeypatch):
    monkeypatch.setattr(risk_guard.queue_manager, "get_kill_switch", lambda: None)
    positions = [
        {"symbol": "BTCUSDT", "positionAmt": "1.0"},
        {"symbol": "ETHUSDT", "positionAmt": "2.0"},
    ]
    client = _FakeClient(positions=positions)
    # 2/2 лонга уже открыто, лимит на сторону = 2 - новый лонг блокируется
    # (max_open=3, так что это НЕ лимит общего числа позиций, а именно A4)
    reason = risk_guard.check_new_position_allowed(client, _limits(max_open=3, max_same_direction=2), side="BUY")
    assert reason is not None
    assert "2/2" in reason
    assert "лонг" in reason


def test_same_direction_limit_allows_opposite_side(monkeypatch):
    monkeypatch.setattr(risk_guard.queue_manager, "get_kill_switch", lambda: None)
    monkeypatch.setattr(risk_guard.queue_manager, "get_risk_daily_baseline", lambda day: 10_000.0)
    monkeypatch.setattr(risk_guard.queue_manager, "set_risk_daily_baseline", lambda day, bal: None)
    positions = [
        {"symbol": "BTCUSDT", "positionAmt": "1.0"},
        {"symbol": "ETHUSDT", "positionAmt": "2.0"},
    ]
    client = _FakeClient(positions=positions, wallet_balance=10_000.0)
    # те же 2 лонга не мешают открыть ШОРТ - лимит считается раздельно по стороне
    reason = risk_guard.check_new_position_allowed(client, _limits(max_open=3, max_same_direction=2), side="SELL")
    assert reason is None


def test_same_direction_limit_disabled_when_not_configured(monkeypatch):
    monkeypatch.setattr(risk_guard.queue_manager, "get_kill_switch", lambda: None)
    monkeypatch.setattr(risk_guard.queue_manager, "get_risk_daily_baseline", lambda day: 10_000.0)
    monkeypatch.setattr(risk_guard.queue_manager, "set_risk_daily_baseline", lambda day, bal: None)
    positions = [{"symbol": "BTCUSDT", "positionAmt": "1.0"}, {"symbol": "ETHUSDT", "positionAmt": "2.0"}]
    client = _FakeClient(positions=positions, wallet_balance=10_000.0)
    # max_same_direction=None (дефолт _limits()) - проверка A4 полностью пропущена
    reason = risk_guard.check_new_position_allowed(client, _limits(max_open=3), side="BUY")
    assert reason is None


def test_same_direction_limit_skipped_when_side_not_passed(monkeypatch):
    monkeypatch.setattr(risk_guard.queue_manager, "get_kill_switch", lambda: None)
    monkeypatch.setattr(risk_guard.queue_manager, "get_risk_daily_baseline", lambda day: 10_000.0)
    monkeypatch.setattr(risk_guard.queue_manager, "set_risk_daily_baseline", lambda day, bal: None)
    positions = [{"symbol": "BTCUSDT", "positionAmt": "1.0"}, {"symbol": "ETHUSDT", "positionAmt": "2.0"}]
    client = _FakeClient(positions=positions, wallet_balance=10_000.0)
    # side не передан (старый вызывающий код/тест) - A4 не проверяется вовсе,
    # несмотря на настроенный лимит - обратная совместимость.
    reason = risk_guard.check_new_position_allowed(client, _limits(max_open=3, max_same_direction=2))
    assert reason is None


# --- всё в норме ---

def test_all_clear_returns_none(monkeypatch):
    monkeypatch.setattr(risk_guard.queue_manager, "get_kill_switch", lambda: None)
    monkeypatch.setattr(risk_guard.queue_manager, "get_risk_daily_baseline", lambda day: 10_000.0)
    monkeypatch.setattr(risk_guard.queue_manager, "set_risk_daily_baseline", lambda day, bal: None)
    client = _FakeClient(positions=[{"symbol": "BTCUSDT"}], wallet_balance=10_100.0, income_rows=_income([10, 20]))
    reason = risk_guard.check_new_position_allowed(client, _limits(max_open=3, max_daily_loss_pct=5.0, max_consecutive_losses=3))
    assert reason is None


# --- P3.7: бета-взвешенная экспозиция (config.BINANCE_FUTURES_MAX_BETA_EXPOSURE) ---

def test_calc_beta_matches_hand_computed_value():
    # BTC доходности: [0.02, -0.01, 0.03, 0.00, 0.01] (variance != 0).
    # Символ идёт РОВНО в 2 раза сильнее BTC каждый день - бета должна
    # сойтись точно к 2.0 (никакого шума, чистая линейная связь).
    btc = [0.02, -0.01, 0.03, 0.00, 0.01]
    sym = [x * 2 for x in btc]
    beta = risk_guard._calc_beta(sym, btc)
    assert beta == pytest_approx_or_close(2.0, 1e-9)


def test_calc_beta_none_with_too_few_points():
    assert risk_guard._calc_beta([0.01, 0.02], [0.01, 0.02]) is None


def test_calc_beta_none_when_btc_variance_zero():
    # BTC "доходности" постоянны (variance=0) - деление на 0, мягкий None.
    btc = [0.01] * 6
    sym = [0.01, 0.02, -0.01, 0.03, 0.0, 0.01]
    assert risk_guard._calc_beta(sym, btc) is None


def test_get_symbol_beta_btc_itself_is_always_one(monkeypatch):
    # Тривиальный случай не должен даже пытаться идти в кэш/сеть.
    monkeypatch.setattr(risk_guard.queue_manager, "get_cached_symbol_beta",
                         lambda symbol: (_ for _ in ()).throw(AssertionError("не должно вызываться для BTCUSDT")))
    assert risk_guard._get_symbol_beta("BTCUSDT") == 1.0


def test_get_symbol_beta_uses_fresh_cache(monkeypatch):
    monkeypatch.setattr(risk_guard.config, "SYMBOL_BETA_CACHE_TTL_HOURS", 24.0)
    monkeypatch.setattr(risk_guard.queue_manager, "get_cached_symbol_beta", lambda symbol: (1.7, time.time()))

    def _boom(symbol, lookback_days):
        raise AssertionError("не должно ходить за свежими данными, если кэш свежий")
    monkeypatch.setattr(risk_guard, "_fetch_daily_log_returns", _boom)

    assert risk_guard._get_symbol_beta("SOLUSDT") == 1.7


def test_get_symbol_beta_recomputes_when_cache_stale(monkeypatch):
    monkeypatch.setattr(risk_guard.config, "SYMBOL_BETA_CACHE_TTL_HOURS", 24.0)
    stale_ts = time.time() - 25 * 3600  # старше TTL
    monkeypatch.setattr(risk_guard.queue_manager, "get_cached_symbol_beta", lambda symbol: (1.7, stale_ts))
    saved = {}
    monkeypatch.setattr(risk_guard.queue_manager, "set_cached_symbol_beta", lambda symbol, beta: saved.setdefault(symbol, beta))

    btc = [0.02, -0.01, 0.03, 0.00, 0.01]
    sym = [x * 1.5 for x in btc]
    monkeypatch.setattr(risk_guard, "_fetch_daily_log_returns",
                         lambda symbol, lookback_days: sym if symbol == "SOLUSDT" else btc)

    beta = risk_guard._get_symbol_beta("SOLUSDT")
    assert beta == pytest_approx_or_close(1.5, 1e-9)
    assert saved["SOLUSDT"] == pytest_approx_or_close(1.5, 1e-9)


def test_get_symbol_beta_none_when_calc_fails_does_not_cache(monkeypatch):
    monkeypatch.setattr(risk_guard.queue_manager, "get_cached_symbol_beta", lambda symbol: None)
    saved = {}
    monkeypatch.setattr(risk_guard.queue_manager, "set_cached_symbol_beta", lambda symbol, beta: saved.setdefault(symbol, beta))
    monkeypatch.setattr(risk_guard, "_fetch_daily_log_returns", lambda symbol, lookback_days: [])  # недостаточно данных

    assert risk_guard._get_symbol_beta("SOLUSDT") is None
    assert saved == {}


def test_beta_weighted_exposure_sums_only_matching_side(monkeypatch):
    positions = [
        {"symbol": "SOLUSDT", "positionAmt": "1.0"},   # лонг, бета 1.5
        {"symbol": "ETHUSDT", "positionAmt": "1.0"},   # лонг, бета 0.8
        {"symbol": "XRPUSDT", "positionAmt": "-1.0"},  # шорт - не считается для BUY
    ]
    betas = {"SOLUSDT": 1.5, "ETHUSDT": 0.8, "XRPUSDT": 3.0}
    monkeypatch.setattr(risk_guard, "_get_symbol_beta", lambda symbol: betas[symbol])

    assert risk_guard._beta_weighted_exposure(positions, "BUY") == pytest_approx_or_close(2.3, 1e-9)
    assert risk_guard._beta_weighted_exposure(positions, "SELL") == pytest_approx_or_close(3.0, 1e-9)


def test_beta_weighted_exposure_falls_back_to_one_when_beta_unknown(monkeypatch):
    # Бету посчитать не удалось (None) - учитываем как "средний вес"
    # (1.0), а не пропускаем позицию вовсе - см. docstring
    # _beta_weighted_exposure про то, почему НЕ 0.
    positions = [{"symbol": "SOLUSDT", "positionAmt": "1.0"}]
    monkeypatch.setattr(risk_guard, "_get_symbol_beta", lambda symbol: None)
    assert risk_guard._beta_weighted_exposure(positions, "BUY") == 1.0


def test_beta_exposure_limit_blocks_when_prospective_exceeds(monkeypatch):
    monkeypatch.setattr(risk_guard.queue_manager, "get_kill_switch", lambda: None)
    positions = [{"symbol": "SOLUSDT", "positionAmt": "1.0"}]  # бета 1.5, уже в лонге
    monkeypatch.setattr(risk_guard, "_get_symbol_beta", lambda symbol: {"SOLUSDT": 1.5, "ETHUSDT": 1.0}[symbol])
    client = _FakeClient(positions=positions)

    # текущая экспозиция 1.5 + новая ETH (бета 1.0) = 2.5 > лимита 2.0
    reason = risk_guard.check_new_position_allowed(
        client, _limits(max_open=5, max_beta_exposure=2.0), side="BUY", symbol="ETHUSDT",
    )
    assert reason is not None
    assert "бета-экспозиция" in reason


def test_beta_exposure_limit_allows_when_prospective_within_limit(monkeypatch):
    monkeypatch.setattr(risk_guard.queue_manager, "get_kill_switch", lambda: None)
    monkeypatch.setattr(risk_guard.queue_manager, "get_risk_daily_baseline", lambda day: 10_000.0)
    monkeypatch.setattr(risk_guard.queue_manager, "set_risk_daily_baseline", lambda day, bal: None)
    positions = [{"symbol": "SOLUSDT", "positionAmt": "1.0"}]  # бета 0.5
    monkeypatch.setattr(risk_guard, "_get_symbol_beta", lambda symbol: {"SOLUSDT": 0.5, "ETHUSDT": 1.0}[symbol])
    client = _FakeClient(positions=positions, wallet_balance=10_000.0)

    # 0.5 + 1.0 = 1.5, укладывается в лимит 2.0
    reason = risk_guard.check_new_position_allowed(
        client, _limits(max_open=5, max_beta_exposure=2.0), side="BUY", symbol="ETHUSDT",
    )
    assert reason is None


def test_beta_exposure_limit_disabled_when_not_configured(monkeypatch):
    monkeypatch.setattr(risk_guard.queue_manager, "get_kill_switch", lambda: None)
    monkeypatch.setattr(risk_guard.queue_manager, "get_risk_daily_baseline", lambda day: 10_000.0)
    monkeypatch.setattr(risk_guard.queue_manager, "set_risk_daily_baseline", lambda day, bal: None)
    positions = [{"symbol": "SOLUSDT", "positionAmt": "1.0"}]
    monkeypatch.setattr(risk_guard, "_get_symbol_beta",
                         lambda symbol: (_ for _ in ()).throw(AssertionError("бета не должна считаться при выключенном лимите")))
    client = _FakeClient(positions=positions, wallet_balance=10_000.0)

    # max_beta_exposure=None (дефолт _limits()) - проверка полностью пропущена
    reason = risk_guard.check_new_position_allowed(client, _limits(max_open=5), side="BUY", symbol="ETHUSDT")
    assert reason is None


def test_beta_exposure_limit_skipped_when_symbol_not_passed(monkeypatch):
    monkeypatch.setattr(risk_guard.queue_manager, "get_kill_switch", lambda: None)
    monkeypatch.setattr(risk_guard.queue_manager, "get_risk_daily_baseline", lambda day: 10_000.0)
    monkeypatch.setattr(risk_guard.queue_manager, "set_risk_daily_baseline", lambda day, bal: None)
    positions = [{"symbol": "SOLUSDT", "positionAmt": "1.0"}]
    monkeypatch.setattr(risk_guard, "_get_symbol_beta",
                         lambda symbol: (_ for _ in ()).throw(AssertionError("не должно вызываться без symbol")))
    client = _FakeClient(positions=positions, wallet_balance=10_000.0)

    # symbol не передан (старый вызывающий код) - лимит настроен, но не проверяется.
    reason = risk_guard.check_new_position_allowed(
        client, _limits(max_open=5, max_beta_exposure=2.0), side="BUY",
    )
    assert reason is None


def pytest_approx_or_close(expected, tol):
    """Мини-хелпер вместо pytest.approx - раннер этого файла не
    гарантированно имеет pytest (см. __main__ ниже)."""
    class _Approx:
        def __init__(self, expected, tol):
            self.expected = expected
            self.tol = tol

        def __eq__(self, other):
            return abs(other - self.expected) <= self.tol

    return _Approx(expected, tol)


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