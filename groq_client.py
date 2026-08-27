"""
Общая обёртка над вызовом Groq chat completions.

Раньше каждый генератор (text_generator, opinion_generator, image_analyzer,
article_generator) делал requests.post(...).raise_for_status() сам по себе -
четыре копии одного и того же кода, и ни одна не отличала 429 (rate limit)
от любой другой ошибки. Из-за этого, например, временный 429 на статье
тушился одним и тем же фиксированным backoff'ом, без учёта Retry-After
от самого Groq, а на валютных постах 429 вообще не давал backoff и мог
за 3 быстрых попытки (MAX_PUBLISH_ATTEMPTS) выбросить совершенно
нормальный сигнал из очереди.

Баг (см. историю правок): иногда в пост уходил пустой хук (LLM вернул
пустую строку) или хук, оборванный на полуслове (ответ упёрся в
max_tokens, finish_reason="length") - ни один из генераторов не проверял
это явно, только "нет ли лишних чисел/английских слов" в уже непустом
тексте. Итог - пост из одного дисклеймера/структурного блока без единой
мысли автора, либо с обрубленным на середине предложением. call_groq
теперь ловит оба случая здесь, в одном месте, и один раз автоматически
перезапрашивает ответ - до генераторов эта проблема почти никогда не
доходит.
"""
import logging

import requests

import config

logger = logging.getLogger(__name__)

GROQ_ENDPOINT = "https://api.groq.com/openai/v1/chat/completions"

# Если Groq не прислал Retry-After - ждём вот столько (секунд) на всякий
# случай, чтобы не долбить API сразу на следующем тике.
DEFAULT_RATE_LIMIT_BACKOFF_SECONDS = 60 * 5

# Ответ короче этого (после strip) считаем "пустым" - реальный хук даже в
# одно короткое предложение всегда длиннее (напр. "$BTC растёт 🚀" - уже
# 15+ символов). Меньше - либо пустая строка, либо мусор вроде "...".
_MIN_RESPONSE_CHARS = 5

# При обрыве по finish_reason="length" - во сколько раз увеличиваем
# max_tokens на повторной попытке (с потолком, чтобы не улететь в
# неадекватно длинный и дорогой запрос).
_TRUNCATION_RETRY_MULTIPLIER = 2
_MAX_RETRY_TOKENS = 1200


class GroqRateLimited(Exception):
    """429 от Groq - превышен лимит запросов/токенов.

    retry_after_seconds - сколько секунд ждать, по данным самого Groq
    (заголовок Retry-After), либо DEFAULT_RATE_LIMIT_BACKOFF_SECONDS,
    если заголовка нет.
    """

    def __init__(self, retry_after_seconds: float):
        self.retry_after_seconds = retry_after_seconds
        super().__init__(f"Groq rate limit, retry after {retry_after_seconds:.0f}s")


class GroqEmptyResponse(Exception):
    """Groq вернул пустой (или почти пустой, короче _MIN_RESPONSE_CHARS)
    текст ответа даже после одной автоматической повторной попытки -
    публиковать пост без единой мысли автора нельзя, лучше пропустить
    этот тик, чем выпустить пост из одного дисклеймера/цифр."""


class GroqTruncatedResponse(Exception):
    """Ответ Groq обрублен по finish_reason="length" (упёрся в max_tokens)
    даже после одной повторной попытки с увеличенным лимитом - мысль не
    закончена, публиковать оборванное на середине предложение нельзя."""


def call_groq(system_prompt: str, user_prompt: str, max_tokens: int = 600,
              temperature: float = 0.8, model: str | None = None, timeout: int = 45) -> str:
    """Делает chat-completion запрос к Groq и возвращает текст ответа.

    Поднимает:
    - GroqRateLimited при 429 (с retry_after_seconds из заголовка
      Retry-After, если он есть) - НЕ ретраится здесь, вызывающий код сам
      решает, когда повторить (см. main.py - разные форматы постов ждут
      по-разному).
    - GroqTruncatedResponse, если ответ дважды (обычная попытка + одна
      автоматическая с увеличенным max_tokens) обрывается по лимиту
      токенов, не закончив мысль.
    - GroqEmptyResponse, если ответ дважды приходит пустым/почти пустым.

    Любая другая ошибка HTTP или сети поднимается как обычно
    (requests.RequestException) - вызывающий код это уже умеет ловить.
    """
    content, finish_reason = _request_once(system_prompt, user_prompt, max_tokens, temperature, model, timeout)

    if finish_reason == "length":
        retry_tokens = min(max_tokens * _TRUNCATION_RETRY_MULTIPLIER, _MAX_RETRY_TOKENS)
        logger.warning(
            "Groq оборвал ответ по лимиту токенов (max_tokens=%d, конец: %r) - "
            "перезапрашиваю с max_tokens=%d",
            max_tokens, content[-60:], retry_tokens,
        )
        content, finish_reason = _request_once(system_prompt, user_prompt, retry_tokens, temperature, model, timeout)
        if finish_reason == "length":
            raise GroqTruncatedResponse(
                f"Ответ дважды обрублен по finish_reason=length (max_tokens={retry_tokens})"
            )

    if len(content) < _MIN_RESPONSE_CHARS:
        logger.warning("Groq вернул пустой/почти пустой ответ (%r) - перезапрашиваю", content)
        content, _ = _request_once(system_prompt, user_prompt, max_tokens, temperature, model, timeout)
        if len(content) < _MIN_RESPONSE_CHARS:
            raise GroqEmptyResponse(f"Groq дважды вернул пустой/почти пустой ответ: {content!r}")

    return content


def _request_once(system_prompt: str, user_prompt: str, max_tokens: int,
                   temperature: float, model: str | None, timeout: int) -> tuple[str, str | None]:
    """Один HTTP-запрос к Groq. Возвращает (текст_ответа, finish_reason)."""
    payload = {
        "model": model or config.GROQ_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    headers = {"Authorization": f"Bearer {config.GROQ_API_KEY}"}

    resp = requests.post(GROQ_ENDPOINT, json=payload, headers=headers, timeout=timeout)

    if resp.status_code == 429:
        retry_after = _parse_retry_after(resp)
        logger.warning(
            "Groq вернул 429 (rate limit) - жду %.0fс перед следующей попыткой "
            "(Retry-After из ответа: %s)",
            retry_after, resp.headers.get("Retry-After", "нет заголовка"),
        )
        raise GroqRateLimited(retry_after)

    resp.raise_for_status()
    data = resp.json()
    choice = data["choices"][0]
    content = choice["message"]["content"].strip()
    finish_reason = choice.get("finish_reason")
    return content, finish_reason


def _parse_retry_after(resp: requests.Response) -> float:
    raw = resp.headers.get("Retry-After")
    if raw:
        try:
            return max(float(raw), 1.0)
        except ValueError:
            pass
    return DEFAULT_RATE_LIMIT_BACKOFF_SECONDS
