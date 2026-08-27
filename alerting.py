"""
alerting.py - best-effort уведомления владельцу в личку Telegram.

Раньше единственный способ узнать о сбое бота - зайти в GitHub Actions
и вручную прочитать лог конкретного запуска. Этот модуль шлёт короткое
сообщение в личку config.YOUR_USER_ID (тот же бот, что слушает личные
сообщения в telegram_listener.py) при:
- необработанном исключении в основном цикле (main.tick),
- "мёртвом" боте - currency-формат не публиковался дольше
  config.DEAD_MANS_SWITCH_HOURS часов (main._check_dead_mans_switch).

ВАЖНО: send_owner_alert() НИКОГДА не бросает исключение наружу - сбой
самого алертинга (например, сетевая ошибка Telegram) не должен ронять
или прерывать остальную часть тика бота. В худшем случае алерт просто
не дойдёт - это заметно хуже, чем упавший тик из-за алертинга.

Троттлинг: один и тот же alert_key не отправляется повторно чаще, чем
раз в min_repeat_hours (по умолчанию 6ч) - иначе повторяющийся сбой
(например, каждый тик валится с одной и той же ошибкой) заваливал бы
личку идентичными сообщениями каждые 10 минут.
"""
import logging
import time

import requests

import config
import queue_manager

logger = logging.getLogger(__name__)

_DEFAULT_MIN_REPEAT_HOURS = 6


def is_configured() -> bool:
    """True, если есть и токен бота, и YOUR_USER_ID - без обоих слать некому."""
    return bool(config.TELEGRAM_BOT_TOKEN and config.YOUR_USER_ID)


def send_owner_alert(alert_key: str, message: str, min_repeat_hours: float = _DEFAULT_MIN_REPEAT_HOURS,
                      prefix: str = "\u26a0\ufe0f Bot alert") -> bool:
    """Отправляет алерт владельцу, если он не настроен - тихо пропускает
    (это не ошибка, YOUR_USER_ID опционален). Возвращает True, если
    сообщение реально ушло (не была троттлинга и не было сбоя API).

    prefix - по умолчанию тот же "⚠️ Bot alert", что и раньше (не ломает
    существующих вызывающих) - но для НЕ-тревожных сообщений (например,
    ops_digest.py шлёт рутинный ежедневный отчёт, а не сигнал о сбое)
    можно передать свой, чтобы не создавать ложное ощущение тревоги
    каждый день на пустом месте."""
    if not is_configured():
        logger.debug("Алертинг владельцу не настроен (нет YOUR_USER_ID/TELEGRAM_BOT_TOKEN) - пропускаю: %s", message)
        return False

    last_sent = queue_manager.get_last_alert_sent(alert_key)
    if last_sent and (time.time() - last_sent) < min_repeat_hours * 3600:
        logger.debug("Алерт '%s' недавно уже отправлялся - пропускаю повтор", alert_key)
        return False

    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage",
            data={"chat_id": config.YOUR_USER_ID, "text": f"{prefix}\n\n{message}"},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        if not data.get("ok"):
            logger.warning("Telegram отклонил алерт владельцу: %s", data)
            return False
    except requests.RequestException as e:
        logger.warning("Сетевая ошибка при отправке алерта владельцу: %s", e)
        return False
    except Exception:
        logger.exception("Неожиданная ошибка при отправке алерта владельцу")
        return False

    queue_manager.set_last_alert_sent(alert_key)
    logger.info("Алерт владельцу отправлен (key=%s)", alert_key)
    return True
