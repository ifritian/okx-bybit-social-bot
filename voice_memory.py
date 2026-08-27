"""
voice_memory.py - "Память о прошлых постах" (последний из четырёх
пунктов работы над голосом, после voice_guidelines.py и cliche_filter.py).

Два независимых механизма:

1. АНТИ-ПОВТОР ЗАЧИНОВ - последние несколько зачинов (первое предложение
   поста) хранятся в queue_manager по ВСЕМ форматам сразу (currency-
   сигнал/opinion/hot_take), и при генерации нового хука LLM получает
   инструкцию не начинать так же. Без этого разные генераторы независимо
   сходятся на одних и тех же зачинах ("Ну что, опять...", "Смотрю на
   график и...") - не потому что кто-то их запрограммировал, а потому
   что это статистически частые паттерны LLM, которые накапливаются без
   явного запрета.

2. ЧЕСТНАЯ ПРЕЕМСТВЕННОСТЬ ПО ТЕМЕ (opinion/hot_take) - если тема (BTC/
   ETH/market) уже обсуждалась раньше, РЕАЛЬНЫЕ факты того раза (число,
   давность) передаются в промпт как "если уместно, можешь на это
   сослаться". Это НЕ выдумка - continuity_block() передаёт только то,
   что мы сами сохранили после реальной публикации, LLM не может
   придумать другую "историю", которой не было (см. record_post).

Оба механизма - подсказки в промпте, не жёсткая валидация: если LLM всё
равно повторит похожий зачин, публикация не отклоняется - в отличие от
чисел/дисклеймера/клише, это вопрос разнообразия, а не корректности.
"""
import logging
import time
from typing import Optional

import queue_manager

logger = logging.getLogger(__name__)

_RECENT_OPENERS_MAX = 15
_RECENT_OPENERS_IN_PROMPT = 8  # последних достаточно для промпта - вся история туда не нужна
_CONTINUITY_MAX_AGE_DAYS = 14  # старше - отсылка потеряла бы актуальность, не показываем


def _first_sentence(text: str) -> str:
    """Берёт первую строку/предложение текста - то, чем пост
    "открывается" - для сравнения с прошлыми зачинами и как краткая
    сводка позиции для continuity_block()."""
    first_line = text.strip().split("\n")[0].strip()
    for sep in (". ", "! ", "? "):
        if sep in first_line:
            return first_line.split(sep)[0].strip() + sep.strip()
    return first_line[:80]


def record_post(text: str, theme: Optional[str] = None, pct: Optional[float] = None) -> None:
    """Вызывать ПОСЛЕ успешной публикации (не до - незачем засорять
    память зачинами постов, которые не прошли валидацию и не были
    опубликованы). Обновляет анти-повтор всегда; если переданы theme и
    pct (opinion/hot_take) - также обновляет честную историю по теме
    для continuity_block()."""
    opener = _first_sentence(text)

    openers = queue_manager.get_recent_openers()
    openers.append(opener)
    if len(openers) > _RECENT_OPENERS_MAX:
        openers = openers[-_RECENT_OPENERS_MAX:]
    queue_manager.set_recent_openers(openers)

    if theme is not None and pct is not None:
        history = queue_manager.get_theme_post_history()
        history[theme] = {"pct": pct, "stance_summary": opener, "timestamp": time.time()}
        queue_manager.set_theme_post_history(history)


def anti_repeat_block() -> str:
    """Блок для user_prompt с последними зачинами - пустая строка, если
    истории ещё нет (самые первые посты бота)."""
    openers = queue_manager.get_recent_openers()
    if not openers:
        return ""

    lines = "\n".join(f"- {o}" for o in openers[-_RECENT_OPENERS_IN_PROMPT:])
    return (
        "\n\nНЕ начинай пост так же, как эти последние посты (другое первое "
        f"предложение по структуре и лексике):\n{lines}"
    )


def continuity_block(theme: str, label: str) -> str:
    """Если по этой теме уже был пост-мнение/хот-тейк раньше (запись
    есть и не старше _CONTINUITY_MAX_AGE_DAYS) - блок с РЕАЛЬНЫМИ
    фактами того раза, которые LLM МОЖЕТ (не обязан) использовать для
    честной отсылки "как я говорил раньше". Пустая строка, если истории
    по теме нет или она устарела."""
    history = queue_manager.get_theme_post_history()
    entry = history.get(theme)
    if entry is None:
        return ""

    age_days = (time.time() - entry["timestamp"]) / 86400
    if age_days > _CONTINUITY_MAX_AGE_DAYS:
        return ""

    sign = "+" if entry["pct"] >= 0 else ""
    return (
        f"\n\nКОНТЕКСТ (использовать НЕ обязательно, только если реально "
        f"уместно): {int(age_days)} дн. назад про {label} ты писал: "
        f"\"{entry['stance_summary']}\" (тогда движение было {sign}{entry['pct']}%). "
        "Если сейчас видно интересное развитие ИМЕННО ЭТОЙ истории - можешь "
        "честно на это сослаться. Не выдумывай других прошлых постов и не "
        "утверждай, что говорил что-то, чего нет в этом контексте."
    )
