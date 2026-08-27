"""
binance_promo_generator.py - формат "Промо" (ТОЛЬКО Binance Square).

В отличие от opinion/hot_take/index_signal - этот формат НЕ про рыночный
сигнал и не считает никаких реальных цифр по ценам. Тема - удобства и
сильные стороны самой площадки Binance Square/Binance (низкие комиссии
у Binance в целом, удобство торговли не выходя из ленты, сообщество
Square, разнообразие инструментов и т.д.), без конкретных процентов по
комиссиям - у бота нет доступа к актуальной официальной тарифной сетке
Binance, а придумывать цифры нельзя (см. validate_binance_promo).

Публикуется ТОЛЬКО на Binance Square (см. main.try_publish_binance_promo) -
минуя Telegram/Bluesky целиком, т.к. рекламировать саму площадку Binance
в других соцсетях бессмысленно.

Ротация темы - через собственный queue_manager.get_last_binance_promo_theme/
set_last_binance_promo_theme (отдельно от opinion/hot_take - чтобы темы
этих форматов не были синхронны).
"""
import logging
import random
import re
from typing import Optional

import cliche_filter
import config
from groq_client import call_groq
from post_format import DISCLAIMER, HOOK_MODES, assemble_post
import voice_guidelines
import voice_memory

logger = logging.getLogger(__name__)

# Темы промо-поста - каждая описывает, про какую сильную сторону
# площадки пишем в этот раз, чтобы посты не повторяли один и тот же
# угол каждый раз.
THEMES: dict[str, dict] = {
    "fees": {
        "label": "низкие комиссии Binance для активной торговли",
    },
    "square": {
        "label": "Binance Square как соцсеть внутри приложения - лента, подписки, обсуждения",
    },
    "convenience": {
        "label": "удобство торговать не выходя из ленты/приложения",
    },
    "tools": {
        "label": "разнообразие инструментов и рынков на Binance (спот, фьючерсы, стейкинг и т.д.)",
    },
    "community": {
        "label": "сообщество трейдеров и авторов на Binance Square",
    },
}

_SYSTEM_PROMPT = """Ты пишешь короткий промо-пост для Binance Square про
саму площадку Binance/Square - НЕ про рыночный сигнал и не про движение
цены какой-либо монеты. 2-4 предложения, живо и по-человечески, можно с
1-2 эмодзи, без канцелярита и без ощущения рекламного буклета.

ЗАПРЕЩЕНО:
- называть любые конкретные цифры - проценты комиссий, суммы, курсы,
  количество пользователей и т.п. У тебя НЕТ доступа к актуальным
  официальным данным, и придумывать их нельзя;
- упоминать движение цены, конкретные монеты/тикеры, сигналы на сделку
  или что-либо похожее на финансовый совет - это пост про площадку, а
  не про рынок;
- звучать как сгенерированная ИИ реклама - шаблонные фразы вроде
  "откройте для себя" или "это меняет правила игры" запрещены.

Пиши от первого лица, как автор, который сам пользуется площадкой и
делится реальным впечатлением. Отвечай только текстом поста, без
пояснений и без кавычек.""" + voice_guidelines.STYLE_DIRECTIVE


def pick_theme(last_theme: Optional[str]) -> str:
    """Выбирает тему, отличную от последней использованной - тот же
    паттерн, что и opinion_generator.pick_theme."""
    themes = list(THEMES.keys())
    if last_theme in themes and len(themes) > 1:
        themes = [t for t in themes if t != last_theme]
    return random.choice(themes)


def generate_binance_promo(theme: str, hook_mode: Optional[str] = None) -> Optional[str]:
    """Возвращает готовый текст промо-поста (хук + дисклеймер), либо
    None, если LLM не смогла выдать нормальный текст.

    В отличие от opinion/hot_take - здесь нет реальных чисел, которые
    нужно передавать LLM и потом проверять (validate_binance_promo
    вместо этого проверяет, что чисел вообще НЕТ - см. её докстринг),
    поэтому и возвращаемое значение - просто строка, а не tuple."""
    if theme not in THEMES:
        logger.warning("Неизвестная тема промо-поста: %s", theme)
        return None

    label = THEMES[theme]["label"]
    system_prompt = _SYSTEM_PROMPT
    if hook_mode is not None:
        system_prompt = f"{_SYSTEM_PROMPT}\n\n{HOOK_MODES[hook_mode]}"

    user_prompt = (
        f"Тема поста: {label}.\n\n"
        f"Напиши промо-пост про эту сильную сторону Binance/Square, "
        f"без единой конкретной цифры и без упоминания монет или движения цены."
    )
    user_prompt += voice_memory.anti_repeat_block() + voice_memory.continuity_block(theme, label)

    hook = call_groq(system_prompt, user_prompt, max_tokens=350, temperature=0.9, model=config.GROQ_MODEL_SECONDARY)

    # Та же подстраховка, что у opinion_generator/hot_take_generator -
    # call_groq уже перезапрашивает пустые ответы сам, это - защита на
    # крайний случай, чтобы не собрать пост из одного дисклеймера.
    if len(hook.strip()) < 10:
        logger.warning("Промо-хук пустой или слишком короткий (%r) - пропускаю это окно", hook)
        return None

    text = assemble_post(hook)
    logger.info("Сгенерирован промо-пост (тема %s): %s", theme, text)
    return text


def validate_binance_promo(text: str) -> tuple:
    """В отличие от opinion/hot_take (где проверяем, что числа - ПОДМНОЖЕСТВО
    посчитанных) - здесь допустимых чисел нет вообще (промо не про
    рыночные данные), поэтому любое число в тексте - повод отклонить
    пост, чтобы не проскочила выдуманная LLM цифра комиссии/статистики.
    Дополнительно проверяем дисклеймер и отсутствие шаблонных ИИ-фраз -
    те же правила, что и у остальных форматов."""
    numbers = re.findall(r"\d+[.,]?\d*\s*%|\$\s?\d+[.,]?\d*|\d+[.,]?\d*", text)
    if numbers:
        return False, f"В промо-посте не должно быть чисел, но найдены: {numbers}"

    if DISCLAIMER.lower() not in text.lower():
        return False, "В тексте отсутствует дисклеймер"

    cliche_ok, found = cliche_filter.check_cliches(text)
    if not cliche_ok:
        return False, f"В тексте есть шаблонные ИИ-фразы: {', '.join(found)}"

    return True, ""


def assemble_binance_promo(text: str) -> str:
    """Текст уже полностью собран в generate_binance_promo (assemble_post
    добавил дисклеймер) - здесь только добавляем CTA-строку про площадку,
    если повезёт по вероятности (maybe_binance_cta), т.к. пост и так уходит
    ТОЛЬКО на Binance Square. Отдельная функция сохранена ради симметрии с
    остальными try_publish_* в main.py (там паттерн "generate -> validate ->
    assemble -> publish" одинаковый для всех форматов)."""
    import post_format

    cta = post_format.maybe_binance_cta()
    if cta:
        return f"{text}\n\n{cta}"
    return text
