"""
okx_draft_publisher.py - доставка готового черновика поста для OKX Orbit
владельцу бота в личный Telegram-чат.

ПОЧЕМУ ЧЕРНОВИК, А НЕ ПУБЛИКАЦИЯ: у OKX Orbit, в отличие от Binance
Square, нет публичного API для постинга - единственный официальный
способ опубликовать пост, это открыть приложение и нажать кнопку
публикации вручную (см. README_OKX1.md). Более того, OKX прямо
занижает вес контента, синхронизированного автоматически - то есть
даже если бы обходной способ постинга существовал, публиковать через
него было бы контрпродуктивно для самой механики наград.

Поэтому этот модуль не публикует ничего сам - он готовит сообщение так,
чтобы его было удобно скопировать (текст отдельным блоком, без лишнего
форматирования, которое могло бы потеряться при копировании) и
публикует его СЕБЕ в личный чат с ботом (config.OKX_ORBIT_DRAFT_CHAT_ID),
отдельно от TELEGRAM_PUBLISH_CHANNEL (тот канал - публичный кросспост
уже опубликованных постов, а не черновики).

Использует тот же бот-токен (config.TELEGRAM_BOT_TOKEN), что и
telegram_publisher.py - создавать отдельного бота не нужно.
"""
import logging
from pathlib import Path
from typing import Optional

import requests

import config

logger = logging.getLogger(__name__)

_API_BASE = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}"

# Отдельный лимит с запасом - у sendMessage лимит 4096, но черновик
# всегда идёт с обвязкой (заголовок формата + напоминание "скопируй и
# опубликуй сам"), поэтому берём текст поста с запасом.
_CAPTION_LIMIT = 1024

_FORMAT_LABELS = {
    "market_take": "Разбор рынка",
    "trading_insight": "Трейдинг-инсайт",
}


class DraftDeliveryError(Exception):
    pass


def is_configured() -> bool:
    """True, если доставка черновиков в Telegram настроена (токен +
    личный чат заданы). Вызывающий код (main.py) должен тихо пропускать
    этот формат, если False - см. try_publish_okx_orbit_draft."""
    return bool(config.TELEGRAM_BOT_TOKEN and config.OKX_ORBIT_DRAFT_CHAT_ID)


def _post(method: str, **kwargs) -> dict:
    try:
        resp = requests.post(f"{_API_BASE}/{method}", timeout=30, **kwargs)
    except requests.RequestException as e:
        raise DraftDeliveryError(f"Сетевая ошибка ({method}): {e}") from e

    try:
        data = resp.json()
    except ValueError:
        raise DraftDeliveryError(f"Не удалось разобрать ответ {method}: {resp.text}") from None

    if not data.get("ok"):
        raise DraftDeliveryError(
            f"Telegram вернул ошибку ({method}): {data.get('description', data)}"
        )
    return data.get("result", {})


def _build_caption(post_text: str, format_type: str) -> str:
    label = _FORMAT_LABELS.get(format_type, format_type)
    return (
        f"🟣 OKX Orbit - черновик ({label})\n"
        f"Скопируй текст ниже и опубликуй вручную в приложении:\n"
        f"{'-' * 24}\n"
        f"{post_text}"
    )


def send_draft(post_text: str, format_type: str, image_path: Optional[Path] = None) -> dict:
    """Отправляет черновик поста в личный чат владельца
    (config.OKX_ORBIT_DRAFT_CHAT_ID).

    Если есть картинка и итоговое сообщение укладывается в лимит
    подписи - одно сообщение (фото + caption). Иначе - фото без
    подписи, затем текст отдельным сообщением (либо просто текстовое
    сообщение, если картинки нет вовсе). Та же схема, что и в
    telegram_publisher.publish_post.

    Поднимает DraftDeliveryError при любой проблеме - вызывающий код
    уже умеет ловить Exception и не должен ронять остальную часть тика
    бота из-за неудачной доставки одного черновика.
    """
    chat_id = config.OKX_ORBIT_DRAFT_CHAT_ID
    caption = _build_caption(post_text, format_type)

    if image_path is not None and Path(image_path).exists():
        if len(caption) <= _CAPTION_LIMIT:
            with open(image_path, "rb") as f:
                result = _post(
                    "sendPhoto",
                    data={"chat_id": chat_id, "caption": caption},
                    files={"photo": f},
                )
            logger.info("Черновик OKX Orbit доставлен (фото+подпись): message_id=%s", result.get("message_id"))
            return result

        with open(image_path, "rb") as f:
            _post("sendPhoto", data={"chat_id": chat_id}, files={"photo": f})
        result = _post("sendMessage", data={"chat_id": chat_id, "text": caption})
        logger.info("Черновик OKX Orbit доставлен (фото + текст отдельно): message_id=%s", result.get("message_id"))
        return result

    result = _post("sendMessage", data={"chat_id": chat_id, "text": caption})
    logger.info("Черновик OKX Orbit доставлен (только текст): message_id=%s", result.get("message_id"))
    return result
