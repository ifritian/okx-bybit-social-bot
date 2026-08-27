"""
index_signal_generator.py - хук для сигналов "удобно докупить/подрезать"
конкретно по монетам из Treasury Index (см. index_signal_scanner.py).

В отличие от text_generator.generate_post_text (сигналы по всему рынку,
подаются как самостоятельная спекулятивная сделка с Входом/Стопом/
Тейком), здесь акцент на УПРАВЛЕНИИ УЖЕ СУЩЕСТВУЮЩЕЙ КОРЗИНОЙ: сигнал
подаётся как удобный момент подкорректировать долю монеты в портфеле -
докупить на просадке (перепроданность) или частично зафиксировать на
перегреве (перекупленность). Числовой блок собирается
post_format.assemble_index_management_post - те же цифры, что в обычном
сигнале, но названы в терминах управления долей (см. эту функцию),
а не входа/стопа/тейка по сделке.
"""
import logging
import re

import cliche_filter
from groq_client import call_groq
import post_format
import signal_parser
from signal_parser import RsiSignal
import treasury_index
from validator import find_suspicious_english_words
import voice_guidelines

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """Ты пишешь короткий хук для поста об управлении долей
монеты из Treasury Index - собственного инфраструктурного крипто-индекса
канала (корзина из 15 монет, разбитая на тиры по риску). Тебе дан тикер,
направление (перекупленность/перепроданность) и тир/вес монеты в
корзине.

КЛЮЧЕВОЕ ОТЛИЧИЕ от обычного торгового сигнала: это НЕ разовая
спекулятивная сделка с входом/стопом/тейком, а рекомендация по
УПРАВЛЕНИЮ ДОЛЕЙ В ПОРТФЕЛЕ - докупить часть позиции на просадке
(перепроданность) или частично зафиксировать прибыль на перегреве
(перекупленность). Пиши именно в терминах доли/позиции/портфеля, а НЕ
"сделка", "вход", "стоп", "тейк" - этих слов быть не должно.

1-3 предложения, живой разговорный тон на русском языке, тикер как
$CASHTAG в начале, уместный эмодзи (не более 1-2).

НЕ упоминай конкретные числа/уровни/RSI - они будут добавлены отдельным
блоком после твоего текста. НЕ добавляй сам дисклеймер.

Отвечай только текстом хука, без пояснений и без кавычек.""" + voice_guidelines.STYLE_DIRECTIVE

_NUMBER_RE = re.compile(r"[+-]?\d+\.?\d*")
_MIN_HOOK_CHARS = 10


def _validate_hook(hook: str) -> tuple[bool, str]:
    """Хук вообще не должен содержать чисел (все цифры - в блоке ниже,
    который добавляется отдельно) и не должен содержать посторонних
    английских слов (см. Фазу с языковым багом).

    Также хук не должен быть пустым/почти пустым - без этой проверки
    LLM, изредка вернувший пустую строку, проходил бы валидацию молча
    (нет чисел, нет английских слов - формально "всё ок"), и в пост
    уходил один структурный блок без единой мысли автора (см. баг:
    пост без хука в ленте). groq_client.call_groq уже перезапрашивает
    пустые ответы сам, это - подстраховка на тот случай, если что-то
    всё равно проскочит."""
    if len(hook.strip()) < _MIN_HOOK_CHARS:
        return False, f"Хук пустой или слишком короткий: {hook!r}"

    numbers = _NUMBER_RE.findall(hook.replace(",", ""))
    if numbers:
        return False, f"В хуке есть числа, хотя их не должно быть: {numbers}"

    suspicious = find_suspicious_english_words(hook)
    if suspicious:
        return False, f"В хуке есть посторонние английские слова: {', '.join(suspicious[:5])}"

    cliche_ok, found = cliche_filter.check_cliches(hook)
    if not cliche_ok:
        return False, f"В хуке есть шаблонные ИИ-фразы: {', '.join(found)}"

    return True, ""


def _fallback_hook(signal: RsiSignal) -> str:
    if signal_parser.is_long_direction(signal.direction):
        return f"${signal.ticker} даёт сигнал ({signal.strategy}) - в рамках Treasury Index может быть удобный момент для докупки доли."
    return f"${signal.ticker} даёт сигнал ({signal.strategy}) - в рамках Treasury Index можно рассмотреть частичную фиксацию доли."


def generate_index_signal_post(signal: RsiSignal) -> str:
    """Поднимает groq_client.GroqRateLimited при 429 - main.py уже умеет
    это ловить (как для accuracy_report/loss_review/treasury/article).

    Тир и вес монеты в индексе берутся заново из treasury_index.BASKET
    по тикеру сигнала (а не парсингом строки description) - надёжнее и
    не ломается, если формат description когда-нибудь поменяется."""
    found = treasury_index.find_coin_by_ticker(signal.ticker)
    if found is None:
        # Не должно происходить в норме (сигнал вообще пришёл из
        # index_signal_scanner, который сканирует только монеты корзины),
        # но на всякий случай - не падаем, а логируем и используем
        # нейтральную подпись.
        logger.warning("Тикер %s не найден в Treasury Index при генерации поста - подпись будет неполной", signal.ticker)
        tier_label, weight = "монета индекса", 0.0
    else:
        tier_key, coin = found
        tier_label, weight = treasury_index.TIER_LABELS[tier_key], coin["weight"]

    user_prompt = f"""Тикер: ${signal.ticker}
Направление: {signal.direction}
Тир в индексе: {tier_label}
Вес в индексе: {weight:g}%

Напиши хук в описанном стиле."""

    hook = call_groq(_SYSTEM_PROMPT, user_prompt, max_tokens=200, temperature=0.8)

    ok, reason = _validate_hook(hook)
    if not ok:
        logger.warning("Хук индекс-сигнала не прошёл проверку (%s) - публикую с нейтральным хуком", reason)
        hook = _fallback_hook(signal)

    text = post_format.assemble_index_management_post(hook, signal, tier_label, weight)
    logger.info("Сгенерирован индекс-сигнал для %s: %s", signal.ticker, text[:150].replace("\n", " "))
    return text
