"""
Генератор поста "личное мнение" - публикуется раз в 2 дня (+- джиттер).

Тема ротируется между трёми вариантами, чтобы не быть всегда про BTC:
- "BTC" - движение цены BTC за 2 дня
- "ETH" - движение цены ETH за 2 дня
- "market" - средний % изменения по корзине топовых монет (BTC, ETH,
  SOL, BNB) - проще, чем тащить отдельный market-cap индекс, но даёт
  ощущение "рынок в целом", а не один актив

Во всех случаях % считаем сами по данным Binance (chart_generator.
fetch_klines), а не доверяем LLM придумывать цифры - LLM получает
готовое число и пишет вокруг него личную рефлексию.
"""
import logging
import random
from typing import Optional

import cliche_filter
import config
import requests
import voice_memory

from chart_generator import fetch_klines
from groq_client import call_groq
from post_format import DISCLAIMER, HOOK_MODES, assemble_post
import voice_guidelines

logger = logging.getLogger(__name__)

THEMES: dict[str, dict] = {
    "BTC": {"label": "$BTC", "tickers": ["BTC"]},
    "ETH": {"label": "$ETH", "tickers": ["ETH"]},
    "market": {"label": "крипторынок в целом (по корзине BTC/ETH/SOL/BNB)", "tickers": ["BTC", "ETH", "SOL", "BNB"]},
}

_SYSTEM_PROMPT = """Ты пишешь личный пост-мнение для Binance Square, в
разговорном фирменном стиле автора - живая реакция человека, который
следит за рынком, а не сухая аналитика. 4-6 предложений, можно с
риторическим вопросом, эмодзи (1-2), без воды - но за счёт длины дай
больше контекста и личной рефлексии, чем просто констатация факта.

Тебе дан НАБОР реальных чисел (см. задание) - используй их ТОЧНО как
дано, не округляй и не придумывай других чисел. Можно использовать не
все числа из набора, если не нужно для текста, но НЕЛЬЗЯ упоминать
числа, которых там нет.

Структура (свободно, не как шаблон):
- зацепка с конкретной цифрой
- что это может значить / на фоне чего это произошло (без выдумывания
  новостей - просто рыночная рефлексия, "похоже на...", "не первый раз
  когда...")
- лёгкий вопрос к читателю или личный вывод

НЕ добавляй сам никакой дисклеймер - это будет добавлено отдельно
после твоего текста.

Отвечай только текстом поста, без пояснений и без кавычек.""" + voice_guidelines.STYLE_DIRECTIVE


def pick_theme(last_theme: Optional[str]) -> str:
    """Выбирает тему, отличную от последней использованной."""
    themes = list(THEMES.keys())
    if last_theme in themes and len(themes) > 1:
        themes = [t for t in themes if t != last_theme]
    return random.choice(themes)


def calc_ticker_stats(ticker: str) -> Optional[dict]:
    """Реальные числа по тикеру за последние 2 дня: % изменения,
    амплитуда (high-low в % от открытия) и текущая цена. Всё считаем
    сами по тем же данным CoinGecko, без участия LLM.

    Публичная функция (без ведущего "_") - переиспользуется
    hot_take_generator.py для формата "Хот-тейк" (та же логика "дай LLM
    готовое реальное число, а не пусть придумывает").
    """
    try:
        klines = fetch_klines(ticker, days=2)
    except requests.RequestException as e:
        logger.warning("Не удалось получить данные %s для поста-мнения: %s", ticker, e)
        return None

    if len(klines) < 2:
        return None

    opens = [float(k["open"]) for k in klines]
    highs = [float(k["high"]) for k in klines]
    lows = [float(k["low"]) for k in klines]
    closes = [float(k["close"]) for k in klines]

    open_price, close_price = opens[0], closes[-1]
    if open_price == 0:
        return None

    pct = round((close_price - open_price) / open_price * 100, 2)
    amplitude_pct = round((max(highs) - min(lows)) / open_price * 100, 2)
    return {"pct": pct, "amplitude_pct": amplitude_pct, "current_price": close_price}


def calc_theme_stats(theme: str) -> Optional[dict]:
    """Для одного тикера (BTC/ETH) - полный набор (pct/амплитуда/цена).
    Для 'market' - % по каждому активу корзины + средний % по корзине
    (амплитуду и цену для разнородной корзины не считаем - бессмысленно
    усреднять цену BTC и SOL).

    Публичная функция - переиспользуется hot_take_generator.py."""
    tickers = THEMES[theme]["tickers"]

    if len(tickers) == 1:
        stats = calc_ticker_stats(tickers[0])
        if stats is None:
            return None
        return {"single": stats}

    breakdown = {}
    for t in tickers:
        stats = calc_ticker_stats(t)
        if stats is not None:
            breakdown[t] = stats["pct"]

    if not breakdown:
        return None

    avg_pct = round(sum(breakdown.values()) / len(breakdown), 2)
    return {"breakdown": breakdown, "avg_pct": avg_pct}


def generate_opinion_post(theme: str, hook_mode: Optional[str] = None) -> Optional[tuple]:
    """Возвращает (готовый текст поста, набор разрешённых чисел для
    проверки), либо None, если не удалось получить данные.

    hook_mode - один из post_format.HOOK_MODES (тот же словарь "ярких
    авторских голосов", что используется для валютных сигналов в
    text_generator.py) - пост-мнение как раз то место, где выраженная
    личная интонация уместнее всего. Если None (например, старый код
    вызывает без параметра) - используется нейтральный системный промпт
    без персоны, как раньше."""
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
            f"Изменение цены за последние 48 часов (2 дня): {sign}{s['pct']}%.\n"
            f"Амплитуда колебаний за это время (high-low в % от начальной цены): {s['amplitude_pct']}%.\n"
            f"Текущая цена: ${s['current_price']:.2f} (пиши без разделителей тысяч, как дано).\n\n"
            f"Если упоминаешь период времени в тексте - используй ТОЧНО '48 часов' или "
            f"'2 дня', не пересчитывай и не округляй по-своему (не '24 часа', не 'сутки').\n\n"
            f"Напиши личное мнение/наблюдение об этом движении рынка."
        )
        # 48 и 2 разрешены отдельно от рыночных данных - промпт выше сам
        # прямо просит модель писать именно "48 часов" или "2 дня" для
        # периода времени, так что эти числа - не "выдуманные" в том
        # смысле, который validate_opinion_post_text призвана ловить.
        allowed_numbers = {s["pct"], s["amplitude_pct"], round(s["current_price"], 2), 48.0, 2.0}
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
            f"Напиши личное мнение/наблюдение об этом движении рынка - можно "
            f"упомянуть как отдельные активы, так и общую картину."
        )
        # См. комментарий в ветке "single" выше - 48/2 разрешены отдельно,
        # промпт сам просит модель писать именно эти числа для периода.
        allowed_numbers = set(stats["breakdown"].values()) | {avg, 48.0, 2.0}
        headline_pct = avg

    user_prompt += voice_memory.anti_repeat_block() + voice_memory.continuity_block(theme, label)

    hook = call_groq(system_prompt, user_prompt, max_tokens=500, temperature=0.9, model=config.GROQ_MODEL_SECONDARY)

    # Хук не должен быть пустым/почти пустым - без этой проверки
    # assemble_post() тихо собрал бы пост из одного дисклеймера, без
    # единой мысли автора (тот же баг, что был у index_signal/treasury/
    # accuracy_report/loss_review - см. их validate_*_hook). call_groq
    # уже перезапрашивает пустые ответы сам, это - подстраховка.
    if len(hook.strip()) < 10:
        logger.warning("Хук поста-мнения пустой или слишком короткий (%r) - публикую с нейтральным хуком", hook)
        hook = f"{label}: как вам последнее движение? 🤔"

    text = assemble_post(hook)
    logger.info("Сгенерирован пост-мнение (тема %s, числа: %s): %s", theme, allowed_numbers, text)
    return text, allowed_numbers, headline_pct


def validate_opinion_post_text(text: str, allowed_numbers: set[float]) -> tuple[bool, str]:
    """Проверяем, что числа в тексте - подмножество тех, что мы сами
    посчитали (allowed_numbers), и что дисклеймер на месте. Текст не
    обязан использовать ВСЕ числа из набора, но не может содержать
    числа, которых там нет."""
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