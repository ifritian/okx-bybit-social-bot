"""
hot_take_generator.py - формат "Хот-тейк" (Bluesky).

Короткий, резкий тезис ПРОТИВ текущего рыночного консенсуса/настроения -
без сигнала, без конкретных уровней входа/стопа/тейка. Идея не в сделке,
а в провокации обсуждения: споры в комментариях = органический охват в
алгоритмической ленте Bluesky (см. main.try_publish_hot_take).

В отличие от opinion_generator (публикуется ВЕЗДЕ - Square/Telegram/
Bluesky) - хот-тейк уходит ТОЛЬКО в Bluesky. Это формат, заточенный под
механику конкретно этой площадки (короткая лента, дискуссии), а не
универсальный контент.

Как и opinion_generator - тема (BTC/ETH/market) и % изменение цены
считаются КОДОМ по реальным данным Binance (opinion_generator.
calc_theme_stats), LLM получает готовое число и пишет вокруг него
провокационный тезис, а не выдумывает цифры сам. Ротация темы - через
собственный queue_manager.get_last_hot_take_theme (отдельно от
opinion - чтобы темы этих двух форматов не были всегда синхронны).
"""
import logging
import re
from typing import Optional

import cliche_filter
import config
from groq_client import call_groq
from opinion_generator import THEMES, calc_theme_stats, pick_theme  # ротация темы - тот же алгоритм, что и у opinion
from post_format import BLUESKY_CHAR_LIMIT, DISCLAIMER, HOOK_MODES
import voice_memory
import voice_guidelines

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """Ты пишешь короткий "хот-тейк" для Bluesky - резкий,
провокационный тезис ПРОТИВ текущего рыночного консенсуса/настроения.
1-3 предложения, без воды. Цель - вызвать обсуждение и несогласие в
комментариях, а НЕ дать сигнал на сделку.

Тебе дан НАБОР реальных чисел (см. задание) - используй их ТОЧНО как
дано, не округляй и не придумывай других чисел. Можно вообще не
использовать числа, если тезис самодостаточен без них.

ЗАПРЕЩЕНО:
- упоминать конкретные уровни входа/стопа/тейка - это НЕ сигнал на
  сделку, а личное мнение;
- давать прямой финансовый совет ("покупайте", "продавайте сейчас").

Пиши хлёстко и коротко - у тебя жёсткий лимит в 220 символов на весь
ответ, уложись в него сам. Без хэштегов и ссылок - они добавятся
отдельно. Отвечай только текстом тезиса, без пояснений и без кавычек.""" + voice_guidelines.STYLE_DIRECTIVE

# Лимит на сам тезис (без дисклеймера) - оставляем запас под
# "\n\n" + DISCLAIMER, чтобы итоговый пост гарантированно укладывался в
# BLUESKY_CHAR_LIMIT (300) даже если LLM проигнорирует инструкцию про
# 220 символов.
_MAX_TAKE_CHARS = BLUESKY_CHAR_LIMIT - len(DISCLAIMER) - 2


def generate_hot_take(theme: str, hook_mode: Optional[str] = None) -> Optional[tuple]:
    """Возвращает (текст поста для Bluesky, набор разрешённых чисел),
    либо None, если не удалось получить данные по теме.

    hook_mode - один из post_format.HOOK_MODES ("яркие авторские
    голоса", те же, что и у валютных сигналов/поста-мнения) - хот-тейк
    в разных голосах звучит по-разному даже при одинаковой контрарной
    формулировке задачи. Если None - нейтральный промпт без персоны,
    как раньше."""
    stats = calc_theme_stats(theme)
    if stats is None:
        return None

    label = THEMES[theme]["label"]
    system_prompt = _SYSTEM_PROMPT
    if hook_mode is not None:
        system_prompt = f"{_SYSTEM_PROMPT}\n\n{HOOK_MODES[hook_mode]}"

    if "single" in stats:
        s = stats["single"]
        sign = "+" if s["pct"] >= 0 else ""
        user_prompt = (
            f"Тема: {label}\n"
            f"Изменение цены за последние 48 часов (2 дня): {sign}{s['pct']}%.\n\n"
            f"Если упоминаешь период времени - используй ТОЧНО '48 часов' или '2 дня', "
            f"не пересчитывай по-своему (не '24 часа', не 'сутки').\n\n"
            f"Напиши хот-тейк - тезис против общего рыночного консенсуса по этому движению."
        )
        allowed_numbers = {s["pct"]}
        headline_pct = s["pct"]
    else:
        breakdown_lines = "\n".join(
            f"  ${t}: {'+' if pct >= 0 else ''}{pct}%" for t, pct in stats["breakdown"].items()
        )
        avg = stats["avg_pct"]
        user_prompt = (
            f"Тема: {label}\n"
            f"Изменение по каждому активу за последние 48 часов (2 дня):\n{breakdown_lines}\n"
            f"Средний % по корзине: {'+' if avg >= 0 else ''}{avg}%.\n\n"
            f"Если упоминаешь период времени - используй ТОЧНО '48 часов' или '2 дня', "
            f"не пересчитывай по-своему (не '24 часа', не 'сутки').\n\n"
            f"Напиши хот-тейк - тезис против общего рыночного консенсуса по рынку в целом."
        )
        allowed_numbers = set(stats["breakdown"].values()) | {avg}
        headline_pct = avg

    user_prompt += voice_memory.anti_repeat_block() + voice_memory.continuity_block(theme, label)

    take = call_groq(system_prompt, user_prompt, max_tokens=280, temperature=1.0, model=config.GROQ_MODEL_SECONDARY)

    # Хук не должен быть пустым/почти пустым - та же подстраховка, что и
    # в opinion_generator/index_signal_generator (call_groq уже
    # перезапрашивает пустые ответы сам, это - защита на крайний случай).
    if len(take.strip()) < 10:
        logger.warning("Хот-тейк пустой или слишком короткий (%r) - пропускаю это окно", take)
        return None

    take = take.strip()
    if len(take) > _MAX_TAKE_CHARS:
        # LLM проигнорировал инструкцию про 220 символов - обрезаем сами,
        # чтобы итоговый пост гарантированно влез в лимит Bluesky, а не
        # ронять публикацию из-за одной лишней фразы.
        take = take[: _MAX_TAKE_CHARS - 1].rstrip() + "…"

    text = f"{take}\n\n{DISCLAIMER}"
    logger.info("Сгенерирован хот-тейк (тема %s, числа: %s): %s", theme, allowed_numbers, text)
    return text, allowed_numbers, headline_pct


def validate_hot_take(text: str, allowed_numbers: set) -> tuple:
    """Проверяем, что числа в тексте - подмножество тех, что мы сами
    посчитали (allowed_numbers), что дисклеймер на месте, и что итог
    укладывается в лимит Bluesky - те же правила, что и у
    opinion_generator.validate_opinion_post_text, плюс проверка длины
    (у обычного поста-мнения лимита нет, у хот-тейка - жёсткий 300)."""
    numbers = {float(n) for n in re.findall(r"[+-]?\d+\.?\d*", text.replace(",", ""))}
    unknown = [n for n in numbers if not any(abs(n - a) < 0.05 for a in allowed_numbers)]
    if unknown:
        return False, f"В тексте есть числа, не из посчитанных данных: {unknown}"

    if DISCLAIMER.lower() not in text.lower():
        return False, "В тексте отсутствует дисклеймер"

    if len(text) > BLUESKY_CHAR_LIMIT:
        return False, f"Текст длиннее лимита Bluesky ({BLUESKY_CHAR_LIMIT}): {len(text)}"

    cliche_ok, found = cliche_filter.check_cliches(text)
    if not cliche_ok:
        return False, f"В тексте есть шаблонные ИИ-фразы: {', '.join(found)}"

    return True, ""
