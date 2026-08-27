"""
Генератор статьи (формат Article в Binance Square) - публикуется раз
в неделю, подводит итог по дайджестам из канала за последние 7 дней.

Числа берутся из накопленной истории (queue_manager.get_digest_history),
которая заполняется при каждом увиденном дайджесте - LLM получает
готовый список фактов и оформляет их в текст статьи, не придумывая
собственных цифр.

Обложка статьи - график BTC за неделю, тот же chart_generator,
который используется для постов про конкретные валюты.
"""
import logging
import re
from pathlib import Path
from typing import Optional

import cliche_filter
from chart_generator import generate_chart_image
from groq_client import GroqRateLimited, call_groq
from post_format import DISCLAIMER, assemble_post
import voice_guidelines

logger = logging.getLogger(__name__)

_WEEK_SECONDS = 7 * 24 * 3600

_SYSTEM_PROMPT = """Ты пишешь еженедельную статью-сводку для Binance Square
в фирменном стиле автора - живо, без канцелярита, но информативно.
Статья подводит итог по сигналам за неделю: что сработало, что не
оправдало ожиданий, общая картина.

Тебе дан список фактов (тикер, % изменения, score, результат) - вставляй
эти числа ТОЧНО как даны, без округления и без придумывания новых чисел
сверх того, что в списке.

Структура статьи:
1. Короткое вступление (1-2 предложения, можно с эмодзи)
2. Краткий разбор 3-5 самых заметных сигналов недели с их % и score
3. Итоговый вывод/мысль

НЕ добавляй сам дисклеймер - он будет добавлен отдельно после текста.

Ответ дай СТРОГО в таком формате (две секции, без дополнительных пояснений
и без кавычек вокруг заголовка):

ЗАГОЛОВОК: <короткий цепляющий заголовок, одна строка, не длиннее 70 символов>
СТАТЬЯ:
<текст статьи без заголовка>""" + voice_guidelines.STYLE_DIRECTIVE

# Дополнительный акцент в зависимости от того, как сложилась неделя -
# добавляется к базовому промпту, чтобы статья не звучала одинаково
# независимо от того, что реально произошло.
_COMPOSITION_EMPHASIS = {
    "mostly_wins": (
        "Эта неделя была в основном удачной (большинство сигналов сработали "
        "в плюс) - сделай акцент на том, что сработало и почему, без "
        "избыточного праздничного тона, но дай это прочувствовать."
    ),
    "mostly_losses": (
        "Эта неделя была сложной (большинство сигналов не оправдали "
        "ожиданий) - не превращай это в извинения, но честно отметь это, "
        "и сделай акцент на выводах и важности риск-менеджмента, а не "
        "только на перечислении фактов."
    ),
    "mixed": (
        "Неделя получилась смешанной (примерно поровну удачных и неудачных "
        "сигналов) - дай сбалансированный разбор, не перекашивая в сторону "
        "только успехов или только неудач."
    ),
}

_TITLE_RE = re.compile(r"ЗАГОЛОВОК:\s*(.+?)\s*(?:\n|$)")
_BODY_RE = re.compile(r"СТАТЬЯ:\s*(.+)", re.DOTALL)
_TICKER_TAG_RE = re.compile(r"\$([A-Za-z][A-Za-z0-9]{0,9})\b")

# Binance Square сам парсит $TICKER в тексте как "монетный тег" и
# у постов есть лимит на их количество ("Coin pair count exceeds the
# allowed limit"). У статьи с разбором 3-5 сигналов это легко превышается,
# если LLM упомянёт мимоходом ещё пару тикеров во вступлении/выводе.
# Поэтому после генерации оставляем как "$TICKER" только первые
# MAX_COIN_TAGS уникальных тикеров (по порядку первого появления в
# тексте - это обычно и есть самые заметные сигналы недели), у всех
# остальных упоминаний просто убираем "$", текст по смыслу не меняется.
MAX_COIN_TAGS = 3


def _limit_coin_tags(text: str, max_tags: int = MAX_COIN_TAGS) -> str:
    kept: set[str] = set()

    def _replace(match: re.Match) -> str:
        ticker = match.group(1).upper()
        if ticker in kept:
            return match.group(0)
        if len(kept) < max_tags:
            kept.add(ticker)
            return match.group(0)
        return ticker  # убираем "$", чтобы Binance не считал это тегом монеты

    return _TICKER_TAG_RE.sub(_replace, text)

# Сколько фактов максимум отправляем в промпт. История (queue_manager)
# может накопить до _HISTORY_MAX_ENTRIES=200 записей за неделю активного
# бота - присылать все 200 в Groq бессмысленно (статье нужно только
# 3-5 самых заметных, см. _SYSTEM_PROMPT) и опасно: это легко даёт
# несколько тысяч input-токенов ОДНИМ запросом и пробивает TPM-лимит
# Groq за раз (см. скрин с пиком токенов прямо на линии Rate Limit).
# Полная история всё равно используется отдельно в validate_article_text -
# усечение касается только того, что видит LLM.
_MAX_FACTS_IN_PROMPT = 20


def _select_facts_for_prompt(history: list[dict]) -> list[dict]:
    """Берёт самые заметные записи (по score), не больше _MAX_FACTS_IN_PROMPT -
    остальные не теряются (полная история всё равно используется при
    валидации и при анализе композиции недели), просто не едут в промпт."""
    if len(history) <= _MAX_FACTS_IN_PROMPT:
        return history
    return sorted(history, key=lambda h: float(h.get("score", 0)), reverse=True)[:_MAX_FACTS_IN_PROMPT]


def _analyze_week_composition(history: list[dict]) -> str:
    """Определяет, как сложилась неделя: 'mostly_wins' (преимущественно
    лонг-сигналы), 'mostly_losses' (преимущественно шорт-сигналы) или
    'mixed' - по доле сигналов с направлением 'лонг' в истории."""
    if not history:
        return "mixed"

    long_count = sum(1 for h in history if "лонг" in h.get("direction", "").lower())
    ratio = long_count / len(history)

    if ratio >= 0.65:
        return "mostly_wins"
    if ratio <= 0.35:
        return "mostly_losses"
    return "mixed"


def _format_facts(history: list[dict]) -> str:
    lines = []
    for h in history:
        lines.append(
            f"- ${h['ticker']} | {h['timeframe']} | {h.get('direction', '')} | "
            f"{h.get('strategy', '')} | {h['change_pct']} | score {h['score']}"
        )
    return "\n".join(lines)


def _parse_title_and_body(raw: str) -> tuple[str, str]:
    """Разбирает один ответ Groq по формату 'ЗАГОЛОВОК: ...\\nСТАТЬЯ:\\n...'
    (раньше заголовок и текст генерировались ДВУМЯ отдельными запросами -
    объединили в один, чтобы расходовать вдвое меньше квоты Groq на
    статью). Если модель не выдержала формат (бывает) - запасной план:
    первая строка считается заголовком, всё остальное - телом статьи."""
    title_match = _TITLE_RE.search(raw)
    body_match = _BODY_RE.search(raw)
    if title_match and body_match:
        title = title_match.group(1).strip().strip('"').strip("«»")
        body_hook = body_match.group(1).strip()
        return title[:70], body_hook

    lines = raw.strip().splitlines()
    title = lines[0].strip().strip('"').strip("«»") if lines else "Итоги недели"
    body_hook = "\n".join(lines[1:]).strip() or raw.strip()
    return title[:70], body_hook


def generate_weekly_article(history: list[dict]) -> Optional[tuple[str, str, list[dict]]]:
    """
    Возвращает (title, body_text, history) или None, если истории
    недостаточно для статьи (например, бот только что запущен и
    данных за неделю ещё не накопилось).

    Поднимает groq_client.GroqRateLimited при 429 от Groq - вызывающий
    код (main.py) ловит это отдельно от прочих ошибок, чтобы выставить
    backoff по Retry-After, а не на глазок.
    """
    if len(history) < 3:
        logger.info("Недостаточно данных для статьи (%d записей за неделю) - пропускаю", len(history))
        return None

    facts = _format_facts(_select_facts_for_prompt(history))
    composition = _analyze_week_composition(history)
    system_prompt = f"{_SYSTEM_PROMPT}\n\n{_COMPOSITION_EMPHASIS[composition]}"

    raw = call_groq(system_prompt, f"Факты за неделю:\n{facts}", max_tokens=700)
    title, body_hook = _parse_title_and_body(raw)

    body = assemble_post(body_hook)
    body = _limit_coin_tags(body)
    logger.info(
        "Сгенерирована статья: %s (фактов: %d, композиция недели: %s)",
        title, len(history), composition,
    )
    return title, body, history


def generate_cover_image() -> Optional[Path]:
    """Обложка статьи - график BTC за последнюю неделю."""
    return generate_chart_image("BTC", days=7)


def validate_article_text(title: str, body: str, history: list[dict]) -> tuple[bool, str]:
    """
    Проверяем, что числа в статье соответствуют истории - то есть
    каждое число, упомянутое в тексте, должно встречаться хотя бы
    в одном из фактов истории (защита от придуманных LLM цифр).
    """
    if not title.strip():
        return False, "Пустой заголовок статьи"

    if DISCLAIMER.lower() not in body.lower():
        return False, "В статье отсутствует дисклеймер"

    # Все числа, которые разрешены - score и % из истории
    allowed_numbers = set()
    for h in history:
        allowed_numbers.add(float(h["score"]))
        try:
            allowed_numbers.add(float(h["change_pct"].rstrip("%")))
        except ValueError:
            pass

    found_numbers = {float(n) for n in re.findall(r"[+-]?\d+\.?\d*", body)}
    unknown = [n for n in found_numbers if not any(abs(n - a) < 1e-6 for a in allowed_numbers)]

    if unknown:
        return False, f"В статье есть числа, не из истории дайджестов: {unknown}"

    cliche_ok, found = cliche_filter.check_cliches(body)
    if not cliche_ok:
        return False, f"В статье есть шаблонные ИИ-фразы: {', '.join(found)}"

    return True, ""
