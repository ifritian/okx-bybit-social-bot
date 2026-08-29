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

import bybit_byx_generator
import bybit_draft_publisher
import config
import groq_client
import news_opinion_generator
import okx_draft_publisher
import okx_orbit_generator
import state_store
import voice_memory

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[logging.StreamHandler(), logging.FileHandler(config.LOG_PATH, encoding="utf-8")],
)
logger = logging.getLogger(__name__)


def _try_publish_news_draft(post_type: str, send_draft, delivery_error_cls) -> bool:
    """Общая логика новостного формата (см. news_opinion_generator.py)
    для обеих бирж - генерирует и доставляет черновик-мнение по свежей
    новости, если окно частоты открыто (не чаще раза в ~3 дня) и
    нашлась непрочитанная новость. Делит общее окно публикации с
    market_take/trading_insight - если сработал новостной формат,
    обычный market-пост в этом тике уже не генерируется (см. вызов ниже).

    Возвращает True, если тик "потреблён" новостным форматом (успешно
    доставлен ИЛИ доставка не удалась и уже поставлен retry-backoff) -
    тогда вызывающий код должен просто выйти, не пытаясь ещё и
    market-пост сгенерировать в этом же тике. False - новостной формат
    сейчас недоступен (окно закрыто/новостей нет/не прошло проверку),
    нужно продолжить обычным путём."""
    if not news_opinion_generator.is_news_window_open(post_type):
        return False

    result = news_opinion_generator.generate_news_take(post_type)
    if result is None:
        return False

    post_text, source_post_id = result
    try:
        delivered = send_draft(post_text, "news_take", None)
    except delivery_error_cls as e:
        logger.error("Ошибка доставки новостного черновика (%s): %s", post_type, e)
        state_store.set_retry_backoff(post_type, 1)
        return True

    news_opinion_generator.mark_news_post_used(post_type, source_post_id)
    voice_memory.record_post(f"{post_type}_news", post_text)

    logger.info("Новостной черновик (%s) доставлен: %s", post_type, delivered)
    state_store.set_last_post_time(post_type)
    jitter_hours = config.OKX_ORBIT_JITTER_HOURS if post_type == "okx_orbit" else config.BYBIT_BYX_JITTER_HOURS
    state_store.roll_new_jitter(post_type, jitter_hours * 3600)
    return True


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

    if config.OKX_NEWS_ENABLED and _try_publish_news_draft(post_type, okx_draft_publisher.send_draft, okx_draft_publisher.DraftDeliveryError):
        return

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

    post_text, allowed_numbers, headline_pct, format_type = result
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
    voice_memory.record_post(post_type, post_text, theme, headline_pct)

    logger.info("Черновик OKX Orbit доставлен: %s", delivered)
    state_store.set_last_post_time(post_type)
    state_store.roll_new_jitter(post_type, config.OKX_ORBIT_JITTER_HOURS * 3600)


# TODO: try_publish_bybit_byx_draft() - строится по образцу
# try_publish_okx_orbit_draft() выше, как только будет готов
# bybit_byx_generator.py / bybit_draft_publisher.py.


def try_publish_bybit_byx_draft() -> None:
    """Готовит черновик поста для Bybit ByX и присылает его владельцу в
    Telegram (см. bybit_draft_publisher.py) - НЕ публикует ничего сам, у
    Bybit ByX нет API для этого (та же ситуация, что и у OKX Orbit, см.
    README.md). Выключено по умолчанию (config.BYBIT_BYX_ENABLED) и
    требует config.BYBIT_BYX_DRAFT_CHAT_ID.
    """
    if not config.BYBIT_BYX_ENABLED:
        return
    if not bybit_draft_publisher.is_configured():
        logger.warning("BYBIT_BYX_ENABLED=true, но BYBIT_BYX_DRAFT_CHAT_ID не задан - пропускаю формат")
        return

    post_type = "bybit_byx"
    seconds_elapsed = state_store.seconds_since_last_post(post_type)
    min_seconds = config.BYBIT_BYX_INTERVAL_HOURS * 3600 + state_store.get_jitter_seconds(post_type)

    if seconds_elapsed < min_seconds:
        return
    if not state_store.should_retry_now(post_type):
        return  # недавно был сбой - ждём отступ, не долбим API на каждом тике

    if config.BYBIT_NEWS_ENABLED and _try_publish_news_draft(post_type, bybit_draft_publisher.send_draft, bybit_draft_publisher.DraftDeliveryError):
        return

    logger.info("Окно черновика (Bybit ByX) открыто - генерирую пост")

    theme = bybit_byx_generator.pick_theme(state_store.get_last_theme(post_type))
    format_type = bybit_byx_generator.pick_format(state_store.get_last_format(post_type))

    try:
        result = bybit_byx_generator.generate_bybit_byx_post(theme, format_type)
    except groq_client.GroqRateLimited as e:
        backoff_hours = max(e.retry_after_seconds / 3600, 5 / 60)
        logger.warning("Groq rate limit на черновике Bybit ByX - жду %.1fч перед следующей попыткой", backoff_hours)
        state_store.set_retry_backoff(post_type, backoff_hours)
        return
    except Exception as e:
        logger.error("Ошибка генерации черновика Bybit ByX: %s", e)
        state_store.set_retry_backoff(post_type, 1)
        return

    if result is None:
        logger.warning("Не удалось получить данные для темы %s (Bybit ByX) - пропускаю до следующего окна", theme)
        state_store.set_retry_backoff(post_type, 1)
        return

    post_text, allowed_numbers, headline_pct, format_type = result
    ok, reason = bybit_byx_generator.validate_bybit_byx_post_text(post_text, allowed_numbers)
    if not ok:
        logger.error("Черновик Bybit ByX не прошёл проверку, доставка отменена: %s", reason)
        state_store.set_retry_backoff(post_type, 1)
        return

    chart_path = None
    try:
        chart_path = bybit_byx_generator.generate_chart_for_post(theme)
    except Exception as e:
        logger.warning("Не удалось сгенерировать график для черновика Bybit ByX: %s - шлю без картинки", e)

    try:
        delivered = bybit_draft_publisher.send_draft(post_text, format_type, chart_path)
    except bybit_draft_publisher.DraftDeliveryError as e:
        logger.error("Ошибка доставки черновика Bybit ByX в Telegram: %s", e)
        state_store.set_retry_backoff(post_type, 1)
        return

    state_store.set_last_theme(post_type, theme)
    state_store.set_last_format(post_type, format_type)
    voice_memory.record_post(post_type, post_text, theme, headline_pct)

    logger.info("Черновик Bybit ByX доставлен: %s", delivered)
    state_store.set_last_post_time(post_type)
    state_store.roll_new_jitter(post_type, config.BYBIT_BYX_JITTER_HOURS * 3600)


def tick() -> None:
    try:
        try_publish_okx_orbit_draft()
    except Exception:
        logger.exception("Неожиданная ошибка в цикле OKX Orbit")

    try:
        try_publish_bybit_byx_draft()
    except Exception:
        logger.exception("Неожиданная ошибка в цикле Bybit ByX")


def main() -> None:
    once = "--once" in sys.argv

    logger.info(
        "Бот запущен. OKX Orbit: enabled=%s, interval=%sч | Bybit ByX: enabled=%s, interval=%sч",
        config.OKX_ORBIT_ENABLED, config.OKX_ORBIT_INTERVAL_HOURS,
        config.BYBIT_BYX_ENABLED, config.BYBIT_BYX_INTERVAL_HOURS,
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
