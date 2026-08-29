"""
news_channel_reader.py - читает последние посты публичного Telegram-
канала через его превью-страницу t.me/s/<канал>, без какого-либо
аккаунта или токена (см. README.md - почему выбран именно этот способ,
а не MTProto/Telethon).

КАК ЭТО РАБОТАЕТ: у любого публичного Telegram-канала есть открытая
HTML-версия по адресу https://t.me/s/<username> - её показывают даже
без входа в Telegram, ей исторически пользуются превью-боты и виджеты
"последние посты с канала" на сайтах. Разметка (data-post,
tgme_widget_message, tgme_widget_message_text) стабильна уже много лет,
но это всё равно НЕОФИЦИАЛЬНЫЙ контракт - Telegram не документирует и
не гарантирует её. Если однажды парсинг перестанет находить посты -
проверить вручную, не поменялась ли разметка страницы.

ВАЖНО ПРО АВТОРСКИЕ ПРАВА: fetch_recent_posts() возвращает СЫРОЙ текст
чужих постов - это нужно только чтобы LLM прочитала и сформировала
СВОЁ мнение. news_opinion_generator.py обязан пересказывать это своими
словами (и даже проверяет это кодом - см. _has_verbatim_overlap там),
а не пересылать/копировать исходный текст.
"""
import logging
from typing import NamedTuple, Optional

import requests
from bs4 import BeautifulSoup

import config

logger = logging.getLogger(__name__)

NEWS_CHANNEL = config.NEWS_SOURCE_CHANNEL

_MIN_TEXT_LENGTH = 40  # короче - скорее всего просто ссылка/картинка без сути, нечего пересказывать

# Строки нижнего колонтитула-навигации, которые ForkLog (и похожие
# каналы) добавляют почти в каждый пост ("Новости | AI | YouTube") -
# это не часть новости, вырезаем, чтобы не путать LLM и не засорять
# текст, который уходит в промпт.
_FOOTER_LINK_TEXTS = {"новости", "ai", "youtube", "подробнее", "читать на forklog"}


class NewsPost(NamedTuple):
    post_id: int
    text: str


def _fetch_html(channel: str) -> str:
    url = f"https://t.me/s/{channel}"
    resp = requests.get(url, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
    resp.raise_for_status()
    return resp.text


def _extract_text(text_div) -> str:
    """Текст поста без ссылок нижнего колонтитула-навигации (см.
    _FOOTER_LINK_TEXTS) - убираем эти <a> перед извлечением текста,
    чтобы их подписи не попадали в результат."""
    for a in text_div.find_all("a"):
        if a.get_text(strip=True).lower() in _FOOTER_LINK_TEXTS:
            a.decompose()
    return text_div.get_text("\n", strip=True)


def _parse_posts(html: str) -> list[NewsPost]:
    soup = BeautifulSoup(html, "html.parser")
    posts = []

    for msg in soup.select("div.tgme_widget_message[data-post]"):
        data_post = msg.get("data-post", "")
        if "/" not in data_post:
            continue
        try:
            post_id = int(data_post.split("/")[-1])
        except ValueError:
            continue

        text_div = msg.select_one("div.tgme_widget_message_text")
        if text_div is None:
            continue  # пост без текста (только медиа/опрос) - нечего пересказывать

        text = _extract_text(text_div)
        if len(text) < _MIN_TEXT_LENGTH:
            continue

        posts.append(NewsPost(post_id=post_id, text=text))

    return posts


def fetch_recent_posts(limit: int = 10) -> list[NewsPost]:
    """Последние `limit` постов канала с текстом (медиа-посты без
    подписи и слишком короткие посты отфильтрованы), от новых к старым.
    При сетевой ошибке возвращает [] и логирует warning - вызывающий
    код должен трактовать пустой список как "пропустить окно", не
    падать намертво (проблема с одним новостным каналом не должна
    ронять весь остальной поток постов)."""
    try:
        html = _fetch_html(NEWS_CHANNEL)
    except requests.RequestException as e:
        logger.warning("Не удалось прочитать новостной канал %s: %s", NEWS_CHANNEL, e)
        return []

    posts = _parse_posts(html)
    return list(reversed(posts))[:limit]
