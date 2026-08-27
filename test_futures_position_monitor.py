#!/usr/bin/env python3
"""
Тесты futures_position_monitor.py - определение причины закрытия позиции,
частичный профит/безубыток/трейлинг-стоп (A1) и полный проход
check_open_positions - на ПОДДЕЛЬНОМ FuturesClient (никакой реальной сети).
"""
import time

import config
import futures_position_monitor as fpm
from futures_client import FuturesApiError


class _FakeClient:
    """Имитирует FuturesClient ровно настолько, насколько нужно
    futures_position_monitor - каждый метод пишет вызов в call_log."""

    def __init__(self, position=None, open_orders=None, income_rows=None,
                 step_size=0.001, fail_on=None):
        self.position = position
        self.open_orders = open_orders if open_orders is not None else []
        self.income_rows = income_rows if income_rows is not None else []
        self.step_size = step_size
        self.fail_on = fail_on or set()
        self.call_log = []

    def _maybe_fail(self, step):
        if step in self.fail_on:
            raise FuturesApiError(f"симулированная ошибка на шаге {step}")

    def get_position(self, symbol):
        self.call_log.append(("get_position", symbol))
        return self.position

    def get_open_orders(self, symbol):
        self._maybe_fail("get_open_orders")
        self.call_log.append(("get_open_orders", symbol))
        return self.open_orders

    def cancel_all_open_orders(self, symbol):
        self.call_log.append(("cancel_all_open_orders", symbol))
        return {}

    def cancel_all_algo_orders(self, symbol):
        self.call_log.append(("cancel_all_algo_orders", symbol))
        return {}

    def get_income_history(self, income_type="REALIZED_PNL", start_time_ms=None, limit=1000):
        self.call_log.append(("get_income_history", income_type))
        return self.income_rows

    def place_market_order(self, symbol, side, quantity):
        self._maybe_fail("place_market_order")
        self.call_log.append(("place_market_order", symbol, side, quantity))
        return {}

    def get_symbol_filters(self, symbol):
        return {"step_size": self.step_size, "tick_size": 0.01, "min_notional": 5.0}

    def place_reduce_only_market_order(self, symbol, side, quantity):
        self._maybe_fail("partial_close")
        self.call_log.append(("place_reduce_only_market_order", symbol, side, quantity))
        return {"orderId": 100}

    def cancel_order(self, symbol, order_id):
        self._maybe_fail("cancel_order")
        self.call_log.append(("cancel_order", symbol, order_id))
        return {}

    def place_stop_market(self, symbol, side, stop_price, close_position=True, quantity=None):
        self._maybe_fail("stop")
        self.call_log.append(("place_stop_market", symbol, side, stop_price))
        return {"orderId": 201}

    def place_trailing_stop_market(self, symbol, side, callback_rate, close_position=True,
                                    quantity=None, activation_price=None):
        self._maybe_fail("trailing")
        self.call_log.append(("place_trailing_stop_market", symbol, side, callback_rate, activation_price))
        return {"orderId": 202}


def _record(**overrides):
    base = {
        "symbol": "BTCUSDT", "side": "BUY", "quantity": 1.0, "original_quantity": 1.0,
        "entry_price": 100.0, "stop_price": 90.0, "take_profit_price": 120.0,
        "stop_order_id": 11, "take_profit_order_id": 12,
        "ticker": "BTC", "direction": "LONG", "strategy": "rsi", "score": 90,
        # Открыта "недавно" (1 час назад), а не в момент unix-эпохи -
        # иначе любая позиция в тестах считалась бы миллионы часов
        # "зависшей" и попадала бы под BINANCE_FUTURES_MAX_POSITION_AGE_HOURS
        # (см. P1.2) в тестах, которые вообще не про таймаут.
        "opened_at": time.time() - 3600,
    }
    base.update(overrides)
    return base


# --- _target_progress_fraction ---

def test_target_progress_fraction_long_halfway():
    frac = fpm._target_progress_fraction(entry=100.0, target=120.0, mark_price=110.0, side="BUY")
    assert abs(frac - 0.5) < 1e-9


def test_target_progress_fraction_short_halfway():
    frac = fpm._target_progress_fraction(entry=100.0, target=80.0, mark_price=90.0, side="SELL")
    assert abs(frac - 0.5) < 1e-9


def test_target_progress_fraction_zero_distance_is_zero():
    assert fpm._target_progress_fraction(entry=100.0, target=100.0, mark_price=110.0, side="BUY") == 0.0


def test_target_progress_fraction_past_target_exceeds_one():
    frac = fpm._target_progress_fraction(entry=100.0, target=120.0, mark_price=130.0, side="BUY")
    assert frac > 1.0


# --- _manage_partial_profit ---

def test_partial_profit_not_triggered_below_threshold():
    client = _FakeClient()
    record = _record()
    # 100 -> 120 тейк, цена 105 - это 25% пути, порог по умолчанию 50%.
    updated = fpm._manage_partial_profit(client, record, mark_price=105.0)
    assert updated is record  # ничего не поменялось
    assert client.call_log == []


def test_partial_profit_triggers_at_threshold(monkeypatch):
    monkeypatch.setattr(config, "BINANCE_FUTURES_PARTIAL_TP_ENABLED", True)
    monkeypatch.setattr(config, "BINANCE_FUTURES_PARTIAL_TP_TRIGGER_FRACTION", 0.5)
    monkeypatch.setattr(config, "BINANCE_FUTURES_PARTIAL_TP_CLOSE_FRACTION", 0.5)
    monkeypatch.setattr(config, "BINANCE_FUTURES_TRAILING_CALLBACK_PCT", 1.0)
    monkeypatch.setattr(fpm.alerting, "send_owner_alert", lambda *a, **k: None)

    client = _FakeClient()
    record = _record(quantity=1.0)
    updated = fpm._manage_partial_profit(client, record, mark_price=110.0)  # ровно 50% пути

    assert updated["partial_tp_done"] is True
    assert abs(updated["quantity"] - 0.5) < 1e-9
    assert updated["stop_order_id"] == 201
    assert updated["take_profit_order_id"] == 202
    assert updated["partial_tp_realized_pnl"] > 0

    steps = [c[0] for c in client.call_log]
    assert steps == [
        "place_reduce_only_market_order", "cancel_order", "cancel_order",
        "place_stop_market", "place_trailing_stop_market",
    ]
    # Закрывающая сторона для лонга - SELL.
    close_call = client.call_log[0]
    assert close_call[2] == "SELL"
    assert abs(close_call[3] - 0.5) < 1e-9
    # Новый стоп поставлен ровно на entry (безубыток).
    stop_call = [c for c in client.call_log if c[0] == "place_stop_market"][0]
    assert stop_call[3] == 100.0


def test_partial_profit_short_side_uses_buy_to_close(monkeypatch):
    monkeypatch.setattr(config, "BINANCE_FUTURES_PARTIAL_TP_ENABLED", True)
    monkeypatch.setattr(config, "BINANCE_FUTURES_PARTIAL_TP_TRIGGER_FRACTION", 0.5)
    monkeypatch.setattr(config, "BINANCE_FUTURES_PARTIAL_TP_CLOSE_FRACTION", 0.5)
    monkeypatch.setattr(fpm.alerting, "send_owner_alert", lambda *a, **k: None)

    client = _FakeClient()
    record = _record(side="SELL", entry_price=100.0, take_profit_price=80.0, quantity=1.0)
    updated = fpm._manage_partial_profit(client, record, mark_price=90.0)  # 50% пути вниз

    assert updated["partial_tp_done"] is True
    close_call = [c for c in client.call_log if c[0] == "place_reduce_only_market_order"][0]
    assert close_call[2] == "BUY"


def test_partial_profit_already_done_is_noop():
    client = _FakeClient()
    record = _record(partial_tp_done=True)
    updated = fpm._manage_partial_profit(client, record, mark_price=200.0)
    assert updated is record
    assert client.call_log == []


def test_partial_profit_disabled_is_noop(monkeypatch):
    monkeypatch.setattr(config, "BINANCE_FUTURES_PARTIAL_TP_ENABLED", False)
    client = _FakeClient()
    record = _record()
    updated = fpm._manage_partial_profit(client, record, mark_price=120.0)
    assert updated is record
    assert client.call_log == []


def test_partial_profit_api_failure_keeps_original_record(monkeypatch):
    monkeypatch.setattr(config, "BINANCE_FUTURES_PARTIAL_TP_ENABLED", True)
    monkeypatch.setattr(config, "BINANCE_FUTURES_PARTIAL_TP_TRIGGER_FRACTION", 0.5)
    monkeypatch.setattr(config, "BINANCE_FUTURES_PARTIAL_TP_CLOSE_FRACTION", 0.5)

    client = _FakeClient(fail_on={"partial_close"})
    record = _record()
    updated = fpm._manage_partial_profit(client, record, mark_price=110.0)
    assert updated is record
    assert not updated.get("partial_tp_done")


def test_partial_profit_zero_remaining_is_skipped(monkeypatch):
    monkeypatch.setattr(config, "BINANCE_FUTURES_PARTIAL_TP_ENABLED", True)
    monkeypatch.setattr(config, "BINANCE_FUTURES_PARTIAL_TP_TRIGGER_FRACTION", 0.5)
    monkeypatch.setattr(config, "BINANCE_FUTURES_PARTIAL_TP_CLOSE_FRACTION", 1.0)  # закрыть всё -> остаток 0

    client = _FakeClient()
    record = _record(quantity=1.0)
    updated = fpm._manage_partial_profit(client, record, mark_price=110.0)
    assert updated is record
    assert client.call_log == []


# --- _determine_close_reason_and_cleanup ---

def test_close_reason_take_profit_before_partial():
    # stop (11) всё ещё висит, tp (12) - нет -> сработал тейк.
    client = _FakeClient(open_orders=[{"orderId": 11}])
    reason = fpm._determine_close_reason_and_cleanup(client, _record(), "BTCUSDT")
    assert reason == "тейк-профит (TP)"
    assert ("cancel_all_open_orders", "BTCUSDT") in client.call_log
    assert ("cancel_all_algo_orders", "BTCUSDT") in client.call_log


def test_close_reason_stop_loss_before_partial():
    client = _FakeClient(open_orders=[{"orderId": 12}])
    reason = fpm._determine_close_reason_and_cleanup(client, _record(), "BTCUSDT")
    assert reason == "стоп-лосс (SL)"


def test_close_reason_relabels_after_partial_tp():
    record = _record(partial_tp_done=True, stop_order_id=201, take_profit_order_id=202)
    # tp (202, теперь трейлинг) не висит -> трейлинг сработал.
    client = _FakeClient(open_orders=[{"orderId": 201}])
    reason = fpm._determine_close_reason_and_cleanup(client, record, "BTCUSDT")
    assert "трейлинг-стоп" in reason

    # stop (201, теперь безубыток) не висит -> безубыток сработал.
    client2 = _FakeClient(open_orders=[{"orderId": 202}])
    reason2 = fpm._determine_close_reason_and_cleanup(client2, record, "BTCUSDT")
    assert "безубытке" in reason2


def test_close_reason_unknown_when_neither_open():
    client = _FakeClient(open_orders=[])
    reason = fpm._determine_close_reason_and_cleanup(client, _record(), "BTCUSDT")
    assert "неизвестно" in reason
    assert not any(c[0] == "cancel_all_open_orders" for c in client.call_log)


# --- check_open_positions: интеграция ---

def test_check_open_positions_calls_partial_profit_for_open_position(monkeypatch):
    monkeypatch.setattr(config, "BINANCE_FUTURES_PARTIAL_TP_ENABLED", True)
    monkeypatch.setattr(config, "BINANCE_FUTURES_PARTIAL_TP_TRIGGER_FRACTION", 0.5)
    monkeypatch.setattr(config, "BINANCE_FUTURES_PARTIAL_TP_CLOSE_FRACTION", 0.5)
    monkeypatch.setattr(fpm.alerting, "send_owner_alert", lambda *a, **k: None)
    monkeypatch.setattr(fpm.queue_manager, "get_open_futures_positions", lambda: [_record()])

    saved = {}
    monkeypatch.setattr(fpm.queue_manager, "replace_open_futures_positions", lambda items: saved.setdefault("items", items))
    monkeypatch.setattr(fpm.queue_manager, "append_closed_futures_positions", lambda items: None)

    client = _FakeClient(position={"positionAmt": "1.0", "markPrice": "110.0"})
    summary = fpm.check_open_positions(client)

    assert summary == {"still_open": 1, "closed": 0}
    assert saved["items"][0]["partial_tp_done"] is True
    assert abs(saved["items"][0]["quantity"] - 0.5) < 1e-9


def test_check_open_positions_uses_original_quantity_for_pnl_pct(monkeypatch):
    monkeypatch.setattr(fpm.queue_manager, "get_open_futures_positions",
                         lambda: [_record(partial_tp_done=True, quantity=0.5, original_quantity=1.0,
                                           partial_tp_realized_pnl=5.0)])
    monkeypatch.setattr(fpm.queue_manager, "replace_open_futures_positions", lambda items: None)
    monkeypatch.setattr(fpm.queue_manager, "append_closed_futures_positions", lambda items: None)

    captured = {}
    monkeypatch.setattr(fpm.alerting, "send_owner_alert",
                         lambda key, message, **k: captured.setdefault("message", message))

    # Позиция закрылась (positionAmt == 0), финальный PnL с биржи 10 USDT.
    # income "time" должен быть ПОСЛЕ opened_at записи (см. _realized_pnl_since) -
    # берём заведомо позже момента открытия, а не абсолютную дату в прошлом.
    income_time_ms = int((time.time()) * 1000)
    client = _FakeClient(position=None, open_orders=[], income_rows=[
        {"symbol": "BTCUSDT", "income": "10.0", "time": income_time_ms},
    ])
    summary = fpm.check_open_positions(client)

    assert summary == {"still_open": 0, "closed": 1}
    # notional должен считаться от original_quantity (1.0), не quantity (0.5):
    # pnl_pct = 10 / (100 * 1.0) * 100 = 10.00%
    assert "10.00%" in captured["message"]
    assert "зафиксированные ранее частичным профитом" in captured["message"]


def test_check_open_positions_marks_cooldown_on_real_stop_loss(monkeypatch):
    monkeypatch.setattr(fpm.queue_manager, "get_open_futures_positions", lambda: [_record()])
    monkeypatch.setattr(fpm.queue_manager, "replace_open_futures_positions", lambda items: None)
    monkeypatch.setattr(fpm.queue_manager, "append_closed_futures_positions", lambda items: None)
    monkeypatch.setattr(fpm.alerting, "send_owner_alert", lambda *a, **k: None)

    marked = []
    monkeypatch.setattr(fpm.queue_manager, "mark_stopped_out", lambda symbol: marked.append(symbol))

    # stop_order_id (11) не висит (сработал), take_profit_order_id (12) всё ещё висит -> сработал стоп.
    client = _FakeClient(position=None, open_orders=[{"orderId": 12}], income_rows=[])
    summary = fpm.check_open_positions(client)

    assert summary == {"still_open": 0, "closed": 1}
    assert marked == ["BTCUSDT"]


def test_check_open_positions_does_not_mark_cooldown_on_take_profit(monkeypatch):
    monkeypatch.setattr(fpm.queue_manager, "get_open_futures_positions", lambda: [_record()])
    monkeypatch.setattr(fpm.queue_manager, "replace_open_futures_positions", lambda items: None)
    monkeypatch.setattr(fpm.queue_manager, "append_closed_futures_positions", lambda items: None)
    monkeypatch.setattr(fpm.alerting, "send_owner_alert", lambda *a, **k: None)

    marked = []
    monkeypatch.setattr(fpm.queue_manager, "mark_stopped_out", lambda symbol: marked.append(symbol))

    # take_profit_order_id (12) не висит (сработал), stop_order_id (11) всё ещё висит -> сработал тейк.
    client = _FakeClient(position=None, open_orders=[{"orderId": 11}], income_rows=[])
    summary = fpm.check_open_positions(client)

    assert summary == {"still_open": 0, "closed": 1}
    assert marked == []


def test_check_open_positions_does_not_mark_cooldown_on_breakeven_stop_after_partial(monkeypatch):
    # После частичного профита stop_order_id держит стоп в безубытке, а не
    # исходный стоп-лосс - это не тот же сигнал "монета пилит у уровня",
    # поэтому cooldown НЕ должен ставиться (см. futures_position_monitor
    # docstring и роадмап фазы 2, пункт P1.1).
    record = _record(partial_tp_done=True, stop_order_id=201, take_profit_order_id=202)
    monkeypatch.setattr(fpm.queue_manager, "get_open_futures_positions", lambda: [record])
    monkeypatch.setattr(fpm.queue_manager, "replace_open_futures_positions", lambda items: None)
    monkeypatch.setattr(fpm.queue_manager, "append_closed_futures_positions", lambda items: None)
    monkeypatch.setattr(fpm.alerting, "send_owner_alert", lambda *a, **k: None)

    marked = []
    monkeypatch.setattr(fpm.queue_manager, "mark_stopped_out", lambda symbol: marked.append(symbol))

    # stop_order_id (201, теперь безубыток) не висит, take_profit_order_id
    # (202, теперь трейлинг) всё ещё висит -> сработал именно безубыток.
    client = _FakeClient(position=None, open_orders=[{"orderId": 202}], income_rows=[])
    summary = fpm.check_open_positions(client)

    assert summary == {"still_open": 0, "closed": 1}
    assert marked == []


def test_check_open_positions_no_mark_price_skips_partial_profit(monkeypatch):
    # markPrice отсутствует/0 (например, поле не пришло от биржи) - не
    # должны падать или вызывать частичный профит с мусорной ценой.
    monkeypatch.setattr(fpm.queue_manager, "get_open_futures_positions", lambda: [_record()])
    monkeypatch.setattr(fpm.queue_manager, "replace_open_futures_positions", lambda items: None)
    monkeypatch.setattr(fpm.queue_manager, "append_closed_futures_positions", lambda items: None)

    client = _FakeClient(position={"positionAmt": "1.0"})  # без markPrice
    summary = fpm.check_open_positions(client)
    assert summary == {"still_open": 1, "closed": 0}
    assert not any(c[0] == "place_reduce_only_market_order" for c in client.call_log)


# --- check_open_positions: таймаут зависшей позиции (P1.2) ---

def test_check_open_positions_force_closes_position_past_max_age(monkeypatch):
    monkeypatch.setattr(config, "BINANCE_FUTURES_MAX_POSITION_AGE_HOURS", 48.0)
    old_record = _record(opened_at=time.time() - 49 * 3600)  # открыта 49ч назад > лимита 48ч
    monkeypatch.setattr(fpm.queue_manager, "get_open_futures_positions", lambda: [old_record])
    saved = {}
    monkeypatch.setattr(fpm.queue_manager, "replace_open_futures_positions", lambda items: saved.setdefault("still_open", items))
    monkeypatch.setattr(fpm.queue_manager, "append_closed_futures_positions", lambda items: saved.setdefault("closed", items))
    captured = {}
    monkeypatch.setattr(fpm.alerting, "send_owner_alert",
                         lambda key, message, **k: captured.setdefault("message", message))

    # positionAmt != 0 - позиция формально ещё открыта на бирже (ни стоп,
    # ни тейк не сработали), но она старше лимита.
    client = _FakeClient(position={"positionAmt": "1.0", "markPrice": "0"},
                          income_rows=[{"symbol": "BTCUSDT", "income": "-2.0", "time": int(time.time() * 1000)}])
    summary = fpm.check_open_positions(client)

    assert summary == {"still_open": 0, "closed": 1}
    assert saved["still_open"] == []
    assert saved["closed"][0]["close_reason"].startswith("таймаут")
    assert saved["closed"][0]["realized_pnl"] == -2.0
    # Принудительное закрытие: сначала отмена условных ордеров, потом маркет-закрытие.
    assert ("cancel_all_open_orders", "BTCUSDT") in client.call_log
    assert ("cancel_all_algo_orders", "BTCUSDT") in client.call_log
    assert any(c[0] == "place_market_order" for c in client.call_log)
    assert "таймаут" in captured["message"]


def test_check_open_positions_keeps_position_open_under_max_age(monkeypatch):
    monkeypatch.setattr(config, "BINANCE_FUTURES_MAX_POSITION_AGE_HOURS", 48.0)
    fresh_record = _record(opened_at=time.time() - 3600)  # открыта всего час назад
    monkeypatch.setattr(fpm.queue_manager, "get_open_futures_positions", lambda: [fresh_record])
    saved = {}
    monkeypatch.setattr(fpm.queue_manager, "replace_open_futures_positions", lambda items: saved.setdefault("still_open", items))
    monkeypatch.setattr(fpm.queue_manager, "append_closed_futures_positions", lambda items: None)

    client = _FakeClient(position={"positionAmt": "1.0", "markPrice": "0"})
    summary = fpm.check_open_positions(client)

    assert summary == {"still_open": 1, "closed": 0}
    assert not any(c[0] == "place_market_order" for c in client.call_log)


def test_check_open_positions_keeps_timed_out_position_tracked_if_force_close_fails(monkeypatch):
    monkeypatch.setattr(config, "BINANCE_FUTURES_MAX_POSITION_AGE_HOURS", 48.0)
    old_record = _record(opened_at=time.time() - 49 * 3600)
    monkeypatch.setattr(fpm.queue_manager, "get_open_futures_positions", lambda: [old_record])
    saved = {}
    monkeypatch.setattr(fpm.queue_manager, "replace_open_futures_positions", lambda items: saved.setdefault("still_open", items))
    monkeypatch.setattr(fpm.queue_manager, "append_closed_futures_positions", lambda items: saved.setdefault("closed", items))

    # place_market_order (внутри emergency_close_all) падает - позиция
    # должна ОСТАТЬСЯ в трекинге, не потеряться молча.
    client = _FakeClient(position={"positionAmt": "1.0", "markPrice": "0"}, fail_on={"place_market_order"})
    summary = fpm.check_open_positions(client)

    assert summary == {"still_open": 1, "closed": 0}
    assert saved["still_open"] == [old_record]


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
