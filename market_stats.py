"""
market_stats.py - реальные рыночные числа (% изменения, амплитуда,
текущая цена) для генераторов постов OKX Orbit / Bybit ByX.

Вычленено из opinion_generator.py Binance-проекта (см. соседний
репозиторий binance-square-bot) сознательно - этот репозиторий должен
быть полностью независим от Binance-кода: свой git, свои секреты, свой
CI. Общая только логика "не давать LLM выдумывать цифры, а считать их
самим и передавать модели готовыми" - это отдельный вычислительный
модуль без Binance-упоминаний, безопасно дублировать между проектами.

Источник данных - тот же публичный API Binance (data-api.binance.vision,
см. chart_generator.fetch_klines) для получения OHLCV. Это не связывает
проект с Binance как площадкой публикации - это просто общедоступный
источник рыночных данных, которым исторически удобно пользоваться (не
требует ключей, высокий рейт-лимит). Если понадобится - можно заменить
на любой другой источник свечей без изменений в остальном коде, у
fetch_klines() стабильный контракт возврата.
"""
import logging
import random
from typing import Optional

import requests

from chart_generator import fetch_klines

logger = logging.getLogger(__name__)

THEMES: dict[str, dict] = {
    "BTC": {"label": "$BTC", "tickers": ["BTC"]},
    "ETH": {"label": "$ETH", "tickers": ["ETH"]},
    "market": {"label": "крипторынок в целом (по корзине BTC/ETH/SOL/BNB)", "tickers": ["BTC", "ETH", "SOL", "BNB"]},
}


def pick_theme(last_theme: Optional[str]) -> str:
    """Выбирает тему, отличную от последней использованной."""
    themes = list(THEMES.keys())
    if last_theme in themes and len(themes) > 1:
        themes = [t for t in themes if t != last_theme]
    return random.choice(themes)


def calc_ticker_stats(ticker: str) -> Optional[dict]:
    """Реальные числа по тикеру за последние 2 дня: % изменения,
    амплитуда (high-low в % от открытия) и текущая цена."""
    try:
        klines = fetch_klines(ticker, days=2)
    except requests.RequestException as e:
        logger.warning("Не удалось получить данные %s: %s", ticker, e)
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
    Для 'market' - % по каждому активу корзины + средний % по корзине."""
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
