"""
bybit_draft_publisher.py - доставка готового черновика поста для Bybit
ByX владельцу бота в Telegram (отдельный чат от OKX Orbit - см.
config.BYBIT_BYX_DRAFT_CHAT_ID).

ПОЧЕМУ ЧЕРНОВИК, А НЕ ПУБЛИКАЦИЯ: у Bybit ByX, как и у OKX Orbit, нет
публичного API для постинга - публикация только вручную через
приложение. У ByX к тому же явно виден статус модерации (Published /
Pending / Not Approved в интерфейсе), значит контент проходит проверку
перед публикацией - ещё одна причина не пытаться обойти это
автоматизацией, а просто готовить качественный черновик для ручной
публикации.

Устроено идентично okx_draft_publisher.py - см. комментарии там для
более подробного объяснения решений. Здесь только Bybit-specific
константы (лейбл, chat_id, эмодзи-маркер, чтобы черновики двух бирж не
путались между собой в переписке с ботом).
"""
import logging
from pathlib import Path
from typing import Optional

import requests

import config

logger = logging.getLogger(__name__)

_API_BASE = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}"

_CAPTION_LIMIT = 1024

_FORMAT_LABELS = {
    "market_take": "Разбор рынка",
    "trading_insight": "Трейдинг-инсайт",
    "news_take": "Мнение по новости",
}


class DraftDeliveryError(Exception):
    pass


def is_configured() -> bool:
    """True, если доставка черновиков Bybit ByX в Telegram настроена
    (токен + чат заданы). main.py должен тихо пропускать формат, если
    False - см. try_publish_bybit_byx_draft."""
    return bool(config.TELEGRAM_BOT_TOKEN and config.BYBIT_BYX_DRAFT_CHAT_ID)


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
        f"🟠 Bybit ByX - черновик ({label})\n"
        f"Скопируй текст ниже и опубликуй вручную в приложении:\n"
        f"{'-' * 24}\n"
        f"{post_text}"
    )


def send_draft(post_text: str, format_type: str, image_path: Optional[Path] = None) -> dict:
    """Отправляет черновик поста в чат для черновиков Bybit ByX
    (config.BYBIT_BYX_DRAFT_CHAT_ID). См. okx_draft_publisher.send_draft
    - логика идентична."""
    chat_id = config.BYBIT_BYX_DRAFT_CHAT_ID
    caption = _build_caption(post_text, format_type)

    if image_path is not None and Path(image_path).exists():
        if len(caption) <= _CAPTION_LIMIT:
            with open(image_path, "rb") as f:
                result = _post(
                    "sendPhoto",
                    data={"chat_id": chat_id, "caption": caption},
                    files={"photo": f},
                )
            logger.info("Черновик Bybit ByX доставлен (фото+подпись): message_id=%s", result.get("message_id"))
            return result

        with open(image_path, "rb") as f:
            _post("sendPhoto", data={"chat_id": chat_id}, files={"photo": f})
        result = _post("sendMessage", data={"chat_id": chat_id, "text": caption})
        logger.info("Черновик Bybit ByX доставлен (фото + текст отдельно): message_id=%s", result.get("message_id"))
        return result

    result = _post("sendMessage", data={"chat_id": chat_id, "text": caption})
    logger.info("Черновик Bybit ByX доставлен (только текст): message_id=%s", result.get("message_id"))
    return result
