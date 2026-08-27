"""
volatility_alert.py - формат "Экстренный" (Bluesky).

Не привязан к конкретному сигналу/тикеру - реагирует на резкое движение
рынка В ЦЕЛОМ, используя $BTC как прокси (стандартная практика: в
моменты паники/эйфории корреляция альткоинов с BTC обычно самая
высокая, а BTC - самый ликвидный и предсказуемо котируемый актив).

Триггерится, если цена BTC изменилась больше чем на
config.VOLATILITY_ALERT_THRESHOLD_PCT за последние
config.VOLATILITY_ALERT_WINDOW_HOURS часов - заметно резче обычного
дневного шума. Такие моменты сами по себе собирают органическое
внимание в ленте (все ищут, что происходит) - грех не воспользоваться,
но без прогнозов и без сигналов на сделку, чисто реакция в моменте.

В отличие от остальных Bluesky-форматов (opinion/hot_take/mini_lesson) -
проверяется на КАЖДОМ тике (см. main.try_publish_emergency_post), а не
по расписанию с интервалом - у волатильности нет фиксированного времени,
реагировать нужно как можно быстрее после реального скачка. Повторные
срабатывания ограничены кулдауном (config.EMERGENCY_COOLDOWN_HOURS), а
не джиттером.

Переиспользует те же часовые свечи, что и chart_generator (без
отдельного API-эндпоинта) - экономим лишний запрос к Binance.
"""
import logging
import re
from typing import Optional

import cliche_filter
import config
from chart_generator import fetch_klines
from groq_client import call_groq
from post_format import BLUESKY_CHAR_LIMIT, DISCLAIMER
import voice_guidelines

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """Ты пишешь короткий "экстренный" пост для Bluesky -
рынок только что резко двинулся, и ты реагируешь на это в моменте,
разговорно и энергично, как будто увидел это только что. 1-3 предложения,
без воды.

Тебе дано ТОЧНОЕ число - используй его КАК ДАНО, не округляй и не
придумывай других чисел.

ЖЁСТКО ЗАПРЕЩЕНО:
- предсказывать, что будет дальше ("значит, теперь будет рост/падение");
- давать сигнал на сделку или конкретные уровни входа/стопа/тейка - это
  реакция на факт движения, а не рекомендация.

Лимит - 200 символов на весь ответ, уложись сам. Без хэштегов и ссылок -
добавятся отдельно. Отвечай только текстом реакции, без пояснений и
без кавычек.""" + voice_guidelines.STYLE_DIRECTIVE

# Резервируем место под "🚨 " в начале и "\n\n" + DISCLAIMER в конце.
_MAX_TAKE_CHARS = BLUESKY_CHAR_LIMIT - len(DISCLAIMER) - 2 - len("🚨 ")


def detect_market_volatility_spike() -> Optional[dict]:
    """Возвращает {"pct", "direction" ("up"/"down"), "window_hours"},
    либо None, если данных недостаточно или движение недостаточно
    резкое (меньше config.VOLATILITY_ALERT_THRESHOLD_PCT)."""
    candles = fetch_klines("BTC", days=2)  # часовые свечи за 48ч
    window_hours = config.VOLATILITY_ALERT_WINDOW_HOURS

    if len(candles) < window_hours + 1:
        logger.warning("Недостаточно свечей BTC для проверки волатильности (%d)", len(candles))
        return None

    window = candles[-(window_hours + 1):]
    start_price = window[0]["close"]
    end_price = window[-1]["close"]
    if start_price <= 0:
        return None

    pct = round((end_price - start_price) / start_price * 100, 2)
    if abs(pct) < config.VOLATILITY_ALERT_THRESHOLD_PCT:
        return None

    return {"pct": pct, "direction": "up" if pct >= 0 else "down", "window_hours": window_hours}


def generate_emergency_post(spike: dict) -> Optional[str]:
    """Возвращает готовый текст поста (с 🚨 и дисклеймером), либо None,
    если генерация не удалась содержательно."""
    direction_word = "вырос" if spike["direction"] == "up" else "упал"
    sign = "+" if spike["pct"] >= 0 else ""
    user_prompt = (
        f"$BTC только что {direction_word} на {sign}{spike['pct']}% за последние "
        f"{spike['window_hours']} часа(ов). Напиши короткую живую реакцию в моменте."
    )

    take = call_groq(_SYSTEM_PROMPT, user_prompt, max_tokens=180, temperature=0.9, model=config.GROQ_MODEL_SECONDARY)

    if len(take.strip()) < 10:
        logger.warning("Экстренный пост пустой/слишком короткий (%r) - пропускаю", take)
        return None

    take = take.strip()
    if len(take) > _MAX_TAKE_CHARS:
        take = take[: _MAX_TAKE_CHARS - 1].rstrip() + "…"

    text = f"🚨 {take}\n\n{DISCLAIMER}"
    logger.info("Сгенерирован экстренный пост (%.2f%% за %dч): %s", spike["pct"], spike["window_hours"], text)
    return text


def validate_emergency_post(text: str, spike: dict) -> tuple:
    """Проверяем числа (только pct и window_hours из spike разрешены, в
    любом знаке/округлении в пределах 0.05), дисклеймер и лимит длины -
    те же правила, что и у hot_take_generator.validate_hot_take."""
    allowed_numbers = {spike["pct"], abs(spike["pct"]), float(spike["window_hours"])}

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
