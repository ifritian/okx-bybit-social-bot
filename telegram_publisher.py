"""
Кросспостинг наших же постов (тех, что уходят на Binance Square) в
собственный Telegram-канал - config.TELEGRAM_PUBLISH_CHANNEL.

ВАЖНО: это ОТДЕЛЬНЫЙ канал от config.FOLLOWUP_CHANNEL_USERNAME -
тот канал бот только ЧИТАЕТ (источник дайджестов для статьи), а сюда
ПУБЛИКУЕТ. Один и тот же бот (TELEGRAM_BOT_TOKEN) должен быть админом
обоих каналов, но с разными ролями.

В отличие от Binance Square, у Telegram Bot API нет лимита на
количество "монетных тегов" в посте и нет presign/S3 flow для
картинок - можно одним запросом sendPhoto отправить фото с подписью
(caption), либо sendMessage, если картинки нет.

Ограничение Telegram, которое РЕАЛЬНО есть: caption у фото не может
быть длиннее 1024 символов (у обычного текстового сообщения - 4096).
Если текст поста длиннее - отправляем картинку без подписи, а текст
отдельным сообщением следом (тогда лимит уже 4096, обычно хватает).
"""
import logging
from pathlib import Path
from typing import Optional

import requests

import config

logger = logging.getLogger(__name__)

_API_BASE = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}"

_CAPTION_LIMIT = 1024


class TelegramPublishError(Exception):
    pass


def is_configured() -> bool:
    """True, если кросспостинг в Telegram настроен (токен + канал
    заданы). Вызывающий код (main.py) должен тихо пропускать
    кросспостинг, если это False - НЕ считать это ошибкой, раз
    кросспостинг опционален."""
    return bool(config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_PUBLISH_CHANNEL)


def _post(method: str, **kwargs) -> dict:
    try:
        resp = requests.post(f"{_API_BASE}/{method}", timeout=30, **kwargs)
    except requests.RequestException as e:
        raise TelegramPublishError(f"Сетевая ошибка ({method}): {e}") from e

    try:
        data = resp.json()
    except ValueError:
        raise TelegramPublishError(f"Не удалось разобрать ответ {method}: {resp.text}") from None

    if not data.get("ok"):
        raise TelegramPublishError(
            f"Telegram вернул ошибку ({method}): {data.get('description', data)}"
        )
    return data.get("result", {})


def publish_post(text: str, image_path: Optional[Path] = None) -> dict:
    """
    Публикует пост в config.TELEGRAM_PUBLISH_CHANNEL.

    Если есть картинка и текст укладывается в лимит подписи (1024
    символа) - одно сообщение (фото + caption). Если текст длиннее -
    два сообщения: фото без подписи, затем текст отдельным сообщением
    (у обычных текстовых сообщений лимит 4096, почти всегда хватает).
    Если картинки нет - просто текстовое сообщение.

    Поднимает TelegramPublishError при любой проблеме - вызывающий код
    уже умеет ловить Exception вокруг публикации и не должен валить
    остальную часть тика бота из-за неудачного кросспоста.
    """
    chat_id = config.TELEGRAM_PUBLISH_CHANNEL

    if image_path is not None and image_path.exists():
        if len(text) <= _CAPTION_LIMIT:
            with open(image_path, "rb") as f:
                result = _post(
                    "sendPhoto",
                    data={"chat_id": chat_id, "caption": text},
                    files={"photo": f},
                )
            logger.info("Кросспост в Telegram опубликован (фото+подпись): message_id=%s", result.get("message_id"))
            return result

        # текст не влезает в caption - фото отдельно, текст отдельно
        with open(image_path, "rb") as f:
            _post("sendPhoto", data={"chat_id": chat_id}, files={"photo": f})
        result = _post("sendMessage", data={"chat_id": chat_id, "text": text})
        logger.info("Кросспост в Telegram опубликован (фото + отдельное сообщение): message_id=%s", result.get("message_id"))
        return result

    result = _post("sendMessage", data={"chat_id": chat_id, "text": text})
    logger.info("Кросспост в Telegram опубликован (без фото): message_id=%s", result.get("message_id"))
    return result


def publish_poll(question: str, options: list, is_anonymous: bool = True) -> dict:
    """
    Публикует нативный Telegram-опрос (не обычное сообщение) - формат
    "Опрос" (см. telegram_engagement.py). Telegram API требует 2-10
    вариантов ответа, вопрос до 300 символов, каждый вариант до 100.

    is_anonymous=True (по умолчанию) - обычная практика для опросов в
    каналах (не в группах): участники видят только агрегированный
    результат, а не кто как проголосовал - это снижает соцдавление
    "проголосовать как большинство" и даёт более честную картину.

    Поднимает TelegramPublishError при любой проблеме - как и
    publish_post, вызывающий код уже умеет это ловить.
    """
    import json

    chat_id = config.TELEGRAM_PUBLISH_CHANNEL
    result = _post(
        "sendPoll",
        data={
            "chat_id": chat_id,
            "question": question,
            "options": json.dumps(options, ensure_ascii=False),
            "is_anonymous": is_anonymous,
        },
    )
    logger.info("Опрос опубликован в Telegram: message_id=%s", result.get("message_id"))
    return result
