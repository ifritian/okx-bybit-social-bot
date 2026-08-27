"""
Анализ постов-картинок через Groq Vision.

ВАЖНОЕ ОГРАНИЧЕНИЕ ПО ДИЗАЙНУ: модель распознаёт с картинки только
тикер и общее направление движения (вверх/вниз/неопределённо) -
качественные вещи, которые сложно перепутать. Она НЕ извлекает
конкретные числа (цены, %, уровни) с графика - распознавание цифр
на скриншоте через vision-модель ненадёжно (легко "прочитать"
0.007187 как 0.007197), а с финансовыми цифрами ошибка особенно
чувствительна. Поэтому пост по картинке всегда без точных уровней,
в духе твоих ранних постов ($ARB, $TRUMP) - качественное наблюдение,
а не цифры.

Используется модель с поддержкой зрения на Groq (бесплатно,
OpenAI-совместимый API, просто передаём image_url напрямую).

При ошибке 429 (превышен лимит запросов) - повтор с экспоненциальной
задержкой (2с, 4с, 8с), до 3 попыток.
"""
import json
import logging
import re
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import requests

import config

logger = logging.getLogger(__name__)

_GROQ_ENDPOINT = "https://api.groq.com/openai/v1/chat/completions"
_DIGIT_RE = re.compile(r"\d")

_VISION_PROMPT = """Посмотри на этот скриншот (вероятно, график цены криптовалюты).
Определи:
1. ticker - тикер монеты, если виден на изображении (например BTC, ETH), иначе null
2. direction - общее направление движения цены: "up", "down" или "unclear"
3. note - одна короткая фраза с качественным наблюдением о том, что видно на графике
   (например "цена пробила локальный максимум" или "видна нисходящая дивергенция RSI")

КРИТИЧЕСКИ ВАЖНО: поле note НЕ должно содержать никаких цифр, чисел,
процентов или конкретных уровней цены - только словесное описание.
Числа на скриншотах легко прочитать неверно, поэтому их нельзя
упоминать вообще.

Ответь ТОЛЬКО в формате JSON, без пояснений:
{"ticker": "...", "direction": "...", "note": "..."}"""


@dataclass
class ImageInsight:
    ticker: str
    direction: str   # up / down / unclear
    note: str
    image_url: str             # ссылка на момент анализа - годна для немедленного vision-запроса, но протухает примерно через час
    photo_file_id: Optional[str] = None  # для повторного скачивания свежей ссылки перед публикацией (см. telegram_listener.get_file_url)


def _strip_if_has_digits(note: str) -> str:
    """Если модель всё же вставила цифры в note - выкидываем их совсем,
    лучше нейтральная фраза, чем риск ошибочного числа."""
    if _DIGIT_RE.search(note):
        logger.warning("Vision-модель вставила цифры в note, заменяю на нейтральный текст: %r", note)
        return "на графике заметна интересная динамика"
    return note


def analyze_chart_image(image_url: str, photo_file_id: Optional[str] = None) -> Optional[ImageInsight]:
    """Анализ картинки с retry при ошибках 429 (превышен лимит запросов)."""
    max_retries = 3
    base_delay = 2  # начальная задержка 2 секунды

    for attempt in range(max_retries):
        try:
            payload = {
                "model": config.GROQ_VISION_MODEL,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": _VISION_PROMPT},
                            {"type": "image_url", "image_url": {"url": image_url}},
                        ],
                    }
                ],
                "temperature": 0.3,
                "max_tokens": 200,
            }
            headers = {"Authorization": f"Bearer {config.GROQ_API_KEY}"}

            resp = requests.post(_GROQ_ENDPOINT, json=payload, headers=headers, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            raw_text = data["choices"][0]["message"]["content"].strip()
            raw_text = raw_text.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
            parsed = json.loads(raw_text)

            ticker = parsed.get("ticker")
            # Модель иногда отвечает буквальной строкой "null"/"NULL"
            # вместо настоящего JSON null - такое тоже считаем "тикер не найден"
            if not ticker or str(ticker).strip().lower() in ("null", "none", "n/a", ""):
                logger.info("На картинке %s не распознан тикер - пропускаю", image_url)
                return None

            direction = parsed.get("direction", "unclear")
            note = _strip_if_has_digits(parsed.get("note", ""))

            return ImageInsight(
                ticker=str(ticker).upper().lstrip("$"),
                direction=direction,
                note=note,
                image_url=image_url,
                photo_file_id=photo_file_id,
            )

        except requests.exceptions.HTTPError as e:
            status = e.response.status_code
            retryable = status == 429 or 500 <= status < 600
            if retryable and attempt < max_retries - 1:
                delay = base_delay * (2 ** attempt)  # 2, 4, 8 секунд
                logger.warning(
                    "Ошибка %d, повторяю попытку %d/%d через %dс",
                    status, attempt + 1, max_retries, delay,
                )
                time.sleep(delay)
                continue
            # response.text - тут обычно человекочитаемая причина от Groq
            # (например "model X has been decommissioned") - requests'овский
            # e (просто "404 Client Error: Not Found for url: ...") её не
            # содержит, из-за чего причину раньше приходилось угадывать
            # по внешним источникам вместо одного взгляда в лог.
            body = (e.response.text or "")[:300]
            logger.warning("Не удалось распознать картинку %s: %s | ответ Groq: %s", image_url, e, body)
            return None
        except (requests.RequestException, KeyError, json.JSONDecodeError, ValueError) as e:
            logger.warning("Не удалось распознать картинку %s: %s", image_url, e)
            return None

    return None


def download_to_tempfile(image_url: str) -> Path:
    """Скачивает оригинальную картинку из канала, чтобы прикрепить её
    к посту как есть (не пересоздаём - просто переиспользуем)."""
    resp = requests.get(image_url, timeout=30)
    resp.raise_for_status()

    charts_dir = config.BASE_DIR / "charts"
    charts_dir.mkdir(exist_ok=True)

    suffix = ".jpg"
    for ext in (".png", ".jpg", ".jpeg", ".webp"):
        if image_url.lower().split("?")[0].endswith(ext):
            suffix = ext
            break

    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix, dir=charts_dir)
    tmp.write(resp.content)
    tmp.close()
    return Path(tmp.name)