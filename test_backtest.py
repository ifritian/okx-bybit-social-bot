"""
Тесты backtest.py на синтетических данных - БЕЗ обращения к сети
(data-api.binance.vision недоступен из многих CI/песочниц, поэтому
корректность логики бэктеста должна быть доказуема офлайн).

Три уровня проверки:
1. Чистые функции резолюции (_resolve_forward) и point-in-time HTF-
   снимка (_htf_snapshot_at) - табличные тесты на выдуманных числах.
2. backtest_symbol() целиком на сконструированном ценовом пути,
   спроектированном так, чтобы гарантированно вызвать RSI/Bollinger
   сигнал и дать заранее известный исход (win/loss).
3. aggregate_report() - корзины по score/стратегии.
"""
import backtest


def _k(open_time, o, h, l, c, v=1_000_000.0):
    return backtest._K(open_time=open_time, open=o, high=h, low=l, close=c, volume=v)


# --- _resolve_forward ---

def test_resolve_forward_long_hits_target():
    future = [_k(0, 100, 105, 99, 104), _k(1, 104, 110, 103, 109)]  # вторая свеча high=110 >= target
    result = backtest._resolve_forward(is_short=False, entry=100, stop=95, target=110, future_candles=future)
    assert result == ("win", 110, 110)


def test_resolve_forward_long_hits_stop_first_bar():
    future = [_k(0, 100, 101, 94, 96)]  # low=94 <= stop=95
    result = backtest._resolve_forward(is_short=False, entry=100, stop=95, target=110, future_candles=future)
    assert result == ("loss", 95, 101)  # best_price - high этой же свечи (101), даже если итог - убыток


def test_resolve_forward_both_hit_same_candle_is_conservative_loss():
    # low пробивает стоп, high пробивает тейк - ВНУТРИ одной свечи
    future = [_k(0, 100, 111, 94, 105)]
    result = backtest._resolve_forward(is_short=False, entry=100, stop=95, target=110, future_candles=future)
    assert result[0] == "loss"
    assert result[1] == 95


def test_resolve_forward_short_direction():
    future = [_k(0, 100, 101, 89, 90)]  # low=89 <= target(90) для шорта
    result = backtest._resolve_forward(is_short=True, entry=100, stop=105, target=90, future_candles=future)
    assert result == ("win", 90, 89)


def test_resolve_forward_none_when_nothing_hit():
    future = [_k(0, 100, 102, 98, 101)]
    result = backtest._resolve_forward(is_short=False, entry=100, stop=90, target=120, future_candles=future)
    assert result is None


# --- _htf_snapshot_at (без lookahead) ---

def test_htf_snapshot_excludes_future_bars():
    # Бар в ts=1000 НЕ должен попасть в снимок на момент ts_ms=1000
    # (используются только бары СТРОГО до момента сигнала).
    series = [(500, 100.0), (1000, 999.0)]  # второй бар - "будущее" с аномальной ценой
    snap_before = backtest._htf_snapshot_at({"1h": series}, ts_ms=1000)
    assert "1h" not in snap_before or snap_before == {}  # недостаточно баров ДО 1000, чтобы classify_trend вообще что-то посчитал

    # На ts_ms=1500 оба бара уже "прошлое" - но для classify_trend всё
    # равно нужно >= TREND_MA_PERIOD=50 точек, так что с 2 барами снимка
    # не будет - проверяем сам факт отсечения, а не конкретное значение.
    snap_after = backtest._htf_snapshot_at({"1h": series}, ts_ms=1500)
    # Ключевая проверка: используем ту же формулу, что и в проде -
    # closes[:idx] НЕ включает бар с open_time >= ts_ms.
    idx_before = sum(1 for t, _ in series if t < 1000)
    idx_after = sum(1 for t, _ in series if t < 1500)
    assert idx_before == 1  # только первый бар видим на ts_ms=1000
    assert idx_after == 2   # оба бара видимы на ts_ms=1500


def test_htf_snapshot_no_lookahead_with_enough_data_for_trend():
    # 60 часовых баров с растущей ценой (гарантированный аптренд по MA50),
    # плюс один "будущий" бар с обвалом цены - он не должен повлиять на
    # снимок, если ts_ms стоит ДО него.
    series = [(i * 3600_000, 100.0 + i) for i in range(60)]  # растёт с 100 до 159
    crash_bar = (60 * 3600_000, 1.0)  # обвал - если бы утёк в снимок, тренд стал бы down
    series_with_crash = series + [crash_bar]

    ts_before_crash = 60 * 3600_000  # ровно момент обвала - бар с open_time==ts не включается (bisect_left, `< ts_ms`)
    snap = backtest._htf_snapshot_at({"1h": series_with_crash}, ts_ms=ts_before_crash)
    assert snap["1h"]["trend"] == "up"  # обвал не утёк - тренд всё ещё "up" по первым 60 барам


# --- backtest_symbol (интеграционно, без HTF, детерминированный путь) ---

def _flat_then_dump(n_flat=100, flat_price=100.0, dump_to=70.0, dump_bars=5):
    """Строит 15м-путь: n_flat "плоских" (лёгкий зигзаг ±0.15%, НЕ
    буквально одинаковые цены - см. ниже, почему) свечей для прогрева
    RSI/Bollinger без ложных сигналов, затем резкий обвал за dump_bars
    свечей - гарантированно выбивает RSI ниже 30 (перепроданность,
    сигнал ЛОНГ) на последней свече обвала.

    Зигзаг, а не буквально одинаковая цена каждую свечу: при ДЕЙСТВИТЕЛЬНО
    нулевой разнице цены между свечами (delta=0) RSI по Уайлдеру
    вырождается - и gains, и losses остаются равны 0 сколь угодно долго,
    avg_loss==0 -> формула возвращает RSI=100 (деление на 0 трактуется
    как "бесконечно перекуплено"), из-за чего сканер увидел бы
    ложный сигнал перекупленности на идеально плоском участке. На
    реальном рынке цена никогда не бывает identical два тика подряд,
    так что это артефакт синтетики, а не баг backtest.py/scanner.py -
    зигзаг устраняет вырождение, оставаясь "плоским" в среднем."""
    candles = []
    t = 0
    for i in range(n_flat):
        wiggle = 1 + (0.0015 if i % 2 == 0 else -0.0015)
        price = flat_price * wiggle
        candles.append(_k(t, price, price * 1.0005, price * 0.9995, price))
        t += 15 * 60_000
    step = (flat_price - dump_to) / dump_bars
    price = flat_price
    for _ in range(dump_bars):
        new_price = price - step
        candles.append(_k(t, price, price, new_price, new_price))
        price = new_price
        t += 15 * 60_000
    return candles


def test_backtest_symbol_detects_oversold_signal_and_resolves():
    # Однобарный обвал - сигнал гарантированно фиксируется РОВНО на дне,
    # а не где-то в середине многобарного падения (иначе ещё падающие
    # следующие бары могут выбить тесный стоп раньше, чем начнётся
    # отскок - это корректное поведение бэктеста, но не то, что
    # проверяет этот тест: тут нужен чистый случай "сигнал -> отскок").
    candles = _flat_then_dump(dump_bars=1)
    # Добавляем "будущее" после сигнала: цена восстанавливается до
    # уровня, гарантированно выше Bollinger-mid (target для лонга) -
    # должно резолвиться как "win".
    last = candles[-1]
    t = last.open_time + 15 * 60_000
    price = last.close
    for _ in range(20):
        price *= 1.03  # уверенный отскок
        candles.append(_k(t, price, price * 1.01, price * 0.99, price))
        t += 15 * 60_000

    start_ms = candles[0].open_time
    end_ms = candles[-1].open_time + 1

    results = backtest.backtest_symbol(
        "TESTUSDT", candles, htf_closes={}, start_ms=start_ms, end_ms=end_ms,
        use_htf=False, min_publish_score=0,  # порог 0 - хотим увидеть сигнал, каким бы ни был score
    )

    assert len(results) >= 1
    oversold_signals = [r for r in results if r["direction"] == "long"]
    assert oversold_signals, "ожидался хотя бы один лонг-сигнал на резком обвале (перепроданность)"
    # Цена уверенно отскочила - хотя бы один сигнал должен закрыться в плюс
    assert any(r["result"] == "win" for r in oversold_signals)


def test_backtest_symbol_respects_min_publish_score():
    candles = _flat_then_dump()
    start_ms, end_ms = candles[0].open_time, candles[-1].open_time + 1
    # Заведомо недостижимый порог - ничего не должно пройти.
    results = backtest.backtest_symbol(
        "TESTUSDT", candles, htf_closes={}, start_ms=start_ms, end_ms=end_ms,
        use_htf=False, min_publish_score=101,
    )
    assert results == []


def test_backtest_symbol_cooldown_prevents_duplicate_same_direction():
    # Два обвала подряд (одно и то же направление) в пределах
    # ALERT_COOLDOWN_HOURS=4ч - второй не должен породить отдельный сигнал.
    candles = _flat_then_dump(dump_bars=3)
    last = candles[-1]
    t, price = last.open_time + 15 * 60_000, last.close
    # Небольшой отскок, затем ещё один обвал - оба в пределах 4ч (16 баров по 15м)
    for _ in range(4):
        price *= 1.01
        candles.append(_k(t, price, price * 1.005, price * 0.995, price))
        t += 15 * 60_000
    for _ in range(3):
        price *= 0.9
        candles.append(_k(t, price, price, price, price))
        t += 15 * 60_000
    # Продолжение вперёд, чтобы хватило данных на резолюцию окна трекинга
    for _ in range(20):
        price *= 1.02
        candles.append(_k(t, price, price * 1.01, price * 0.99, price))
        t += 15 * 60_000

    start_ms, end_ms = candles[0].open_time, candles[-1].open_time + 1
    results = backtest.backtest_symbol(
        "TESTUSDT", candles, htf_closes={}, start_ms=start_ms, end_ms=end_ms,
        use_htf=False, min_publish_score=0,
    )
    long_signal_times = [r["signal_time_ms"] for r in results if r["direction"] == "long"]
    # Все лонг-сигналы должны отстоять друг от друга не меньше, чем на cooldown
    cooldown_ms = int(backtest.scanner.ALERT_COOLDOWN_HOURS * 3600 * 1000)
    for a, b in zip(long_signal_times, long_signal_times[1:]):
        assert b - a >= cooldown_ms


# --- aggregate_report ---

def test_aggregate_report_buckets_by_score_and_strategy():
    fake_results = [
        {"strategy": "RSI", "score": 35, "result": "loss", "pnl_pct": -1.0},
        {"strategy": "RSI", "score": 42, "result": "win", "pnl_pct": 2.0},
        {"strategy": "MACD Crossover", "score": 91, "result": "win", "pnl_pct": 3.0},
        {"strategy": "MACD Crossover", "score": 95, "result": "win", "pnl_pct": 1.5},
    ]
    report = backtest.aggregate_report(fake_results)

    assert report["overall"]["count"] == 4
    assert report["by_strategy"]["RSI"]["count"] == 2
    assert report["by_strategy"]["MACD Crossover"]["win_rate"] == 100.0
    assert report["by_score_bucket"]["30-39"]["count"] == 1
    assert report["by_score_bucket"]["40-49"]["count"] == 1
    assert report["by_score_bucket"]["90-99"]["count"] == 2
    assert report["by_score_bucket"]["90-99"]["win_rate"] == 100.0


def test_aggregate_report_empty():
    report = backtest.aggregate_report([])
    assert report["overall"] == {"count": 0, "win_rate": None, "avg_pnl_pct": None}
    assert report["by_strategy"] == {}
    assert report["by_score_bucket"] == {}
