"""
okx_orbit_generator.py - генерация постов под OKX Orbit Creator Rewards.

ВАЖНО: у OKX Orbit нет публичного API для публикации - в отличие от
Binance Square, эти посты НЕ публикуются автоматически.
okx_draft_publisher.py отправляет готовый текст (и картинку, если есть)
владельцу в Telegram, публикация - вручную через приложение OKX. Именно
поэтому здесь нет ни $CASHTAG-механики, ни хэштегов - это чужая
площадка со своими правилами дискаверабилити, которых мы не знаем
наверняка, поэтому текст максимально нейтральный и самодостаточный.

Два формата, ротируются между собой (см. pick_format) - выбраны так,
чтобы попадать в категории, которые OKX сам называет приоритетными для
начисления наград (market analysis, trading insights, market commentary):

1. "market_take" - развёрнутое наблюдение/мнение о движении рынка.
2. "trading_insight" - короткое, конкретное наблюдение по сетапу/уровню
   без явного сигнала на сделку.

Реальные числа (% изменения, амплитуда) считаются кодом через
market_stats.calc_theme_stats - LLM получает готовые цифры и пишет
вокруг них, чтобы не выдумывать данные.
"""
import logging
from pathlib import Path
from typing import Optional

import cliche_filter
import config
import voice_guidelines
from chart_generator import generate_chart_image
from groq_client import call_groq
from market_stats import THEMES, calc_theme_stats, pick_theme
from post_format import DISCLAIMER
import voice_memory

logger = logging.getLogger(__name__)

FORMATS = ("market_take", "trading_insight")

_MARKET_TAKE_SYSTEM_PROMPT = """Ты - независимый криптотрейдер, который
публикует авторский разбор рынка на OKX Orbit (соцплатформа для крипто-
контент-мейкеров). Пиши развёрнутое наблюдение о движении рынка: 4-6
предложений, разговорный тон, без канцелярита и без пустых вводных
фраз. Дай контекст (на фоне чего это движение, что это может значить),
не просто констатируй цифру.

Тебе дан НАБОР реальных чисел (см. задание) - используй их ТОЧНО как
дано, не округляй и не придумывай других чисел. Можно использовать не
все числа из набора, но НЕЛЬЗЯ упоминать чисел, которых там нет.

ЗАПРЕЩЕНО: конкретные уровни входа/стопа/тейка, прямые призывы
"покупайте"/"продавайте" - это авторское наблюдение, а не сигнал на
сделку и не финансовая рекомендация.

Не упоминай Binance или любую конкретную биржу. Не добавляй сам никакой
дисклеймер - он будет добавлен отдельно после твоего текста. Отвечай
только текстом поста, без пояснений и без кавычек.""" + voice_guidelines.STYLE_DIRECTIVE

_TRADING_INSIGHT_SYSTEM_PROMPT = """Ты - независимый криптотрейдер,
который публикует короткие трейдинг-инсайты на OKX Orbit (соцплатформа
для крипто-контент-мейкеров). 2-4 предложения: конкретное наблюдение по
рынку (например, про волатильность, диапазон, силу/слабость движения) -
без воды, по делу, как заметка в блокноте трейдера, а не общий обзор.

Тебе дан НАБОР реальных чисел (см. задание) - используй их ТОЧНО как
дано, не округляй и не придумывай других чисел.

ЗАПРЕЩЕНО: конкретные уровни входа/стопа/тейка, прямые призывы
"покупайте"/"продавайте сейчас" - это наблюдение, а не сигнал на сделку.

Не упоминай Binance или любую конкретную биржу. Не добавляй сам никакой
дисклеймер - он будет добавлен отдельно после твоего текста. Отвечай
только текстом поста, без пояснений и без кавычек.""" + voice_guidelines.STYLE_DIRECTIVE


def pick_format(last_format: Optional[str]) -> str:
    """Выбирает формат, отличный от последнего использованного - тот же
    принцип ротации, что и market_stats.pick_theme."""
    import random
    formats = list(FORMATS)
    if last_format in formats and len(formats) > 1:
        formats = [f for f in formats if f != last_format]
    return random.choice(formats)


def _build_user_prompt(theme: str, stats: dict, task_line: str) -> tuple[str, set[float], float]:
    label = THEMES[theme]["label"]

    if "single" in stats:
        s = stats["single"]
        sign = "+" if s["pct"] >= 0 else ""
        user_prompt = (
            f"Тема: {label}\n"
            f"Изменение цены за последние 48 часов (2 дня): {sign}{s['pct']}%.\n"
            f"Амплитуда колебаний за это время (high-low в % от начальной цены): {s['amplitude_pct']}%.\n"
            f"Текущая цена: ${s['current_price']:.2f} (пиши без разделителей тысяч, как дано).\n\n"
            f"Если упоминаешь период времени в тексте - используй ТОЧНО '48 часов' или "
            f"'2 дня', не пересчитывай и не округляй по-своему (не '24 часа', не 'сутки').\n\n"
            f"{task_line}"
            f"{voice_memory.anti_repeat_block('okx_orbit')}"
            f"{voice_memory.continuity_block('okx_orbit', theme, label)}"
        )
        # 48 и 2 разрешены отдельно от рыночных данных - промпт выше сам
        # прямо просит модель писать именно "48 часов" или "2 дня" для
        # периода времени, так что эти числа - не "выдуманные" в том
        # смысле, который проверка ниже призвана ловить.
        allowed_numbers = {s["pct"], s["amplitude_pct"], round(s["current_price"], 2), 48.0, 2.0}
        old_pct = voice_memory.continuity_pct("okx_orbit", theme)
        if old_pct is not None:
            allowed_numbers.add(old_pct)
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
            f"Если упоминаешь период времени в тексте - используй ТОЧНО '48 часов' или "
            f"'2 дня', не пересчитывай и не округляй по-своему (не '24 часа', не 'сутки').\n\n"
            f"{task_line}"
            f"{voice_memory.anti_repeat_block('okx_orbit')}"
            f"{voice_memory.continuity_block('okx_orbit', theme, label)}"
        )
        # См. комментарий в ветке "single" выше - 48/2 разрешены отдельно,
        # промпт сам просит модель писать именно эти числа для периода.
        allowed_numbers = set(stats["breakdown"].values()) | {avg, 48.0, 2.0}
        old_pct = voice_memory.continuity_pct("okx_orbit", theme)
        if old_pct is not None:
            allowed_numbers.add(old_pct)
        headline_pct = avg

    return user_prompt, allowed_numbers, headline_pct


def generate_okx_orbit_post(theme: str, format_type: str) -> Optional[tuple]:
    """Возвращает (текст поста, набор разрешённых чисел, headline_pct,
    format_type), либо None, если не удалось получить данные по теме."""
    stats = calc_theme_stats(theme)
    if stats is None:
        return None

    if format_type == "market_take":
        system_prompt = _MARKET_TAKE_SYSTEM_PROMPT
        task_line = "Напиши развёрнутое авторское наблюдение об этом движении рынка."
        max_tokens, temperature = 500, 0.9
    else:
        system_prompt = _TRADING_INSIGHT_SYSTEM_PROMPT
        task_line = "Напиши короткий трейдинг-инсайт по этому движению - конкретное наблюдение, а не общий обзор."
        max_tokens, temperature = 280, 0.85

    user_prompt, allowed_numbers, headline_pct = _build_user_prompt(theme, stats, task_line)

    hook = call_groq(system_prompt, user_prompt, max_tokens=max_tokens,
                      temperature=temperature, model=config.GROQ_MODEL_SECONDARY)

    if len(hook.strip()) < 10:
        logger.warning("Хук OKX Orbit-поста пустой или слишком короткий (%r) - пропускаю окно", hook)
        return None

    text = f"{hook.strip()}\n\n{DISCLAIMER}"
    logger.info("Сгенерирован OKX Orbit-пост (тема %s, формат %s, числа: %s)", theme, format_type, allowed_numbers)
    return text, allowed_numbers, headline_pct, format_type


def validate_okx_orbit_post_text(text: str, allowed_numbers: set[float]) -> tuple[bool, str]:
    """Числа только из посчитанных данных, дисклеймер на месте, без
    ИИ-штампов."""
    import re

    numbers = {float(n) for n in re.findall(r"[+-]?\d+\.?\d*", text.replace(",", ""))}
    unknown = [n for n in numbers if not any(abs(n - a) < 0.05 for a in allowed_numbers)]
    if unknown:
        return False, f"В тексте есть числа, не из посчитанных данных: {unknown}"

    if DISCLAIMER.lower() not in text.lower():
        return False, "В тексте отсутствует дисклеймер"

    cliche_ok, found = cliche_filter.check_cliches(text)
    if not cliche_ok:
        return False, f"В тексте есть шаблонные ИИ-фразы: {', '.join(found)}"

    return True, ""


def generate_chart_for_post(theme: str) -> Optional[Path]:
    """Возвращает путь к PNG-графику для темы, БЕЗ водяного знака
    BINANCE - график для чужой площадки не должен выглядеть как
    скриншот с Binance.

    Для темы 'market' (несколько тикеров в корзине) строим график по
    первому тикеру корзины как представительный.
    """
    ticker = THEMES[theme]["tickers"][0]
    return generate_chart_image(ticker, days=2, watermark_text=None, filename_suffix="_okx")
