"""
backtest.py - бэктест-харнесс для формулы score и стратегий сканера.

ПРОБЛЕМА, которую закрывает этот модуль: единственный способ проверить,
работает ли формула score / конкретная стратегия / HTF-подтверждение -
ждать несколько дней живых прогонов на testnet (см.
outcome_tracker.get_accuracy_stats). Это дорого по времени: за всю
историю бота набралось 34 закрытых сигнала, win-rate 17.6%, и неясно,
это проблема формулы или просто мало данных для статистики.

ПОДХОД: этот модуль прогоняет ТЕ ЖЕ САМЫЕ функции построения сигнала
(scanner._build_signal, strategies.build_macd_signal/build_breakout_signal)
и подтверждения старшими ТФ (multi_timeframe.classify_trend/
_calc_rsi_last/evaluate_confluence - импортируются НЕИЗМЕНЁННЫМИ, не
переписываются) по историческим свечам, и резолвит исход ТЕМ ЖЕ
алгоритмом, что и outcome_tracker._resolve_outcome (см. _resolve_forward
ниже - логика идентична, отличается только источник свечей: заранее
загруженный в память массив вместо свежего запроса). За счёт переиспользования
живых функций гарантируется, что бэктест считает ТО ЖЕ САМОЕ, что
подтвердила бы "жизнь" - просто за минуты вместо дней.

ЧТО ЭТО НЕ ДЕЛАЕТ (важно понимать границы применимости):
- Не учитывает slippage/комиссии/задержку исполнения ордера - оценивает
  качество СИГНАЛА (вошёл бы ровно по entry, вышел бы ровно по
  target/stop), не качество реального исполнения. Трекинг реального
  slippage на testnet-сделках - см. futures_executor._calc_slippage_pct
  (пишется в момент входа) и outcome_tracker.get_slippage_stats
  (агрегация) - это ОТДЕЛЬНАЯ метрика качества ИСПОЛНЕНИЯ, не сигнала,
  бэктест её сознательно не учитывает и дальше не будет: смешивать
  качество формулы score с качеством конкретных ордеров на конкретной
  бирже сделало бы бэктест бесполезным для своей исходной цели (быстрая
  проверка ИДЕИ стратегии без ожидания дней живых прогонов).
- HTF-подтверждение считается ТОЧКА-В-ТОЧКУ (point-in-time) - для
  каждого 15м-сигнала используются ТОЛЬКО те бары 1ч/4ч/1д, которые уже
  ЗАКРЫЛИСЬ к моменту сигнала (см. _htf_snapshot_at, bisect по
  open_time). Это критично: наивная реализация здесь легко скатывается
  в lookahead bias (использование будущих данных), который завышает
  результаты бэктеста относительно того, что было бы доступно боту в
  реальном времени.
- strategy_tuner.get_effective_min_score НЕ применяется - его адаптация
  порога зависит от истории live-прогонов (get_accuracy_stats), которой
  в бэктесте по определению нет. Используется голый config.MIN_SIGNAL_SCORE_TO_PUBLISH
  (или --min-score), без поправки.
- Один открытый сигнал на (ticker, direction) одновременно - тот же
  cooldown (scanner.ALERT_COOLDOWN_HOURS), что и в проде.

ИСТОЧНИК ДАННЫХ: тот же data-api.binance.vision, что и у scanner.py.
ЭТОТ ХОСТ НЕДОСТУПЕН из части песочниц, где пишется код (см. белый
список сети инструмента) - реальный прогон бэктеста нужно делать там,
где крутится сам бот (вручную через workflow_dispatch, см.
.github/workflows/backtest.yml, или локально с доступом в интернет).

ИСПОЛЬЗОВАНИЕ:
    python backtest.py --symbols BTCUSDT,ETHUSDT,SOLUSDT --days 30
    python backtest.py --top 30 --days 14                  # топ-30 по объёму, как в проде
    python backtest.py --top 30 --days 14 --no-htf          # без HTF, для сравнения влияния
    python backtest.py --top 30 --days 30 --min-score 50    # с другим порогом публикации
"""
import argparse
import bisect
import json
import logging
import time
from dataclasses import dataclass, replace

import requests

import config
import multi_timeframe
import outcome_tracker
import scanner
import signal_parser
import strategies

logger = logging.getLogger(__name__)

_BASE_URL = "https://data-api.binance.vision/api/v3"

# То же окно, что и scanner._fetch_klines(symbol) на каждом реальном
# тике (limit=100) - сигнал в бэктесте видит РОВНО столько же истории,
# сколько видел бы живой сканер в этот момент, не больше.
WARMUP_CANDLES = 100
# 24ч по 15м-свечам (96 штук) - прокси для quote_volume, которую в
# проде даёт отдельный /ticker/24hr (см. scanner._fetch_universe). В
# бэктесте отдельного запроса на каждый исторический момент нет -
# считаем скользящую сумму объёма по уже загруженным свечам.
QUOTE_VOLUME_WINDOW = 96


@dataclass
class _K:
    open_time: int  # ms, начало свечи - используется как момент "сигнал появился"
    open: float
    high: float
    low: float
    close: float
    volume: float


def _fetch_klines_range(symbol: str, interval: str, start_ms: int, end_ms: int) -> list[_K]:
    """Пагинированная загрузка исторических klines за [start_ms, end_ms) -
    Binance отдаёт максимум 1000 свечей за один запрос."""
    out: list[_K] = []
    cursor = start_ms
    while cursor < end_ms:
        try:
            resp = requests.get(
                f"{_BASE_URL}/klines",
                params={"symbol": symbol, "interval": interval, "startTime": cursor,
                        "endTime": end_ms, "limit": 1000},
                timeout=20,
            )
            resp.raise_for_status()
            rows = resp.json()
        except requests.RequestException as e:
            logger.warning("Бэктест: не удалось получить %s %s: %s", symbol, interval, e)
            break
        if not rows:
            break
        for r in rows:
            out.append(_K(open_time=int(r[0]), open=float(r[1]), high=float(r[2]),
                           low=float(r[3]), close=float(r[4]),
                           volume=float(r[7]) if len(r) > 7 else 0.0))
        cursor = int(rows[-1][0]) + 1
        if len(rows) < 1000:
            break
    return out


def _htf_snapshot_at(htf_closes: dict[str, list[tuple[int, float]]], ts_ms: int) -> dict:
    """Точка-в-точку снимок старших ТФ на момент ts_ms - используются
    ТОЛЬКО бары, чьё open_time < ts_ms (закрылись до момента сигнала,
    без заглядывания в будущее). classify_trend/_calc_rsi_last
    переиспользуются из multi_timeframe.py НЕИЗМЕНЁННЫМИ - те же чистые
    функции над списком closes, что считает и прод."""
    snapshot = {}
    for tf, series in htf_closes.items():
        times = [t for t, _ in series]
        idx = bisect.bisect_left(times, ts_ms)  # бары СТРОГО до ts_ms
        closes = [c for _, c in series[:idx]]
        if not closes:
            continue
        trend = multi_timeframe.classify_trend(closes)
        rsi = multi_timeframe._calc_rsi_last(closes)
        if trend is None and rsi is None:
            continue
        snapshot[tf] = {"trend": trend, "rsi": rsi}
    return snapshot


def _resolve_forward(is_short: bool, entry: float, stop: float, target: float,
                      future_candles: list[_K]) -> tuple[str, float, float] | None:
    """Тот же алгоритм, что и outcome_tracker._resolve_outcome (см. его
    docstring про намеренное упрощение "оба уровня в одной свече = loss") -
    отличие только в источнике свечей: уже загруженный в память массив.
    Возвращает (result, exit_price, best_price) либо None, если ни один
    уровень пока не задет в пределах future_candles."""
    best_price = None
    for c in future_candles:
        if is_short:
            best_price = c.low if best_price is None else min(best_price, c.low)
            hit_target, hit_stop = c.low <= target, c.high >= stop
        else:
            best_price = c.high if best_price is None else max(best_price, c.high)
            hit_target, hit_stop = c.high >= target, c.low <= stop

        if hit_target and hit_stop:
            return "loss", stop, best_price
        if hit_target:
            return "win", target, best_price
        if hit_stop:
            return "loss", stop, best_price
    return None


def backtest_symbol(symbol: str, candles_15m: list[_K], htf_closes: dict,
                     start_ms: int, end_ms: int, use_htf: bool, min_publish_score: int) -> list[dict]:
    """Прогоняет ОДИН символ по всей истории candles_15m в диапазоне
    [start_ms, end_ms), на каждом баре имитируя то, что увидел бы живой
    сканер в этот момент (candles[:i+1], обрезанные до последних
    WARMUP_CANDLES - см. модульный docstring). Возвращает список
    закрытых (win/loss/timeout) сигналов - той же формы, что и записи
    closed_outcomes в проде."""
    ticker = symbol.replace("USDT", "")
    track_bars = max(1, round(config.OUTCOME_MAX_TRACK_HOURS * 4))  # 15м-баров в окне трекинга
    cooldown_ms = int(scanner.ALERT_COOLDOWN_HOURS * 3600 * 1000)
    last_alert_at: dict[str, int] = {}

    open_times = [k.open_time for k in candles_15m]
    start_idx = max(bisect.bisect_left(open_times, start_ms), WARMUP_CANDLES - 1)
    results: list[dict] = []

    for i in range(start_idx, len(candles_15m)):
        k = candles_15m[i]
        if k.open_time >= end_ms:
            break

        window = candles_15m[max(0, i - WARMUP_CANDLES + 1):i + 1]
        candle_objs = [scanner._Candle(open=c.open, high=c.high, low=c.low, close=c.close, volume=c.volume)
                       for c in window]
        vol_window = candles_15m[max(0, i - QUOTE_VOLUME_WINDOW + 1):i + 1]
        quote_volume = sum(c.volume for c in vol_window)

        candidates = [scanner._build_signal(symbol, candle_objs, quote_volume)]
        for build_extra in strategies.ADDITIONAL_STRATEGIES:
            candidates.append(build_extra(symbol, candle_objs, quote_volume))
        candidates = [c for c in candidates if c is not None]

        for signal in candidates:
            is_long = signal_parser.is_long_direction(signal.direction)
            direction_key = "long" if is_long else "short"

            last = last_alert_at.get(direction_key)
            if last is not None and k.open_time - last < cooldown_ms:
                continue  # тот же cooldown, что queue_manager.was_recently_alerted в проде

            if use_htf:
                snap = _htf_snapshot_at(htf_closes, k.open_time)
                if snap:
                    adjustment, veto, _note = multi_timeframe.evaluate_confluence(is_long, snap)
                    if veto:
                        continue
                    signal = replace(signal, score=str(max(0, min(100, int(signal.score) + adjustment))))

            last_alert_at[direction_key] = k.open_time

            score = int(signal.score)
            if score < min_publish_score:
                continue

            entry = (float(signal.entry_low) + float(signal.entry_high)) / 2
            stop = float(signal.invalidation)
            target = float(signal.target)
            if not (entry and stop and target):
                continue

            future = candles_15m[i + 1:i + 1 + track_bars]
            resolved = _resolve_forward(not is_long, entry, stop, target, future)
            if resolved is None:
                if len(future) < track_bars:
                    continue  # не хватает данных до конца окна трекинга - пропускаем, как "ещё открыт" в проде
                best_price = (min(c.low for c in future) if not is_long else max(c.high for c in future))
                result, exit_price = "timeout", future[-1].close
            else:
                result, exit_price, best_price = resolved

            mfe_pct = outcome_tracker._mfe_pct(entry, best_price, not is_long)
            sign = -1 if not is_long else 1
            pnl_pct = round(sign * (exit_price - entry) / entry * 100, 3) if entry else 0.0

            results.append({
                "ticker": ticker, "symbol": symbol, "direction": direction_key,
                "strategy": signal.strategy, "quality": signal.quality, "score": score,
                "result": result, "pnl_pct": pnl_pct, "mfe_pct": mfe_pct,
                "signal_time_ms": k.open_time,
            })

    return results


def _summarize(items: list[dict]) -> dict:
    n = len(items)
    if n == 0:
        return {"count": 0, "win_rate": None, "avg_pnl_pct": None}
    wins = sum(1 for r in items if r["result"] == "win")
    decided = sum(1 for r in items if r["result"] in ("win", "loss"))
    avg_pnl = sum(r["pnl_pct"] for r in items) / n
    return {
        "count": n,
        "win_rate": round(wins / decided * 100, 1) if decided else None,
        "avg_pnl_pct": round(avg_pnl, 3),
    }


def aggregate_report(all_results: list[dict]) -> dict:
    """overall + по стратегии + по 10-очковым корзинам score. Последнее -
    главный ответ на вопрос "откалибрована ли формула score вообще":
    если win-rate по корзинам НЕ растёт вместе со score, формула не
    предсказывает исход, и дело не в недостатке данных."""
    by_strategy: dict[str, list[dict]] = {}
    by_bucket: dict[str, list[dict]] = {}
    for r in all_results:
        by_strategy.setdefault(r["strategy"], []).append(r)
        lo = (r["score"] // 10) * 10
        by_bucket.setdefault(f"{lo}-{lo + 9}", []).append(r)

    return {
        "overall": _summarize(all_results),
        "by_strategy": {k: _summarize(v) for k, v in by_strategy.items()},
        "by_score_bucket": {k: _summarize(v) for k, v in sorted(by_bucket.items())},
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--symbols", help="Через запятую, напр. BTCUSDT,ETHUSDT,SOLUSDT")
    parser.add_argument("--top", type=int, default=0,
                         help="Топ-N пар по 24ч объёму вместо --symbols (тот же список, что видит live-сканер)")
    parser.add_argument("--days", type=float, default=14, help="Глубина истории в днях")
    parser.add_argument("--no-htf", action="store_true", help="Отключить HTF-подтверждение (для сравнения влияния)")
    parser.add_argument("--min-score", type=int, default=config.MIN_SIGNAL_SCORE_TO_PUBLISH,
                         help=f"Порог публикации без поправки strategy_tuner (по умолчанию {config.MIN_SIGNAL_SCORE_TO_PUBLISH})")
    parser.add_argument("--use-atr-stops", action="store_true",
                         help="A2: ATR вместо фиксированного % отступа для стопа (см. config.USE_ATR_STOPS) - "
                              "для сравнения с дефолтным поведением прогоните ОДНИ И ТЕ ЖЕ --symbols/--days "
                              "дважды, с этим флагом и без, и сравните aggregate_report")
    parser.add_argument("--atr-period", type=int, default=config.ATR_PERIOD,
                         help=f"Период ATR при --use-atr-stops (по умолчанию {config.ATR_PERIOD})")
    parser.add_argument("--atr-multiplier", type=float, default=config.ATR_STOP_MULTIPLIER,
                         help=f"Множитель ATR при --use-atr-stops (по умолчанию {config.ATR_STOP_MULTIPLIER})")
    parser.add_argument("--use-atr-targets", action="store_true",
                         help="P3.8: ATR вместо фиксированного измеренного движения/экстремума для цели "
                              "(см. config.USE_ATR_TARGETS) - для сравнения с дефолтным поведением "
                              "прогоните ОДНИ И ТЕ ЖЕ --symbols/--days дважды, с этим флагом и без, "
                              "и сравните aggregate_report, как и с --use-atr-stops")
    parser.add_argument("--atr-target-multiplier", type=float, default=config.ATR_TARGET_MULTIPLIER,
                         help=f"Множитель ATR при --use-atr-targets (по умолчанию {config.ATR_TARGET_MULTIPLIER})")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    # Прямая подмена config - те же атрибуты читают scanner._build_signal/
    # strategies.build_macd_signal/build_breakout_signal (см. A2) - без
    # отдельного протаскивания флага через backtest_symbol/build_signal.
    if args.use_atr_stops:
        config.USE_ATR_STOPS = True
        config.ATR_PERIOD = args.atr_period
        config.ATR_STOP_MULTIPLIER = args.atr_multiplier
        logger.info("A2: ATR-стопы ВКЛЮЧЕНЫ для этого прогона (период=%d, множитель=%.2f)",
                    args.atr_period, args.atr_multiplier)

    if args.use_atr_targets:
        config.USE_ATR_TARGETS = True
        config.ATR_PERIOD = args.atr_period
        config.ATR_TARGET_MULTIPLIER = args.atr_target_multiplier
        logger.info("P3.8: ATR-цели ВКЛЮЧЕНЫ для этого прогона (период=%d, множитель=%.2f)",
                    args.atr_period, args.atr_target_multiplier)

    if args.symbols:
        symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    elif args.top:
        universe = scanner._fetch_universe()
        symbols = [s for s, _ in universe[:args.top]]
        if not symbols:
            parser.error("Не удалось получить список пар по --top (сеть/API недоступны)")
    else:
        parser.error("Укажите --symbols BTCUSDT,ETHUSDT,... или --top N")

    end_ms = int(time.time() * 1000)
    start_ms = end_ms - int(args.days * 24 * 3600 * 1000)
    warmup_ms = WARMUP_CANDLES * 15 * 60 * 1000
    # Запас на TREND_MA_PERIOD=50 баров даже на 1d ТФ (50 дней) - с
    # большим запасом, чтобы classify_trend не голодал данными в начале
    # диапазона бэктеста.
    htf_warmup_ms = 60 * 24 * 3600 * 1000
    track_ms = int(config.OUTCOME_MAX_TRACK_HOURS * 3600 * 1000)

    all_results: list[dict] = []
    for symbol in symbols:
        logger.info("Бэктест: %s - загружаю историю...", symbol)
        candles_15m = _fetch_klines_range(symbol, "15m", start_ms - warmup_ms, end_ms + track_ms)
        if len(candles_15m) < WARMUP_CANDLES:
            logger.warning("Бэктест: %s - недостаточно 15м-истории (%d свечей), пропускаю",
                            symbol, len(candles_15m))
            continue

        htf_closes: dict[str, list[tuple[int, float]]] = {}
        if not args.no_htf:
            for tf in multi_timeframe.HTF_TIMEFRAMES:
                rows = _fetch_klines_range(symbol, tf, start_ms - htf_warmup_ms, end_ms)
                htf_closes[tf] = [(r.open_time, r.close) for r in rows]

        results = backtest_symbol(symbol, candles_15m, htf_closes, start_ms, end_ms,
                                   use_htf=not args.no_htf, min_publish_score=args.min_score)
        all_results.extend(results)
        logger.info("Бэктест: %s - %d закрытых сигналов", symbol, len(results))

    report = aggregate_report(all_results)
    report["settings"] = {
        "use_atr_stops": args.use_atr_stops,
        "atr_period": args.atr_period if args.use_atr_stops else None,
        "atr_multiplier": args.atr_multiplier if args.use_atr_stops else None,
        "use_htf": not args.no_htf,
        "min_score": args.min_score,
        "days": args.days,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()