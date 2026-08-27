"""
main.py - раннер для генерации и доставки черновиков OKX Orbit / Bybit
ByX. Та же схема, что у binance-square-bot: один тик за раз, каждый
формат сам решает, открыто ли его окно публикации (см. state_store.py).

Режимы запуска:
- `python main.py --once` - один тик и выход (используется GitHub
  Actions - см. .github/workflows/bot.yml).
- `python main.py` - непрерывный локальный запуск через
  BlockingScheduler (для разработки/отладки вне Actions).
"""
import logging
import sys

from apscheduler.schedulers.blocking import BlockingScheduler

import config
import groq_client
import okx_draft_publisher
import okx_orbit_generator
import state_store

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[logging.StreamHandler(), logging.FileHandler(config.LOG_PATH, encoding="utf-8")],
)
logger = logging.getLogger(__name__)


def try_publish_okx_orbit_draft() -> None:
    """Готовит черновик поста для OKX Orbit и присылает его владельцу в
    Telegram (см. okx_draft_publisher.py) - НЕ публикует ничего сам, у
    OKX Orbit нет API для этого (см. README.md). Выключено по умолчанию
    (config.OKX_ORBIT_ENABLED) и требует config.OKX_ORBIT_DRAFT_CHAT_ID.
    """
    if not config.OKX_ORBIT_ENABLED:
        return
    if not okx_draft_publisher.is_configured():
        logger.warning("OKX_ORBIT_ENABLED=true, но OKX_ORBIT_DRAFT_CHAT_ID не задан - пропускаю формат")
        return

    post_type = "okx_orbit"
    seconds_elapsed = state_store.seconds_since_last_post(post_type)
    min_seconds = config.OKX_ORBIT_INTERVAL_HOURS * 3600 + state_store.get_jitter_seconds(post_type)

    if seconds_elapsed < min_seconds:
        return
    if not state_store.should_retry_now(post_type):
        return  # недавно был сбой - ждём отступ, не долбим API на каждом тике

    logger.info("Окно черновика (OKX Orbit) открыто - генерирую пост")

    theme = okx_orbit_generator.pick_theme(state_store.get_last_theme(post_type))
    format_type = okx_orbit_generator.pick_format(state_store.get_last_format(post_type))

    try:
        result = okx_orbit_generator.generate_okx_orbit_post(theme, format_type)
    except groq_client.GroqRateLimited as e:
        backoff_hours = max(e.retry_after_seconds / 3600, 5 / 60)
        logger.warning("Groq rate limit на черновике OKX Orbit - жду %.1fч перед следующей попыткой", backoff_hours)
        state_store.set_retry_backoff(post_type, backoff_hours)
        return
    except Exception as e:
        logger.error("Ошибка генерации черновика OKX Orbit: %s", e)
        state_store.set_retry_backoff(post_type, 1)
        return

    if result is None:
        logger.warning("Не удалось получить данные для темы %s (OKX Orbit) - пропускаю до следующего окна", theme)
        state_store.set_retry_backoff(post_type, 1)
        return

    post_text, allowed_numbers, _headline_pct, format_type = result
    ok, reason = okx_orbit_generator.validate_okx_orbit_post_text(post_text, allowed_numbers)
    if not ok:
        logger.error("Черновик OKX Orbit не прошёл проверку, доставка отменена: %s", reason)
        state_store.set_retry_backoff(post_type, 1)
        return

    chart_path = None
    try:
        chart_path = okx_orbit_generator.generate_chart_for_post(theme)
    except Exception as e:
        logger.warning("Не удалось сгенерировать график для черновика OKX Orbit: %s - шлю без картинки", e)

    try:
        delivered = okx_draft_publisher.send_draft(post_text, format_type, chart_path)
    except okx_draft_publisher.DraftDeliveryError as e:
        logger.error("Ошибка доставки черновика OKX Orbit в Telegram: %s", e)
        state_store.set_retry_backoff(post_type, 1)
        return

    state_store.set_last_theme(post_type, theme)
    state_store.set_last_format(post_type, format_type)

    logger.info("Черновик OKX Orbit доставлен: %s", delivered)
    state_store.set_last_post_time(post_type)
    state_store.roll_new_jitter(post_type, config.OKX_ORBIT_JITTER_HOURS * 3600)


# TODO: try_publish_bybit_byx_draft() - строится по образцу
# try_publish_okx_orbit_draft() выше, как только будет готов
# bybit_byx_generator.py / bybit_draft_publisher.py.


def tick() -> None:
    try:
        try_publish_okx_orbit_draft()
        # try_publish_bybit_byx_draft()  # раскомментировать, когда будет готов
    except Exception:
        logger.exception("Неожиданная ошибка в основном цикле")


def main() -> None:
    once = "--once" in sys.argv

    logger.info(
        "Бот запущен. OKX Orbit: enabled=%s, interval=%sч",
        config.OKX_ORBIT_ENABLED, config.OKX_ORBIT_INTERVAL_HOURS,
    )

    if once:
        # Режим разового запуска (GitHub Actions: python main.py --once) -
        # делаем ровно один проход и выходим, НЕ запускаем планировщик.
        logger.info("Режим --once: запуск одного тика")
        tick()
        logger.info("Тик завершён, выход")
        return

    scheduler = BlockingScheduler()
    scheduler.add_job(tick, "interval", seconds=60, next_run_time=None)
    tick()  # сразу один проход при старте, не дожидаясь первого интервала
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Бот остановлен.")


if __name__ == "__main__":
    main()
