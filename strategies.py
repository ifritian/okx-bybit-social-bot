"""
strategies.py - дополнительные, НЕЗАВИСИМЫЕ от RSI/Bollinger стратегии
обнаружения потенциально прибыльных сетапов.

До этого файла у бота была ОДНА логика поиска сигналов - RSI(14) +
Bollinger Bands(20,2) на 15-минутках (см. scanner._build_signal). Она
хорошо ловит МИН-РЕВЕРСИЮ (перекупленность/перепроданность в рамках
бокового/умеренно трендового рынка), но принципиально не видит других
типов возможностей - например, начало нового тренда (MACD) или выход
цены из длительной консолидации с объёмом (пробой диапазона). Рынок не
всегда находится в состоянии, подходящем для мин-реверсии - в сильном
тренде RSI может часами находиться в "экстремальной" зоне, ничего не
разворачиваясь, и наоборот, пропускать momentum-сетапы, которые как раз
для тренда куда надёжнее.

Стратегии этого файла НЕ комбинируются с RSI/Bollinger и друг с другом -
каждая работает полностью самостоятельно на тех же уже полученных 15м-
свечах (без лишних сетевых запросов) и, если находит свой собственный
сетап, возвращает ГОТОВЫЙ RsiSignal - ровно так же, как и
scanner._build_signal. scanner.run_scan/index_signal_scanner.run_index_scan
вызывают ВСЕ стратегии по очереди на одних и тех же свечах - сигнал от
любой из них независимо проходит через тот же дальнейший конвейер
(multi_timeframe-подтверждение, cooldown, порог публикации, статистика
outcome_tracker/strategy_tuner - см. docstring там же, они уже
универсальны по названию стратегии и не требуют доработки).

Стратегии:
- MACD Crossover - пересечение линии MACD(12,26) и её сигнальной линии
  EMA(9) - классический индикатор смены momentum, наиболее показателен
  ИМЕННО в начале/конце тренда (в отличие от RSI, который лучше себя
  ведёт в боковике).
- Donchian Breakout - пробой канала последних BREAKOUT_LOOKBACK свечей
  с подтверждением объёмом - ловит выход из консолидации, самый
  распространённый повод для начала нового трендового движения.
"""
import logging
from datetime import datetime, timezone
from typing import Optional

import config
from signal_parser import RsiSignal

logger = logging.getLogger(__name__)

# --- MACD Crossover ---
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9

# --- Donchian Breakout ---
BREAKOUT_LOOKBACK = 20          # сколько предыдущих свечей формируют канал (без текущей)
BREAKOUT_VOLUME_LOOKBACK = 20   # окно для "обычного" объёма, с которым сравнивается объём свечи пробоя
BREAKOUT_VOLUME_RATIO_MIN = 1.5  # во сколько раз объём пробойной свечи должен превышать средний, чтобы считаться подтверждённым


def _ema_series(values: list[float], period: int) -> list[float]:
    """Простая EMA - возвращает серию той же длины, что values (первое
    значение - SMA за period, дальше обычная рекурсивная EMA)."""
    if len(values) < period:
        return []
    k = 2 / (period + 1)
    ema = [sum(values[:period]) / period]
    for price in values[period:]:
        ema.append(price * k + ema[-1] * (1 - k))
    return ema


def _quality_from_score(score: int) -> str:
    """Та же шкала, что и scanner._score_and_quality/multi_timeframe -
    отдельная копия (не импорт из scanner.py), чтобы не создавать
    циклическую зависимость: scanner.py импортирует ЭТОТ модуль, а не
    наоборот."""
    if score >= 90:
        return "Conservative"
    if score >= 70:
        return "Moderate"
    return "Aggressive"


def calc_atr_series(candles: list, period: int = 14) -> list[float]:
    """Как calc_atr ниже, но возвращает ВЕСЬ ряд ATR (одно значение на
    каждую свечу начиная с period-й), а не только последнее число.
    Нужно, чтобы сравнить ТЕКУЩИЙ ATR с распределением ATR за
    предыдущие N свечей - см. P2.5 (config.ATR_PERCENTILE_LOOKBACK_DAYS/
    _THRESHOLD, scanner._atr_percentile_exceeded). calc_atr ниже - это
    просто calc_atr_series(...)[-1], один и тот же расчёт, без
    дублирования логики Уайлдера в двух местах.

    [] (не None), если данных недостаточно - вызывающему коду проще
    работать с пустым списком (можно сразу проверить len()/срез), чем
    отдельно обрабатывать None."""
    if len(candles) < period + 1:
        return []
    true_ranges = []
    for i in range(1, len(candles)):
        high, low, prev_close = candles[i].high, candles[i].low, candles[i - 1].close
        true_ranges.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
    if len(true_ranges) < period:
        return []
    atr = sum(true_ranges[:period]) / period
    series = [atr]
    for tr in true_ranges[period:]:
        atr = (atr * (period - 1) + tr) / period
        series.append(atr)
    return series


def calc_atr(candles: list, period: int = 14) -> Optional[float]:
    """Average True Range по Уайлдеру (то же рекурсивное сглаживание,
    что и RSI в этом проекте - см. multi_timeframe._calc_rsi_last) -
    средний ИСТИННЫЙ диапазон за period свечей, а не просто high-low:
    True Range = max(high-low, |high-prev_close|, |low-prev_close|),
    так учитываются и гэпы между свечами, не только размах внутри одной.

    Используется для A2 (см. config.USE_ATR_STOPS) - привязать ширину
    стопа к текущей волатильности конкретной монеты вместо одного
    фиксированного % отступа на все монеты сразу. Живёт здесь (не в
    scanner.py), потому что strategies.py уже не имеет обратной
    зависимости от scanner.py (см. _quality_from_score выше) - a
    scanner._build_signal просто импортирует strategies и зовёт эту
    функцию, тем же способом, каким уже импортирует ADDITIONAL_STRATEGIES.

    None, если свечей меньше period+1 - для ПЕРВОГО значения ATR нужно
    period штук True Range, а TR самой первой свечи посчитать не из
    чего (нет предыдущего close). Вызывающий код (scanner._build_signal
    и стратегии этого файла) в этом случае тихо откатывается на старую
    формулу с фиксированным %, а не пропускает сигнал."""
    series = calc_atr_series(candles, period)
    return series[-1] if series else None


def _atr_target(entry_price: float, is_long: bool, atr: Optional[float]) -> Optional[float]:
    """P3.8 - цель (target), масштабированная по ТЕКУЩЕЙ волатильности
    (ATR) от цены входа, вместо фиксированной СТРУКТУРНОЙ дистанции
    (измеренное движение канала / ближайший экстремум последних N
    свечей - см. config.USE_ATR_TARGETS про мотивацию). На затихшем
    рынке структурная цель может оказаться нереалистично широкой
    относительно текущего потенциала хода, на резко разогнавшемся -
    наоборот, заниженной.

    None, если atr не посчитан (недостаточно свечей, см. calc_atr) -
    вызывающий код сам решает откат на прежнюю (структурную) логику,
    та же ответственность за фолбэк, что и у ATR-буфера стопа (A2)."""
    if atr is None:
        return None
    distance = atr * config.ATR_TARGET_MULTIPLIER
    return entry_price + distance if is_long else entry_price - distance


def build_macd_signal(symbol: str, candles: list, quote_volume: float) -> Optional[RsiSignal]:
    """Ищет свежее пересечение линии MACD и сигнальной линии на ПОСЛЕДНЕЙ
    свече (пересечение произошло только что, не несколько свечей назад -
    иначе сигнал уже неактуален). Возвращает None, если пересечения
    прямо сейчас нет или данных недостаточно."""
    closes = [c.close for c in candles]
    if len(closes) < MACD_SLOW + MACD_SIGNAL + 2:
        return None

    ema_fast = _ema_series(closes, MACD_FAST)
    ema_slow = _ema_series(closes, MACD_SLOW)
    # ema_fast длиннее ema_slow (короче период - раньше стартует) -
    # выравниваем по правому краю (последней свече), чтобы вычитать
    # значения одного и того же момента времени.
    offset = len(ema_fast) - len(ema_slow)
    macd_line = [f - s for f, s in zip(ema_fast[offset:], ema_slow)]

    signal_line = _ema_series(macd_line, MACD_SIGNAL)
    if len(signal_line) < 2:
        return None
    # macd_line длиннее signal_line на MACD_SIGNAL-1 - выравниваем так же.
    macd_aligned = macd_line[-len(signal_line):]

    macd_now, macd_prev = macd_aligned[-1], macd_aligned[-2]
    sig_now, sig_prev = signal_line[-1], signal_line[-2]

    bullish_cross = macd_prev <= sig_prev and macd_now > sig_now
    bearish_cross = macd_prev >= sig_prev and macd_now < sig_now
    if not (bullish_cross or bearish_cross):
        return None  # прямо сейчас пересечения нет - по этой паре сказать нечего

    histogram = macd_now - sig_now
    current_price = closes[-1]
    hist_pct = abs(histogram) / current_price * 100 if current_price else 0.0

    # Пересечение "со стороны разворота" (бычье ниже нулевой линии,
    # медвежье выше неё) статистически интереснее, чем продолжение уже
    # идущего давно движения - momentum только начинает разворачиваться,
    # а не истощается на исходе тренда.
    reversal_side = (bullish_cross and macd_now < 0) or (bearish_cross and macd_now > 0)

    score = 30 + min(hist_pct * 40, 40)
    if reversal_side:
        score += 15
    if quote_volume >= 5_000_000:
        score += 5
    score = round(min(score, 100))
    quality = _quality_from_score(score)

    recent_high = max(c.high for c in candles[-20:])
    recent_low = min(c.low for c in candles[-20:])
    atr = None
    if config.USE_ATR_STOPS or config.USE_ATR_TARGETS:
        atr = calc_atr(candles, config.ATR_PERIOD)
    atr_buffer = atr * config.ATR_STOP_MULTIPLIER if (config.USE_ATR_STOPS and atr) else None

    if bullish_cross:
        direction = "Лонг (бычье пересечение MACD)"
        entry_low, entry_high = current_price * 0.999, current_price * 1.002
        invalidation = recent_low - atr_buffer if atr_buffer else recent_low * 0.997
        target = _atr_target(current_price, True, atr) if config.USE_ATR_TARGETS else None
        if target is None:
            target = recent_high
    else:
        direction = "Шорт (медвежье пересечение MACD)"
        entry_low, entry_high = current_price * 0.998, current_price * 1.001
        invalidation = recent_high + atr_buffer if atr_buffer else recent_high * 1.003
        target = _atr_target(current_price, False, atr) if config.USE_ATR_TARGETS else None
        if target is None:
            target = recent_low

    ticker = symbol.replace("USDT", "")
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    change_24h_str = ""
    if len(closes) >= 97 and closes[-97] != 0:
        change_24h = (current_price - closes[-97]) / closes[-97] * 100
        change_24h_str = f"{'+' if change_24h >= 0 else ''}{change_24h:.2f}%"

    description = (
        f"{'Бычье' if bullish_cross else 'Медвежье'} пересечение MACD/сигнальной линии на 15m"
        + (", разворотного типа (пересечение по другую сторону нуля)" if reversal_side else "")
        + ". Собственный сканер бота, независимая от RSI/Bollinger стратегия."
    )

    return RsiSignal(
        ticker=ticker, timeframe="15m", strategy="MACD Crossover", direction=direction,
        current_price=f"{current_price:.6g}", rsi_now="н/д", score=str(score), quality=quality,
        entry_low=f"{entry_low:.6g}", entry_high=f"{entry_high:.6g}",
        invalidation=f"{invalidation:.6g}", target=f"{target:.6g}",
        change_24h=change_24h_str, volume=f"{quote_volume / 1_000_000:.2f}M", rsi_live="н/д",
        created_at=now_str, description=description,
        raw_text="(сгенерировано собственным сканером, без исходного текста)",
    )


def build_breakout_signal(symbol: str, candles: list, quote_volume: float) -> Optional[RsiSignal]:
    """Ищет пробой канала последних BREAKOUT_LOOKBACK свечей (без учёта
    текущей) НА ТЕКУЩЕЙ свече, с подтверждением объёмом (см.
    BREAKOUT_VOLUME_RATIO_MIN) - пробой на обычном/низком объёме
    статистически куда чаще оказывается ложным (быстро возвращается
    обратно в диапазон), чем пробой с явным всплеском объёма."""
    lookback = candles[-(BREAKOUT_LOOKBACK + 1):-1]
    if len(lookback) < BREAKOUT_LOOKBACK:
        return None

    current = candles[-1]
    channel_high = max(c.high for c in lookback)
    channel_low = min(c.low for c in lookback)

    breakout_up = current.close > channel_high
    breakout_down = current.close < channel_low
    if not (breakout_up or breakout_down):
        return None  # цена всё ещё внутри канала - пробоя прямо сейчас нет

    volume_window = candles[-(BREAKOUT_VOLUME_LOOKBACK + 1):-1]
    avg_volume = sum(c.volume for c in volume_window) / len(volume_window) if volume_window else 0.0
    volume_ratio = (current.volume / avg_volume) if avg_volume > 0 else 0.0
    volume_confirmed = volume_ratio >= BREAKOUT_VOLUME_RATIO_MIN

    if not volume_confirmed:
        # Без объёма пробой слишком часто оказывается ложным - не
        # публикуем такой сетап вообще, а не просто занижаем score (тот
        # же принцип строгости, что и veto в multi_timeframe.py).
        return None

    level = channel_high if breakout_up else channel_low
    range_height = channel_high - channel_low
    current_price = current.close
    breakout_strength_pct = abs(current_price - level) / level * 100 if level else 0.0

    atr = None
    if config.USE_ATR_STOPS or config.USE_ATR_TARGETS:
        atr = calc_atr(candles, config.ATR_PERIOD)
    atr_buffer = atr * config.ATR_STOP_MULTIPLIER if (config.USE_ATR_STOPS and atr) else None

    score = 30 + min(breakout_strength_pct * 25, 30)
    score += min((volume_ratio - BREAKOUT_VOLUME_RATIO_MIN) * 10, 20)  # чем сильнее объём выше порога, тем увереннее пробой
    if quote_volume >= 5_000_000:
        score += 5
    score = round(min(max(score, 0), 100))
    quality = _quality_from_score(score)

    ticker = symbol.replace("USDT", "")
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    closes = [c.close for c in candles]

    change_24h_str = ""
    if len(closes) >= 97 and closes[-97] != 0:
        change_24h = (current_price - closes[-97]) / closes[-97] * 100
        change_24h_str = f"{'+' if change_24h >= 0 else ''}{change_24h:.2f}%"

    if breakout_up:
        direction = "Лонг (пробой диапазона вверх)"
        entry_low, entry_high = current_price * 0.999, current_price * 1.003
        invalidation = level - atr_buffer if atr_buffer else level * 0.995
        # P3.8 - см. _atr_target и config.USE_ATR_TARGETS: тот же
        # принцип, что и в build_signal выше - фиксированное измеренное
        # движение канала (range_height) может оказаться нереалистично
        # узким на резко разогнавшемся после пробоя рынке (ATR тогда
        # выше своей нормы) или неоправданно широким на затихшем.
        # Фолбэк на range_height, если ATR не посчитан или выключено.
        target = _atr_target(current_price, True, atr) if config.USE_ATR_TARGETS else None
        if target is None:
            target = level + range_height
    else:
        direction = "Шорт (пробой диапазона вниз)"
        entry_low, entry_high = current_price * 0.997, current_price * 1.001
        invalidation = level + atr_buffer if atr_buffer else level * 1.005
        target = _atr_target(current_price, False, atr) if config.USE_ATR_TARGETS else None
        if target is None:
            target = level - range_height

    description = (
        f"Пробой {BREAKOUT_LOOKBACK}-свечного диапазона {'вверх' if breakout_up else 'вниз'} на 15m, "
        f"объём пробойной свечи в {volume_ratio:.1f}x выше среднего. "
        "Собственный сканер бота, независимая от RSI/Bollinger стратегия."
    )

    return RsiSignal(
        ticker=ticker, timeframe="15m", strategy="Donchian Breakout", direction=direction,
        current_price=f"{current_price:.6g}", rsi_now="н/д", score=str(score), quality=quality,
        entry_low=f"{entry_low:.6g}", entry_high=f"{entry_high:.6g}",
        invalidation=f"{invalidation:.6g}", target=f"{target:.6g}",
        change_24h=change_24h_str, volume=f"{quote_volume / 1_000_000:.2f}M", rsi_live="н/д",
        created_at=now_str, description=description,
        raw_text="(сгенерировано собственным сканером, без исходного текста)",
    )


# Реестр всех дополнительных стратегий - scanner.run_scan/
# index_signal_scanner.run_index_scan перебирают его целиком, пробуя
# каждую НЕЗАВИСИМО на одних и тех же уже полученных свечах (в
# дополнение к основной RSI/Bollinger, которая живёт в scanner.py и
# сюда сознательно не включена - у неё уже есть отдельный вызов в
# обоих местах).
ADDITIONAL_STRATEGIES = (build_macd_signal, build_breakout_signal)