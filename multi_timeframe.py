"""
multi_timeframe.py - подтверждение сигналов сканера (RSI/Bollinger на
15м, см. scanner.py) старшими таймфреймами (1ч, 4ч, 1д).

ПРОБЛЕМА, которую решает этот модуль: RSI(14) на 15-минутных свечах
может формально показать "перепроданность" ПРЯМО ПОСЕРЕДИНЕ уверенного
даунтренда старшего порядка - сигнал валиден по своей же формуле, но
статистически куда менее надёжен, чем тот же самый сигнал в рамках
восходящего (или хотя бы нейтрального) тренда старшего таймфрейма. Это
классическая идея технического анализа "торгуй по тренду старшего ТФ,
входи по младшему" - без неё сканер регулярно предлагал бы "купить
перепроданность", которая на самом деле является ранней стадией падения,
а не разворотом (classic falling-knife trap).

Этот модуль НЕ переписывает формулу RSI/Bollinger (см. scanner._build_signal
и index_signal_scanner.py - там она заслуженно не меняется) - он
ДОПОЛНИТЕЛЬНО сверяет уже найденный 15м-сигнал со старшими ТФ и:
- добавляет бонус к score за каждый старший ТФ, подтверждающий
  направление сделки (см. CONFLUENCE_BONUS_PER_TF);
- вычитает штраф за каждый ТФ, идущий против (см. CONFLUENCE_PENALTY_PER_TF);
- полностью отклоняет сигнал (veto), если против сделки идут сразу
  VETO_MIN_CONFLICTING_TF старших ТФ одновременно - входить против
  уверенного тренда 4ч И 1д сразу значимо менее надёжно, чем локальный
  разворот на 15-минутках, и не должно доходить до публикации вообще.

Подключается ПОСЛЕ того, как 15м-сигнал уже найден (см. refine_signal,
вызывается из scanner.run_scan/index_signal_scanner.run_index_scan) -
три дополнительных запроса свечей идут только на реальных кандидатов
(единицы из 150 пар), а не на каждую пару "вхолостую".
"""
import logging
from dataclasses import replace
from typing import Optional

import requests

import signal_parser

logger = logging.getLogger(__name__)

_BASE_URL = "https://data-api.binance.vision/api/v3"

# Таймфреймы-"судьи" - от младшего к старшему. Сам вход (15м) сюда не
# входит, он уже посчитан в scanner.py/index_signal_scanner.py.
HTF_TIMEFRAMES = ("1h", "4h", "1d")

RSI_PERIOD = 14
TREND_MA_PERIOD = 50           # скользящая средняя для определения тренда ТФ
TREND_NEUTRAL_BAND_PCT = 0.5    # цена в пределах +-0.5% от MA - тренд "neutral", не притягиваем к сторонам искусственно

# Поправки к score - та же шкала 0-100, что и у scanner._score_and_quality.
CONFLUENCE_BONUS_PER_TF = 8      # за каждый старший ТФ, подтверждающий направление сделки
CONFLUENCE_PENALTY_PER_TF = 10   # за каждый старший ТФ, идущий против направления сделки
VETO_MIN_CONFLICTING_TF = 2      # столько (и более) ТФ против сделки одновременно - сигнал отклоняется целиком


def _fetch_closes(symbol: str, interval: str, limit: int = 120) -> list[float]:
    try:
        resp = requests.get(
            f"{_BASE_URL}/klines",
            params={"symbol": symbol, "interval": interval, "limit": limit},
            timeout=15,
        )
        resp.raise_for_status()
        rows = resp.json()
    except requests.RequestException as e:
        logger.debug("HTF: не удалось получить свечи %s %s: %s", symbol, interval, e)
        return []
    return [float(r[4]) for r in rows]


def _calc_rsi_last(closes: list[float], period: int = RSI_PERIOD) -> Optional[float]:
    """Тот же RSI по Уайлдеру, что и scanner._calc_rsi_series, но
    возвращает только ПОСЛЕДНЕЕ значение - для контекста старшего ТФ
    вся серия не нужна. Отдельная копия формулы (не импорт из scanner.py),
    чтобы у зависимости было одно направление: scanner/index_signal_scanner
    опционально используют этот модуль, а не наоборот - без циклического
    импорта."""
    if len(closes) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        delta = closes[i] - closes[i - 1]
        gains.append(max(delta, 0.0))
        losses.append(max(-delta, 0.0))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def classify_trend(closes: list[float], ma_period: int = TREND_MA_PERIOD) -> Optional[str]:
    """"up" / "down" / "neutral" - направление тренда на этом ТФ, по
    отклонению текущей цены от скользящей средней за ma_period свечей.
    Сознательно простая, устойчивая эвристика - модуль не пытается
    поймать точный разворот, только грубо отличить "плыть по течению"
    от "против". None, если данных недостаточно (пара недавно
    листнута и т.п. - тогда этот ТФ просто не участвует в подтверждении,
    см. fetch_htf_snapshot)."""
    if len(closes) < ma_period:
        return None
    ma = sum(closes[-ma_period:]) / ma_period
    if ma == 0:
        return None
    price = closes[-1]
    deviation_pct = (price - ma) / ma * 100
    if deviation_pct > TREND_NEUTRAL_BAND_PCT:
        return "up"
    if deviation_pct < -TREND_NEUTRAL_BAND_PCT:
        return "down"
    return "neutral"


def fetch_htf_snapshot(symbol: str) -> dict:
    """{tf: {"trend": ..., "rsi": ...}} по каждому таймфрейму из
    HTF_TIMEFRAMES, для которого удалось получить данные. Сетевой сбой
    по ОДНОМУ таймфрейму не убирает остальные - подтверждение получится
    менее полным, а не отсутствующим целиком."""
    snapshot = {}
    for tf in HTF_TIMEFRAMES:
        closes = _fetch_closes(symbol, tf)
        if not closes:
            continue
        trend = classify_trend(closes)
        rsi = _calc_rsi_last(closes)
        if trend is None and rsi is None:
            continue
        snapshot[tf] = {"trend": trend, "rsi": rsi}
    return snapshot


def evaluate_confluence(is_long: bool, htf_snapshot: dict) -> tuple[int, bool, str]:
    """Сверяет направление сигнала (is_long=True - лонг, любая причина:
    перепроданность по RSI, бычье пересечение MACD, пробой диапазона
    вверх и т.п.; False - шорт) со снимком старших ТФ.

    "За" сигнал - тренд этого ТФ совпадает по направлению сделки (up для
    лонга, down для шорта). "Против" - тренд направлен в обратную
    сторону. Нейтральный тренд ТФ не считается ни за, ни против -
    старший ТФ сейчас "без мнения", это не аргумент ни в одну сторону.

    Возвращает (score_adjustment, veto, note):
    - score_adjustment - прибавка/вычет к итоговому score сигнала;
    - veto=True - сигнал стоит отклонить целиком (см. VETO_MIN_CONFLICTING_TF) -
      входить против уверенного тренда сразу нескольких старших ТФ
      статистически значительно менее надёжно, чем локальный разворот
      младшего таймфрейма;
    - note - короткая человекочитаемая строка для description сигнала
      (прозрачность для читателя поста - видно, ПОЧЕМУ score именно
      такой, а не просто цифра из ниоткуда)."""
    wanted_trend = "up" if is_long else "down"
    against_trend = "down" if is_long else "up"

    confirming, conflicting = [], []
    for tf, data in htf_snapshot.items():
        trend = data.get("trend")
        if trend == wanted_trend:
            confirming.append(tf)
        elif trend == against_trend:
            conflicting.append(tf)

    adjustment = len(confirming) * CONFLUENCE_BONUS_PER_TF - len(conflicting) * CONFLUENCE_PENALTY_PER_TF
    veto = len(conflicting) >= VETO_MIN_CONFLICTING_TF

    if confirming and not conflicting:
        note = f"подтверждено старшими ТФ ({'/'.join(confirming)})"
    elif conflicting and not confirming:
        note = f"против тренда старших ТФ ({'/'.join(conflicting)})"
    elif confirming and conflicting:
        note = f"смешанная картина по ТФ (за: {'/'.join(confirming)}, против: {'/'.join(conflicting)})"
    else:
        note = "старшие ТФ нейтральны"

    return adjustment, veto, note


def _quality_from_score(score: int) -> str:
    """Та же шкала качества, что и scanner._score_and_quality - после
    поправки score старшими ТФ ярлык (Conservative/Moderate/Aggressive)
    пересчитывается заново, иначе он остался бы от ДО-поправочного
    score и мог разойтись с итоговой цифрой в посте."""
    if score >= 90:
        return "Conservative"
    if score >= 70:
        return "Moderate"
    return "Aggressive"


def refine_signal(signal, symbol: str):
    """Подтягивает старшие ТФ для symbol и возвращает ОБНОВЛЁННУЮ копию
    signal (score и quality пересчитаны, description дополнено пометкой
    про старшие ТФ) - либо None, если сигнал стоит отклонить целиком
    (см. veto в evaluate_confluence).

    Если снимок старших ТФ пустой (все три запроса не удались - редкий
    сетевой сбой) - сигнал возвращается БЕЗ ИЗМЕНЕНИЙ: лучше сигнал без
    HTF-подтверждения, чем полное отсутствие сигнала из-за временного
    сбоя вспомогательной проверки."""
    is_long = signal_parser.is_long_direction(signal.direction)

    htf_snapshot = fetch_htf_snapshot(symbol)
    if not htf_snapshot:
        return signal

    adjustment, veto, note = evaluate_confluence(is_long, htf_snapshot)
    if veto:
        logger.info(
            "HTF veto: %s %s (score был %s) отклонён - %s",
            symbol, signal.direction, signal.score, note,
        )
        return None

    new_score = max(0, min(100, int(signal.score) + adjustment))
    if adjustment != 0:
        logger.info(
            "HTF confluence: %s %s score %s -> %s (%s)",
            symbol, signal.direction, signal.score, new_score, note,
        )

    return replace(
        signal,
        score=str(new_score),
        quality=_quality_from_score(new_score),
        description=f"{signal.description} [Старшие ТФ: {note}]",
    )
