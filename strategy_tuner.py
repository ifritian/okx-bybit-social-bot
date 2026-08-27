"""
strategy_tuner.py - самокорректировка порога публикации по стратегиям
на основе реальной статистики точности (outcome_tracker, Фаза 1).

Идея: если у какой-то стратегии статистически ДОСТАТОЧНО ДАННЫХ и её
win-rate заметно ниже нормы, порог публикации для НЕЁ конкретно
повышается - бот сам становится строже там, где реально ошибается
чаще, вместо того чтобы вслепую продолжать публиковать с одинаковым
порогом для всех стратегий.

Специально ОДНОСТОРОННЕ (только штраф за плохую статистику, не бонус за
хорошую) - симметричная система рисковала бы занижать порог на короткой
удачной серии, а потом расплачиваться за это доверием, когда удача
закончится (regression to the mean). Штраф за плохую статистику -
безопасное решение: хуже с точки зрения объёма контента, но не хуже с
точки зрения репутации.

При МАЛОМ количестве закрытых сигналов по стратегии (< MIN_SAMPLES_FOR_TUNING)
адаптация вообще не применяется - на выборке в 5-10 сигналов win-rate
0% или 100% ничего не доказывает статистически, реагировать на такой шум
было бы хуже, чем не реагировать вообще.

Пересчитывается каждый тик (дёшево - работает по уже посчитанным в
памяти closed_outcomes, без сети), сохраняется в queue_manager,
применяется в scanner.py / index_signal_scanner.py в момент фильтрации
сигналов перед постановкой в очередь на публикацию.
"""
import logging

import outcome_tracker
import queue_manager

logger = logging.getLogger(__name__)

MIN_SAMPLES_FOR_TUNING = 15      # меньше - статистика ничего не доказывает, не трогаем порог
MAX_PENALTY = 15                 # потолок штрафа (score points) - не блокируем стратегию полностью
WEAK_WIN_RATE_THRESHOLD = 40.0   # ниже этого при достаточной выборке - считаем стратегию слабой
TUNING_LOOKBACK_DAYS = 30.0      # окно для расчёта - достаточно широкое, чтобы не гоняться за шумом недели


def recompute_adjustments(days: float = TUNING_LOOKBACK_DAYS) -> dict:
    """Пересчитывает штрафы по стратегиям на основе статистики за
    последние `days` дней. Возвращает и сохраняет словарь
    {strategy: penalty}, penalty >= 1 - на сколько очков строже порог
    публикации для этой стратегии. Стратегии без штрафа в словаре нет
    (а не penalty=0), чтобы легко отличить "не хватает данных/всё
    нормально" от "штраф применён"."""
    stats = outcome_tracker.get_accuracy_stats(days=days)
    adjustments = {}

    for strategy, s in stats.get("by_strategy", {}).items():
        if s["count"] < MIN_SAMPLES_FOR_TUNING or s["win_rate"] is None:
            continue  # недостаточно данных - не судим по шуму

        if s["win_rate"] < WEAK_WIN_RATE_THRESHOLD:
            # Чем сильнее win-rate ниже порога слабости, тем строже
            # штраф - но не более MAX_PENALTY (сознательно не даём
            # автоматике полностью заблокировать стратегию без участия
            # человека - для этого есть ручной MIN_SIGNAL_SCORE_TO_PUBLISH).
            deficit = WEAK_WIN_RATE_THRESHOLD - s["win_rate"]
            penalty = min(MAX_PENALTY, round(deficit / 2))
            if penalty > 0:
                adjustments[strategy] = penalty

    previous = queue_manager.get_strategy_adjustments()
    if adjustments != previous:
        logger.info("Автокоррекция тактики (окно %.0fд): штрафы по стратегиям изменились: %s -> %s",
                    days, previous, adjustments)
    queue_manager.set_strategy_adjustments(adjustments)
    return adjustments


def get_effective_min_score(strategy: str, base_min_score: int) -> int:
    """Эффективный порог публикации для конкретной стратегии - базовый
    порог + штраф (если есть). Только ЧТЕНИЕ уже посчитанного штрафа -
    пересчёт статистики (recompute_adjustments) должен вызываться
    отдельно (main.py, раз в тик), чтобы не дёргать агрегацию по всем
    закрытым сигналам при каждой проверке отдельного сигнала."""
    penalty = queue_manager.get_strategy_adjustments().get(strategy, 0)
    return base_min_score + penalty


def describe_active_adjustments() -> str:
    """Человекочитаемая строка активных штрафов - для check_state.py и
    для упоминания в accuracy_report (прозрачность: аудитория должна
    видеть, что порог реально меняется, а не быть скрытым от неё)."""
    adjustments = queue_manager.get_strategy_adjustments()
    if not adjustments:
        return "нет активных корректировок"
    return "; ".join(f"{strategy}: +{penalty} к порогу" for strategy, penalty in adjustments.items())
