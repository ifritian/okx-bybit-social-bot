"""
Кросспостинг постов (тех же, что уходят на Binance Square) в Bluesky
(AT Protocol) - config.BLUESKY_HANDLE / config.BLUESKY_APP_PASSWORD.

Как и telegram_publisher.py, это ОПЦИОНАЛЬНЫЙ и НЕЗАВИСИМЫЙ кросспост -
если Bluesky не настроен, main.py просто пропускает публикацию сюда, а
если запрос упал - логирует предупреждение и идёт дальше (см.
main._crosspost_to_bluesky).

Почему Bluesky вместо Threads: AT Protocol не требует Developer-портала,
App Review или добавления себя тестером - только "App password", который
создаётся в самом Bluesky (Settings -> App passwords) за 30 секунд.

AT Protocol flow:
1. POST /xrpc/com.atproto.server.createSession {identifier, password}
   -> accessJwt (токен на сессию) + did (постоянный ID аккаунта).
   Сессия НЕ кешируется между запусками бота - процесс python стартует
   заново на каждый тик (GitHub Actions job), кешировать по сути негде
   и не нужно при текущей частоте постов.
2. (если есть картинка) POST /xrpc/com.atproto.repo.uploadBlob - СЫРЫЕ
   БАЙТЫ картинки (не URL, как было у Threads!) -> blob-ссылка для embed.
3. POST /xrpc/com.atproto.repo.createRecord - сам пост (text, createdAt,
   facets для кликабельных ссылок, embed с картинкой, если есть).

Лимит текста Bluesky - 300 символов (жёстче, чем было у Threads/Square) -
обрезка делается в post_format.build_bluesky_post, сюда приходит уже
готовый текст.
"""
import logging
from datetime import datetime, timezone
from typing import Optional

import requests

import config

logger = logging.getLogger(__name__)

_PDS_BASE = "https://bsky.social/xrpc"


class BlueskyPublishError(Exception):
    pass


def is_configured() -> bool:
    """True, если кросспостинг в Bluesky настроен (handle + app password
    заданы). Вызывающий код (main.py) должен тихо пропускать кросспост,
    если это False - НЕ считать это ошибкой, раз кросспостинг опционален."""
    return bool(config.BLUESKY_HANDLE and config.BLUESKY_APP_PASSWORD)


def _parse(resp, step: str) -> dict:
    try:
        data = resp.json()
    except ValueError:
        raise BlueskyPublishError(f"Не удалось разобрать ответ Bluesky ({step}): {resp.text}") from None
    if not resp.ok:
        raise BlueskyPublishError(f"Bluesky вернул ошибку ({step}): {data.get('message', data)}")
    return data


def _create_session() -> dict:
    try:
        resp = requests.post(
            f"{_PDS_BASE}/com.atproto.server.createSession",
            json={"identifier": config.BLUESKY_HANDLE, "password": config.BLUESKY_APP_PASSWORD},
            timeout=30,
        )
    except requests.RequestException as e:
        raise BlueskyPublishError(f"Сетевая ошибка при авторизации в Bluesky: {e}") from e

    data = _parse(resp, "createSession")
    if "accessJwt" not in data or "did" not in data:
        raise BlueskyPublishError(f"Bluesky не вернул accessJwt/did: {data}")
    return data


def _upload_image(access_jwt: str, image_bytes: bytes, content_type: str) -> dict:
    try:
        resp = requests.post(
            f"{_PDS_BASE}/com.atproto.repo.uploadBlob",
            data=image_bytes,
            headers={"Authorization": f"Bearer {access_jwt}", "Content-Type": content_type},
            timeout=60,
        )
    except requests.RequestException as e:
        raise BlueskyPublishError(f"Сетевая ошибка при загрузке картинки: {e}") from e

    data = _parse(resp, "uploadBlob")
    blob = data.get("blob")
    if not blob:
        raise BlueskyPublishError(f"Bluesky не вернул blob картинки: {data}")
    return blob


def _byte_facets(text: str, links: list) -> list:
    """Строит facets (кликабельные ссылки) для указанных подстрок.

    links - список пар (подстрока_в_тексте, url). AT Protocol считает
    смещения в БАЙТАХ UTF-8, а не в символах Python, поэтому кодируем
    текст целиком и ищем байтовые индексы подстроки, а не str.find по
    символам - иначе ссылки на кириллице/эмодзи в тексте сдвинули бы
    диапазон и facet указывал бы не на ту часть поста.

    Подстрока, которая не нашлась (например, обрезана при укладке в
    лимит 300 символов - см. build_bluesky_post) просто пропускается:
    лучше пост без кликабельной ссылки, чем сломанный facet."""
    encoded = text.encode("utf-8")
    facets = []
    for substring, url in links:
        sub_bytes = substring.encode("utf-8")
        start = encoded.find(sub_bytes)
        if start == -1:
            continue
        end = start + len(sub_bytes)
        facets.append(
            {
                "index": {"byteStart": start, "byteEnd": end},
                "features": [{"$type": "app.bsky.richtext.facet#link", "uri": url}],
            }
        )
    return facets


def thread_ref(post_result: dict) -> dict:
    """Достаёт {uri, cid} из результата publish_post - именно эта пара
    identifies конкретный пост в AT Protocol (одного uri недостаточно,
    Bluesky требует cid для reply/root ссылок). Использовать для
    построения следующего поста треда через reply_refs()."""
    if "uri" not in post_result or "cid" not in post_result:
        raise BlueskyPublishError(f"В результате публикации нет uri/cid, не могу продолжить тред: {post_result}")
    return {"uri": post_result["uri"], "cid": post_result["cid"]}


def reply_refs(root_result: dict, parent_result: Optional[dict] = None) -> dict:
    """Собирает structure "reply" для publish_post() - AT Protocol требует
    ОБЕ ссылки, root (первый пост треда) и parent (пост, на который прямо
    отвечаем) - для второго поста треда они совпадают, для третьего и
    далее - разные (root всегда первый пост, parent - предыдущий)."""
    root_ref = thread_ref(root_result)
    parent_ref = thread_ref(parent_result) if parent_result is not None else root_ref
    return {"root": root_ref, "parent": parent_ref}


def publish_post(
    text: str,
    image_bytes: Optional[bytes] = None,
    image_content_type: str = "image/png",
    link_facets: Optional[list] = None,
    reply_to: Optional[dict] = None,
) -> dict:
    """
    Публикует пост в Bluesky.

    image_bytes - СЫРЫЕ БАЙТЫ картинки (не URL) - AT Protocol требует
    именно загрузку блоба через uploadBlob, ссылкой передать нельзя (в
    отличие от Threads, где было наоборот). Вызывающий код (main.py)
    должен прочитать локальный файл картинки и передать его содержимое.

    link_facets - список пар (подстрока, url) для кликабельных ссылок -
    см. post_format.build_bluesky_post, который возвращает этот список
    готовым, синхронизированным с текстом ссылок в самом посте.

    reply_to - результат reply_refs(root, parent) - если задан, пост
    публикуется как реплай в треде (используется для формата "Тред-разбор
    сильных сетапов" и связки Win-reveal/До-После, см. main.py). Без
    этого параметра пост публикуется как обычный, самостоятельный (корень
    нового возможного треда).
    """
    session = _create_session()
    access_jwt = session["accessJwt"]
    did = session["did"]

    record: dict = {
        "$type": "app.bsky.feed.post",
        "text": text,
        "createdAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }

    if reply_to:
        record["reply"] = reply_to

    facets = _byte_facets(text, link_facets or [])
    if facets:
        record["facets"] = facets

    if image_bytes:
        blob = _upload_image(access_jwt, image_bytes, image_content_type)
        record["embed"] = {
            "$type": "app.bsky.embed.images",
            "images": [{"image": blob, "alt": "chart"}],
        }

    try:
        resp = requests.post(
            f"{_PDS_BASE}/com.atproto.repo.createRecord",
            json={"repo": did, "collection": "app.bsky.feed.post", "record": record},
            headers={"Authorization": f"Bearer {access_jwt}"},
            timeout=30,
        )
    except requests.RequestException as e:
        raise BlueskyPublishError(f"Сетевая ошибка при публикации: {e}") from e

    data = _parse(resp, "createRecord")
    logger.info("Опубликовано в Bluesky%s: %s", " (реплай в треде)" if reply_to else "", data.get("uri"))
    return data


def publish_thread(posts: list, image_bytes: Optional[bytes] = None, image_content_type: str = "image/png") -> list:
    """
    Публикует цепочку постов как тред (первый пост - обычный, остальные -
    реплаи на предыдущий, с root всегда на первый). Картинка (если есть)
    прикрепляется ТОЛЬКО к первому посту треда - это "обложка" треда в
    ленте Bluesky, дальше идёт текст.

    posts - список либо строк, либо пар (текст, link_facets) - facets
    нужны только тем постам треда, где реально есть ссылки (обычно
    последнему, см. post_format.build_bluesky_thread_signal).

    Останавливается и поднимает BlueskyPublishError при первой же
    неудачной публикации ЛЮБОГО поста треда - опубликованные до этого
    посты треда остаются в Bluesky как есть (частичный тред без вывода
    лучше, чем полное отсутствие, но вызывающий код может залогировать
    это отдельно, см. main._crosspost_thread_to_bluesky)."""
    if not posts:
        raise BlueskyPublishError("Пустой список постов для треда")

    results = []
    root_result = None
    parent_result = None

    for i, item in enumerate(posts):
        text, facets = item if isinstance(item, tuple) else (item, None)

        reply_to = reply_refs(root_result, parent_result) if root_result is not None else None
        result = publish_post(
            text,
            image_bytes=image_bytes if i == 0 else None,
            image_content_type=image_content_type,
            link_facets=facets,
            reply_to=reply_to,
        )
        results.append(result)

        if root_result is None:
            root_result = result
        parent_result = result

    return results
