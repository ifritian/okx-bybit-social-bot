#!/usr/bin/env python3
"""
Тесты futures_executor.py - расчёт риска/размера позиции и полный цикл
open_protected_position - на ПОДДЕЛЬНОМ FuturesClient (никакой реальной
сети, ни testnet, ни mainnet).
"""
import futures_executor as fe
import risk_guard
from futures_client import FuturesApiError


class _FakeClient:
    """Имитирует FuturesClient ровно настолько, насколько нужно
    open_protected_position - каждый метод просто пишет вызов в call_log
    и возвращает заранее заданный результат/ошибку."""

    def __init__(self, balance=10_000.0, mark_price=100.0,
                 step_size=0.001, tick_size=0.01, min_notional=5.0,
                 fail_on=None, fill_price=None):
        self.balance = balance
        self.mark_price = mark_price
        self.step_size = step_size
        self.tick_size = tick_size
        self.min_notional = min_notional
        self.fail_on = fail_on or set()  # набор шагов, на которых нужно бросить ошибку
        self.fill_price = fill_price  # None -> avgPrice не возвращается (как раньше)
        self.call_log = []
        self.position = None  # для emergency-close сценариев

    def _maybe_fail(self, step):
        if step in self.fail_on:
            raise FuturesApiError(f"симулированная ошибка на шаге {step}")

    def set_leverage(self, symbol, leverage):
        self.call_log.append(("set_leverage", symbol, leverage))

    def set_margin_type(self, symbol, margin_type):
        self.call_log.append(("set_margin_type", symbol, margin_type))

    def get_symbol_filters(self, symbol):
        return {"step_size": self.step_size, "tick_size": self.tick_size, "min_notional": self.min_notional}

    def get_mark_price(self, symbol):
        return self.mark_price

    def get_available_balance(self, asset="USDT"):
        return self.balance

    def place_market_order(self, symbol, side, quantity):
        self._maybe_fail("entry")
        self.call_log.append(("place_market_order", symbol, side, quantity))
        order = {"orderId": 1, "side": side, "quantity": quantity}
        if self.fill_price is not None:
            order["avgPrice"] = str(self.fill_price)
        return order

    def place_stop_market(self, symbol, side, stop_price, close_position=True, quantity=None):
        self._maybe_fail("stop")
        self.call_log.append(("place_stop_market", symbol, side, stop_price))
        return {"orderId": 2}

    def place_take_profit_market(self, symbol, side, stop_price, close_position=True, quantity=None):
        self._maybe_fail("take_profit")
        self.call_log.append(("place_take_profit_market", symbol, side, stop_price))
        return {"orderId": 3}

    def get_position(self, symbol):
        return self.position

    def cancel_all_open_orders(self, symbol):
        self.call_log.append(("cancel_all_open_orders", symbol))
        return {}

    def cancel_all_algo_orders(self, symbol):
        self.call_log.append(("cancel_all_algo_orders", symbol))
        return {}

    # --- нужны только тестам с risk_limits (см. секцию ниже) ---

    def get_all_positions(self):
        self.call_log.append(("get_all_positions",))
        return []

    def get_wallet_balance(self, asset="USDT"):
        self.call_log.append(("get_wallet_balance",))
        return self.balance

    def get_income_history(self, income_type="REALIZED_PNL", start_time_ms=None, limit=1000):
        self.call_log.append(("get_income_history",))
        return []


# --- calc_position_size / round_to_step ---

def test_calc_position_size_basic():
    # Баланс 10000, риск 1% = 100 USDT риска. Вход 100, стоп 95 -> дистанция 5.
    # qty = 100 / 5 = 20.
    qty = fe.calc_position_size(balance=10_000, risk_pct=1.0, entry_price=100.0, stop_price=95.0)
    assert abs(qty - 20.0) < 1e-9


def test_calc_position_size_independent_of_leverage():
    # calc_position_size вообще не принимает leverage - подтверждаем
    # сигнатуру функции (риск не зависит от плеча, см. docstring модуля).
    import inspect
    params = inspect.signature(fe.calc_position_size).parameters
    assert "leverage" not in params


def test_calc_position_size_zero_distance_raises():
    try:
        fe.calc_position_size(10_000, 1.0, 100.0, 100.0)
        assert False, "должно было бросить ValueError"
    except ValueError:
        pass


def test_calc_position_size_zero_balance_returns_zero():
    assert fe.calc_position_size(0, 1.0, 100.0, 95.0) == 0.0


def test_round_to_step_rounds_down():
    assert fe.round_to_step(20.0037, 0.001) == 20.003
    assert fe.round_to_step(0.0, 0.001) == 0.0


def test_round_to_step_zero_step_is_noop():
    assert fe.round_to_step(20.0037, 0) == 20.0037


# --- open_protected_position: happy path ---

def test_open_protected_position_long_happy_path():
    client = _FakeClient(balance=10_000, mark_price=100.0)
    result = fe.open_protected_position(
        client, "BTCUSDT", "BUY", stop_price=95.0, take_profit_price=110.0,
        risk_pct=1.0, leverage=3,
    )
    assert result.symbol == "BTCUSDT"
    assert result.side == "BUY"
    assert result.quantity == 20.0  # см. test_calc_position_size_basic
    assert result.risk_amount == 100.0

    steps = [c[0] for c in client.call_log]
    assert steps == [
        "set_leverage", "set_margin_type", "place_market_order",
        "place_stop_market", "place_take_profit_market",
    ]
    # Закрывающие ордера (стоп/тейк) для ЛОНГА должны быть SELL.
    assert client.call_log[3][2] == "SELL"
    assert client.call_log[4][2] == "SELL"


def test_open_protected_position_short_uses_buy_to_close():
    client = _FakeClient(balance=10_000, mark_price=100.0)
    fe.open_protected_position(
        client, "BTCUSDT", "SELL", stop_price=105.0, take_profit_price=90.0,
        risk_pct=1.0, leverage=3,
    )
    assert client.call_log[3][2] == "BUY"
    assert client.call_log[4][2] == "BUY"


def test_open_protected_position_invalid_side_raises():
    client = _FakeClient()
    try:
        fe.open_protected_position(client, "BTCUSDT", "HOLD", 95.0, 110.0, 1.0, 3)
        assert False, "должно было бросить ValueError"
    except ValueError:
        pass


def test_open_protected_position_too_small_notional_raises_before_entry():
    # Баланс крошечный -> посчитанный размер даст notional меньше min_notional.
    client = _FakeClient(balance=1.0, mark_price=100.0, min_notional=5.0)
    try:
        fe.open_protected_position(client, "BTCUSDT", "BUY", 95.0, 110.0, risk_pct=1.0, leverage=3)
        assert False, "должно было бросить ExecutionError"
    except fe.ExecutionError:
        pass
    # Вход НЕ должен был случиться - ошибка была ДО place_market_order.
    assert not any(c[0] == "place_market_order" for c in client.call_log)


# --- open_protected_position: аварийное закрытие при сбое SL/TP ---

def test_stop_failure_triggers_emergency_close():
    client = _FakeClient(balance=10_000, mark_price=100.0, fail_on={"stop"})
    client.position = {"positionAmt": "20.0"}  # позиция "открыта" после входа

    try:
        fe.open_protected_position(client, "BTCUSDT", "BUY", 95.0, 110.0, risk_pct=1.0, leverage=3)
        assert False, "должно было бросить ExecutionError"
    except fe.ExecutionError as e:
        assert "аварийно закрыта" in str(e)

    # Позиция должна быть закрыта ПРОТИВОПОЛОЖНОЙ стороной (лонг -> SELL).
    close_calls = [c for c in client.call_log if c[0] == "place_market_order"]
    assert len(close_calls) == 2  # 1) сам вход, 2) аварийное закрытие
    assert close_calls[1][2] == "SELL"


def test_take_profit_failure_triggers_emergency_close():
    client = _FakeClient(balance=10_000, mark_price=100.0, fail_on={"take_profit"})
    client.position = {"positionAmt": "20.0"}

    try:
        fe.open_protected_position(client, "BTCUSDT", "BUY", 95.0, 110.0, risk_pct=1.0, leverage=3)
        assert False, "должно было бросить ExecutionError"
    except fe.ExecutionError as e:
        assert "тейк-профит" in str(e)


def test_emergency_close_failure_raises_critical_error():
    # И SL не встал, И аварийное закрытие тоже не удалось - самый
    # опасный сценарий, ошибка должна кричать об этом явно.
    client = _FakeClient(balance=10_000, mark_price=100.0, fail_on={"stop", "entry_close"})
    client.position = {"positionAmt": "20.0"}

    def failing_market_order(symbol, side, quantity):
        if side == "SELL" and quantity == 20.0:
            # Это аварийное закрытие (после входа BUY 20.0) - роняем его.
            raise FuturesApiError("биржа недоступна")
        return {"orderId": 1}

    client.place_market_order = failing_market_order

    try:
        fe.open_protected_position(client, "BTCUSDT", "BUY", 95.0, 110.0, risk_pct=1.0, leverage=3)
        assert False, "должно было бросить ExecutionError"
    except fe.ExecutionError as e:
        assert "КРИТИЧНО" in str(e)


def test_emergency_close_when_position_already_gone():
    client = _FakeClient(balance=10_000, mark_price=100.0, fail_on={"stop"})
    client.position = None  # позиция уже закрылась сама (например, другим ордером)

    try:
        fe.open_protected_position(client, "BTCUSDT", "BUY", 95.0, 110.0, risk_pct=1.0, leverage=3)
        assert False, "должно было бросить ExecutionError"
    except fe.ExecutionError as e:
        assert "закрылась сама" in str(e)


# --- emergency_close_all ---

def test_emergency_close_all_no_position_returns_none():
    client = _FakeClient()
    client.position = None
    assert fe.emergency_close_all(client, "BTCUSDT") is None
    assert ("cancel_all_open_orders", "BTCUSDT") in client.call_log
    assert ("cancel_all_algo_orders", "BTCUSDT") in client.call_log


def test_emergency_close_all_closes_long_position_with_sell():
    client = _FakeClient()
    client.position = {"positionAmt": "15.0"}
    fe.emergency_close_all(client, "BTCUSDT")
    close_calls = [c for c in client.call_log if c[0] == "place_market_order"]
    assert close_calls[0][2] == "SELL"
    assert close_calls[0][3] == 15.0


def test_emergency_close_all_closes_short_position_with_buy():
    client = _FakeClient()
    client.position = {"positionAmt": "-7.5"}
    fe.emergency_close_all(client, "BTCUSDT")
    close_calls = [c for c in client.call_log if c[0] == "place_market_order"]
    assert close_calls[0][2] == "BUY"
    assert close_calls[0][3] == 7.5


# --- risk_limits: предохранители поверх отдельной сделки ---

def test_no_risk_limits_means_no_guard_calls():
    # risk_limits не передан (по умолчанию None) - guard-методы клиента
    # вообще не должны вызываться (обратная совместимость/старые тесты).
    client = _FakeClient(balance=10_000, mark_price=100.0)
    fe.open_protected_position(
        client, "BTCUSDT", "BUY", stop_price=95.0, take_profit_price=110.0,
        risk_pct=1.0, leverage=3,
    )
    guard_calls = [c for c in client.call_log if c[0].startswith("get_all_positions")
                   or c[0].startswith("get_wallet_balance") or c[0].startswith("get_income_history")]
    assert guard_calls == []


def test_risk_limits_blocked_prevents_any_exchange_call(monkeypatch):
    # Kill switch взведён -> check_new_position_allowed отказывает СРАЗУ,
    # ни один метод биржи (set_leverage и дальше) не должен вызваться.
    monkeypatch.setattr(risk_guard.queue_manager, "get_kill_switch",
                         lambda: {"reason": "тест", "tripped_at": 0})
    client = _FakeClient(balance=10_000, mark_price=100.0)
    limits = risk_guard.RiskLimits(max_open_positions=3, max_daily_loss_pct=5.0, max_consecutive_losses=3)
    try:
        fe.open_protected_position(
            client, "BTCUSDT", "BUY", stop_price=95.0, take_profit_price=110.0,
            risk_pct=1.0, leverage=3, risk_limits=limits,
        )
        assert False, "должно было бросить ExecutionError"
    except fe.ExecutionError as e:
        assert "предохранителями риска" in str(e)
    # get_all_positions - единственный вызов, который guard успевает сделать
    # до kill switch (на самом деле kill switch проверяется раньше всех,
    # поэтому вообще ни один метод клиента не должен был вызваться).
    assert client.call_log == []


def test_risk_limits_allowed_proceeds_to_normal_flow(monkeypatch):
    monkeypatch.setattr(risk_guard.queue_manager, "get_kill_switch", lambda: None)
    monkeypatch.setattr(risk_guard.queue_manager, "get_risk_daily_baseline", lambda day: 10_000.0)
    monkeypatch.setattr(risk_guard.queue_manager, "set_risk_daily_baseline", lambda day, bal: None)
    client = _FakeClient(balance=10_000, mark_price=100.0)
    limits = risk_guard.RiskLimits(max_open_positions=3, max_daily_loss_pct=5.0, max_consecutive_losses=3)
    result = fe.open_protected_position(
        client, "BTCUSDT", "BUY", stop_price=95.0, take_profit_price=110.0,
        risk_pct=1.0, leverage=3, risk_limits=limits,
    )
    assert result.symbol == "BTCUSDT"
    steps = [c[0] for c in client.call_log]
    # guard-проверки идут ДО set_leverage
    assert steps[:3] == ["get_all_positions", "get_wallet_balance", "get_income_history"]
    assert "set_leverage" in steps


# --- проскальзывание на входе (D1, см. _extract_fill_price/_calc_slippage_pct) ---

def test_extract_fill_price_uses_avg_price_when_present():
    assert fe._extract_fill_price({"avgPrice": "101.5"}, fallback_price=100.0) == 101.5


def test_extract_fill_price_falls_back_when_avg_price_missing():
    assert fe._extract_fill_price({}, fallback_price=100.0) == 100.0


def test_extract_fill_price_falls_back_when_avg_price_is_zero():
    # биржа иногда синхронно отдаёт "0" - ордер уже исполнен, но поле не успело
    # проставиться - не должно считаться как реальная цена 0.
    assert fe._extract_fill_price({"avgPrice": "0"}, fallback_price=100.0) == 100.0


def test_calc_slippage_pct_long_paid_more_is_positive_unfavorable():
    # лонг: переплатили (вошли выше ориентира) - невыгодно -> положительное
    pct = fe._calc_slippage_pct("BUY", reference_price=100.0, fill_price=100.3)
    assert round(pct, 4) == 0.3


def test_calc_slippage_pct_long_paid_less_is_negative_favorable():
    pct = fe._calc_slippage_pct("BUY", reference_price=100.0, fill_price=99.7)
    assert round(pct, 4) == -0.3


def test_calc_slippage_pct_short_received_less_is_positive_unfavorable():
    # шорт: продали дешевле ориентира - невыгодно -> положительное (знак развёрнут)
    pct = fe._calc_slippage_pct("SELL", reference_price=100.0, fill_price=99.7)
    assert round(pct, 4) == 0.3


def test_calc_slippage_pct_short_received_more_is_negative_favorable():
    pct = fe._calc_slippage_pct("SELL", reference_price=100.0, fill_price=100.3)
    assert round(pct, 4) == -0.3


def test_calc_slippage_pct_zero_reference_returns_zero():
    assert fe._calc_slippage_pct("BUY", reference_price=0.0, fill_price=100.0) == 0.0


def test_open_protected_position_uses_actual_fill_price_as_entry_price():
    client = _FakeClient(balance=10_000, mark_price=100.0, fill_price=100.4)
    result = fe.open_protected_position(
        client, "BTCUSDT", "BUY", stop_price=95.0, take_profit_price=110.0,
        risk_pct=1.0, leverage=3,
    )
    assert result.entry_price == 100.4          # реальная цена исполнения, не ориентир
    assert result.reference_price == 100.0       # mark price ДО ордера
    assert round(result.slippage_pct, 4) == 0.4  # (100.4-100.0)/100.0*100


def test_open_protected_position_zero_slippage_without_avg_price_in_response():
    # без avgPrice в ответе (как у "старого" FuturesClient до этого изменения)
    # entry_price = reference_price, slippage_pct = 0 - поведение не должно
    # ломаться там, где биржа/фейк не возвращает avgPrice.
    client = _FakeClient(balance=10_000, mark_price=100.0)  # fill_price не задан
    result = fe.open_protected_position(
        client, "BTCUSDT", "BUY", stop_price=95.0, take_profit_price=110.0,
        risk_pct=1.0, leverage=3,
    )
    assert result.entry_price == 100.0
    assert result.reference_price == 100.0
    assert result.slippage_pct == 0.0


if __name__ == "__main__":
    import sys
    import types

    class _MiniMonkeypatch:
        """Тот же минимальный monkeypatch, что и в test_strategy_tuner.py -
        нужен новым тестам risk_limits (подменяют queue_manager под
        risk_guard, без реальной SQLite)."""

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
