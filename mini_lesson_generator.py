"""
mini_lesson_generator.py - формат "Мини-урок" (Bluesky).

Короткий пост БЕЗ сигнала и без сделки - просто объяснение одного
торгового понятия (RSI, дивергенция, риск/прибыль и т.п.) простым
языком. Не про то, "что купить сейчас", а про то, "как это вообще
работает" - строит образ эксперта, а не просто "бота с сигналами", и
такой контент репостят чаще самих сигналов (evergreen, не устаревает).

Публикуется ТОЛЬКО в Bluesky (см. main.try_publish_mini_lesson), как и
"Хот-тейк" - это формат, заточенный под механику конкретно этой
площадки, а не универсальный контент для Square/Telegram.

В отличие от opinion_generator/hot_take_generator - здесь НЕТ реальных
чисел с рынка (не про конкретное движение цены, а про общее понятие),
поэтому нет allowed_numbers/проверки чисел. Вместо этого системный
промпт явно запрещает называть текущие/конкретные цены реальных активов
(это были бы непроверяемые фактические утверждения) - validate_mini_lesson
проверяет это простым паттерном на всякий случай.
"""
import logging
import random
import re
from typing import Optional

import cliche_filter
import config
from groq_client import call_groq
from post_format import BLUESKY_CHAR_LIMIT, DISCLAIMER
import voice_guidelines

logger = logging.getLogger(__name__)

TOPICS: dict[str, str] = {
    "rsi": "Что такое RSI (индекс относительной силы) и как его читать",
    "divergence": "Что такое дивергенция между ценой и индикатором и почему на неё обращают внимание",
    "support_resistance": "Что такое уровни поддержки и сопротивления",
    "overbought_oversold": "Что значит 'перекупленность' и 'перепроданность' актива",
    "risk_reward": "Что такое соотношение риск/прибыль в сделке и почему это важнее самого прогноза",
    "stop_loss": "Зачем нужен стоп-лосс и почему без него даже хорошая идея может слить депозит",
    "volume": "Почему объём торгов важен для подтверждения движения цены",
    "bollinger": "Что такое полосы Боллинджера и что значит 'касание полосы'",
    "timeframes": "Как выбор таймфрейма (15м/1ч/1д) меняет то, что ты вообще видишь на графике",
    "position_sizing": "Почему размер позиции важнее, чем 'правильный' вход",
}

_SYSTEM_PROMPT = """Ты пишешь короткий образовательный пост для Bluesky -
объясняешь ОДНО торговое понятие простым языком, как для новичка, но без
снисходительности. 2-4 предложения. Можно живую аналогию или пример "из
жизни", можно закончить лёгким вопросом к читателю.

ЖЁСТКО ЗАПРЕЩЕНО:
- называть текущую или прошлую цену любого реального актива ($BTC,
  $ETH и т.п.) - это непроверяемое фактическое утверждение, у тебя нет
  доступа к актуальным данным;
- давать сигнал на сделку или прогноз движения цены - это чисто
  обучающий пост про КОНЦЕПЦИЮ, а не про конкретный актив сейчас.

Если нужен пример - используй условные/гипотетические числа ("допустим,
если RSI выше 70...") без привязки к конкретному активу и без слов
"сейчас"/"сегодня".

Лимит - 220 символов на весь ответ, уложись сам. Без хэштегов и ссылок -
добавятся отдельно. Отвечай только текстом поста, без пояснений и кавычек.""" + voice_guidelines.STYLE_DIRECTIVE

_MAX_LESSON_CHARS = BLUESKY_CHAR_LIMIT - len(DISCLAIMER) - 2

# Непроверяемое фактическое утверждение о текущей цене реального актива -
# паттерн вида "$BTC ... 65000" или "$BTC на 65000" в одном предложении.
# Грубая эвристика (не идеальный NLP), но ловит самый частый и самый
# рискованный случай - LLM называет конкретную цифру цены рядом с cashtag.
_SUSPICIOUS_PRICE_CLAIM = re.compile(r"\$[A-Z]{2,10}\D{0,40}\d{3,}")


def pick_topic(last_topic: Optional[str]) -> str:
    """Выбирает тему, отличную от последней использованной - тот же
    принцип ротации, что и opinion_generator.pick_theme/hot_take_generator."""
    topics = list(TOPICS.keys())
    if last_topic in topics and len(topics) > 1:
        topics = [t for t in topics if t != last_topic]
    return random.choice(topics)


def generate_mini_lesson(topic: str) -> Optional[str]:
    """Возвращает готовый текст поста для Bluesky, либо None, если
    generation не удалась содержательно (пустой ответ после ретраев
    call_groq - крайне маловероятно, но проверяем на всякий случай)."""
    if topic not in TOPICS:
        logger.error("Неизвестная тема мини-урока: %s", topic)
        return None

    user_prompt = f"Тема: {TOPICS[topic]}\n\nНапиши мини-урок на эту тему."
    lesson = call_groq(_SYSTEM_PROMPT, user_prompt, max_tokens=320, temperature=0.85, model=config.GROQ_MODEL_SECONDARY)

    if len(lesson.strip()) < 10:
        logger.warning("Мини-урок пустой или слишком короткий (%r) - пропускаю это окно", lesson)
        return None

    lesson = lesson.strip()
    if len(lesson) > _MAX_LESSON_CHARS:
        lesson = lesson[: _MAX_LESSON_CHARS - 1].rstrip() + "…"

    text = f"{lesson}\n\n{DISCLAIMER}"
    logger.info("Сгенерирован мини-урок (тема %s): %s", topic, text)
    return text


def validate_mini_lesson(text: str) -> tuple:
    """Проверяем дисклеймер, лимит длины Bluesky и отсутствие
    подозрительных заявлений о конкретной цене реального актива (см.
    _SUSPICIOUS_PRICE_CLAIM) - в мини-уроке таких утверждений быть не
    должно вообще, это чисто концептуальный пост."""
    if DISCLAIMER.lower() not in text.lower():
        return False, "В тексте отсутствует дисклеймер"

    if len(text) > BLUESKY_CHAR_LIMIT:
        return False, f"Текст длиннее лимита Bluesky ({BLUESKY_CHAR_LIMIT}): {len(text)}"

    match = _SUSPICIOUS_PRICE_CLAIM.search(text)
    if match:
        return False, f"Похоже на утверждение о конкретной цене актива: {match.group(0)!r}"

    cliche_ok, found = cliche_filter.check_cliches(text)
    if not cliche_ok:
        return False, f"В тексте есть шаблонные ИИ-фразы: {', '.join(found)}"

    return True, ""
