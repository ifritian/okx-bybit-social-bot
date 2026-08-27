#!/usr/bin/env python3
"""
Тесты futures_signal_bridge.py - на ПОДДЕЛЬНОМ FuturesClient (никакой
реальной сети) - конвертация сигнала в параметры сделки и все отказы
ДО open_protected_position (плохие числа, score, зона входа, дубликат
позиции)."""
import futures_signal_bridge as bridge
import queue_manager
import risk_guard
from futures_client import FuturesApiError
from signal_parser import RsiSignal
from types import SimpleNamespace


def _signal(ticker="SOL", direction="Лонг (перепроданность)", score="85",
            entry_low="100", entry_high="102", invalidation="95", target="115"):
    return RsiSignal(
        ticker=ticker, timeframe="15m", strategy="RSI + Bollinger Touch", direction=direction,
        current_price="101", rsi_now="25.0", score=score, quality="Moderate",
        entry_low=entry_low, entry_high=entry_high, invalidation=invalidation, target=target,
        change_24h="+1.0%", volume="10M", rsi_live="25.0", created_at="2026-07-29 00:00:00 UTC",
        description="тест", raw_text="тест",
    )


class _FakeClient:
    def __init__(self, mark_price=101.0, position=None, fail_position_check=False, fail_price=False,
                 funding_rate=0.0, fail_funding=False):
        self.mark_price = mark_price
        self.position = position
        self.fail_position_check = fail_position_check
        self.fail_price = fail_price
        self.funding_rate = funding_rate
        self.fail_funding = fail_funding
        self.opened = []
        self.calls = []

    def get_position(self, symbol):
        self.calls.append(("get_position", symbol))
        if self.fail_position_check:
            raise FuturesApiError("симулированный сбой")
        return self.position

    def get_mark_price(self, symbol):
        self.calls.append(("get_mark_price", symbol))
        if self.fail_price:
            raise FuturesApiError("симулированный сбой цены")
        return self.mark_price

    def get_funding_rate(self, symbol):
        self.calls.append(("get_funding_rate", symbol))
        if self.fail_funding:
            raise FuturesApiError("симулированный сбой фандинга")
        return self.funding_rate


def _limits():
    return risk_guard.RiskLimits(max_open_positions=3, max_daily_loss_pct=5.0, max_consecutive_losses=3)


# --- signal_to_trade_params ---

def test_signal_to_trade_params_long_ok():
    params = bridge.signal_to_trade_params(_signal())
    assert params is not None
    assert params.symbol == "SOLUSDT"
    assert params.side == "BUY"
    assert params.stop_price == 95.0
    assert params.take_profit_price == 115.0


def test_signal_to_trade_params_short_ok():
    s = _signal(direction="Шорт (перекупленность)", entry_low="98", entry_high="100",
                invalidation="105", target="85")
    params = bridge.signal_to_trade_params(s)
    assert params is not None
    assert params.side == "SELL"
    assert params.stop_price == 105.0
    assert params.take_profit_price == 85.0


def test_signal_to_trade_params_rejects_unparseable_numbers():
    s = _signal(invalidation="н/д")
    assert bridge.signal_to_trade_params(s) is None


def test_signal_to_trade_params_rejects_stop_on_wrong_side_for_long():
    # для лонга стоп ДОЛЖЕН быть ниже входа - тут он выше
    s = _signal(entry_low="100", entry_high="102", invalidation="103", target="115")
    assert bridge.signal_to_trade_params(s) is None


def test_signal_to_trade_params_rejects_target_on_wrong_side_for_short():
    s = _signal(direction="Шорт (перекупленность)", entry_low="98", entry_high="100",
                invalidation="105", target="99")  # тейк должен быть НИЖЕ входа
    assert bridge.signal_to_trade_params(s) is None


def test_signal_to_trade_params_rejects_inverted_entry_range():
    s = _signal(entry_low="105", entry_high="100")  # low > high
    assert bridge.signal_to_trade_params(s) is None


# --- execute_signal: гейты ДО open_protected_position ---

def test_execute_signal_below_min_score_skips_without_any_client_call():
    client = _FakeClient()
    result = bridge.execute_signal(client, _signal(score="70"), risk_pct=1.0, leverage=3,
                                    risk_limits=_limits(), min_score=80)
    assert result is None
    assert client.calls == []


def test_execute_signal_bad_numbers_skips_without_any_client_call():
    client = _FakeClient()
    result = bridge.execute_signal(client, _signal(invalidation="bad"), risk_pct=1.0, leverage=3,
                                    risk_limits=_limits(), min_score=0)
    assert result is None
    assert client.calls == []


def test_execute_signal_skips_if_position_already_open():
    client = _FakeClient(position={"symbol": "SOLUSDT", "positionAmt": "1.0"})
    result = bridge.execute_signal(client, _signal(), risk_pct=1.0, leverage=3,
                                    risk_limits=_limits(), min_score=0)
    assert result is None
    # не должны были даже дойти до проверки цены - незачем
    assert ("get_mark_price", "SOLUSDT") not in client.calls


def test_execute_signal_skips_if_symbol_in_stop_cooldown(monkeypatch):
    monkeypatch.setattr(queue_manager, "was_recently_stopped_out", lambda *a, **k: True)
    client = _FakeClient(position=None)
    result = bridge.execute_signal(client, _signal(), risk_pct=1.0, leverage=3,
                                    risk_limits=_limits(), min_score=0)
    assert result is None
    # не должны были дойти до проверки цены - cooldown отсеивает раньше
    assert ("get_mark_price", "SOLUSDT") not in client.calls


def test_execute_signal_proceeds_if_symbol_not_in_stop_cooldown(monkeypatch):
    monkeypatch.setattr(queue_manager, "was_recently_stopped_out", lambda *a, **k: False)
    client = _FakeClient(position=None, mark_price=110.0)  # цена вне зоны входа -> отказ дальше по цепочке
    result = bridge.execute_signal(client, _signal(), risk_pct=1.0, leverage=3,
                                    risk_limits=_limits(), min_score=0)
    assert result is None
    # дошли до проверки цены, значит cooldown не заблокировал сигнал раньше времени
    assert ("get_mark_price", "SOLUSDT") in client.calls


def test_execute_signal_skips_if_position_check_fails():
    client = _FakeClient(fail_position_check=True)
    result = bridge.execute_signal(client, _signal(), risk_pct=1.0, leverage=3,
                                    risk_limits=_limits(), min_score=0)
    assert result is None


def test_execute_signal_skips_if_price_moved_far_from_entry_zone():
    # зона входа 100-102, допуск = ширина*0.5 = 1 -> граница 99..103
    client = _FakeClient(mark_price=110.0, position=None)
    result = bridge.execute_signal(client, _signal(), risk_pct=1.0, leverage=3,
                                    risk_limits=_limits(), min_score=0)
    assert result is None


def test_execute_signal_allows_small_slippage_within_tolerance():
    # мимо зоны 100-102, но в пределах допуска (граница 99..103)
    client = _FakeClient(mark_price=102.8, position=None)
    # get_position -> None (ок), get_mark_price -> 102.8 (в допуске) -> дошли бы дальше,
    # но у _FakeClient нет get_income_history/остальных методов FuturesClient -
    # убеждаемся, что дошли именно до этой точки по AttributeError, а не по None раньше
    # (см. test_execute_signal_applies_soft_derisk_multiplier ниже - там дальше идёт
    # уже полноценный монки-патч open_protected_position, здесь достаточно факта,
    # что не отвалились раньше времени).
    try:
        bridge.execute_signal(client, _signal(), risk_pct=1.0, leverage=3, risk_limits=_limits(), min_score=0)
        assert False, "ожидался AttributeError - _FakeClient не реализует остальные методы FuturesClient"
    except AttributeError:
        pass
    assert ("get_mark_price", "SOLUSDT") in client.calls


# --- фильтр по ставке фандинга (см. _funding_rate_too_unfavorable) ---

def test_funding_rate_too_unfavorable_blocks_long_on_high_positive_rate():
    # порог по умолчанию 0.001 (0.1%) - берём заведомо выше
    assert bridge._funding_rate_too_unfavorable(0.002, bridge._LONG_SIDE) is True


def test_funding_rate_too_unfavorable_allows_long_on_negative_rate():
    # отрицательный фандинг ПЛАТИТ лонгу - не блокирует, как бы велик ни был по модулю
    assert bridge._funding_rate_too_unfavorable(-0.05, bridge._LONG_SIDE) is False


def test_funding_rate_too_unfavorable_blocks_short_on_high_negative_rate():
    assert bridge._funding_rate_too_unfavorable(-0.002, bridge._SHORT_SIDE) is True


def test_funding_rate_too_unfavorable_allows_short_on_positive_rate():
    assert bridge._funding_rate_too_unfavorable(0.05, bridge._SHORT_SIDE) is False


def test_funding_rate_within_threshold_does_not_block_either_side():
    assert bridge._funding_rate_too_unfavorable(0.0005, bridge._LONG_SIDE) is False
    assert bridge._funding_rate_too_unfavorable(-0.0005, bridge._SHORT_SIDE) is False


def test_execute_signal_skips_if_funding_unfavorable_for_long():
    # _signal() по умолчанию - лонг (перепроданность); ставка сильно положительная
    client = _FakeClient(mark_price=101.0, position=None, funding_rate=0.005)
    result = bridge.execute_signal(client, _signal(), risk_pct=1.0, leverage=3,
                                    risk_limits=_limits(), min_score=0)
    assert result is None
    assert ("get_funding_rate", "SOLUSDT") in client.calls


def test_execute_signal_continues_if_funding_fetch_fails(monkeypatch):
    # сбой получения фандинга - не должен блокировать сделку сам по себе,
    # проверка просто пропускается (доходим до реального открытия позиции)
    client = _FakeClient(mark_price=101.0, position=None, fail_funding=True)
    monkeypatch.setattr(risk_guard, "_consecutive_losses", lambda client, lookback=50, since_ts=None: 0)
    monkeypatch.setattr(bridge.queue_manager, "add_open_futures_position", lambda record: None)

    def fake_open_protected_position(client, symbol, side, stop_price, take_profit_price,
                                      risk_pct, leverage, risk_limits=None, margin_type="ISOLATED"):
        return SimpleNamespace(
            symbol=symbol, side=side, quantity=1.0, entry_price=101.0,
            stop_price=stop_price, take_profit_price=take_profit_price,
            stop_order={"orderId": 1}, take_profit_order={"orderId": 2},
            reference_price=101.0, slippage_pct=0.0,
        )

    monkeypatch.setattr(bridge, "open_protected_position", fake_open_protected_position)

    result = bridge.execute_signal(client, _signal(), risk_pct=1.0, leverage=3,
                                    risk_limits=_limits(), min_score=0)
    assert result is not None


# --- мягкое снижение риска (см. risk_guard.get_risk_multiplier) ---

def test_execute_signal_applies_soft_derisk_multiplier(monkeypatch):
    client = _FakeClient(mark_price=101.0, position=None)
    # 2 убытка подряд -> при soft_derisk_after_losses=2 (дефолт RiskLimits) риск должен уполовиниться
    monkeypatch.setattr(risk_guard, "_consecutive_losses", lambda client, lookback=50, since_ts=None: 2)
    monkeypatch.setattr(bridge.queue_manager, "add_open_futures_position", lambda record: None)

    captured = {}

    def fake_open_protected_position(client, symbol, side, stop_price, take_profit_price,
                                      risk_pct, leverage, risk_limits=None, margin_type="ISOLATED"):
        captured["risk_pct"] = risk_pct
        return SimpleNamespace(
            symbol=symbol, side=side, quantity=1.0, entry_price=101.0,
            stop_price=stop_price, take_profit_price=take_profit_price,
            stop_order={"orderId": 1}, take_profit_order={"orderId": 2},
            reference_price=101.0, slippage_pct=0.0,
        )

    monkeypatch.setattr(bridge, "open_protected_position", fake_open_protected_position)

    bridge.execute_signal(client, _signal(), risk_pct=1.0, leverage=3, risk_limits=_limits(), min_score=0)

    assert captured["risk_pct"] == 0.5  # 1.0 * soft_derisk_multiplier(0.5)


def test_execute_signal_keeps_full_risk_without_loss_streak(monkeypatch):
    client = _FakeClient(mark_price=101.0, position=None)
    monkeypatch.setattr(risk_guard, "_consecutive_losses", lambda client, lookback=50, since_ts=None: 0)
    monkeypatch.setattr(bridge.queue_manager, "add_open_futures_position", lambda record: None)

    captured = {}

    def fake_open_protected_position(client, symbol, side, stop_price, take_profit_price,
                                      risk_pct, leverage, risk_limits=None, margin_type="ISOLATED"):
        captured["risk_pct"] = risk_pct
        return SimpleNamespace(
            symbol=symbol, side=side, quantity=1.0, entry_price=101.0,
            stop_price=stop_price, take_profit_price=take_profit_price,
            stop_order={"orderId": 1}, take_profit_order={"orderId": 2},
            reference_price=101.0, slippage_pct=0.0,
        )

    monkeypatch.setattr(bridge, "open_protected_position", fake_open_protected_position)

    bridge.execute_signal(client, _signal(), risk_pct=1.0, leverage=3, risk_limits=_limits(), min_score=0)

    assert captured["risk_pct"] == 1.0


if __name__ == "__main__":
    import sys
    import types

    class _MiniMonkeypatch:
        """Тот же минимальный monkeypatch, что и в остальных test_*.py
        (см. test_futures_executor.py) - нужен тестам soft-derisk/
        risk_multiplier ниже, которые подменяют risk_guard/queue_manager.
        Раньше этот раннер не поддерживал monkeypatch вовсе - тесты,
        объявленные с параметром monkeypatch, падали с TypeError
        ("missing 1 required positional argument"), который не ловится
        `except AssertionError` - весь скрипт аварийно останавливался, а
        не просто помечал эти тесты как FAIL. В частности,
        test_execute_signal_applies_soft_derisk_multiplier молча никогда
        не запускался."""

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
