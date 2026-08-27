"""
Парсинг постов канала @resultrsi, которые на самом деле приходят от
бота @syndicateproobot (RSI/Bollinger/Divergence алерты с полным
сетапом: вход, инвалидация (стоп), цель (тейк), RSI, score).

Пример реального поста (упрощённо):

BEATUSDT • 15m
[Свежий] [RSI + Bollinger Touch] [Шорт]
RSI stayed above 70 while price tagged the upper Bollinger Band...
Сетап
Стратегия: RSI + Bollinger Touch
Сейчас: 2.225
Направление: Шорт
RSI / Score сейчас: 81.74 / 89/100
RSI / Score на сигнале: 81.74 / 89/100
Качество: Conservative
Фоллоу-ап: Включён
Уровни
Вход: 2.205 - 2.2178
Инвалидация: 2.2371
Цель: 2.1729
Окно RSI: 30.00 / 70.00
Контекст
24h: +35.67%
Объем: 57.67M
Режим: Directional
RSI live: 82.64
Создан: 2026-06-23 22:44:59 EEST
...

Один пост Telegram может содержать НЕСКОЛЬКО таких блоков подряд
(бот шлёт пачкой) - parse_signals возвращает все найденные.

Числа, которые нельзя искажать в финальном тексте поста: цена входа
(диапазон), инвалидация (стоп), цель (тейк), RSI и score.
"""
import re
from dataclasses import dataclass
from typing import Optional


@dataclass
class RsiSignal:
    ticker: str            # BEAT (без USDT)
    timeframe: str          # 15m
    strategy: str            # "RSI + Bollinger Touch"
    direction: str            # "Шорт" / "Лонг" / "Шорт (перекупленность)" и т.п. (как в посте)
    current_price: str         # "2.225"
    rsi_now: str                 # "81.74"
    score: str                    # "89"
    quality: str                    # "Conservative"
    entry_low: str                    # "2.205"
    entry_high: str                     # "2.2178"
    invalidation: str                     # "2.2371"  (стоп-лосс)
    target: str                             # "2.1729"  (тейк-профит)
    change_24h: str                           # "+35.67%"
    volume: str                                 # "57.67M"
    rsi_live: str                                 # "82.64"
    created_at: str                                 # "2026-06-23 22:44:59 EEST"
    description: str                                  # короткое описание сетапа (для контекста LLM)
    raw_text: str                                       # исходный блок целиком


# Заголовок одного блока: "BEATUSDT • 15m" (разделитель может быть
# обычным "•" или похожим символом - на всякий случай матчим широко).
_HEADER_RE = re.compile(
    r"([A-Z0-9]+)USDT\s*[•·]\s*(\w+)\s*\n", re.MULTILINE
)

_FIELD_PATTERNS = {
    "strategy": r"Стратегия:\s*(.+)",
    "current_price": r"Сейчас:\s*([\d.,]+)",
    "direction": r"Направление:\s*(.+)",
    "rsi_score_now": r"RSI\s*/\s*Score\s*сейчас:\s*([\d.,]+)\s*/\s*(\d+)\s*/\s*100",
    "quality": r"Качество:\s*(.+)",
    "entry": r"Вход:\s*([\d.,]+)\s*-\s*([\d.,]+)",
    "invalidation": r"Инвалидация:\s*([\d.,]+)",
    "target": r"Цель:\s*([\d.,]+)",
    "change_24h": r"24h:\s*([+-]?[\d.,]+%)",
    "volume": r"Объем:\s*([\d.,]+\w*)",
    "rsi_live": r"RSI live:\s*([\d.,]+)",
    "created_at": r"Создан:\s*(.+)",
}

_DESCRIPTION_RE = re.compile(
    r"\]\s*\n(.+?)\nСетап", re.DOTALL
)


def _extract(pattern: str, block: str):
    return re.search(pattern, block)


def _parse_block(ticker: str, timeframe: str, block: str) -> Optional[RsiSignal]:
    strategy_m = _extract(_FIELD_PATTERNS["strategy"], block)
    price_m = _extract(_FIELD_PATTERNS["current_price"], block)
    direction_m = _extract(_FIELD_PATTERNS["direction"], block)
    rsi_score_m = _extract(_FIELD_PATTERNS["rsi_score_now"], block)
    quality_m = _extract(_FIELD_PATTERNS["quality"], block)
    entry_m = _extract(_FIELD_PATTERNS["entry"], block)
    invalidation_m = _extract(_FIELD_PATTERNS["invalidation"], block)
    target_m = _extract(_FIELD_PATTERNS["target"], block)
    change_m = _extract(_FIELD_PATTERNS["change_24h"], block)
    volume_m = _extract(_FIELD_PATTERNS["volume"], block)
    rsi_live_m = _extract(_FIELD_PATTERNS["rsi_live"], block)
    created_m = _extract(_FIELD_PATTERNS["created_at"], block)
    desc_m = _DESCRIPTION_RE.search(block)

    # Минимально необходимые поля, без которых пост не считаем валидным
    # сигналом (лучше пропустить, чем опубликовать с дырами).
    required = [strategy_m, price_m, direction_m, rsi_score_m, entry_m,
                invalidation_m, target_m]
    if not all(required):
        return None

    return RsiSignal(
        ticker=ticker,
        timeframe=timeframe,
        strategy=strategy_m.group(1).strip(),
        direction=direction_m.group(1).strip(),
        current_price=price_m.group(1).strip(),
        rsi_now=rsi_score_m.group(1).strip(),
        score=rsi_score_m.group(2).strip(),
        quality=quality_m.group(1).strip() if quality_m else "",
        entry_low=entry_m.group(1).strip(),
        entry_high=entry_m.group(2).strip(),
        invalidation=invalidation_m.group(1).strip(),
        target=target_m.group(1).strip(),
        change_24h=change_m.group(1).strip() if change_m else "",
        volume=volume_m.group(1).strip() if volume_m else "",
        rsi_live=rsi_live_m.group(1).strip() if rsi_live_m else "",
        created_at=created_m.group(1).strip() if created_m else "",
        description=desc_m.group(1).strip() if desc_m else "",
        raw_text=block.strip(),
    )


def is_signal_message(text: str) -> bool:
    return bool(_HEADER_RE.search(text))


def parse_signals(text: str) -> list[RsiSignal]:
    """Один пост в Telegram может содержать несколько блоков сигналов
    подряд (бот шлёт пачкой) - возвращаем все, которые удалось
    распарсить."""
    headers = list(_HEADER_RE.finditer(text))
    if not headers:
        return []

    results = []
    for i, h in enumerate(headers):
        start = h.start()
        end = headers[i + 1].start() if i + 1 < len(headers) else len(text)
        block = text[start:end]
        signal = _parse_block(h.group(1), h.group(2), block)
        if signal is not None:
            results.append(signal)
    return results


def pick_entry(signals: list[RsiSignal], recent_tickers: list[str]) -> RsiSignal:
    """Выбирает сигнал из пачки, стараясь не повторять тикеры, которые
    публиковались недавно (recent_tickers - последние N тикеров)."""
    recent_set = {t.upper() for t in recent_tickers}
    for s in signals:
        if s.ticker.upper() not in recent_set:
            return s
    return signals[0]


def is_long_direction(direction: str) -> bool:
    """True, если direction описывает ЛОНГ - в ЛЮБОЙ формулировке
    причины в скобках ("Лонг (перепроданность)", "Лонг (пробой вверх)",
    "Лонг (бычье пересечение MACD)" и т.п.).

    Единственное, что ГАРАНТИРОВАНО присутствует в любом direction
    (см. RsiSignal.direction) - слово "Лонг" или "Шорт" как основа,
    независимо от того, КАКАЯ стратегия/причина указана в скобках.
    Раньше в нескольких местах кода направление угадывалось по
    RSI-специфичным словам "перепрод"/"перекуплен" - это работало,
    пока единственной стратегией была RSI, но молча ломалось бы для
    любой новой стратегии с другой формулировкой причины (например,
    пробоя диапазона или пересечения MACD, где "перепроданность" вообще
    не подходящее по смыслу слово). Весь код теперь должен определять
    направление ЧЕРЕЗ ЭТУ функцию, а не через собственный substring-поиск."""
    return "лонг" in direction.lower()


def _to_float(value) -> float:
    try:
        return float(str(value).replace(",", "").replace("%", "").strip())
    except (TypeError, ValueError):
        return 0.0


def calc_risk_reward_ratio(signal: "RsiSignal") -> Optional[float]:
    """Соотношение потенциальной прибыли к риску (reward/risk) - ЕДИНЫЙ
    источник правды для этого расчёта, чтобы R:R в посте
    (post_format.signal_risk_reward_line), в фильтре качества сигналов
    (scanner._meets_min_risk_reward) и где угодно ещё в будущем всегда
    была ОДНА И ТА ЖЕ цифра, а не три чуть разные реализации одной идеи.

    Вход берётся серединой диапазона entry_low..entry_high - та же
    логика, что и в outcome_tracker.record_signal_outcome (трекинг
    результата считает от этого же entry).

    None, если риск (|entry - stop|) равен нулю или числа не
    распознались - "не удалось посчитать", а не "риск бесконечный/нулевой"."""
    entry_low = _to_float(signal.entry_low)
    entry_high = _to_float(signal.entry_high)
    entry = (entry_low + entry_high) / 2 if (entry_low and entry_high) else (entry_low or entry_high)
    stop = _to_float(signal.invalidation)
    target = _to_float(signal.target)

    if not (entry and stop and target):
        return None

    risk = abs(entry - stop)
    if risk <= 0:
        return None

    return abs(target - entry) / risk