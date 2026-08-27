#!/usr/bin/env python3
"""
Тесты scanner.py: ручной денай-лист тикеров (config.EXCLUDED_TICKERS) -
монеты в списке должны полностью исчезать из universe, даже если
формально проходят фильтр по объёму.
"""
import config
import scanner
import strategies


class _FakeResponse:
    def __init__(self, json_data):
        self._json = json_data

    def raise_for_status(self):
        pass

    def json(self):
        return self._json


def _row(symbol: str, quote_volume: float) -> dict:
    return {"symbol": symbol, "quoteVolume": str(quote_volume)}


def test_fetch_universe_excludes_denylisted_ticker(monkeypatch):
    monkeypatch.setattr(config, "EXCLUDED_TICKERS", {"PHB"})
    rows = [_row("PHBUSDT", 28_000_000), _row("SOLUSDT", 900_000_000)]
    monkeypatch.setattr(scanner.requests, "get", lambda *a, **k: _FakeResponse(rows))

    universe = scanner._fetch_universe()

    symbols = [s for s, _ in universe]
    assert "PHBUSDT" not in symbols
    assert "SOLUSDT" in symbols


def test_fetch_universe_keeps_non_denylisted_tickers(monkeypatch):
    monkeypatch.setattr(config, "EXCLUDED_TICKERS", set())
    rows = [_row("PHBUSDT", 28_000_000)]
    monkeypatch.setattr(scanner.requests, "get", lambda *a, **k: _FakeResponse(rows))

    universe = scanner._fetch_universe()

    symbols = [s for s, _ in universe]
    assert "PHBUSDT" in symbols


def test_fetch_universe_denylist_is_exact_ticker_not_substring(monkeypatch):
    # Денай-лист должен матчить именно тикер целиком (без USDT), а не
    # произвольную подстроку символа - иначе "PH" случайно вырезал бы
    # что-то вроде "ALPHAUSDT".
    monkeypatch.setattr(config, "EXCLUDED_TICKERS", {"PH"})
    rows = [_row("PHBUSDT", 28_000_000), _row("ALPHAUSDT", 5_000_000)]
    monkeypatch.setattr(scanner.requests, "get", lambda *a, **k: _FakeResponse(rows))

    universe = scanner._fetch_universe()

    symbols = [s for s, _ in universe]
    assert "PHBUSDT" in symbols  # "PHB" != "PH", не исключается


# --- A2: ATR-стопы в _build_signal (RSI + Bollinger) ---

def _downtrend_candles(n=60, start=100.0, step=0.5):
    """Устойчивый нисходящий тренд - RSI(14) уходит к 0, гарантированно
    ниже RSI_OVERSOLD(30), даёт стабильный сигнал "Лонг (перепроданность)"
    для проверки формулы стопа."""
    candles = []
    price = start
    for _ in range(n):
        price -= step
        candles.append(scanner._Candle(open=price + 0.05, high=price + 0.1, low=price - 0.1, close=price, volume=100_000))
    return candles


def test_build_signal_uses_fixed_pct_stop_by_default(monkeypatch):
    monkeypatch.setattr(config, "USE_ATR_STOPS", False)
    candles = _downtrend_candles()
    signal = scanner._build_signal("TESTUSDT", candles, 8_000_000)
    assert signal is not None
    recent_low = min(c.low for c in candles[-20:])
    assert abs(float(signal.invalidation) - recent_low * 0.997) < 1e-6


def test_build_signal_uses_atr_stop_when_enabled(monkeypatch):
    monkeypatch.setattr(config, "USE_ATR_STOPS", True)
    monkeypatch.setattr(config, "ATR_PERIOD", 14)
    monkeypatch.setattr(config, "ATR_STOP_MULTIPLIER", 1.5)
    candles = _downtrend_candles()
    signal = scanner._build_signal("TESTUSDT", candles, 8_000_000)
    assert signal is not None
    recent_low = min(c.low for c in candles[-20:])
    atr = strategies.calc_atr(candles, 14)
    expected = recent_low - atr * 1.5
    assert abs(float(signal.invalidation) - expected) < 1e-6
    assert abs(float(signal.invalidation) - recent_low * 0.997) > 1e-6  # реально другая формула, не совпадение


def test_build_signal_atr_disabled_ignores_atr_config(monkeypatch):
    # USE_ATR_STOPS=False - даже если ATR_STOP_MULTIPLIER настроен на
    # что-то экзотическое, формула не должна его использовать вовсе.
    monkeypatch.setattr(config, "USE_ATR_STOPS", False)
    monkeypatch.setattr(config, "ATR_STOP_MULTIPLIER", 99.0)
    candles = _downtrend_candles()
    signal = scanner._build_signal("TESTUSDT", candles, 8_000_000)
    assert signal is not None
    recent_low = min(c.low for c in candles[-20:])
    assert abs(float(signal.invalidation) - recent_low * 0.997) < 1e-6


# --- _process_signal_candidate: колбэк on_signal_accepted ---

def _accepted_signal(ticker="SOL", score="85"):
    from signal_parser import RsiSignal
    return RsiSignal(
        ticker=ticker, timeframe="15m", strategy="RSI", direction="Лонг (перепроданность)",
        current_price="100", rsi_now="25", score=score, quality="Moderate",
        entry_low="99", entry_high="101", invalidation="95", target="110",
        change_24h="+1%", volume="10M", rsi_live="25", created_at="2026-07-29 00:00:00 UTC",
        description="тест", raw_text="тест",
    )


def test_process_signal_candidate_calls_callback_when_accepted(monkeypatch):
    monkeypatch.setattr(scanner.queue_manager, "was_recently_alerted", lambda *a, **k: False)
    monkeypatch.setattr(scanner.multi_timeframe, "refine_signal", lambda signal, symbol: signal)
    monkeypatch.setattr(scanner, "_atr_percentile_exceeded", lambda symbol: False)
    monkeypatch.setattr(scanner.shadow_filters, "evaluate_and_log", lambda signal, symbol: None)
    monkeypatch.setattr(scanner.strategy_tuner, "get_effective_min_score", lambda strategy, cfg: cfg)
    monkeypatch.setattr(scanner.queue_manager, "push_pending_signal", lambda signal: None)
    monkeypatch.setattr(scanner.queue_manager, "mark_alerted", lambda ticker, direction: None)

    calls = []
    signal = _accepted_signal()
    accepted = scanner._process_signal_candidate(
        signal, "SOLUSDT", "SOL", min_score_cfg=70,
        on_signal_accepted=lambda s, sym: calls.append((s.ticker, sym)),
    )
    assert accepted is True
    assert calls == [("SOL", "SOLUSDT")]


def test_process_signal_candidate_without_callback_is_backward_compatible(monkeypatch):
    # Поведение по умолчанию (on_signal_accepted=None) не должно меняться.
    monkeypatch.setattr(scanner.queue_manager, "was_recently_alerted", lambda *a, **k: False)
    monkeypatch.setattr(scanner.multi_timeframe, "refine_signal", lambda signal, symbol: signal)
    monkeypatch.setattr(scanner, "_atr_percentile_exceeded", lambda symbol: False)
    monkeypatch.setattr(scanner.shadow_filters, "evaluate_and_log", lambda signal, symbol: None)
    monkeypatch.setattr(scanner.strategy_tuner, "get_effective_min_score", lambda strategy, cfg: cfg)
    monkeypatch.setattr(scanner.queue_manager, "push_pending_signal", lambda signal: None)
    monkeypatch.setattr(scanner.queue_manager, "mark_alerted", lambda ticker, direction: None)

    accepted = scanner._process_signal_candidate(_accepted_signal(), "SOLUSDT", "SOL", min_score_cfg=70)
    assert accepted is True


def test_process_signal_candidate_callback_exception_does_not_propagate(monkeypatch):
    # Сигнал уже прошёл в очередь публикации - упавший колбэк не должен
    # превращать успешный accept в исключение наружу.
    monkeypatch.setattr(scanner.queue_manager, "was_recently_alerted", lambda *a, **k: False)
    monkeypatch.setattr(scanner.multi_timeframe, "refine_signal", lambda signal, symbol: signal)
    monkeypatch.setattr(scanner, "_atr_percentile_exceeded", lambda symbol: False)
    monkeypatch.setattr(scanner.shadow_filters, "evaluate_and_log", lambda signal, symbol: None)
    monkeypatch.setattr(scanner.strategy_tuner, "get_effective_min_score", lambda strategy, cfg: cfg)
    monkeypatch.setattr(scanner.queue_manager, "push_pending_signal", lambda signal: None)
    monkeypatch.setattr(scanner.queue_manager, "mark_alerted", lambda ticker, direction: None)

    def _boom(signal, symbol):
        raise RuntimeError("симулированный сбой в колбэке")

    accepted = scanner._process_signal_candidate(
        _accepted_signal(), "SOLUSDT", "SOL", min_score_cfg=70, on_signal_accepted=_boom,
    )
    assert accepted is True  # публикация в очередь уже прошла успешно, несмотря на упавший колбэк


def test_process_signal_candidate_callback_not_called_when_below_score(monkeypatch):
    monkeypatch.setattr(scanner.queue_manager, "was_recently_alerted", lambda *a, **k: False)
    monkeypatch.setattr(scanner.multi_timeframe, "refine_signal", lambda signal, symbol: signal)
    monkeypatch.setattr(scanner, "_atr_percentile_exceeded", lambda symbol: False)
    monkeypatch.setattr(scanner.shadow_filters, "evaluate_and_log", lambda signal, symbol: None)
    monkeypatch.setattr(scanner.strategy_tuner, "get_effective_min_score", lambda strategy, cfg: cfg)

    calls = []
    accepted = scanner._process_signal_candidate(
        _accepted_signal(score="50"), "SOLUSDT", "SOL", min_score_cfg=70,
        on_signal_accepted=lambda s, sym: calls.append((s.ticker, sym)),
    )
    assert accepted is False
    assert calls == []


def test_process_signal_candidate_accepts_score_exactly_at_threshold(monkeypatch):
    # Регресс-тест на off-by-one: score, РОВНО равный порогу публикации,
    # обязан пройти (порог - это "минимум, который проходит", а не
    # "минимум + 1"). Раньше здесь стояло `<=`, из-за чего сигналы,
    # которые multi_timeframe.refine_signal часто подтягивает ровно до
    # порога (например 54 -> 70 при подтверждении старшими ТФ), тихо
    # отбрасывались - ни в очередь постов, ни колбэку futures-автотрейдинга.
    monkeypatch.setattr(scanner.queue_manager, "was_recently_alerted", lambda *a, **k: False)
    monkeypatch.setattr(scanner.multi_timeframe, "refine_signal", lambda signal, symbol: signal)
    monkeypatch.setattr(scanner, "_atr_percentile_exceeded", lambda symbol: False)
    monkeypatch.setattr(scanner.shadow_filters, "evaluate_and_log", lambda signal, symbol: None)
    monkeypatch.setattr(scanner.strategy_tuner, "get_effective_min_score", lambda strategy, cfg: cfg)
    monkeypatch.setattr(scanner.queue_manager, "push_pending_signal", lambda signal: None)
    monkeypatch.setattr(scanner.queue_manager, "mark_alerted", lambda ticker, direction: None)

    calls = []
    accepted = scanner._process_signal_candidate(
        _accepted_signal(score="70"), "SOLUSDT", "SOL", min_score_cfg=70,
        on_signal_accepted=lambda s, sym: calls.append((s.ticker, sym)),
    )
    assert accepted is True
    assert calls == [("SOL", "SOLUSDT")]


# --- _process_signal_candidate: минимальный R:R (см. config.MIN_RISK_REWARD_RATIO) ---

def _poor_rr_signal(ratio: float, score="85"):
    """entry mid = 100, риск фиксирован в 10 (стоп=90) - target подобран
    так, чтобы reward/risk == ratio ровно."""
    from signal_parser import RsiSignal
    target = 100 + 10 * ratio
    return RsiSignal(
        ticker="SOL", timeframe="15m", strategy="RSI", direction="Лонг (перепроданность)",
        current_price="100", rsi_now="25", score=score, quality="Moderate",
        entry_low="99", entry_high="101", invalidation="90", target=f"{target:.6g}",
        change_24h="+1%", volume="10M", rsi_live="25", created_at="2026-07-29 00:00:00 UTC",
        description="тест", raw_text="тест",
    )


def test_process_signal_candidate_rejected_when_risk_reward_below_threshold(monkeypatch):
    monkeypatch.setattr(scanner.queue_manager, "was_recently_alerted", lambda *a, **k: False)
    monkeypatch.setattr(scanner.multi_timeframe, "refine_signal", lambda signal, symbol: signal)
    monkeypatch.setattr(scanner, "_atr_percentile_exceeded", lambda symbol: False)
    monkeypatch.setattr(scanner.shadow_filters, "evaluate_and_log", lambda signal, symbol: None)
    monkeypatch.setattr(config, "MIN_RISK_REWARD_RATIO", 1.2)

    calls = []
    accepted = scanner._process_signal_candidate(
        _poor_rr_signal(ratio=0.8), "SOLUSDT", "SOL", min_score_cfg=70,
        on_signal_accepted=lambda s, sym: calls.append((s.ticker, sym)),
    )
    assert accepted is False
    assert calls == []


def test_process_signal_candidate_accepts_risk_reward_exactly_at_threshold(monkeypatch):
    # ratio РОВНО равный порогу должен пройти (порог - "минимум, который
    # проходит", та же логика, что и в off-by-one тесте для score выше).
    monkeypatch.setattr(scanner.queue_manager, "was_recently_alerted", lambda *a, **k: False)
    monkeypatch.setattr(scanner.multi_timeframe, "refine_signal", lambda signal, symbol: signal)
    monkeypatch.setattr(scanner, "_atr_percentile_exceeded", lambda symbol: False)
    monkeypatch.setattr(scanner.shadow_filters, "evaluate_and_log", lambda signal, symbol: None)
    monkeypatch.setattr(scanner.strategy_tuner, "get_effective_min_score", lambda strategy, cfg: cfg)
    monkeypatch.setattr(scanner.queue_manager, "push_pending_signal", lambda signal: None)
    monkeypatch.setattr(scanner.queue_manager, "mark_alerted", lambda ticker, direction: None)
    monkeypatch.setattr(config, "MIN_RISK_REWARD_RATIO", 1.2)

    calls = []
    accepted = scanner._process_signal_candidate(
        _poor_rr_signal(ratio=1.2), "SOLUSDT", "SOL", min_score_cfg=70,
        on_signal_accepted=lambda s, sym: calls.append((s.ticker, sym)),
    )
    assert accepted is True
    assert calls == [("SOL", "SOLUSDT")]


def test_process_signal_candidate_high_score_does_not_rescue_poor_risk_reward(monkeypatch):
    # ключевая идея фильтра: плохой R:R не спасти высоким score.
    monkeypatch.setattr(scanner.queue_manager, "was_recently_alerted", lambda *a, **k: False)
    monkeypatch.setattr(scanner.multi_timeframe, "refine_signal", lambda signal, symbol: signal)
    monkeypatch.setattr(scanner, "_atr_percentile_exceeded", lambda symbol: False)
    monkeypatch.setattr(scanner.shadow_filters, "evaluate_and_log", lambda signal, symbol: None)
    monkeypatch.setattr(scanner.strategy_tuner, "get_effective_min_score", lambda strategy, cfg: cfg)
    monkeypatch.setattr(config, "MIN_RISK_REWARD_RATIO", 1.2)

    accepted = scanner._process_signal_candidate(
        _poor_rr_signal(ratio=0.5, score="100"), "SOLUSDT", "SOL", min_score_cfg=0,
    )
    assert accepted is False


def test_process_signal_candidate_not_blocked_when_ratio_cannot_be_computed(monkeypatch):
    # числа не распознались/риск=0 -> calc_risk_reward_ratio вернёт None -
    # фильтр НЕ блокирует из-за собственной невозможности посчитать,
    # проверка просто пропускается (как и остальные "мягкие" проверки).
    from signal_parser import RsiSignal
    signal = RsiSignal(
        ticker="SOL", timeframe="15m", strategy="RSI", direction="Лонг (перепроданность)",
        current_price="100", rsi_now="25", score="85", quality="Moderate",
        entry_low="100", entry_high="100", invalidation="100", target="130",  # риск = 0
        change_24h="+1%", volume="10M", rsi_live="25", created_at="2026-07-29 00:00:00 UTC",
        description="тест", raw_text="тест",
    )
    monkeypatch.setattr(scanner.queue_manager, "was_recently_alerted", lambda *a, **k: False)
    monkeypatch.setattr(scanner.multi_timeframe, "refine_signal", lambda s, sym: s)
    monkeypatch.setattr(scanner, "_atr_percentile_exceeded", lambda symbol: False)
    monkeypatch.setattr(scanner.shadow_filters, "evaluate_and_log", lambda signal, symbol: None)
    monkeypatch.setattr(scanner.strategy_tuner, "get_effective_min_score", lambda strategy, cfg: cfg)
    monkeypatch.setattr(scanner.queue_manager, "push_pending_signal", lambda signal: None)
    monkeypatch.setattr(scanner.queue_manager, "mark_alerted", lambda ticker, direction: None)
    monkeypatch.setattr(config, "MIN_RISK_REWARD_RATIO", 1.2)

    accepted = scanner._process_signal_candidate(signal, "SOLUSDT", "SOL", min_score_cfg=70)
    assert accepted is True


# --- P2.5: фильтр по перцентилю ATR (config.ATR_PERCENTILE_LOOKBACK_DAYS/_THRESHOLD) ---

def _daily_kline_row(high: float, low: float, close: float):
    """Подделка под сырую строку /klines от Binance - _fetch_klines
    читает open=[1], high=[2], low=[3], close=[4], объём=[7] по индексу."""
    return ["0", "0", str(high), str(low), str(close), "0", "0", "0"]


def _flat_daily_rows(n: int, price: float = 100.0, day_range: float = 1.0):
    """n дневных свечей с одинаковым диапазоном day_range - ATR должен
    сойтись к этому же диапазону и оставаться постоянным изо дня в день."""
    return [_daily_kline_row(price + day_range / 2, price - day_range / 2, price) for _ in range(n)]


def test_percentile_matches_hand_computed_linear_interpolation():
    # [10, 20, 30, 40, 50], перцентиль 75 -> k=(5-1)*0.75=3.0 -> ровно 40.
    assert scanner._percentile([10, 20, 30, 40, 50], 75) == 40
    # перцентиль 50 (медиана нечётной длины) -> ровно средний элемент.
    assert scanner._percentile([10, 20, 30, 40, 50], 50) == 30
    # перцентиль 0/100 -> крайние значения.
    assert scanner._percentile([10, 20, 30], 0) == 10
    assert scanner._percentile([10, 20, 30], 100) == 30


def test_atr_percentile_exceeded_false_on_insufficient_data(monkeypatch):
    monkeypatch.setattr(config, "ATR_PERCENTILE_LOOKBACK_DAYS", 30)
    monkeypatch.setattr(config, "ATR_PERIOD", 14)
    # Свечей меньше, чем lookback+period+1 - мягкий отказ, не блокируем.
    monkeypatch.setattr(scanner.requests, "get", lambda *a, **k: _FakeResponse(_flat_daily_rows(10)))
    assert scanner._atr_percentile_exceeded("BTCUSDT") is False


def test_atr_percentile_exceeded_false_on_network_error(monkeypatch):
    def _boom(*a, **k):
        raise scanner.requests.RequestException("симулированная сетевая ошибка")
    monkeypatch.setattr(config, "ATR_PERCENTILE_LOOKBACK_DAYS", 30)
    monkeypatch.setattr(config, "ATR_PERIOD", 14)
    monkeypatch.setattr(scanner.requests, "get", _boom)
    assert scanner._atr_percentile_exceeded("BTCUSDT") is False


def test_atr_percentile_exceeded_false_for_stable_volatility(monkeypatch):
    # Волатильность стабильна изо дня в день - текущий ATR примерно
    # равен всей своей же истории, не должен превышать 95-й перцентиль.
    monkeypatch.setattr(config, "ATR_PERCENTILE_LOOKBACK_DAYS", 30)
    monkeypatch.setattr(config, "ATR_PERIOD", 14)
    monkeypatch.setattr(config, "ATR_PERCENTILE_THRESHOLD", 95)
    rows = _flat_daily_rows(30 + 14 + 1, price=100.0, day_range=1.0)
    monkeypatch.setattr(scanner.requests, "get", lambda *a, **k: _FakeResponse(rows))
    assert scanner._atr_percentile_exceeded("BTCUSDT") is False


def test_atr_percentile_exceeded_true_for_volatility_spike(monkeypatch):
    # 30+14 дней стабильной узкой волатильности (диапазон 1.0), затем
    # РЕЗКИЙ всплеск последнего дня (диапазон 50.0) - текущий ATR
    # должен намного превысить 95-й перцентиль спокойной истории.
    monkeypatch.setattr(config, "ATR_PERCENTILE_LOOKBACK_DAYS", 30)
    monkeypatch.setattr(config, "ATR_PERIOD", 14)
    monkeypatch.setattr(config, "ATR_PERCENTILE_THRESHOLD", 95)
    rows = _flat_daily_rows(30 + 14, price=100.0, day_range=1.0)
    rows.append(_daily_kline_row(high=150.0, low=100.0, close=100.0))  # диапазон 50 - аномалия
    monkeypatch.setattr(scanner.requests, "get", lambda *a, **k: _FakeResponse(rows))
    assert scanner._atr_percentile_exceeded("BTCUSDT") is True


def test_process_signal_candidate_rejected_when_atr_percentile_exceeded(monkeypatch):
    monkeypatch.setattr(scanner.queue_manager, "was_recently_alerted", lambda *a, **k: False)
    monkeypatch.setattr(scanner.multi_timeframe, "refine_signal", lambda signal, symbol: signal)
    monkeypatch.setattr(scanner.strategy_tuner, "get_effective_min_score", lambda strategy, cfg: cfg)
    monkeypatch.setattr(scanner, "_atr_percentile_exceeded", lambda symbol: True)

    calls = []
    accepted = scanner._process_signal_candidate(
        _accepted_signal(), "SOLUSDT", "SOL", min_score_cfg=70,
        on_signal_accepted=lambda s, sym: calls.append((s.ticker, sym)),
    )
    assert accepted is False
    assert calls == []


# --- P2.6: конфлюенция нескольких стратегий (config.STRATEGY_CONFLUENCE_BONUS) ---

def _confluence_signal(strategy: str, direction: str, score: str = "60"):
    from signal_parser import RsiSignal
    return RsiSignal(
        ticker="SOL", timeframe="15m", strategy=strategy, direction=direction,
        current_price="100", rsi_now="25", score=score, quality="Moderate",
        entry_low="99", entry_high="101", invalidation="95", target="110",
        change_24h="+1%", volume="10M", rsi_live="25", created_at="2026-07-29 00:00:00 UTC",
        description="тест", raw_text="тест",
    )


def test_confluence_bonus_not_applied_to_single_candidate():
    candidates = [_confluence_signal("RSI", "Лонг (перепроданность)")]
    result = scanner._apply_strategy_confluence_bonus(candidates)
    assert result[0].score == "60"


def test_confluence_bonus_applied_when_two_strategies_agree(monkeypatch):
    monkeypatch.setattr(config, "STRATEGY_CONFLUENCE_BONUS", 10)
    candidates = [
        _confluence_signal("RSI + Bollinger Touch", "Лонг (перепроданность)", score="60"),
        _confluence_signal("MACD Crossover", "Лонг (бычье пересечение MACD)", score="55"),
    ]
    result = scanner._apply_strategy_confluence_bonus(candidates)
    assert result[0].score == "70"
    assert result[1].score == "65"
    # score вырос сквозь границу 70 - quality должно пересчитаться (Aggressive -> Moderate).
    assert result[0].quality == "Moderate"


def test_confluence_bonus_not_applied_when_directions_disagree():
    # Одна LONG, одна SHORT по тому же символу в этом же тике - это не
    # согласие, а конфликт, бонуса быть не должно ни у одной из них.
    candidates = [
        _confluence_signal("RSI + Bollinger Touch", "Лонг (перепроданность)", score="60"),
        _confluence_signal("MACD Crossover", "Шорт (медвежье пересечение MACD)", score="55"),
    ]
    result = scanner._apply_strategy_confluence_bonus(candidates)
    assert result[0].score == "60"
    assert result[1].score == "55"


def test_confluence_bonus_not_applied_when_same_strategy_appears_twice():
    # Вырожденный случай: одна и та же strategy дважды в списке -
    # согласие сигнала САМ С СОБОЙ не считается конфлюенцией, нужны
    # РАЗНЫЕ стратегии (см. docstring _apply_strategy_confluence_bonus).
    candidates = [
        _confluence_signal("RSI + Bollinger Touch", "Лонг (перепроданность)", score="60"),
        _confluence_signal("RSI + Bollinger Touch", "Лонг (перепроданность)", score="55"),
    ]
    result = scanner._apply_strategy_confluence_bonus(candidates)
    assert result[0].score == "60"
    assert result[1].score == "55"


def test_confluence_bonus_score_capped_at_100(monkeypatch):
    monkeypatch.setattr(config, "STRATEGY_CONFLUENCE_BONUS", 10)
    candidates = [
        _confluence_signal("RSI + Bollinger Touch", "Лонг (перепроданность)", score="95"),
        _confluence_signal("MACD Crossover", "Лонг (бычье пересечение MACD)", score="98"),
    ]
    result = scanner._apply_strategy_confluence_bonus(candidates)
    assert result[0].score == "100"
    assert result[1].score == "100"


def test_confluence_bonus_applies_to_all_three_when_three_strategies_agree(monkeypatch):
    monkeypatch.setattr(config, "STRATEGY_CONFLUENCE_BONUS", 5)
    candidates = [
        _confluence_signal("RSI + Bollinger Touch", "Лонг (перепроданность)", score="60"),
        _confluence_signal("MACD Crossover", "Лонг (бычье пересечение MACD)", score="60"),
        _confluence_signal("Donchian Breakout", "Лонг (пробой вверх)", score="60"),
    ]
    result = scanner._apply_strategy_confluence_bonus(candidates)
    assert [c.score for c in result] == ["65", "65", "65"]


def test_run_scan_applies_confluence_bonus_before_processing(monkeypatch):
    # Интеграционный тест: убеждаемся, что _apply_strategy_confluence_bonus
    # реально встроен в конвейер run_scan (не только протестирован как
    # изолированная функция) - базовая RSI-стратегия и одна доп.
    # стратегия из ADDITIONAL_STRATEGIES соглашаются по направлению на
    # одном и том же символе/тике, и _process_signal_candidate должен
    # получить УЖЕ забустенный score.
    monkeypatch.setattr(config, "STRATEGY_CONFLUENCE_BONUS", 10)
    monkeypatch.setattr(scanner, "_fetch_universe", lambda: [("SOLUSDT", 10_000_000.0)])
    monkeypatch.setattr(scanner, "_fetch_klines", lambda symbol, **k: [object()])  # непустой список - достаточно
    monkeypatch.setattr(scanner, "_is_actively_trading", lambda symbol: True)
    monkeypatch.setattr(
        scanner, "_build_signal",
        lambda symbol, candles, qv: _confluence_signal("RSI + Bollinger Touch", "Лонг (перепроданность)", score="60"),
    )
    monkeypatch.setattr(
        scanner.strategies, "ADDITIONAL_STRATEGIES",
        [lambda symbol, candles, qv: _confluence_signal("MACD Crossover", "Лонг (бычье пересечение MACD)", score="55")],
    )

    seen_scores = []
    monkeypatch.setattr(
        scanner, "_process_signal_candidate",
        lambda signal, symbol, ticker, min_score_cfg, on_signal_accepted=None: seen_scores.append(signal.score) or False,
    )

    scanner.run_scan()
    assert seen_scores == ["70", "65"]  # 60+10 и 55+10 - оба забустены


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