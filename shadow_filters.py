"""
shadow_filters.py - P3.9: "теневой" (shadow/A-B) прогон для новых
жёстких фильтров ДО того, как они реально начинают блокировать сигналы.

ПРОБЛЕМА: несколько кандидатов на новые фильтры (P2.4 - вето против
макро-тренда BTC, P1.3 - штраф за тонкие часы/выходные) потенциально
режут статистически валидные сигналы, если гипотеза окажется неверной.
Включить их сразу боевыми - значит месяцами терять хорошие сделки без
возможности узнать причину до того, как просядет общая статистика.

РЕШЕНИЕ: каждый такой фильтр регистрируется здесь как SHADOW-проверка -
вызывается на КАЖДЫЙ сигнал, который уже прошёл ВСЕ существующие боевые
фильтры (см. scanner._process_signal_candidate) и готов идти в очередь
публикации. Вердикт (заблокировал бы фильтр этот сигнал или нет, и
почему) ЛОГИРУЕТСЯ через queue_manager.add_shadow_verdict, но НИКАК не
влияет на реальную публикацию - сигнал идёт в очередь как обычно.

Через 1-2 недели get_shadow_stats сопоставляет накопленные вердикты с
РЕАЛЬНЫМ исходом сделки (см. outcome_tracker/queue_manager.
get_closed_outcomes) и сравнивает win-rate группы "фильтр бы
заблокировал" против "фильтр бы пропустил". Только после этого имеет
смысл переключать конкретный фильтр в боевой (блокирующий) режим -
это отдельное осознанное решение, эта инфраструктура его не принимает
автоматически.

Как добавить новый теневой фильтр: написать функцию с сигнатурой
(signal, symbol) -> tuple[bool, str] (заблокировал бы, причина) и
добавить её в SHADOW_FILTERS ниже.
"""
import logging
import time
from datetime import datetime, timezone

import config
import multi_timeframe
import queue_manager
import signal_parser

logger = logging.getLogger(__name__)


def _btc_macro_trend_shadow_check(signal, symbol: str) -> tuple[bool, str]:
    """P2.4 (в тени) - заблокировал бы, если сигнал идёт ПРОТИВ тренда
    BTC на 4ч ИЛИ 1д. Переиспользует multi_timeframe.classify_trend -
    ту же простую MA-эвристику, что уже используется для HTF-
    подтверждения самой монеты (см. multi_timeframe.py), только
    применённую к BTCUSDT - роадмап явно предлагал завязаться на этот
    же модуль, а не изобретать отдельный расчёт EMA200.

    Сигналы ПО САМОМУ BTC не сравниваются с собой - там понятие "против
    тренда BTC" не имеет смысла (BTC не может идти против самого себя)."""
    if symbol == "BTCUSDT":
        return False, "сигнал по самому BTC - макрофильтр не применяется"

    is_long = signal_parser.is_long_direction(signal.direction)
    against_trend = "down" if is_long else "up"

    against = []
    for tf in ("4h", "1d"):
        closes = multi_timeframe._fetch_closes("BTCUSDT", tf)
        trend = multi_timeframe.classify_trend(closes) if closes else None
        if trend == against_trend:
            against.append(tf)

    if against:
        return True, f"против тренда BTC на {'/'.join(against)}"
    return False, "не против тренда BTC на 4h/1d"


def _time_of_day_shadow_check(signal, symbol: str) -> tuple[bool, str]:
    """P1.3 (в тени) - заблокировал бы за тонкие часы (см.
    config.THIN_HOURS_UTC) или выходные (см.
    config.THIN_LIQUIDITY_WEEKEND_ENABLED). signal/symbol не
    используются - проверка зависит только от текущего момента, но
    сигнатура унифицирована с остальными теневыми проверками (см.
    evaluate_and_log)."""
    now = datetime.now(timezone.utc)
    reasons = []
    if now.hour in config.THIN_HOURS_UTC:
        reasons.append(f"тонкий час UTC ({now.hour}:00)")
    if config.THIN_LIQUIDITY_WEEKEND_ENABLED and now.weekday() >= 5:  # 5=суббота, 6=воскресенье
        reasons.append("выходной")

    if reasons:
        return True, ", ".join(reasons)
    return False, "обычное время торговли"


# Реестр теневых проверок - (имя_фильтра, функция). Имя используется
# как ключ группировки в get_shadow_stats и в самих залогированных
# записях (queue_manager.get_shadow_verdicts).
SHADOW_FILTERS = [
    ("btc_macro_trend", _btc_macro_trend_shadow_check),
    ("time_of_day_weekend", _time_of_day_shadow_check),
]


def evaluate_and_log(signal, symbol: str) -> None:
    """Прогоняет ВСЕ зарегистрированные теневые фильтры (SHADOW_FILTERS)
    на сигнале, который уже прошёл все РЕАЛЬНЫЕ фильтры. Вызывать из
    scanner._process_signal_candidate непосредственно ПЕРЕД
    queue_manager.push_pending_signal - см. docstring модуля целиком.

    Ошибка внутри ОДНОЙ теневой проверки не должна ронять ни публикацию
    сигнала, ни остальные теневые проверки - та же логика, что и у
    on_signal_accepted в scanner.py (одна плохая проверка не должна
    останавливать весь конвейер)."""
    for name, check_fn in SHADOW_FILTERS:
        try:
            would_block, reason = check_fn(signal, symbol)
        except Exception:
            logger.exception(
                "shadow_filters: теневая проверка '%s' упала на %s - "
                "пропускаю, публикация сигнала не затронута", name, symbol,
            )
            continue

        record = {
            "filter_name": name,
            "ticker": signal.ticker,
            "symbol": symbol,
            "direction": "long" if signal_parser.is_long_direction(signal.direction) else "short",
            "strategy": signal.strategy,
            "would_block": would_block,
            "reason": reason,
            "logged_at": time.time(),
        }
        queue_manager.add_shadow_verdict(record)
        if would_block:
            logger.info(
                "shadow_filters: '%s' заблокировал бы %s %s (%s) - %s",
                name, signal.ticker, signal.direction, signal.strategy, reason,
            )


def _summarize(items: list[dict]) -> dict:
    """Та же формула, что и outcome_tracker._summarize (win_rate только
    по решённым win/loss, avg_pnl_pct по всем) - продублирована, а не
    импортирована: это приватный helper другого модуля, а не общий
    контракт между ними."""
    n = len(items)
    if n == 0:
        return {"count": 0, "win_rate": None, "avg_pnl_pct": None}
    wins = sum(1 for c in items if c["result"] == "win")
    decided = sum(1 for c in items if c["result"] in ("win", "loss"))
    avg_pnl = sum(c["pnl_pct"] for c in items) / n
    return {
        "count": n,
        "win_rate": round(wins / decided * 100, 1) if decided else None,
        "avg_pnl_pct": round(avg_pnl, 3),
    }


def get_shadow_stats(filter_name: str, days: float | None = None,
                      max_correlation_seconds: float = 900) -> dict:
    """Сравнивает win-rate РЕАЛЬНЫХ исходов (см.
    queue_manager.get_closed_outcomes, наполняется outcome_tracker.py)
    для сигналов, которые теневой фильтр filter_name бы ЗАБЛОКИРОВАЛ
    ("blocked"), против тех, что бы ПРОПУСТИЛ ("allowed").

    Корреляция вердикта с исходом - по (ticker, direction, strategy) +
    ближайшее совпадение по времени в пределах max_correlation_seconds.
    Нужно, потому что вердикт логируется в scanner.py ДО публикации
    сигнала, а трекинг результата стартует ПОСЛЕ публикации в main.py -
    разница на секунды/минуты в рамках одного и того же тика, не
    больше. Если совпадений несколько - берём ближайшее по времени
    (тот же тикер+направление+стратегия редко повторяется дважды в
    пределах 15 минут). Вердикты без найденного исхода (сигнал ещё не
    закрылся или так и не был опубликован) в статистику не попадают -
    это ожидаемо, а не ошибка.

    days=None - за всё время вердиктов (по polю logged_at), иначе
    только за последние `days` дней."""
    verdicts = [v for v in queue_manager.get_shadow_verdicts() if v.get("filter_name") == filter_name]
    if days is not None:
        cutoff = time.time() - days * 24 * 3600
        verdicts = [v for v in verdicts if v.get("logged_at", 0) >= cutoff]

    closed_outcomes = queue_manager.get_closed_outcomes()

    def _find_match(verdict: dict) -> dict | None:
        candidates = [
            c for c in closed_outcomes
            if c.get("ticker") == verdict["ticker"]
            and c.get("direction") == verdict["direction"]
            and c.get("strategy") == verdict["strategy"]
            and abs(c.get("published_at", 0) - verdict["logged_at"]) <= max_correlation_seconds
        ]
        if not candidates:
            return None
        return min(candidates, key=lambda c: abs(c.get("published_at", 0) - verdict["logged_at"]))

    blocked_outcomes, allowed_outcomes = [], []
    for v in verdicts:
        match = _find_match(v)
        if match is None:
            continue
        (blocked_outcomes if v["would_block"] else allowed_outcomes).append(match)

    return {
        "blocked": _summarize(blocked_outcomes),
        "allowed": _summarize(allowed_outcomes),
        "verdicts_total": len(verdicts),
        "verdicts_matched": len(blocked_outcomes) + len(allowed_outcomes),
    }
