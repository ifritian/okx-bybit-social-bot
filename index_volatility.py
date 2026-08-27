"""
index_volatility.py - волатильность и просадка Treasury Index на основе
накопленной истории доходностей по периодам (queue_manager.append_treasury_return).

Раньше посты показывали только "с запуска +X%" - кумулятивную доходность,
но ни слова о том, насколько тряско было по дороге. Для управления
портфелем это не менее важно: два индекса с одинаковым "+20% с запуска"
могут быть совершенно разными по риску, если один шёл ровно, а другой -
через просадку в -40% и обратно.

Волатильность считается как стандартное отклонение доходностей по
периодам (обычно раз в TREASURY_INTERVAL_HOURS=12ч) - чем больше
разброс, тем "трясёт" сильнее. Max drawdown - наибольшее падение от
локального пика до последующего минимума за всю историю, восстановленное
из ряда доходностей (не требует отдельного хранения самого пути - он
пересчитывается кумулятивным произведением с базой 100, как и в
update_treasury_history, но здесь для КАЖДОЙ точки истории, а не только
финальной).
"""
import statistics


def _reconstruct_cumulative_series(returns_pct: list[float]) -> list[float]:
    """Восстанавливает путь кумулятивного значения индекса (база 100) по
    списку % доходностей за периоды - та же формула, что в
    queue_manager.update_treasury_history, применённая последовательно
    ко всей истории, а не только к последней точке."""
    series = [100.0]
    for pct in returns_pct:
        series.append(series[-1] * (1 + pct / 100))
    return series


def compute_volatility_and_drawdown(returns_pct: list[float]) -> dict:
    """Возвращает {"volatility_pct": ..., "max_drawdown_pct": ...,
    "current_drawdown_pct": ..., "periods": n}. Значения None, если
    истории недостаточно (меньше 2 точек - для std нужно хотя бы 2,
    для просадки хотя бы 1)."""
    n = len(returns_pct)
    if n < 2:
        return {"volatility_pct": None, "max_drawdown_pct": None, "current_drawdown_pct": None, "periods": n}

    volatility_pct = round(statistics.stdev(returns_pct), 2)

    series = _reconstruct_cumulative_series(returns_pct)
    peak = series[0]
    max_drawdown = 0.0
    for value in series:
        if value > peak:
            peak = value
        drawdown = (value - peak) / peak * 100
        if drawdown < max_drawdown:
            max_drawdown = drawdown

    all_time_peak = max(series)
    current_value = series[-1]
    current_drawdown = round((current_value - all_time_peak) / all_time_peak * 100, 2)

    return {
        "volatility_pct": volatility_pct,
        "max_drawdown_pct": round(max_drawdown, 2),
        "current_drawdown_pct": current_drawdown,
        "periods": n,
    }


def format_volatility_block(stats: dict, period_hours: float) -> str:
    """Короткая строка для поста Treasury Index. Пустая строка, если
    данных недостаточно (меньше 2 периодов истории - слишком рано после
    запуска, ещё не показываем, чтобы не путать читателя одной точкой)."""
    if stats["volatility_pct"] is None:
        return ""

    return (
        f"📉 Волатильность (по {stats['periods']} периодам x {period_hours:g}ч): "
        f"{stats['volatility_pct']}% | Макс. просадка с запуска: {stats['max_drawdown_pct']}% "
        f"| Текущая просадка от пика: {stats['current_drawdown_pct']}%"
    )
