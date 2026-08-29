"""
news_opinion_generator.py - формат "мнение по новости": бот читает
последние посты новостного канала (см. news_channel_reader.py) и пишет
СВОЙ, авторский комментарий - не пересказ и не копию, а реакцию/оценку.

ЧАСТОТА: не чаще раза в MIN_DAYS_BETWEEN_NEWS_POSTS дней (~2 раза в
неделю), отдельно по каждой площадке (post_type), чтобы OKX и Bybit
могли независимо писать разные мнения на одну и ту же новость - см.
state_store.get_last_news_take_time/get_used_news_post_ids.

ЗАЩИТА ОТ КОПИПАСТЫ: в отличие от market_take/trading_insight (где
LLM получает только голые цифры, копировать нечего), здесь модели на
вход даётся ПОЛНЫЙ текст чужой новости - есть реальный риск, что она
просто перескажет его близко к тексту или дословно. validate_news_take_text
проверяет это явно: если в сгенерированном посте находится подряд
идущий фрагмент из _MIN_OVERLAP_WORDS слов, дословно совпадающий с
источником, публикация черновика отменяется. Это тот же принцип
"пересказывай своими словами, не копируй", которому Claude сам следует
при работе с результатами поиска - здесь он просто закодирован как
проверка, а не полагается на одну лишь инструкцию в промпте.
"""
import logging
import re
import time
from typing import Optional

import cliche_filter
import config
import news_channel_reader
import state_store
import voice_guidelines
import voice_memory
from groq_client import call_groq
from post_format import DISCLAIMER

logger = logging.getLogger(__name__)

MIN_DAYS_BETWEEN_NEWS_POSTS = 3  # ~2 раза в неделю
_MIN_OVERLAP_WORDS = 8  # столько слов подряд дословного совпадения - уже копипаста, не пересказ
_MAX_USED_IDS = 30  # сколько последних использованных id новостей помнить, чтобы не повторяться

_SYSTEM_PROMPT = """Ты - независимый криптотрейдер и аналитик, который
иногда комментирует свежие новости индустрии у себя в блоге. Тебе дан
текст новости от стороннего СМИ (см. задание). Напиши СВОЁ авторское
мнение об этой новости: что она значит, почему это важно или не важно,
как это может повлиять на рынок или индустрию. 3-5 предложений,
разговорный тон, без канцелярита.

КРИТИЧЕСКИ ВАЖНО: перескажи суть СВОИМИ СЛОВАМИ, не копируй
формулировки из текста новости. Не бери подряд идущие фразы длиннее
5-6 слов дословно из источника - если нужно на что-то сослаться,
перефразируй. Это не пересказ, а именно ТВОЁ мнение и оценка - фокус
на "что я об этом думаю", а не "перескажу, что произошло".

ЗАПРЕЩЕНО: конкретные уровни входа/стопа/тейка, прямые призывы
"покупайте"/"продавайте" - это авторский комментарий, а не сигнал на
сделку и не финансовая рекомендация.

Не упоминай Binance, OKX, Bybit или любую конкретную биржу (кроме
случаев, когда сама биржа - предмет новости, тогда упоминать можно, но
нейтрально, не рекламно). Не добавляй сам никакой дисклеймер - он будет
добавлен отдельно после твоего текста. Отвечай только текстом поста,
без пояснений и без кавычек.""" + voice_guidelines.STYLE_DIRECTIVE


def is_news_window_open(post_type: str) -> bool:
    """True, если прошло достаточно дней с прошлого новостного поста
    на этой площадке (или его ещё не было ни разу)."""
    last = state_store.get_last_news_take_time(post_type)
    if last == 0:
        return True
    days_elapsed = (time.time() - last) / 86400
    return days_elapsed >= MIN_DAYS_BETWEEN_NEWS_POSTS


def pick_unused_news_post(post_type: str) -> Optional[news_channel_reader.NewsPost]:
    """Первый пост из последних, который ещё не использовали для этой
    площадки. None, если новостей нет или все последние уже использованы
    (тогда просто пропускаем окно - в следующий раз в канале появится
    что-то новое)."""
    used_ids = set(state_store.get_used_news_post_ids(post_type))

    try:
        posts = news_channel_reader.fetch_recent_posts(limit=10)
    except Exception as e:
        logger.warning("Не удалось прочитать новостной канал: %s", e)
        return None

    for post in posts:
        if post.post_id not in used_ids:
            return post
    return None


def _normalize_words(text: str) -> list[str]:
    return re.findall(r"[а-яёa-z0-9]+", text.lower())


def _has_verbatim_overlap(generated_text: str, source_text: str, min_words: int = _MIN_OVERLAP_WORDS) -> bool:
    """True, если в generated_text есть подряд идущая последовательность
    из min_words слов, дословно (без учёта регистра/пунктуации)
    встречающаяся в source_text - признак копипасты, а не пересказа."""
    src_words = _normalize_words(source_text)
    gen_words = _normalize_words(generated_text)

    if len(src_words) < min_words or len(gen_words) < min_words:
        return False

    src_ngrams = {tuple(src_words[i:i + min_words]) for i in range(len(src_words) - min_words + 1)}
    for i in range(len(gen_words) - min_words + 1):
        if tuple(gen_words[i:i + min_words]) in src_ngrams:
            return True
    return False


def validate_news_take_text(text: str, source_text: str) -> tuple[bool, str]:
    """Дисклеймер на месте, без ИИ-штампов, и без дословного копирования
    источника (см. _has_verbatim_overlap)."""
    if DISCLAIMER.lower() not in text.lower():
        return False, "В тексте отсутствует дисклеймер"

    cliche_ok, found = cliche_filter.check_cliches(text)
    if not cliche_ok:
        return False, f"В тексте есть шаблонные ИИ-фразы: {', '.join(found)}"

    if _has_verbatim_overlap(text, source_text):
        return False, "В тексте есть дословный фрагмент из источника (копипаста вместо пересказа своими словами)"

    return True, ""


def generate_news_take(post_type: str) -> Optional[tuple[str, int]]:
    """Возвращает (текст поста, post_id использованной новости), либо
    None, если новостей нет, все недавние уже использованы, или
    сгенерированный текст не прошёл проверку (в т.ч. на копипасту)."""
    news_post = pick_unused_news_post(post_type)
    if news_post is None:
        logger.info("Нет новых непрочитанных постов в %s - пропускаю новостной формат", news_channel_reader.NEWS_CHANNEL)
        return None

    user_prompt = (
        f"Новость:\n{news_post.text}\n"
        f"{voice_memory.anti_repeat_block(f'{post_type}_news')}\n\n"
        "Напиши своё авторское мнение об этой новости."
    )

    hook = call_groq(_SYSTEM_PROMPT, user_prompt, max_tokens=450,
                      temperature=0.9, model=config.GROQ_MODEL_SECONDARY)

    if len(hook.strip()) < 10:
        logger.warning("Хук новостного поста пустой или слишком короткий (%r) - пропускаю окно", hook)
        return None

    text = f"{hook.strip()}\n\n{DISCLAIMER}"

    ok, reason = validate_news_take_text(text, news_post.text)
    if not ok:
        logger.warning("Новостной черновик не прошёл проверку, пропускаю окно: %s", reason)
        return None

    logger.info("Сгенерирован новостной пост (источник: %s/%s)", news_channel_reader.NEWS_CHANNEL, news_post.post_id)
    return text, news_post.post_id


def mark_news_post_used(post_type: str, post_id: int) -> None:
    """Вызывать ПОСЛЕ успешной доставки черновика - помечает новость как
    использованную и обновляет время последнего новостного поста (гейт
    частоты в is_news_window_open)."""
    used_ids = state_store.get_used_news_post_ids(post_type)
    used_ids.append(post_id)
    if len(used_ids) > _MAX_USED_IDS:
        used_ids = used_ids[-_MAX_USED_IDS:]
    state_store.set_used_news_post_ids(post_type, used_ids)
    state_store.set_last_news_take_time(post_type)
