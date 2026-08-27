"""
index_health_monitor.py - отслеживает, не начала ли какая-то монета
Treasury Index систематически не резолвиться (делистинг, смена пары,
проблемы с API у конкретного тикера - когда не отвечает ни основной
тикер, ни fallback).

Разовый сбой - это нормально (сеть моргнула, временный глюк) - поэтому
здесь считается СЕРИЯ подряд идущих неудач, а не единичный случай.
Отсчёт идёт по вызовам treasury_index.compute_index(), который
запускается раз в config.TREASURY_INTERVAL_HOURS (по умолчанию 12ч) -
порог в MISS_STREAK_ALERT_THRESHOLD=3 подряд означает "монета не
резолвится больше суток" - разумный сигнал пересмотреть состав
корзины, а не шум одного неудачного запроса.
"""
import logging

import alerting
import queue_manager
import treasury_index

logger = logging.getLogger(__name__)

MISS_STREAK_ALERT_THRESHOLD = 3


def record_check_results(missing: list[str]) -> dict:
    """Обновляет счётчики подряд идущих неудач по каждой монете корзины
    на основе результата последнего compute_index() (result.missing).
    Возвращает обновлённые счётчики - используется и для алертинга
    (check_and_alert), и для отображения в check_state.py."""
    streaks = queue_manager.get_coin_miss_streaks()
    all_tickers = {c["ticker"] for coins in treasury_index.BASKET.values() for c in coins}
    missing_set = set(missing)

    for ticker in all_tickers:
        if ticker in missing_set:
            streaks[ticker] = streaks.get(ticker, 0) + 1
        else:
            streaks[ticker] = 0

    queue_manager.set_coin_miss_streaks(streaks)
    return streaks


def check_and_alert(streaks: dict) -> list[str]:
    """Если какая-то монета не резолвится MISS_STREAK_ALERT_THRESHOLD
    раз подряд - шлёт алерт владельцу (троттлится персонально по
    тикеру - раз в неделю максимум, проблема не решается за час).
    Возвращает список тикеров, которые сейчас считаются "нездоровыми"."""
    unhealthy = [ticker for ticker, streak in streaks.items() if streak >= MISS_STREAK_ALERT_THRESHOLD]

    for ticker in unhealthy:
        alerting.send_owner_alert(
            f"index_coin_unhealthy_{ticker}",
            f"Монета {ticker} из Treasury Index не резолвится (ни основной тикер, ни fallback) "
            f"уже {streaks[ticker]} проверок(и) подряд. Возможно, делистинг или ребрендинг - "
            f"стоит проверить и при необходимости обновить состав корзины (treasury_index.py, BASKET).",
            min_repeat_hours=24 * 7,
        )
        logger.warning("Монета %s из Treasury Index нездорова (%d проверок подряд без данных)", ticker, streaks[ticker])

    return unhealthy
