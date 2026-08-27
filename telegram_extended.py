"""
telegram_extended.py - формат "Разбор без купюр" (Telegram).

В отличие от Bluesky (тизер/тред-разбор частями) и Binance Square
(короткий, самодостаточный сигнал) - Telegram-канал уже читает тёплая,
подписанная аудитория, которая пришла именно за глубиной, а не за
скоростью. Здесь к тому же хуку и тому же сетапу (вход/стоп/тейк/RSI/
score - идентичные Square, без риска расхождения, см. post_format.
signal_setup_lines) добавляется ЕЩЁ один блок - "Контекст": почему
именно этот уровень имеет значение, что означает такое RSI в связке с
этой стратегией, и, если данных достаточно, историческая точность
именно этой стратегии (реальные цифры из outcome_tracker, не выдумка).

Это и есть содержательная причина оставаться именно в Telegram, а не
просто читать более короткую версию того же сигнала на Square/Bluesky -
reciprocity: чем больше настоящей, обоснованной пользы человек получает
бесплатно именно тут, тем сильнее держится за канал как источник.

Генерация блока "Контекст" - НЕ обязательный шаг: если LLM вернула
пусто/не прошла проверку чисел, main.py тихо публикует обычный (без
блока) текст - расширенный разбор это бонус, а не условие публикации.
"""
import logging
import re
from typing import Optional

import cliche_filter
import config
from groq_client import call_groq
import outcome_tracker
import voice_guidelines

logger = logging.getLogger(__name__)

# Меньше сэмплов - статистика по стратегии слишком шаткая, чтобы её
# показывать как аргумент ("историческая точность 100%, n=1" вводит в
# заблуждение сильнее, чем полное отсутствие цифры).
MIN_STRATEGY_SAMPLES = 5
STRATEGY_STATS_WINDOW_DAYS = 60

_SYSTEM_PROMPT = """Ты пишешь короткий блок "Контекст" для Telegram-канала
трейдинг-сигналов - он идёт ПОСЛЕ хука и сетапа (вход/стоп/тейк читатель
уже видел выше, не повторяй их и не дублируй хук). Объясни 2-4
предложениями, почему этот сетап заслуживает внимания - например, что
означает именно такое значение RSI для этой стратегии, чем этот случай
отличается от обычного шума.

Тебе даны реальные числа сигнала и, если есть, историческая статистика
по стратегии - используй их ТОЧНО как даны, не придумывай других чисел
и не давай гарантий на будущее ("значит, точно сработает").

Не добавляй дисклеймер - он будет добавлен отдельно после твоего блока.
Разговорный, но содержательный тон, без канцелярита. Отвечай только
текстом блока, без пояснений и без кавычек.""" + voice_guidelines.STYLE_DIRECTIVE


def _to_float(value) -> float:
    try:
        return round(float(str(value).replace("%", "").strip()), 2)
    except (TypeError, ValueError):
        return 0.0


def generate_extended_context(signal, hook: str) -> Optional[tuple]:
    """Возвращает (текст блока "Контекст", allowed_numbers), либо None,
    если генерация не удалась содержательно (пустой/слишком короткий
    ответ - call_groq уже перезапрашивает пустые ответы сам, это
    подстраховка на крайний случай)."""
    stats = outcome_tracker.get_accuracy_stats(days=STRATEGY_STATS_WINDOW_DAYS)
    strategy_stats = stats["by_strategy"].get(signal.strategy)

    rsi_now = _to_float(signal.rsi_now)
    score = _to_float(signal.score)
    # 100.0 - не факт из данных, а фиксированная шкала score ("89 из 100") -
    # без неё любое упоминание "из 100" ложно ловилось бы как чужое число.
    allowed_numbers = {rsi_now, score, 100.0}

    facts = [
        f"RSI сейчас: {signal.rsi_now}",
        f"Score сетапа: {signal.score}/100",
        f"Стратегия: {signal.strategy}",
        f"Направление: {signal.direction}",
    ]

    if strategy_stats and strategy_stats["count"] >= MIN_STRATEGY_SAMPLES:
        wr = strategy_stats["win_rate"]
        n = strategy_stats["count"]
        facts.append(
            f"Историческая точность стратегии '{signal.strategy}' за {STRATEGY_STATS_WINDOW_DAYS} дней: "
            f"win-rate {wr}% (n={n})"
        )
        if wr is not None:
            allowed_numbers.add(wr)
        allowed_numbers.add(float(n))
        allowed_numbers.add(float(STRATEGY_STATS_WINDOW_DAYS))

    user_prompt = (
        "Хук поста (для контекста, не повторяй дословно):\n" + hook.strip() + "\n\n"
        "Факты:\n" + "\n".join(facts) + "\n\n"
        "Напиши блок 'Контекст'."
    )

    context = call_groq(_SYSTEM_PROMPT, user_prompt, max_tokens=450, temperature=0.8, model=config.GROQ_MODEL_SECONDARY)

    if len(context.strip()) < 15:
        logger.warning("Блок 'Контекст' пустой/слишком короткий (%r) - публикую без него", context)
        return None

    return context.strip(), allowed_numbers


def validate_extended_context(text: str, allowed_numbers: set) -> tuple:
    """Числа в блоке должны быть подмножеством того, что мы сами
    посчитали/передали (allowed_numbers) - та же логика, что и в
    hot_take_generator/volatility_alert. Допуск 0.1, шире, чем в
    остальных валидаторах (0.05) - здесь встречаются проценты
    win-rate с одним знаком после запятой, где LLM иногда слегка иначе
    округляет при пересказе."""
    numbers = {float(n) for n in re.findall(r"[+-]?\d+\.?\d*", text.replace(",", ""))}
    unknown = [n for n in numbers if not any(abs(n - a) < 0.1 for a in allowed_numbers)]
    if unknown:
        return False, f"В блоке 'Контекст' есть числа, не подтверждённые данными: {unknown}"

    cliche_ok, found = cliche_filter.check_cliches(text)
    if not cliche_ok:
        return False, f"В блоке 'Контекст' есть шаблонные ИИ-фразы: {', '.join(found)}"

    return True, ""
