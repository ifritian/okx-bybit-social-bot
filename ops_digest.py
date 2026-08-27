#!/usr/bin/env python3
"""
ops_digest.py - ежедневный отчёт владельцу в личку Telegram.

ПРОБЛЕМА, которую закрывает этот модуль: сейчас, чтобы понять, всё ли в
порядке с ботом, нужно руками лезть в GitHub Actions и читать вывод
check_state.py по частям (мы это делали вручную весь этот разговор).
Этот модуль раз в сутки, под конец дня по UTC (см. config.
OPS_DIGEST_HOUR_UTC), сам присылает в личку короткую сводку: когда
последний раз публиковалось, что со статистикой сигналов (симуляция по
цене) и реальными сделками testnet за последние 24ч и за всё время,
взведён ли kill switch, сколько открыто позиций.

Переиспользует уже существующую инфраструктуру, не добавляет новую:
- _is_end_of_day_window (см. config.OPS_DIGEST_HOUR_UTC) - гейт по часу
  UTC, чтобы отправка была привязана к концу дня, а не плавала по
  времени суток. alerting.send_owner_alert - та же функция, что шлёт
  алерты о сбоях (см. её docstring про троттлинг) - дайджест использует
  тот же механизм с alert_key="daily_ops_digest" и порогом
  config.OPS_DIGEST_MIN_REPEAT_HOURS как подстраховку от дубля внутри
  окна конца дня (см. её докстринг выше), поэтому не нужен отдельный
  APScheduler/cron - этот скрипт можно (и нужно) запускать на каждом
  обычном тике бота (как futures_position_monitor.py): вне окна конца
  дня main() выходит сразу же, почти бесплатно.
- outcome_tracker.get_accuracy_stats/get_futures_trade_stats - те же
  функции, что видно в check_state.py, просто с days=1 и days=None.
- risk_guard.status - тот же снимок, что risk_guard_cli.py status.
  ТРЕБУЕТ живого клиента (запрос баланса/позиций на биржу) - если
  BINANCE_FUTURES_API_KEY/SECRET не заданы в окружении этого шага (или
  запрос к бирже не удался), эта часть дайджеста помечается как
  недоступная, а НЕ роняет отправку всего остального - лучше неполный
  дайджест, чем никакого (та же философия degrade gracefully, что и в
  fetch_htf_snapshot/is_actively_trading).

Использование (те же переменные окружения, что и futures_auto_trade.py -
опционально, без них риск-секция просто будет неполной):
    export BINANCE_FUTURES_API_KEY=...
    export BINANCE_FUTURES_API_SECRET=...
    python3 ops_digest.py
"""
import logging
import os
import sys
from datetime import datetime, timezone

import alerting
import config
import outcome_tracker
import queue_manager
import risk_guard

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("ops_digest")


def _fmt_win_rate(stats: dict) -> str:
    wr = stats.get("win_rate")
    return f"{wr}%" if wr is not None else "н/д"


def _publishing_section() -> str:
    hours_since = queue_manager.seconds_since_last_post("currency") / 3600
    if hours_since == float("inf"):
        return "Публикации: ещё ни одной не было"
    flag = " ⚠️" if hours_since >= config.DEAD_MANS_SWITCH_HOURS else ""
    return (f"С последней публикации (валюта): {hours_since:.1f}ч "
            f"(порог тревоги {config.DEAD_MANS_SWITCH_HOURS}ч){flag}")


def _signal_stats_section() -> str:
    day = outcome_tracker.get_accuracy_stats(days=1)["overall"]
    total = outcome_tracker.get_accuracy_stats(days=None)["overall"]
    return (f"Сигналы (симуляция по цене) за 24ч: n={day['count']}, win-rate={_fmt_win_rate(day)}\n"
            f"  за всё время: n={total['count']}, win-rate={_fmt_win_rate(total)}")


def _futures_stats_section() -> str:
    day = outcome_tracker.get_futures_trade_stats(days=1)["overall"]
    total = outcome_tracker.get_futures_trade_stats(days=None)["overall"]
    open_positions = queue_manager.get_open_futures_positions()
    return (f"Реальные сделки testnet за 24ч: n={day['count']}, win-rate={_fmt_win_rate(day)}\n"
            f"  за всё время: n={total['count']}, win-rate={_fmt_win_rate(total)}, "
            f"суммарный PnL={total.get('total_pnl_usdt', 0.0):+.4f} USDT\n"
            f"  сейчас открыто: {len(open_positions)}")


def _risk_section() -> str:
    api_key = os.environ.get("BINANCE_FUTURES_API_KEY", "")
    api_secret = os.environ.get("BINANCE_FUTURES_API_SECRET", "")
    if not (api_key and api_secret):
        return "Риск-статус: недоступен (нет ключей testnet в этом шаге)"

    try:
        from futures_client import FuturesClient, TESTNET_BASE_URL
        client = FuturesClient(api_key=api_key, api_secret=api_secret, base_url=TESTNET_BASE_URL)
        limits = risk_guard.limits_from_config(config)
        st = risk_guard.status(client, limits)
    except Exception as e:
        logger.warning("ops_digest: не удалось получить risk_guard.status: %s", e)
        return "Риск-статус: ошибка при запросе к бирже (см. лог этого шага в Actions)"

    kill_line = "🔴 KILL SWITCH ВЗВЕДЁН - новые позиции не открываются" if st["kill_switch"] else "🟢 kill switch не взведён"
    symbols = f" ({', '.join(st['open_positions_symbols'])})" if st["open_positions_symbols"] else ""
    return (
        f"{kill_line}\n"
        f"Открытых позиций: {st['open_positions']}/{st['max_open_positions']}{symbols}\n"
        f"Дневной убыток: {st['daily_loss_pct']:+.2f}% (лимит {st['max_daily_loss_pct']}%)\n"
        f"Убытков подряд: {st['consecutive_losses']}/{st['max_consecutive_losses']}"
    )


def _is_end_of_day_window() -> bool:
    """True, если сейчас "конец дня" по UTC (см. config.OPS_DIGEST_HOUR_UTC) -
    т.е. текущий час >= порога. НЕ "час ровно == порогу", потому что
    GitHub Actions не гарантирует cron минута-в-минуту (см. .github/
    workflows/bot.yml) - строгое равенство рискует пропустить окно
    целиком, если конкретно этот тик задержался и попал уже на
    следующий час."""
    return datetime.now(timezone.utc).hour >= config.OPS_DIGEST_HOUR_UTC


def build_digest() -> str:
    return (
        f"{_publishing_section()}\n\n"
        f"{_signal_stats_section()}\n\n"
        f"{_futures_stats_section()}\n\n"
        f"{_risk_section()}"
    )


def main() -> int:
    if not _is_end_of_day_window():
        logger.debug(
            "ops_digest: сейчас не конец дня по UTC (порог час %d) - пропускаю без попытки отправки",
            config.OPS_DIGEST_HOUR_UTC,
        )
        return 0

    message = build_digest()
    sent = alerting.send_owner_alert(
        "daily_ops_digest", message, min_repeat_hours=config.OPS_DIGEST_MIN_REPEAT_HOURS,
        prefix="\U0001F4CA Ежедневный отчёт бота",
    )
    logger.info("ops_digest: %s\n%s", "отправлен" if sent else "пропущен (троттлинг или алертинг не настроен)", message)
    return 0


if __name__ == "__main__":
    sys.exit(main())
