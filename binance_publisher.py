"""
Публикация постов в Binance Square, включая загрузку изображений.

Формат запросов и сам flow загрузки картинок взяты из официального
открытого репозитория Binance (binance/binance-skills-hub,
skills/binance/square-post/scripts/lib.mjs) - это эталонная реализация
от самого Binance, портирована на Python 1:1.

Flow загрузки картинки:
1. POST /image/presignedUrl {imageName} -> {presignedUrl, fileTicket}
2. PUT presignedUrl - заливаем сами байты картинки прямо в S3
3. POST /image/imageStatus {fileTicket} - опрашиваем, пока status != processing
   (status 1 = готово, 2 = ошибка обработки)
4. Готовый imageUrl передаём в /content/add как imageList

Известные особенности:
- лимит 100 постов/день, 400 загрузок/день на ключ
- сервер сам парсит #хэштеги и $тикеры из текста в кликабельные ссылки
- бывает ответ 504 от /content/add без id/link при фактически успешной
  публикации - это не обязательно ошибка
"""
import logging
import mimetypes
import time
from pathlib import Path
from typing import Optional

import requests

import config

logger = logging.getLogger(__name__)

_POLL_INTERVAL_SECONDS = 3
_MAX_POLL_RETRIES = 10


class PublishError(Exception):
    pass


def _headers() -> dict:
    return {
        "X-Square-OpenAPI-Key": config.BINANCE_SQUARE_API_KEY,
        "Content-Type": "application/json",
        "clienttype": "binanceSkill",
    }


def _api_v2(endpoint: str, body: dict) -> dict:
    url = f"{config.BINANCE_SQUARE_BASE_V2}{endpoint}"
    try:
        resp = requests.post(url, json=body, headers=_headers(), timeout=30)
    except requests.RequestException as e:
        raise PublishError(f"Сетевая ошибка ({endpoint}): {e}") from e

    try:
        data = resp.json()
    except ValueError:
        raise PublishError(f"Не удалось разобрать ответ {endpoint}: {resp.text}") from None

    if data.get("code") != "000000":
        raise PublishError(f"Ошибка {endpoint}: {data.get('code')} {data.get('message')}")
    return data.get("data", {})


def _upload_to_s3(presigned_url: str, image_path: Path, content_type: str) -> None:
    with open(image_path, "rb") as f:
        file_bytes = f.read()
    resp = requests.put(presigned_url, data=file_bytes, headers={"Content-Type": content_type}, timeout=60)
    if not resp.ok:
        raise PublishError(f"Загрузка в S3 не удалась: {resp.status_code} {resp.text}")


def _poll_image_status(file_ticket: str) -> dict:
    for attempt in range(_MAX_POLL_RETRIES):
        data = _api_v2("/image/imageStatus", {"fileTicket": file_ticket})
        if data.get("status") == 1:
            return data
        if data.get("status") == 2:
            raise PublishError(f"Обработка картинки не удалась: {data.get('failedReason')}")
        logger.info("Картинка обрабатывается на сервере... (%s/%s)", attempt + 1, _MAX_POLL_RETRIES)
        time.sleep(_POLL_INTERVAL_SECONDS)
    raise PublishError("Превышено время ожидания обработки картинки")


def upload_image(image_path: Path) -> str:
    """Загружает картинку в Binance Square, возвращает её итоговый imageUrl."""
    image_path = Path(image_path)
    content_type = mimetypes.guess_type(image_path.name)[0] or "image/png"

    logger.info("Загружаю картинку: %s", image_path.name)
    presign_data = _api_v2("/image/presignedUrl", {"imageName": image_path.name})
    presigned_url = presign_data["presignedUrl"]
    file_ticket = presign_data["fileTicket"]

    _upload_to_s3(presigned_url, image_path, content_type)
    logger.info("Картинка загружена в S3, ждём обработки...")

    status = _poll_image_status(file_ticket)
    logger.info("Картинка готова: %s", status["imageUrl"])
    return status["imageUrl"]


def publish_post(text: str, image_paths: Optional[list[Path]] = None) -> dict:
    """
    Публикует пост. Если передан image_paths (до 4 файлов) - сначала
    загружает каждую картинку, затем публикует пост с imageList.
    Если изображений нет - публикует обычный текстовый пост.

    Результат включает ключ "image_urls" (список уже загруженных
    публичных URL картинок, [] если картинок не было) - историческое
    поле не используется кросспостом в Bluesky (там картинка грузится
    как сырые байты с диска, см. main._crosspost_to_bluesky), но
    оставлено на случай, если понадобится публичная ссылка на картинку
    где-то ещё.
    """
    body: dict = {"contentType": 1, "bodyTextOnly": text}

    image_urls: list[str] = []
    if image_paths:
        if len(image_paths) > 4:
            raise PublishError("Максимум 4 картинки на пост")
        image_urls = [upload_image(p) for p in image_paths]
        body["imageList"] = image_urls

    result = _send_content(body)
    result["image_urls"] = image_urls
    return result


def publish_article(title: str, text: str, cover_path: Optional[Path] = None) -> dict:
    """
    Публикует длинную статью (contentType=2) с заголовком и опциональной
    обложкой - формат "Article" в Binance Square, отдельная вкладка от
    обычных постов. Обложка - ровно одна картинка (не список).

    Результат включает ключ "cover_url" (публичный URL уже загруженной
    обложки, None если её не было) - историческое поле, кросспост в
    Bluesky использует локальный cover_path напрямую (см.
    main.try_publish_article_post), а не этот URL.
    """
    body: dict = {"contentType": 2, "bodyTextOnly": text, "title": title}

    cover_url = None
    if cover_path is not None:
        cover_url = upload_image(cover_path)
        body["cover"] = cover_url

    result = _send_content(body)
    result["cover_url"] = cover_url
    return result


def _send_content(body: dict) -> dict:
    try:
        resp = requests.post(config.BINANCE_SQUARE_ENDPOINT, json=body, headers=_headers(), timeout=30)
    except requests.RequestException as e:
        raise PublishError(f"Сетевая ошибка при публикации: {e}") from e

    if resp.status_code == 504:
        logger.warning(
            "Получен 504 от Binance Square. Пост может быть опубликован, "
            "но id/link недоступны. Проверь вручную в приложении."
        )
        return {"id": None, "status": "unknown_504"}

    try:
        data = resp.json()
    except ValueError:
        raise PublishError(f"Не удалось разобрать ответ Binance Square: {resp.text}") from None

    if data.get("code") != "000000":
        raise PublishError(f"Binance Square вернул ошибку: {data.get('code')} {data.get('message')}")

    post_id = data.get("data", {}).get("id")
    logger.info("Пост успешно опубликован, id=%s", post_id)
    return {"id": post_id, "status": "ok"}