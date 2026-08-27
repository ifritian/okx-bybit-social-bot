"""
loss_review_generator.py - честный разбор неудачных сигналов за период
(раз в config.LOSS_REVIEW_INTERVAL_HOURS, по умолчанию 4 дня).

Идея та же, что в accuracy_report_generator и treasury_generator: сам
блок с конкретными случаями (тикер, направление, стратегия, где сработал
стоп, % результата) собирается ПОЛНОСТЬЮ кодом из outcome_tracker
(Фаза 1) - LLM никогда не видит задачу "перечисли проигрышные сделки" и
не может ни исказить цифры, ни придумать сделку, которой не было.

ВАЖНОЕ ОТЛИЧИЕ от первой версии: теперь по каждому случаю есть РЕАЛЬНЫЙ
количественный признак того, что случилось - Max Favorable Excursion
(насколько цена всё-таки прошла в сторону тейка до разворота) и время
до срабатывания стопа (см. outcome_tracker._mfe_pct / _resolve_outcome).
На основе этих измеримых фактов код (не LLM!) относит каждый случай к
одной из трёх категорий - "близкий промах", "сразу пошло против" или
"стоп в рамках обычной волатильности". Это и есть "анализ причины" -
но основанный на факте движения цены, а не на догадке про новости или
манипуляции, которые никто не проверял.

LLM пишет только короткий хук поверх готового блока (с категориями и
цифрами) и по-прежнему НЕ должен придумывать внешние причины (новости,
манипуляции) - только опираться на данные категории и общую мысль о
риск-менеджменте.

Публикуется отдельно от еженедельного accuracy_report (Фаза 4, первая
часть) - там сводная статистика по всем сигналам, здесь - фокус
конкретно на промахах, с интервалом покороче (4 дня), подобранным так,
чтобы соседние отчёты почти не пересекались одними и теми же сделками.
"""
import logging
import re
import time

import cliche_filter
from groq_client import call_groq
import outcome_tracker
import post_format
import queue_manager
from validator import find_suspicious_english_words
import voice_guidelines

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """Ты пишешь короткий хук (вводную фразу) для честного
разбора неудачных сигналов канала за последние дни. Хук идёт ПЕРЕД
готовым блоком с конкретными случаями (тикер, направление, стратегия,
насколько цена прошла к тейку прежде чем развернуться, время до стопа,
% результата) - сам блок тебе показан только для контекста, повторять
его в ответе не нужно, он будет добавлен отдельно после твоего текста.

1-3 предложения, живой разговорный стиль на русском языке, без
канцелярита. Тон спокойный и честный, БЕЗ самобичевания и БЕЗ
оправданий.

В блоке ниже по каждому случаю указана КАТЕГОРИЯ, посчитанная из
реального движения цены (не догадка) - "близкий промах" (цена почти
дошла до тейка, но развернулась), "сразу пошло против" (почти не
продвинулась к цели) или "стоп в рамках обычной волатильности". В хуке
можно опереться на эти категории как на факт (например, если большинство
случаев - "близкий промах", можно сказать, что сетапы были близки к
успеху, но не дотянули) - это законное наблюдение по данным.

КРИТИЧЕСКИ ВАЖНО: НЕ придумывай ничего СВЕРХ данных категорий - никаких
внешних причин (новости, манипуляции рынком, чьи-то действия). Можно
упомянуть общую мысль о риск-менеджменте - что стопы для того и нужны,
чтобы ограничивать потери, когда сетап не срабатывает, даже если он был
близко к успеху.

Тебе дан набор реальных чисел (см. задание) - если используешь цифры в
хуке, то ТОЧНО как даны, не округляя иначе и не придумывая других.
Можно вообще не называть цифры в хуке (они и так есть в блоке ниже).

НЕ добавляй сам дисклеймер и НЕ дублируй блок с кейсами. Отвечай только
текстом хука на русском языке, без пояснений и без кавычек.""" + voice_guidelines.STYLE_DIRECTIVE

_NUMBER_RE = re.compile(r"[+-]?\d+\.?\d*")


def _extract_numbers(text: str) -> set[float]:
    return {round(float(n), 2) for n in _NUMBER_RE.findall(text.replace(",", ""))}


def classify_miss(loss: dict) -> str:
    """Категория промаха на основе РЕАЛЬНЫХ данных о пути цены (MFE,
    время до стопа) - измеримый факт, а не догадка модели о причине.
    progress_ratio - какую долю пути к тейку цена всё-таки прошла,
    прежде чем развернуться к стопу (может быть отрицательным, если
    цена сразу пошла против сделки, даже не приблизившись к входу)."""
    entry, target = loss.get("entry"), loss.get("target")
    mfe = loss.get("mfe_pct")
    hours = loss.get("hours_to_close")

    if mfe is None or not entry or not target or entry == target:
        return "недостаточно данных о пути цены для анализа"

    target_distance_pct = abs(target - entry) / entry * 100
    progress_ratio = mfe / target_distance_pct if target_distance_pct else 0

    if progress_ratio >= 0.6:
        label = f"близкий промах - цена прошла ~{progress_ratio * 100:.0f}% пути к тейку, но развернулась к стопу"
    elif progress_ratio <= 0.15:
        label = "сигнал сразу пошёл против сделки, почти не продвинувшись к цели"
    else:
        label = f"стоп сработал в рамках обычной волатильности (~{progress_ratio * 100:.0f}% пути к тейку)"

    if hours is not None:
        label += f", решилось за ~{hours:.1f}ч" if hours <= 6 else f", заняло ~{hours:.1f}ч"

    return label


def _format_losses_block(losses: list[dict], total_closed: int, days: float, max_shown: int = 5) -> str:
    lines = [f"📉 Разбор неудачных сигналов за последние {days:g} дней"]
    lines.append(f"Не сработало: {len(losses)} из {total_closed} закрытых сигналов за период")

    # Худшие по % результата - показываем не больше max_shown, чтобы пост
    # не разрастался, если убыточных случаев было много.
    worst = sorted(losses, key=lambda c: c["pnl_pct"])[:max_shown]
    lines.append("")
    for c in worst:
        direction_ru = "Лонг" if c["direction"] == "long" else "Шорт"
        lines.append(
            f"{c['ticker']} | {direction_ru} | {c.get('strategy', '?')} | "
            f"вход {c['entry']:g}, стоп {c['stop']:g} (сработал) | результат {c['pnl_pct']:+.2f}%"
        )
        lines.append(f"  -> {classify_miss(c)}")

    if len(losses) > max_shown:
        lines.append(f"...и ещё {len(losses) - max_shown}")

    return "\n".join(lines)


def generate_loss_review_post(days: float | None = None) -> tuple[str, str] | None:
    """Возвращает (текст для Binance Square, текст для кросспоста в
    Telegram) либо None, если убыточных сигналов за период недостаточно
    (меньше config.LOSS_REVIEW_MIN_LOSSES).

    Поднимает groq_client.GroqRateLimited при 429 - вызывающий код
    (main.py) уже умеет это ловить, как для accuracy_report/treasury/article."""
    import config

    days = days if days is not None else config.LOSS_REVIEW_LOOKBACK_DAYS

    closed = queue_manager.get_closed_outcomes()
    cutoff = time.time() - days * 24 * 3600
    period_closed = [c for c in closed if c.get("closed_at", 0) >= cutoff]
    losses = [c for c in period_closed if c.get("result") == "loss"]

    if len(losses) < config.LOSS_REVIEW_MIN_LOSSES:
        logger.info(
            "Недостаточно убыточных сигналов за %.1f дней (%d < %d) - пропускаю разбор промахов",
            days, len(losses), config.LOSS_REVIEW_MIN_LOSSES,
        )
        return None

    losses_block = _format_losses_block(losses, len(period_closed), days)
    allowed_numbers = _extract_numbers(losses_block) | {days}

    user_prompt = (
        f"Блок с кейсами целиком (для контекста, НЕ копируй его в ответ):\n{losses_block}\n\n"
        f"Напиши короткий честный хук, который встанет перед этим блоком."
    )

    hook = call_groq(_SYSTEM_PROMPT, user_prompt, max_tokens=200, temperature=0.8)

    ok, reason = validate_loss_review_hook(hook, allowed_numbers)
    if not ok:
        logger.warning("Хук разбора промахов не прошёл проверку (%s) - публикую с нейтральным хуком", reason)
        hook = "📉 Честный разбор: не все сигналы за этот период сработали, вот детали."

    text = "\n\n".join([hook.strip(), losses_block, post_format.DISCLAIMER])

    logger.info("Сгенерирован разбор промахов (n=%d из %d закрытых): %s",
                len(losses), len(period_closed), text[:150].replace("\n", " "))
    return text, text


def validate_loss_review_hook(hook: str, allowed_numbers: set[float]) -> tuple[bool, str]:
    """Та же логика, что validate_accuracy_hook/validate_treasury_hook:
    хук не должен содержать чисел вне уже посчитанных, и не должен
    содержать посторонних английских слов (см. Фазу "языковой баг").
    Также не должен быть пустым/почти пустым (см. тот же баг в
    index_signal_generator/treasury_generator/accuracy_report_generator)."""
    if len(hook.strip()) < 10:
        return False, f"Хук пустой или слишком короткий: {hook!r}"

    numbers = _extract_numbers(hook)
    unknown = [n for n in numbers if not any(abs(n - a) < 0.05 for a in allowed_numbers)]
    if unknown:
        return False, f"В хуке есть числа не из посчитанных данных: {unknown}"

    suspicious = find_suspicious_english_words(hook)
    if suspicious:
        return False, f"В хуке есть посторонние английские слова: {', '.join(suspicious[:5])}"

    cliche_ok, found = cliche_filter.check_cliches(hook)
    if not cliche_ok:
        return False, f"В хуке есть шаблонные ИИ-фразы: {', '.join(found)}"

    return True, ""
