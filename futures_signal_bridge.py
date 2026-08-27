"""
futures_signal_bridge.py - связывает сигналы scanner.py (RSI/Bollinger/
MACD/Breakout - см. strategies.py) с реальным открытием защищённой
позиции (см. futures_executor.open_protected_position).

ЭТО САМЫЙ РИСКОВАННЫЙ МОДУЛЬ В ПРОЕКТЕ: единственный путь, где решение
"открыть позицию" принимается БЕЗ участия человека на каждую сделку (в
отличие от futures_testnet_demo.py, где человек подтверждает каждый вход
вручную). Именно поэтому:

1. risk_guard.py (max открытых позиций / дневной убыток / серия подряд)
   - обязателен, не опционален - см. futures_auto_trade.py, который
   всегда передаёт risk_limits.
2. Здесь ЕЩЁ ОДИН слой проверок ПОВЕРХ risk_guard - специфичный для
   автоматического исполнения сигнала (см. signal_to_trade_params и
   execute_signal ниже): числа сигнала должны быть физически осмысленны
   (стоп/тейк по правильную сторону от входа), рынок не должен успеть
   уйти далеко от зоны входа сигнала, на этот символ не должно уже
   быть открытой позиции (иначе один и тот же тикер молча наращивал бы
   размер от нескольких стратегий подряд, сигналящих одно и то же), и
   ставка фандинга не должна быть слишком невыгодна для направления
   сделки (см. _funding_rate_too_unfavorable,
   config.BINANCE_FUTURES_MAX_UNFAVORABLE_FUNDING_RATE) - иначе
   стоимость удержания позиции может съесть заметную часть ожидаемой
   прибыли ещё до срабатывания тейка/стопа.
3. futures_auto_trade.py (единственный вызывающий код) жёстко использует
   TESTNET - см. его docstring - независимо от risk_limits/config.

execute_signal НИКОГДА не бросает исключение наружу - ошибка на ОДНОМ
сигнале (плохие числа, сбой API, заблокировано risk_guard) не должна
прерывать обработку остальных сигналов в этом же тике сканирования (см.
scanner._process_signal_candidate, который уже сам ловит исключения из
колбэка - здесь дополнительно ловим ожидаемые, чтобы вызывающий код мог
просто проверить None, не оборачивая каждый вызов в try/except).
"""
import logging
import time
from dataclasses import dataclass
from typing import Optional

import config
import queue_manager
import risk_guard
import signal_parser
from futures_client import FuturesApiError
from futures_executor import ExecutionError, ProtectedPositionResult, open_protected_position
from risk_guard import RiskLimits
from signal_parser import RsiSignal

logger = logging.getLogger(__name__)

_LONG_SIDE, _SHORT_SIDE = "BUY", "SELL"

# Насколько далеко (в долях от ширины зоны входа сигнала) рынок может
# уйти от entry_low/entry_high к моменту исполнения и всё ещё считаться
# "тем же самым" сетапом - сигнал строится по свечам, которые уже
# закрылись, а между сканированием и исполнением проходит время (сетевые
# запросы, обработка остальных ~150 пар в этом же тике). Слишком узкий
# допуск - сигналы почти никогда не исполнялись бы, слишком широкий -
# можно зайти по цене, для которой стоп/тейк сигнала уже не имеют смысла.
_ENTRY_TOLERANCE_FRACTION = 0.5


@dataclass
class TradeParams:
    symbol: str
    side: str
    stop_price: float
    take_profit_price: float
    entry_low: float
    entry_high: float


def _parse_price(value: str) -> Optional[float]:
    try:
        return float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def signal_to_trade_params(signal: RsiSignal) -> Optional[TradeParams]:
    """Переводит RsiSignal в параметры для open_protected_position, либо
    None, если сигнал не годится для реального открытия позиции. ЭТО НЕ
    решение "стоит ли торговать по бизнес-логике" (score/quality - см.
    execute_signal) - только проверка на то, что цифры сигнала вообще
    физически осмысленны: стоп и тейк должны быть по ПРАВИЛЬНУЮ сторону
    от зоны входа (для лонга: стоп ниже входа, тейк выше; для шорта -
    наоборот) - иначе open_protected_position либо бросит ValueError на
    нулевой/отрицательной дистанции до стопа, либо (хуже) откроет
    позицию с бессмысленным риском."""
    stop = _parse_price(signal.invalidation)
    target = _parse_price(signal.target)
    entry_low = _parse_price(signal.entry_low)
    entry_high = _parse_price(signal.entry_high)
    if None in (stop, target, entry_low, entry_high):
        logger.warning("futures_signal_bridge: не удалось распарсить числа сигнала %s (%s) - пропускаю",
                        signal.ticker, signal.direction)
        return None
    if entry_low <= 0 or entry_high <= 0 or entry_low > entry_high:
        logger.warning("futures_signal_bridge: некорректная зона входа у сигнала %s (%s-%s) - пропускаю",
                        signal.ticker, entry_low, entry_high)
        return None

    is_long = signal_parser.is_long_direction(signal.direction)
    side = _LONG_SIDE if is_long else _SHORT_SIDE
    if is_long:
        sane = stop < entry_low and entry_high < target
    else:
        sane = target < entry_low and entry_high < stop
    if not sane:
        logger.warning(
            "futures_signal_bridge: стоп/тейк не по ту сторону от входа у сигнала %s (%s): "
            "стоп=%.6g вход=%.6g-%.6g тейк=%.6g - пропускаю",
            signal.ticker, signal.direction, stop, entry_low, entry_high, target,
        )
        return None

    return TradeParams(
        symbol=f"{signal.ticker}USDT", side=side,
        stop_price=stop, take_profit_price=target,
        entry_low=entry_low, entry_high=entry_high,
    )


def _price_still_near_entry_zone(mark_price: float, params: TradeParams) -> bool:
    width = params.entry_high - params.entry_low
    tolerance = width * _ENTRY_TOLERANCE_FRACTION if width > 0 else params.entry_high * 0.001
    return (params.entry_low - tolerance) <= mark_price <= (params.entry_high + tolerance)


def _funding_rate_too_unfavorable(funding_rate: float, side: str) -> bool:
    """funding_rate - доля (0.001 = 0.1%), не проценты. Для ЛОНГА
    невыгоден высокий ПОЛОЖИТЕЛЬНЫЙ фандинг (лонги платят шортам) -
    сравниваем funding_rate с +порогом. Для ШОРТА невыгоден сильно
    ОТРИЦАТЕЛЬНЫЙ фандинг (шорты платят лонгам) - сравниваем с
    -порогом. Фандинг ПО направлению сделки (отрицательный для лонга,
    положительный для шорта) никогда не блокирует - он там наоборот
    платит нам, а не съедает прибыль."""
    threshold = config.BINANCE_FUTURES_MAX_UNFAVORABLE_FUNDING_RATE
    if side == _LONG_SIDE:
        return funding_rate > threshold
    return funding_rate < -threshold


def execute_signal(
    client,
    signal: RsiSignal,
    risk_pct: float,
    leverage: int,
    risk_limits: RiskLimits,
    min_score: int = 0,
) -> Optional[ProtectedPositionResult]:
    """Пытается открыть защищённую позицию по сигналу. Возвращает
    ProtectedPositionResult при успехе, None при любом отказе (плохие
    числа, score ниже min_score, рынок ушёл от зоны входа, уже есть
    открытая позиция по этому символу, risk_guard заблокировал,
    ошибка API) - причина всегда логируется, вызывающему коду достаточно
    проверить None. Намеренно не бросает исключения - см. docstring
    модуля про то, почему одна плохая сделка не должна ронять весь тик."""
    try:
        score = int(signal.score)
    except (TypeError, ValueError):
        score = 0
    if score <= min_score:
        logger.info("futures_signal_bridge: %s score %s <= порога %d - пропускаю",
                    signal.ticker, signal.score, min_score)
        return None

    params = signal_to_trade_params(signal)
    if params is None:
        return None

    try:
        existing = client.get_position(params.symbol)
    except FuturesApiError as e:
        logger.error("futures_signal_bridge: не удалось проверить текущую позицию %s: %s", params.symbol, e)
        return None
    if existing is not None:
        logger.info("futures_signal_bridge: по %s уже есть открытая позиция - пропускаю сигнал, "
                    "чтобы не наращивать размер по одному и тому же тикеру от нескольких стратегий", params.symbol)
        return None

    if queue_manager.was_recently_stopped_out(params.symbol, config.FUTURES_SYMBOL_COOLDOWN_HOURS):
        logger.info(
            "futures_signal_bridge: %s недавно закрылся по стопу (cooldown %.1fч) - пропускаю сигнал, "
            "чтобы не входить сразу после потенциального \"пиления\" у того же уровня",
            params.symbol, config.FUTURES_SYMBOL_COOLDOWN_HOURS,
        )
        return None

    try:
        mark_price = client.get_mark_price(params.symbol)
    except FuturesApiError as e:
        logger.error("futures_signal_bridge: не удалось получить цену %s: %s", params.symbol, e)
        return None
    if not _price_still_near_entry_zone(mark_price, params):
        logger.info(
            "futures_signal_bridge: %s - рынок (%.6g) ушёл от зоны входа сигнала (%.6g-%.6g) - пропускаю",
            params.symbol, mark_price, params.entry_low, params.entry_high,
        )
        return None

    try:
        funding_rate = client.get_funding_rate(params.symbol)
    except FuturesApiError as e:
        # Не удалось получить фандинг - не блокируем сделку из-за этого
        # (это доп. фильтр поверх основной логики, а не обязательное
        # условие), просто логируем и идём дальше без проверки.
        logger.warning("futures_signal_bridge: не удалось получить ставку фандинга %s: %s", params.symbol, e)
        funding_rate = None
    if funding_rate is not None and _funding_rate_too_unfavorable(funding_rate, params.side):
        logger.info(
            "futures_signal_bridge: %s - ставка фандинга %.4f%% слишком невыгодна для %s - пропускаю",
            params.symbol, funding_rate * 100, "лонга" if params.side == _LONG_SIDE else "шорта",
        )
        return None

    risk_multiplier, loss_streak = risk_guard.get_risk_multiplier(client, risk_limits)
    effective_risk_pct = risk_pct * risk_multiplier
    if risk_multiplier < 1.0:
        logger.info(
            "futures_signal_bridge: %d убыточных сделок подряд - риск снижен с %.2f%% до %.2f%% на эту сделку",
            loss_streak, risk_pct, effective_risk_pct,
        )

    try:
        result = open_protected_position(
            client, params.symbol, params.side,
            stop_price=params.stop_price, take_profit_price=params.take_profit_price,
            risk_pct=effective_risk_pct, leverage=leverage, risk_limits=risk_limits,
        )
    except (ExecutionError, FuturesApiError) as e:
        logger.error("futures_signal_bridge: не удалось открыть позицию по сигналу %s (%s): %s",
                      signal.ticker, signal.direction, e)
        return None

    logger.info(
        "futures_signal_bridge: открыта позиция по сигналу %s %s (%s, score %s): qty=%.8g вход~%.6g",
        signal.ticker, signal.direction, signal.strategy, signal.score, result.quantity, result.entry_price,
    )

    queue_manager.add_open_futures_position({
        "symbol": result.symbol,
        "side": result.side,
        "quantity": result.quantity,
        # Исходный размер позиции, зафиксированный НАВСЕГДА - "quantity"
        # выше будет уменьшаться после частичного профита (см.
        # futures_position_monitor._manage_partial_profit), а этот - нет.
        # Нужен, чтобы правильно считать % PnL от ПОЛНОГО риска сделки в
        # уведомлении о закрытии, даже если часть уже была зафиксирована
        # раньше отдельным ордером.
        "original_quantity": result.quantity,
        "entry_price": result.entry_price,
        # Ориентировочная цена ДО входа (mark price) и фактическое
        # проскальзывание при исполнении - см. futures_executor.
        # ProtectedPositionResult и outcome_tracker.get_slippage_stats.
        "reference_price": result.reference_price,
        "slippage_pct": result.slippage_pct,
        "stop_price": result.stop_price,
        "take_profit_price": result.take_profit_price,
        "stop_order_id": result.stop_order.get("orderId"),
        "take_profit_order_id": result.take_profit_order.get("orderId"),
        "ticker": signal.ticker,
        "direction": signal.direction,
        "strategy": signal.strategy,
        "score": signal.score,
        "opened_at": time.time(),
    })

    return result
