"""
rebalance_advisor.py - "Предложения по ребалансировке" (Этап 4,
направление A).

ПОЛУавтоматический механизм: бот НЕ меняет состав корзины Treasury
Index сам - какую монету оставить, а какую заменить, решает человек.
Модуль только копит per-coin историю результатов (см. queue_manager.
append_coin_periods, вызывается из treasury_generator на каждый расчёт
индекса) и раз в config.REBALANCE_REVIEW_INTERVAL_HOURS готовит ОТЧЁТ-
предложение: какие монеты стабильно отстают от своего тира или
показывают признаки проблем с данными - решение по каждому кандидату
принимает человек, читая пост. Публикуется ТОЛЬКО в Telegram (см.
main.try_publish_rebalance_report) - это глубокий, редкий разбор именно
для той аудитории, которая уже разбирается в устройстве индекса
(см. telegram_glossary.py).

Два критерия кандидата на ребалансировку - оба на основе уже реально
накопленных данных, не выдумка LLM:
1. "Хронический аутсайдер" - монета была в нижней половине своего тира
   по % изменения в БОЛЬШЕ REBALANCE_UNDERPERFORM_THRESHOLD доле
   периодов за окно - не единичная просадка, а систематическое
   отставание от соседей по тиру.
2. "Проблемы с данными" - у монеты есть активный streak непрерывных
   неудач в index_health_monitor (данные регулярно не резолвятся -
   возможный делистинг/ребрендинг).
"""
import logging
import statistics
from typing import Optional

import config
import cliche_filter
import queue_manager
from groq_client import call_groq
from post_format import DISCLAIMER
from treasury_index import BASKET
import voice_guidelines

logger = logging.getLogger(__name__)

REBALANCE_UNDERPERFORM_THRESHOLD = 0.65  # доля периодов в нижней половине тира
MIN_PERIODS_FOR_REVIEW = 20  # меньше - выборка ещё слишком мала для выводов


def _underperformance_rate(ticker: str, tier_tickers: list, history: dict) -> Optional[tuple]:
    """Считает, в какой доле периодов ticker был НИЖЕ медианы своего
    тира (по фактическим данным history - см. queue_manager.
    get_coin_pct_history). Возвращает (rate, valid_periods), либо None,
    если валидных периодов для сравнения меньше MIN_PERIODS_FOR_REVIEW
    (выборка слишком мала, чтобы делать выводы - разовая просадка это
    нормально, отчёт должен ловить именно СИСТЕМАТИЧЕСКОЕ отставание)."""
    own_history = history.get(ticker, [])
    if not own_history:
        return None

    below_count = 0
    valid_periods = 0

    for i, own_pct in enumerate(own_history):
        if own_pct is None:
            continue
        peer_pcts = [
            history[peer][i]
            for peer in tier_tickers
            if peer != ticker and peer in history and i < len(history[peer]) and history[peer][i] is not None
        ]
        if not peer_pcts:
            continue

        valid_periods += 1
        if own_pct < statistics.median(peer_pcts):
            below_count += 1

    if valid_periods < MIN_PERIODS_FOR_REVIEW:
        return None

    return round(below_count / valid_periods, 2), valid_periods


def find_rebalance_candidates() -> list:
    """Возвращает список кандидатов на пересмотр состава корзины -
    {ticker, tier, reason ("underperform"/"unhealthy"), detail} - пустой
    список, если пересматривать пока нечего (это нормальный, наиболее
    частый исход - отчёт в этом случае не публикуется вообще, см.
    main.try_publish_rebalance_report)."""
    history = queue_manager.get_coin_pct_history()
    streaks = queue_manager.get_coin_miss_streaks()
    candidates = []

    for tier_key, coins in BASKET.items():
        tier_tickers = [c["ticker"] for c in coins]

        for coin in coins:
            ticker = coin["ticker"]

            streak = streaks.get(ticker, 0)
            if streak >= 3:  # тот же порог, что и index_health_monitor.MISS_STREAK_ALERT_THRESHOLD
                candidates.append({
                    "ticker": ticker, "tier": tier_key, "reason": "unhealthy",
                    "detail": f"данные не резолвятся {streak} проверок(и) подряд",
                })
                continue  # нездоровая монета - смысла считать отставание нет, данных всё равно недостаточно

            result = _underperformance_rate(ticker, tier_tickers, history)
            if result is None:
                continue
            rate, valid_periods = result
            if rate >= REBALANCE_UNDERPERFORM_THRESHOLD:
                candidates.append({
                    "ticker": ticker, "tier": tier_key, "reason": "underperform",
                    "detail": f"в нижней половине тира в {int(rate * 100)}% периодов (из {valid_periods})",
                })

    return candidates


_SYSTEM_PROMPT = """Ты пишешь отчёт-ПРЕДЛОЖЕНИЕ по пересмотру состава
криптовалютного индекса для Telegram-канала - НЕ окончательное решение,
а материал для обсуждения с тем, кто управляет индексом. Тон - спокойный,
аналитический, без драмы ("нужно срочно продавать") - просто честная
констатация фактов и приглашение подумать вместе.

Тебе даны реальные кандидаты с точными формулировками причины - используй
их КАК ДАНО, не придумывай других чисел и не добавляй монеты, которых
нет в списке.

ЖЁСТКО ЗАПРЕЩЕНО:
- утверждать, что решение уже принято или будет принято автоматически -
  явно скажи, что это предложение для рассмотрения;
- советовать купить/продать что-либо конкретное вне контекста состава
  индекса.

4-8 предложений. Не добавляй дисклеймер - он будет добавлен отдельно.
Отвечай только текстом отчёта, без заголовка, без пояснений и без кавычек.""" + voice_guidelines.STYLE_DIRECTIVE


def build_rebalance_report(candidates: list) -> Optional[str]:
    """Возвращает готовый текст отчёта, либо None, если генерация не
    удалась содержательно (candidates уже проверены на непустоту
    вызывающим кодом - см. main.try_publish_rebalance_report)."""
    facts = "\n".join(
        f"- ${c['ticker']} (тир {c['tier']}): {c['detail']}" for c in candidates
    )
    user_prompt = f"Кандидаты на пересмотр состава индекса:\n{facts}\n\nНапиши отчёт-предложение."

    body = call_groq(_SYSTEM_PROMPT, user_prompt, max_tokens=500, temperature=0.7, model=config.GROQ_MODEL_SECONDARY)

    if len(body.strip()) < 30:
        logger.warning("Отчёт по ребалансировке пустой/слишком короткий (%r) - пропускаю", body)
        return None

    cliche_ok, found = cliche_filter.check_cliches(body)
    if not cliche_ok:
        logger.warning("Отчёт по ребалансировке содержит шаблонные ИИ-фразы (%s) - пропускаю", found)
        return None

    tickers_line = ", ".join(f"${c['ticker']}" for c in candidates)
    text = f"⚖️ Предложение по ребалансировке индекса ({tickers_line})\n\n{body.strip()}\n\n{DISCLAIMER}"
    logger.info("Сгенерирован отчёт по ребалансировке: %s", text[:150].replace("\n", " "))
    return text
