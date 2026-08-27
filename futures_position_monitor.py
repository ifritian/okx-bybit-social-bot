#!/usr/bin/env python3
"""
futures_position_monitor.py - следит за позициями, открытыми
futures_signal_bridge.execute_signal (см. queue_manager.
get_open_futures_positions), и уведомляет владельца в Telegram, когда
позиция закрывается - по стопу, по тейку, или как-то иначе.

Зачем это отдельный шаг, а не часть futures_auto_trade.py: открытие
новых позиций и слежение за уже открытыми - разные по риску операции с
разными последствиями отказа. У этой ещё есть побочная обязанность -
чистить "осиротевший" условный ордер: Binance Futures НЕ отменяет пару
STOP_MARKET/TAKE_PROFIT_MARKET автоматически (это не OCO), так что
после срабатывания одного из них второй остаётся висеть сам по себе.
Без чистки: (а) позиция по этому символу не сможет переоткрыться,
пока get_position видит что-то по нему связанное, и (б) оставшийся
ордер рано или поздно исполнится сам по себе, если цена туда вернётся,
уже без всякой связи с исходным сигналом.

Как определяется причина закрытия:
1. Позиции больше нет на бирже (client.get_position -> None или
   positionAmt == 0) - значит закрылась (по стопу, тейку, ликвидации
   или вручную с сайта/CLI).
2. Смотрим, какой из двух условных ордеров (stop_order_id /
   take_profit_order_id, сохранённых futures_signal_bridge при входе)
   всё ещё висит в open orders по символу - тот, что НЕ висит,
   сработал и закрыл позицию. Если висит один из двух - отменяем
   оставшийся (см. выше). Если ни один не висит - позиция закрыта
   как-то иначе (вручную, ликвидация) - помечаем "неизвестно".
3. Реальный PnL берём из client.get_income_history (incomeType=
   REALIZED_PNL) за период с момента открытия - это фактическое число
   с биржи (уже с учётом комиссий и проскальзывания), а не оценка по
   цене входа/выхода.

ВТОРАЯ обязанность этого модуля (см. _manage_partial_profit) - частичный
профит и перевод в безубыток на ЕЩЁ ОТКРЫТЫХ позициях, ДО того, как они
закроются: когда цена проходит config.BINANCE_FUTURES_PARTIAL_TP_TRIGGER_FRACTION
пути от входа до тейка сигнала, часть позиции (config.
BINANCE_FUTURES_PARTIAL_TP_CLOSE_FRACTION) закрывается по рынку, старые
стоп/тейк снимаются, на остаток ставится стоп в безубыток и трейлинг-стоп
(вместо исходного фиксированного тейка) - см. docstring
_manage_partial_profit ниже про то, почему именно так, а не просто
"подвинуть тейк". Срабатывает максимум ОДИН раз на позицию (record
"partial_tp_done"), проверяется на каждом прогоне ДО проверки "закрылась
ли позиция" - иначе позиция, закрывшаяся ровно в этот же тик, могла бы
пропустить частичный профит и уйти сразу в обработку закрытия.

Использование (те же переменные окружения, что и futures_auto_trade.py):
    export BINANCE_FUTURES_API_KEY=...
    export BINANCE_FUTURES_API_SECRET=...
    python3 futures_position_monitor.py
"""
import logging
import os
import sys
import time
from typing import Optional

import alerting
import config
import futures_executor
from futures_client import FuturesApiError, FuturesClient, TESTNET_BASE_URL
import queue_manager

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("futures_position_monitor")


def _realized_pnl_since(client, symbol: str, since_ts: float) -> float:
    """Сумма REALIZED_PNL по символу с момента since_ts (unix-секунды).
    get_income_history отдаёт время в мс - тот же формат, что уже
    использует risk_guard._consecutive_losses."""
    since_ms = int(since_ts * 1000)
    rows = client.get_income_history(income_type="REALIZED_PNL", limit=200)
    return sum(
        float(r.get("income", 0)) for r in rows
        if r.get("symbol") == symbol and int(r.get("time", 0)) >= since_ms
    )


def _determine_close_reason_and_cleanup(client, record: dict, symbol: str) -> str:
    """Возвращает человекочитаемую причину закрытия и попутно отменяет
    "осиротевший" условный ордер, если он остался висеть (см. docstring
    модуля).

    Если по позиции уже сработал частичный профит (record
    "partial_tp_done" - см. _manage_partial_profit), то stop_order_id/
    take_profit_order_id по факту держат уже не исходные стоп/тейк, а
    стоп в безубытке и трейлинг-стоп соответственно - подписи причины
    закрытия отражают это, а не молча называют трейлинг-стоп "тейк-
    профитом"."""
    try:
        open_orders = client.get_open_orders(symbol)
    except FuturesApiError as e:
        logger.warning(
            "futures_position_monitor: не удалось получить open orders для %s: %s - причина закрытия неизвестна",
            symbol, e,
        )
        return "неизвестно (ошибка API при проверке ордеров)"

    open_order_ids = {o.get("orderId") for o in open_orders}
    stop_still_open = record.get("stop_order_id") in open_order_ids
    tp_still_open = record.get("take_profit_order_id") in open_order_ids
    after_partial = bool(record.get("partial_tp_done"))

    if stop_still_open and not tp_still_open:
        reason = "трейлинг-стоп остатка позиции (после частичного профита)" if after_partial else "тейк-профит (TP)"
    elif tp_still_open and not stop_still_open:
        reason = "стоп-лосс в безубытке (после частичного профита)" if after_partial else "стоп-лосс (SL)"
    elif not stop_still_open and not tp_still_open:
        reason = "неизвестно (оба условных ордера уже неактивны - возможно, закрыта вручную)"
    else:
        reason = "неизвестно (оба условных ордера всё ещё формально активны)"

    if stop_still_open or tp_still_open:
        try:
            client.cancel_all_open_orders(symbol)
            client.cancel_all_algo_orders(symbol)
            logger.info("futures_position_monitor: отменён оставшийся условный ордер по %s", symbol)
        except FuturesApiError as e:
            logger.warning("futures_position_monitor: не удалось отменить оставшийся ордер по %s: %s", symbol, e)

    return reason


def _target_progress_fraction(entry: float, target: float, mark_price: float, side: str) -> float:
    """Доля пройденного пути от входа к тейку сигнала, где 0.0 - ещё в
    точке входа, 1.0 - ровно на тейке (может быть и больше 1, если цена
    уже прошла дальше тейка, и меньше 0, если цена ушла в сторону стопа) -
    считается по направлению сделки, а не как "цена выросла", т.к. для
    шорта тейк ниже входа. Возвращает 0.0, если entry/target совпадают
    (нет пути, чтобы посчитать прогресс) - защита от деления на ноль на
    испорченной/старой записи."""
    total_distance = (target - entry) if side == "BUY" else (entry - target)
    if total_distance <= 0:
        return 0.0
    progressed = (mark_price - entry) if side == "BUY" else (entry - mark_price)
    return progressed / total_distance


def _manage_partial_profit(client, record: dict, mark_price: float) -> dict:
    """Проверяет, прошла ли цена config.BINANCE_FUTURES_PARTIAL_TP_TRIGGER_FRACTION
    пути от входа до тейка сигнала - если да, и это ещё не делалось для
    этой позиции (record["partial_tp_done"]), закрывает config.
    BINANCE_FUTURES_PARTIAL_TP_CLOSE_FRACTION позиции по рынку, снимает
    старые стоп/тейк и ставит на остаток стоп в безубыток + трейлинг-стоп
    (см. модульный докстринг). Возвращает ОБНОВЛЁННУЮ запись - вызывающий
    код (check_open_positions) обязан сохранить её обратно в трекинг
    вместо исходной.

    Срабатывает не больше одного раза за жизнь позиции - вторая частичная
    фиксация на и без того урезанном остатке не входила в план (A1) и
    только усложнила бы код без явной пользы на масштабе этого бота.

    НИКОГДА не бросает исключение - ошибка API здесь (сеть, отклонённый
    ордер и т.п.) логируется и позиция остаётся с исходным стопом/тейком
    до следующего прогона: это не хуже, чем было раньше (просто "ещё не
    улучшено"), а не новый источник риска."""
    if not config.BINANCE_FUTURES_PARTIAL_TP_ENABLED or record.get("partial_tp_done"):
        return record

    entry = record.get("entry_price", 0) or 0
    target = record.get("take_profit_price", 0) or 0
    side = record.get("side", "")
    quantity = record.get("quantity", 0) or 0
    symbol = record.get("symbol", "")
    if entry <= 0 or target <= 0 or quantity <= 0 or side not in ("BUY", "SELL"):
        return record

    progress = _target_progress_fraction(entry, target, mark_price, side)
    if progress < config.BINANCE_FUTURES_PARTIAL_TP_TRIGGER_FRACTION:
        return record

    close_side = "SELL" if side == "BUY" else "BUY"

    try:
        filters = client.get_symbol_filters(symbol)
        step = filters.get("step_size") or 0.0
        raw_close_qty = quantity * config.BINANCE_FUTURES_PARTIAL_TP_CLOSE_FRACTION
        close_qty = futures_executor.round_to_step(raw_close_qty, step) if step else raw_close_qty
        remaining_qty = futures_executor.round_to_step(quantity - close_qty, step) if step else quantity - close_qty
        if close_qty <= 0 or remaining_qty <= 0:
            logger.info(
                "futures_position_monitor: %s - частичное закрытие дало бы нулевую долю/остаток "
                "(qty=%.8g, step=%s) - пропускаю, оставляю исходные стоп/тейк",
                symbol, quantity, step,
            )
            return record

        client.place_reduce_only_market_order(symbol, close_side, close_qty)

        for order_id_key in ("stop_order_id", "take_profit_order_id"):
            order_id = record.get(order_id_key)
            if order_id is None:
                continue
            try:
                client.cancel_order(symbol, order_id)
            except FuturesApiError as e:
                logger.warning(
                    "futures_position_monitor: не удалось отменить старый %s (%s) для %s: %s - "
                    "новый ордер всё равно ставлю, но старый может остаться висеть до следующей чистки",
                    order_id_key, order_id, symbol, e,
                )

        breakeven_stop = client.place_stop_market(symbol, close_side, entry, close_position=True)
        trailing_stop = client.place_trailing_stop_market(
            symbol, close_side, config.BINANCE_FUTURES_TRAILING_CALLBACK_PCT,
            close_position=True, activation_price=mark_price,
        )
    except FuturesApiError as e:
        logger.warning(
            "futures_position_monitor: не удалось выполнить частичный профит по %s: %s - "
            "оставляю исходные стоп/тейк, попробую снова на следующем прогоне",
            symbol, e,
        )
        return record

    partial_pnl = (mark_price - entry) * close_qty if side == "BUY" else (entry - mark_price) * close_qty

    message = (
        f"\U0001F3AF Частичный профит: {record.get('ticker', symbol)} {record.get('direction', '')}\n"
        f"Закрыто ~{config.BINANCE_FUTURES_PARTIAL_TP_CLOSE_FRACTION * 100:.0f}% позиции по рынку "
        f"(~{partial_pnl:+.4f} USDT)\n"
        f"Стоп на остаток переведён в безубыток ({entry:.6g}), дальше остаток ({remaining_qty:.8g}) "
        f"ведётся трейлинг-стопом (callback {config.BINANCE_FUTURES_TRAILING_CALLBACK_PCT:.2f}%) "
        "вместо исходного тейка"
    )
    alerting.send_owner_alert(
        f"futures_partial_tp:{symbol}:{record.get('opened_at', 0)}",
        message,
        min_repeat_hours=0,  # ключ уникален на конкретную позицию - троттлить тут нечего
    )
    logger.info(
        "futures_position_monitor: %s - частичный профит выполнен (%.8g закрыто, %.8g в трейлинге)",
        symbol, close_qty, remaining_qty,
    )

    updated = dict(record)
    updated["quantity"] = remaining_qty
    updated["stop_order_id"] = breakeven_stop.get("orderId")
    updated["take_profit_order_id"] = trailing_stop.get("orderId")
    updated["partial_tp_done"] = True
    updated["partial_tp_realized_pnl"] = partial_pnl
    return updated


def _finalize_closed_position(record: dict, symbol: str, reason: str, pnl: float) -> dict:
    """Строит запись закрытой позиции, шлёт уведомление владельцу и
    логирует - общая часть для двух путей закрытия: (1) позиция уже
    закрылась НА БИРЖЕ сама (по стопу/тейку/вручную - см.
    _determine_close_reason_and_cleanup) и (2) позицию принудительно
    закрыл САМ БОТ по таймауту (см. _close_timed_out_position), пока она
    формально ещё оставалась открытой. Раньше это было частью
    check_open_positions только для случая (1); вынесено в отдельную
    функцию, чтобы не дублировать форматирование сообщения и подсчёт
    pnl_pct для случая (2)."""
    closed_record = dict(record, closed_at=time.time(), close_reason=reason, realized_pnl=pnl)

    entry = record.get("entry_price", 0) or 0
    # original_quantity (весь риск сделки на момент входа) - не
    # "quantity" (может быть уже уменьшено частичным профитом, см.
    # _manage_partial_profit) - иначе % PnL считался бы только от
    # ОСТАВШЕЙСЯ части позиции и выглядел бы задранным relative к
    # исходному риску. Старые записи без этого поля (до A1) просто
    # используют quantity - никакого поведенческого изменения для них.
    original_quantity = record.get("original_quantity") or record.get("quantity", 0) or 0
    notional = entry * original_quantity
    pnl_pct = (pnl / notional * 100) if notional else 0.0
    emoji = "\U0001F7E2" if pnl > 0 else ("\U0001F534" if pnl < 0 else "\u26AA")

    partial_note = ""
    if record.get("partial_tp_done"):
        partial_note = (
            f" (включает ~{record.get('partial_tp_realized_pnl', 0):+.4f} USDT, "
            "зафиксированные ранее частичным профитом)"
        )

    message = (
        f"{emoji} Позиция закрыта: {record.get('ticker', symbol)} {record.get('direction', '')}\n"
        f"Причина: {reason}\n"
        f"Вход: {entry:.6g}  Кол-во (исходное): {original_quantity:.8g}\n"
        f"Реализованный PnL: {pnl:+.4f} USDT ({pnl_pct:+.2f}% от исходного размера позиции){partial_note}\n"
        f"Стратегия: {record.get('strategy', '?')} (score {record.get('score', '?')})"
    )
    alerting.send_owner_alert(
        f"futures_position_closed:{symbol}:{record.get('opened_at', 0)}",
        message,
        min_repeat_hours=0,  # ключ уникален на конкретную позицию - троттлить тут нечего
    )
    logger.info("futures_position_monitor: %s закрыта (%s), PnL %.4f USDT", symbol, reason, pnl)
    return closed_record


def _close_timed_out_position(client, record: dict, symbol: str, max_age_hours: float) -> Optional[dict]:
    """Позиция открыта дольше config.BINANCE_FUTURES_MAX_POSITION_AGE_HOURS -
    закрываем её ПРИНУДИТЕЛЬНО по рынку, не дожидаясь стопа/тейка (которые
    могут вообще не сработать, если рынок ушёл в затяжной боковик далеко
    от обоих уровней - изначальный сетап сигнала к этому моменту уже не
    имеет отношения к текущей цене). Используем futures_executor.
    emergency_close_all - тот же штатный путь "отменить оба условных
    ордера, закрыть по рынку", что и ручная команда владельца, а НЕ
    _emergency_close_or_raise (тот - только для error path сразу после
    открытия позиции, см. futures_executor docstring, и бросает
    исключение наружу вместо возврата результата).

    Возвращает готовую closed_record при успехе, None при сбое API -
    в этом случае позиция остаётся в трекинге до следующего прогона
    (см. вызывающий код), как и при любой другой сетевой ошибке в этом
    модуле."""
    logger.warning(
        "futures_position_monitor: %s открыта дольше %.0fч (лимит %.0fч) - закрываю принудительно по рынку",
        symbol, (time.time() - record.get("opened_at", 0)) / 3600, max_age_hours,
    )
    try:
        futures_executor.emergency_close_all(client, symbol)
    except FuturesApiError as e:
        logger.error(
            "futures_position_monitor: не удалось закрыть по таймауту зависшую позицию %s: %s - "
            "оставляю в трекинге, попробую на следующем прогоне", symbol, e,
        )
        return None

    pnl = _realized_pnl_since(client, symbol, record.get("opened_at", 0))
    reason = f"таймаут (позиция была открыта дольше {max_age_hours:.0f}ч)"
    return _finalize_closed_position(record, symbol, reason, pnl)


def check_open_positions(client) -> dict:
    """Проходит по всем отслеживаемым позициям (queue_manager.
    get_open_futures_positions), для каждой проверяет, закрылась ли она
    на бирже, и если да - шлёт уведомление владельцу в Telegram и
    переносит запись в closed_futures_positions. Возвращает сводку
    {"still_open": N, "closed": N}. Сбой на ОДНОЙ позиции (сетевая
    ошибка и т.п.) не прерывает проверку остальных - позиция просто
    остаётся в трекинге до следующего запуска."""
    tracked = queue_manager.get_open_futures_positions()
    if not tracked:
        return {"still_open": 0, "closed": 0}

    still_open = []
    newly_closed = []

    for record in tracked:
        symbol = record.get("symbol", "")
        try:
            position = client.get_position(symbol)
        except FuturesApiError as e:
            logger.warning(
                "futures_position_monitor: не удалось проверить позицию %s: %s - оставляю в трекинге до следующего раза",
                symbol, e,
            )
            still_open.append(record)
            continue

        position_amt = float(position["positionAmt"]) if position else 0.0
        if position_amt != 0:
            mark_price = float(position.get("markPrice", 0) or 0)
            if mark_price > 0:
                record = _manage_partial_profit(client, record, mark_price)

            age_hours = (time.time() - record.get("opened_at", 0)) / 3600
            if age_hours >= config.BINANCE_FUTURES_MAX_POSITION_AGE_HOURS:
                closed_record = _close_timed_out_position(
                    client, record, symbol, config.BINANCE_FUTURES_MAX_POSITION_AGE_HOURS,
                )
                if closed_record is not None:
                    newly_closed.append(closed_record)
                    continue
                # emergency_close_all не удался (сбой API) - остаётся в
                # трекинге, попробуем снова на следующем прогоне (см.
                # _close_timed_out_position docstring).

            still_open.append(record)
            continue

        reason = _determine_close_reason_and_cleanup(client, record, symbol)
        pnl = _realized_pnl_since(client, symbol, record.get("opened_at", 0))

        # Cooldown по символу (см. config.FUTURES_SYMBOL_COOLDOWN_HOURS) -
        # только для НАСТОЯЩЕГО стоп-лосса, не для стопа в безубытке после
        # частичного профита: там цена уже успела пройти в нашу пользу и
        # статистического сигнала "монета пилит у этого уровня" нет в той
        # же мере - см. роадмап фазы 2, пункт P1.1.
        if reason == "стоп-лосс (SL)":
            queue_manager.mark_stopped_out(symbol)

        newly_closed.append(_finalize_closed_position(record, symbol, reason, pnl))

    queue_manager.replace_open_futures_positions(still_open)
    if newly_closed:
        queue_manager.append_closed_futures_positions(newly_closed)

    return {"still_open": len(still_open), "closed": len(newly_closed)}


def main() -> int:
    api_key = os.environ.get("BINANCE_FUTURES_API_KEY", "")
    api_secret = os.environ.get("BINANCE_FUTURES_API_SECRET", "")
    if not api_key or not api_secret:
        logger.error(
            "Не заданы BINANCE_FUTURES_API_KEY/BINANCE_FUTURES_API_SECRET (testnet-ключи, "
            "см. https://testnet.binancefuture.com) - выставь через export, не хардкодь в файл."
        )
        return 1

    client = FuturesClient(api_key=api_key, api_secret=api_secret, base_url=TESTNET_BASE_URL)
    summary = check_open_positions(client)
    logger.info(
        "Готово: %d позиций всё ещё открыто, %d закрыто и обработано в этом прогоне",
        summary["still_open"], summary["closed"],
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
