"""
futures_executor.py - высокоуровневый "полный цикл" поверх
futures_client.FuturesClient: посчитать безопасный размер позиции по
риску, войти в рынок и СРАЗУ ЖЕ поставить стоп-лосс и тейк-профит на
стороне биржи (см. config.BINANCE_FUTURES_RISK_PCT_PER_TRADE).

КЛЮЧЕВОЙ ПРИНЦИП РИСК-МЕНЕДЖМЕНТА: риск считается НЕ от размера позиции
и НЕ от плеча, а от РАССТОЯНИЯ ДО СТОПА. Плечо влияет только на то,
сколько маржи требуется, чтобы удерживать позицию заданного размера -
оно НЕ увеличивает и не уменьшает то, сколько вы потеряете при
срабатывании стопа (это всегда risk_pct% от баланса, по построению).
Именно поэтому calc_position_size вообще не принимает leverage - высокое
плечо здесь не равно "рискнуть больше", оно просто требует меньше маржи
для той же самой, фиксированной по риску, позиции. Опасность высокого
плеча в другом - в риске ЛИКВИДАЦИИ РАНЬШЕ, чем сработает стоп (см.
_warn_if_liquidation_before_stop).

БЕЗОПАСНОСТЬ ПОСЛЕ ВХОДА: рыночный вход БЕЗ мгновенно поставленных SL/TP -
самый опасный момент во всей системе (позиция открыта, риск ничем не
ограничен). Если после успешного входа не удаётся поставить стоп ИЛИ
тейк - open_protected_position немедленно закрывает позицию по рынку
(см. _emergency_close_or_raise), а не оставляет её "голой" в расчёте на
следующий тик бота (следующего тика может не быть ещё много часов - см.
docstring про GitHub Actions cron в предыдущей версии архитектуры).

ПРЕДОХРАНИТЕЛИ ПОВЕРХ ОТДЕЛЬНОЙ СДЕЛКИ (см. risk_guard.py): параметр
risk_limits ниже - максимум одновременно открытых позиций, дневной
лимит убытка, серия убытков подряд. Он опционален (None по умолчанию)
ИСКЛЮЧИТЕЛЬНО ради обратной совместимости и юнит-тестов
(test_futures_executor.py использует поддельный клиент без методов,
которые нужны risk_guard) - ЛЮБОЙ реальный вызывающий код (CLI, будущий
автоматический вход по сигналу из scanner.py) ОБЯЗАН передавать его
(см. futures_testnet_demo.py). Без него ничто не ограничивает риск на
уровне ПОРТФЕЛЯ, только риск каждой отдельной сделки - именно поэтому
автоматический вход по сигналу без risk_limits запускать опасно.
Проверка выполняется ПЕРВЫМ делом, до единого API-вызова на изменение
чего-либо на бирже - отказ здесь гарантирует, что позиция вообще не
откроется.
"""
import logging
from dataclasses import dataclass
from typing import Optional

import risk_guard
from futures_client import FuturesClient, FuturesApiError

logger = logging.getLogger(__name__)

_LONG_SIDE, _SHORT_SIDE = "BUY", "SELL"

# Если до ликвидации (по приблизительной оценке) остаётся меньше этой
# доли расстояния до стопа - предупреждаем явно (см.
# _warn_if_liquidation_before_stop). Не блокирует сделку - решение
# всё равно за вызывающим кодом/пользователем, но молчать об этом нельзя.
_LIQUIDATION_SAFETY_MARGIN = 1.2

# Проскальзывание на входе (см. _calc_slippage_pct) выше этого порога
# (в %, уже нормализованного знака - положительное всегда невыгодно)
# логируется как WARNING, а не INFO - не блокирует вход (ордер уже
# исполнен к этому моменту, блокировать нечего), просто привлекает
# внимание к необычно плохому исполнению конкретного ордера.
_SLIPPAGE_WARN_THRESHOLD_PCT = 0.3


class ExecutionError(Exception):
    """Что-то в цикле открытия защищённой позиции пошло не так - текст
    исключения объясняет, на каком именно шаге и что сейчас с позицией
    (открыта голая / уже закрыта аварийно / не открывалась вовсе)."""


@dataclass
class ProtectedPositionResult:
    symbol: str
    side: str
    quantity: float
    entry_price: float
    stop_price: float
    take_profit_price: float
    risk_amount: float
    entry_order: dict
    stop_order: dict
    take_profit_order: dict
    # Цена, по которой считался размер позиции/риска - mark price ДО
    # отправки рыночного ордера на вход (см. open_protected_position).
    # entry_price выше - это РЕАЛЬНАЯ средняя цена исполнения (avgPrice
    # из ответа биржи на MARKET-ордер), если её удалось получить - иначе
    # (biржа изредка не успевает вернуть avgPrice синхронно) откатывается
    # на reference_price же. slippage_pct - разница между ними в % от
    # reference_price, ЗНАК так, что ПОЛОЖИТЕЛЬНОЕ значение всегда
    # означает НЕВЫГОДНОЕ исполнение (переплатили на входе в лонг /
    # недополучили на входе в шорт), независимо от стороны сделки - это
    # специально сделано, чтобы можно было усреднять slippage_pct по
    # сделкам разных направлений и получать осмысленное число (см.
    # outcome_tracker.get_slippage_stats).
    reference_price: float = 0.0
    slippage_pct: float = 0.0


def round_to_step(value: float, step: float) -> float:
    """Округляет ВНИЗ до ближайшего кратного step - Binance отклоняет
    ордер, если quantity/price не кратны step_size/tick_size биржи
    (см. FuturesClient.get_symbol_filters). Округление именно ВНИЗ (не
    к ближайшему) - чтобы никогда не запросить БОЛЬШЕ, чем позволяет
    посчитанный риск."""
    if step <= 0:
        return value
    steps = int(value / step)
    return round(steps * step, 10)


def calc_position_size(balance: float, risk_pct: float, entry_price: float, stop_price: float) -> float:
    """Возвращает размер позиции (в базовом активе, например BTC для
    BTCUSDT) такой, что при срабатывании стопа убыток равен ровно
    risk_pct% от balance - НЕЗАВИСИМО от плеча (см. модульный docstring
    про то, почему leverage сюда не передаётся)."""
    if balance <= 0 or risk_pct <= 0:
        return 0.0
    price_distance = abs(entry_price - stop_price)
    if price_distance <= 0:
        raise ValueError("entry_price и stop_price совпадают - расстояние до стопа не может быть нулевым")
    risk_amount = balance * risk_pct / 100
    return risk_amount / price_distance


def _opposite_side(side: str) -> str:
    return _SHORT_SIDE if side == _LONG_SIDE else _LONG_SIDE


def _extract_fill_price(entry_order: dict, fallback_price: float) -> float:
    """Реальная средняя цена исполнения MARKET-ордера входа -
    поле avgPrice в ответе Binance. Иногда (редко, но бывает) биржа
    синхронно возвращает avgPrice="0" - ордер уже исполнился, но поле
    ещё не успело проставиться в ответе на сам вызов - в этом случае
    откатываемся на fallback_price (reference mark price), а не на 0,
    чтобы не считать 100% slippage там, где реальных данных просто нет."""
    try:
        avg_price = float(entry_order.get("avgPrice", 0) or 0)
    except (TypeError, ValueError):
        avg_price = 0.0
    return avg_price if avg_price > 0 else fallback_price


def _calc_slippage_pct(side: str, reference_price: float, fill_price: float) -> float:
    """См. докстринг ProtectedPositionResult.slippage_pct - знак
    нормализован так, что положительное значение ВСЕГДА невыгодно,
    независимо от стороны сделки."""
    if reference_price <= 0:
        return 0.0
    raw_pct = (fill_price - reference_price) / reference_price * 100
    return raw_pct if side == _LONG_SIDE else -raw_pct


def _warn_if_liquidation_before_stop(side: str, entry_price: float, stop_price: float, leverage: int) -> None:
    """Грубая (не биржевая точная - настоящая формула ликвидации
    учитывает maintenance margin rate по уровню, комиссии и т.п.)
    прикидка: при исходной марже 1/leverage от notional, позиция
    ликвидируется примерно при движении цены на ~(1/leverage)*100% от
    входа. Если это расстояние МЕНЬШЕ, чем расстояние до стопа (с
    запасом _LIQUIDATION_SAFETY_MARGIN) - стоп физически не успеет
    сработать, позицию ликвидирует раньше биржа (на менее выгодных для
    вас условиях, чем ваш собственный стоп). Не блокирует исполнение -
    только логирует явное предупреждение, решение остаётся за вызывающим
    кодом/пользователем."""
    if leverage <= 0:
        return
    approx_liquidation_distance_pct = (1 / leverage) * 100
    stop_distance_pct = abs(entry_price - stop_price) / entry_price * 100
    if stop_distance_pct * _LIQUIDATION_SAFETY_MARGIN >= approx_liquidation_distance_pct:
        logger.warning(
            "ВНИМАНИЕ: при плече %dx расстояние до примерной ликвидации (~%.2f%%) "
            "близко к расстоянию до стопа (%.2f%%) - позицию может ликвидировать "
            "раньше, чем сработает стоп-лосс. Рассмотрите меньшее плечо.",
            leverage, approx_liquidation_distance_pct, stop_distance_pct,
        )


def _emergency_close_or_raise(client: FuturesClient, symbol: str, step: str, original_error: Exception) -> None:
    """Вызывается, когда стоп ИЛИ тейк не удалось поставить ПОСЛЕ
    успешного входа - позиция сейчас "голая" (без защиты). Пытается
    немедленно закрыть её по рынку, чтобы не оставлять риск открытым
    до следующего запуска бота. Если и это не удаётся - пробрасывает
    ExecutionError с явным указанием, что позиция ТРЕБУЕТ РУЧНОГО
    вмешательства прямо сейчас."""
    logger.error("Не удалось поставить %s для %s (%s) - аварийно закрываю позицию по рынку", step, symbol, original_error)
    try:
        position = client.get_position(symbol)
        if position is None:
            raise ExecutionError(
                f"Ошибка при постановке {step} для {symbol} ({original_error}), "
                f"но открытой позиции уже нет - похоже, она закрылась сама (например, по другому стопу)."
            )
        position_amt = float(position["positionAmt"])
        close_side = _SHORT_SIDE if position_amt > 0 else _LONG_SIDE
        client.place_market_order(symbol, close_side, abs(position_amt))
        raise ExecutionError(
            f"Не удалось поставить {step} для {symbol} ({original_error}) - позиция аварийно "
            f"закрыта по рынку сразу после входа, без защиты не оставлена."
        )
    except FuturesApiError as e:
        raise ExecutionError(
            f"КРИТИЧНО: не удалось поставить {step} для {symbol} ({original_error}), "
            f"И аварийное закрытие тоже не удалось ({e}). ПОЗИЦИЯ ОТКРЫТА БЕЗ ЗАЩИТЫ - "
            f"нужно вмешательство вручную немедленно."
        ) from e


def open_protected_position(
    client: FuturesClient,
    symbol: str,
    side: str,
    stop_price: float,
    take_profit_price: float,
    risk_pct: float,
    leverage: int,
    margin_type: str = "ISOLATED",
    risk_limits: Optional[risk_guard.RiskLimits] = None,
) -> ProtectedPositionResult:
    """Полный цикл: предохранители риска портфеля -> плечо/маржа ->
    расчёт размера по риску -> вход по рынку -> стоп-лосс -> тейк-профит.
    Любая ошибка ДО входа просто прерывает выполнение (ExecutionError/
    FuturesApiError) - позиция не открывается. Любая ошибка ПОСЛЕ входа
    запускает аварийное закрытие (см. _emergency_close_or_raise) -
    позиция никогда не остаётся без защиты дольше, чем требуется на
    пару API-вызовов.

    side - "BUY" (лонг) или "SELL" (шорт). stop_price/take_profit_price -
    абсолютные цены (не проценты) - вызывающий код (см. пример в
    README/futures_signal_bridge.py) сам решает, откуда их брать: из
    сигнала сканера (entry/invalidation/target), вручную и т.п.

    risk_limits - см. модульный docstring про то, почему это опционально
    в сигнатуре, но обязательно на практике для любого реального
    вызывающего кода."""
    if side not in (_LONG_SIDE, _SHORT_SIDE):
        raise ValueError(f"side должен быть '{_LONG_SIDE}' или '{_SHORT_SIDE}', получено: {side}")

    if risk_limits is not None:
        blocked_reason = risk_guard.check_new_position_allowed(client, risk_limits, side=side, symbol=symbol)
        if blocked_reason is not None:
            raise ExecutionError(
                f"Открытие позиции {symbol} заблокировано предохранителями риска: {blocked_reason}"
            )

    client.set_leverage(symbol, leverage)
    client.set_margin_type(symbol, margin_type)

    filters = client.get_symbol_filters(symbol)
    entry_price = client.get_mark_price(symbol)
    balance = client.get_available_balance("USDT")

    _warn_if_liquidation_before_stop(side, entry_price, stop_price, leverage)

    raw_quantity = calc_position_size(balance, risk_pct, entry_price, stop_price)
    quantity = round_to_step(raw_quantity, filters["step_size"] or 0.0)
    if quantity <= 0:
        raise ExecutionError(
            f"Посчитанный размер позиции для {symbol} равен нулю (баланс {balance} USDT, "
            f"риск {risk_pct}%, дистанция до стопа {abs(entry_price - stop_price):.6g}) - "
            "либо баланс слишком мал, либо стоп слишком далеко от входа."
        )
    if filters.get("min_notional") and quantity * entry_price < filters["min_notional"]:
        raise ExecutionError(
            f"Позиция {symbol} размером {quantity} (~{quantity * entry_price:.2f} USDT) "
            f"меньше минимально допустимой биржей ({filters['min_notional']} USDT) - "
            "риск на сделку слишком мал относительно баланса/плеча."
        )

    tick_size = filters.get("tick_size") or 0.0
    stop_price = round_to_step(stop_price, tick_size) if tick_size else stop_price
    take_profit_price = round_to_step(take_profit_price, tick_size) if tick_size else take_profit_price

    entry_order = client.place_market_order(symbol, side, quantity)
    fill_price = _extract_fill_price(entry_order, fallback_price=entry_price)
    slippage_pct = _calc_slippage_pct(side, reference_price=entry_price, fill_price=fill_price)
    if abs(slippage_pct) >= _SLIPPAGE_WARN_THRESHOLD_PCT:
        logger.warning(
            "futures_executor: %s - проскальзывание на входе %.3f%% (ориентир %.6g -> факт %.6g)",
            symbol, slippage_pct, entry_price, fill_price,
        )
    reference_price = entry_price
    entry_price = fill_price
    close_side = _opposite_side(side)

    try:
        stop_order = client.place_stop_market(symbol, close_side, stop_price, close_position=True)
    except FuturesApiError as e:
        _emergency_close_or_raise(client, symbol, "стоп-лосс", e)
        raise  # для линтера/mypy - _emergency_close_or_raise всегда бросает исключение выше

    try:
        take_profit_order = client.place_take_profit_market(symbol, close_side, take_profit_price, close_position=True)
    except FuturesApiError as e:
        _emergency_close_or_raise(client, symbol, "тейк-профит", e)
        raise

    logger.info(
        "Открыта защищённая позиция %s %s %s: qty=%.8g, вход~%.6g (ориентир %.6g, "
        "проскальзывание %.3f%%), стоп=%.6g, тейк=%.6g, риск=%.2f%% баланса",
        symbol, side, "LONG" if side == _LONG_SIDE else "SHORT",
        quantity, entry_price, reference_price, slippage_pct, stop_price, take_profit_price, risk_pct,
    )

    return ProtectedPositionResult(
        symbol=symbol, side=side, quantity=quantity, entry_price=entry_price,
        stop_price=stop_price, take_profit_price=take_profit_price,
        risk_amount=balance * risk_pct / 100,
        entry_order=entry_order, stop_order=stop_order, take_profit_order=take_profit_order,
        reference_price=reference_price, slippage_pct=slippage_pct,
    )


def emergency_close_all(client: FuturesClient, symbol: str) -> Optional[dict]:
    """Ручной "красная кнопка" для одного символа - отменяет все
    открытые ордера (и обычные, и algo - см. futures_client.
    cancel_all_algo_orders про то, почему это два отдельных вызова) и
    закрывает позицию по рынку, если она есть. None, если открытой
    позиции не было."""
    client.cancel_all_open_orders(symbol)
    client.cancel_all_algo_orders(symbol)
    position = client.get_position(symbol)
    if position is None:
        return None
    position_amt = float(position["positionAmt"])
    close_side = _SHORT_SIDE if position_amt > 0 else _LONG_SIDE
    return client.place_market_order(symbol, close_side, abs(position_amt))