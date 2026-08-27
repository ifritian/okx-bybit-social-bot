"""
Treasury Index - собственный инфраструктурный крипто-индекс канала.

Философия: не "топ по капитализации" (там всё тянет BTC) и не "весь
рынок" - а конкретная корзина из 15 монет, которые строят инфраструктуру
крипты (L1 / L2 / DeFi), без BTC/ETH/BNB (это "базис", не инфраструктура
в этом смысле), без мемкоинов и без стейблов.

Корзина разбита на три уровня риска по классическому принципу
"кошелёк-пирамида" (стабильное ядро - средний риск - высокий риск):

  Tier 1 "Фундамент" (60%) - крупные L1 с состоявшимся adoption
  Tier 2 "Рост"       (30%) - зрелые DeFi-протоколы и L2 с продуктом
  Tier 3 "Риск"       (10%) - новые L1/L2 и emerging DeFi

Внутри каждого тира веса тоже не равны - монета с бОльшим доверием
получает больший вес (см. WEIGHT в BASKET ниже). Сумма всех весов = 100.

Модуль самостоятельный - не зависит от остального бота (queue_manager,
config и т.п.), только requests + stdlib. Данные берутся с того же
источника, что и weiter в scanner.py/chart_generator.py:
data-api.binance.vision - публичное зеркало Binance без авторизации и
без гео-ограничений.
"""
import logging
from dataclasses import dataclass, field
from typing import Optional

import requests

logger = logging.getLogger(__name__)

_BASE_URL = "https://data-api.binance.vision/api/v3"

# --- Состав корзины ---
# ticker - тикер, под которым монета известна аудитории (то, что пишем
# в посте). binance_symbol_candidates - список тикеров для сопоставления
# с реальным символом на Binance (SYMBOL + USDT), в порядке приоритета -
# первый, который реально торгуется, используется. Нужно для MATIC/POL:
# Polygon мигрировал токен на POL, но старый тикер может быть ещё жив
# или наоборот уже недоступен - код сам определяет, что сейчас торгуется,
# вместо того чтобы это захардкодить и сломаться при следующей миграции.

TIER_LABELS = {
    "tier1": "🔵 Фундамент",
    "tier2": "🟡 Рост",
    "tier3": "🔴 Риск",
}

TIER_WEIGHTS = {"tier1": 60.0, "tier2": 30.0, "tier3": 10.0}

BASKET: dict[str, list[dict]] = {
    "tier1": [
        {"ticker": "SOL", "weight": 20.0},
        {"ticker": "AVAX", "weight": 15.0},
        {"ticker": "NEAR", "weight": 10.0},
        {"ticker": "ARB", "weight": 10.0},
        {"ticker": "OP", "weight": 5.0},
    ],
    "tier2": [
        {"ticker": "AAVE", "weight": 8.0},
        {"ticker": "UNI", "weight": 7.0},
        # MATIC -> POL (ребрендинг Polygon) - пробуем POL первым,
        # откатываемся на MATIC, если POL вдруг не торгуется.
        {"ticker": "POL", "weight": 7.0, "fallback": "MATIC"},
        {"ticker": "JUP", "weight": 5.0},
        {"ticker": "DYDX", "weight": 3.0},
    ],
    "tier3": [
        {"ticker": "SUI", "weight": 2.5},
        {"ticker": "APT", "weight": 2.5},
        {"ticker": "STRK", "weight": 2.0},
        {"ticker": "MANTA", "weight": 1.5},
        {"ticker": "PENDLE", "weight": 1.5},
    ],
}

# Сверка: суммы весов внутри тиров должны совпадать с TIER_WEIGHTS, а
# сумма всех тиров - быть равна 100. Проверяется один раз при импорте
# модуля - если кто-то поменяет веса и забудет пересчитать, бот упадёт
# сразу при старте, а не молча опубликует кривой индекс.
for _tier, _coins in BASKET.items():
    _sum = round(sum(c["weight"] for c in _coins), 6)
    assert _sum == TIER_WEIGHTS[_tier], (
        f"Веса тира {_tier} суммируются в {_sum}, а не в {TIER_WEIGHTS[_tier]} - "
        f"проверь BASKET/TIER_WEIGHTS"
    )
assert round(sum(TIER_WEIGHTS.values()), 6) == 100.0, "Сумма весов тиров должна быть 100"


# Защита от выбросов: если |% изменения| одной монеты превышает этот
# порог - подозреваем не реальный рыночный памп/дамп, а артефакт данных
# (задержавшаяся/пустая свеча, кривой ответ Binance по низколиквидной
# паре и т.п.). Порог намеренно высокий - крипто-альты реально могут
# двигаться на 20-30% за 12ч на новостях, отсекаем только совсем
# аномальные случаи. Такая монета НЕ участвует в расчёте среднего (её
# вес перенормируется между остальными, как и при полном отсутствии
# данных), но остаётся видна в посте с пометкой - решение публиковать
# её процент или нет всё равно за человеком, а не молча в среднем.
OUTLIER_THRESHOLD_PCT = 40.0


@dataclass
class CoinChange:
    ticker: str
    weight: float
    pct: Optional[float]          # None, если данные не удалось получить
    symbol_used: Optional[str]    # реальный символ на Binance (может быть fallback)
    suspicious: bool = False      # |pct| > OUTLIER_THRESHOLD_PCT - подозрение на артефакт данных


@dataclass
class TierResult:
    key: str
    label: str
    pct: Optional[float]          # взвешенное изменение тира, None если ни одна монета не получена
    coins: list[CoinChange] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)


@dataclass
class TreasuryIndexResult:
    total_pct: Optional[float]
    period_hours: float
    tiers: list[TierResult]
    missing: list[str]            # все тикеры, по которым не удалось получить данные
    suspicious: list[str] = field(default_factory=list)  # тикеры-выбросы, исключённые из среднего


def _fetch_symbol_change_pct(symbol: str, period_hours: float) -> Optional[float]:
    """% изменения цены symbol (например 'SOLUSDT') за последние
    period_hours часов, по 1-часовым свечам с data-api.binance.vision.
    Возвращает None при любой ошибке сети/данных (тикер не торгуется,
    таймаут и т.п.) - вызывающий код сам решает, что делать дальше."""
    limit = max(int(round(period_hours)), 1) + 1  # +1 свеча, чтобы взять open первой из них
    try:
        resp = requests.get(
            f"{_BASE_URL}/klines",
            params={"symbol": symbol, "interval": "1h", "limit": limit},
            timeout=15,
        )
        resp.raise_for_status()
        rows = resp.json()
    except requests.RequestException as e:
        logger.warning("Не удалось получить свечи Treasury Index для %s: %s", symbol, e)
        return None

    if not isinstance(rows, list) or len(rows) < 2:
        logger.warning("Binance вернул неожиданный/пустой ответ для %s: %s", symbol, rows)
        return None

    open_price = float(rows[0][1])
    close_price = float(rows[-1][4])
    if open_price == 0:
        return None

    return round((close_price - open_price) / open_price * 100, 2)


def _resolve_coin_change(coin: dict, period_hours: float) -> CoinChange:
    """Пробует основной тикер, при неудаче - fallback (если задан)."""
    primary = coin["ticker"]
    symbol = f"{primary}USDT"
    pct = _fetch_symbol_change_pct(symbol, period_hours)
    used = symbol if pct is not None else None

    if pct is None and coin.get("fallback"):
        fb_symbol = f"{coin['fallback']}USDT"
        logger.info("%s (%s) не торгуется/недоступен, пробую fallback %s", primary, symbol, fb_symbol)
        pct = _fetch_symbol_change_pct(fb_symbol, period_hours)
        used = fb_symbol if pct is not None else None

    suspicious = pct is not None and abs(pct) > OUTLIER_THRESHOLD_PCT
    if suspicious:
        logger.warning(
            "%s (%s): %s%% за %sч - похоже на выброс/артефакт данных (порог %s%%), "
            "исключаю из расчёта среднего, но показываю в посте с пометкой",
            primary, used, pct, period_hours, OUTLIER_THRESHOLD_PCT,
        )

    return CoinChange(ticker=primary, weight=coin["weight"], pct=pct, symbol_used=used, suspicious=suspicious)


def compute_index(period_hours: float = 12.0, fetch_fn=_resolve_coin_change) -> TreasuryIndexResult:
    """Считает Treasury Index за последние period_hours часов.

    fetch_fn - параметр для тестов (подмена сетевого похода на синтетику),
    в проде не передаётся.

    Если по какой-то монете данных нет - она исключается, а вес внутри
    тира ПЕРЕНОРМИРУЕТСЯ между оставшимися (а не просто обнуляется) -
    иначе один выпавший тикер занижал бы % тира без всякой причины.
    Если из тира не получилось получить ни одной монеты - тир исключается
    из общего индекса, а его вес перераспределяется на оставшиеся тиры
    (тоже пропорционально их весов), по той же логике.
    """
    tiers: list[TierResult] = []
    all_missing: list[str] = []
    all_suspicious: list[str] = []

    for tier_key, coins_def in BASKET.items():
        coins = [fetch_fn(c, period_hours) for c in coins_def]
        # В расчёт среднего идут только монеты с данными И не помеченные
        # как выброс - suspicious-монеты остаются в coins (видны в посте),
        # но не участвуют в weighted average, как и полностью пропущенные.
        ok_coins = [c for c in coins if c.pct is not None and not c.suspicious]
        missing = [c.ticker for c in coins if c.pct is None]
        all_missing.extend(missing)
        all_suspicious.extend(c.ticker for c in coins if c.suspicious)

        if ok_coins:
            weight_sum = sum(c.weight for c in ok_coins)
            tier_pct = round(sum(c.pct * c.weight for c in ok_coins) / weight_sum, 2)
        else:
            tier_pct = None

        tiers.append(TierResult(
            key=tier_key, label=TIER_LABELS[tier_key], pct=tier_pct,
            coins=coins, missing=missing,
        ))

    ok_tiers = [t for t in tiers if t.pct is not None]
    if ok_tiers:
        weight_sum = sum(TIER_WEIGHTS[t.key] for t in ok_tiers)
        total_pct = round(sum(t.pct * TIER_WEIGHTS[t.key] for t in ok_tiers) / weight_sum, 2)
    else:
        total_pct = None

    if all_missing:
        logger.warning("Treasury Index: не удалось получить данные по %s", all_missing)
    if all_suspicious:
        logger.warning("Treasury Index: исключены как выбросы (не в среднем, но видны в посте): %s", all_suspicious)

    return TreasuryIndexResult(
        total_pct=total_pct, period_hours=period_hours, tiers=tiers,
        missing=all_missing, suspicious=all_suspicious,
    )


def _sign(pct: float) -> str:
    return "+" if pct >= 0 else ""


def format_index_block(result: TreasuryIndexResult) -> str:
    """Готовый числовой блок поста - собран КОДОМ, не LLM, чтобы цифры
    были гарантированно точными (LLM, если подключим, получит этот
    блок как готовый факт и будет писать текст ВОКРУГ него, не внутри).

    Формат соответствует утверждённому в обсуждении примеру:

    📊 Treasury Index: +2.1% (12ч)

    🔵 Фундамент (+1.4%): SOL +2.1%, AVAX +0.8%, NEAR ...
    🟡 Рост (+3.2%): AAVE +5.1%, UNI +2.3%, ...
    🔴 Риск (+4.8%): SUI +7.2%, PENDLE +3.1%, ...
    """
    period_label = f"{result.period_hours:g}ч"

    if result.total_pct is None:
        header = f"📊 Treasury Index: н/д ({period_label})"
    else:
        header = f"📊 Treasury Index: {_sign(result.total_pct)}{result.total_pct}% ({period_label})"

    lines = [header, ""]

    for tier in result.tiers:
        if tier.pct is None:
            lines.append(f"{tier.label}: н/д (нет данных)")
            continue

        coin_parts = []
        for c in sorted(tier.coins, key=lambda c: (c.pct is None, -(c.pct or 0))):
            if c.pct is None:
                continue
            mark = " ⚠️" if c.suspicious else ""
            coin_parts.append(f"{c.ticker} {_sign(c.pct)}{c.pct}%{mark}")

        coins_str = ", ".join(coin_parts)
        lines.append(f"{tier.label} ({_sign(tier.pct)}{tier.pct}%): {coins_str}")

    if result.suspicious:
        lines.append("")
        lines.append(
            f"⚠️ {', '.join(result.suspicious)} - аномальное движение, "
            f"исключено из расчёта среднего (проверить вручную перед публикацией)"
        )

    return "\n".join(lines)


def fetch_reference_change_pct(symbol: str, period_hours: float) -> Optional[float]:
    """Публичная обёртка над _fetch_symbol_change_pct - % изменения
    произвольного актива (например 'BTCUSDT') за тот же период, что и
    сам индекс. Нужна для сравнения "Treasury Index vs BTC" в посте -
    расхождение с BTC интереснее и более "расшариваемо", чем голая
    цифра индекса саму по себе."""
    return _fetch_symbol_change_pct(symbol, period_hours)


# Равновзвешенная корзина топ-капитализации для более честного ориентира,
# чем один BTC - топ-1 монета иногда двигается нетипично для рынка в
# целом (например, на новостях по конкретно BTC-ETF), среднее по
# нескольким тяжеловесам сглаживает этот эффект. Та же четвёрка, что и
# в opinion_generator.THEMES["market"] - единая терминология "рынок в
# целом" по всему боту.
MARKET_BENCHMARK_TICKERS = ["BTC", "ETH", "SOL", "BNB"]


def fetch_market_benchmark_pct(period_hours: float) -> Optional[float]:
    """Равновзвешенный % по MARKET_BENCHMARK_TICKERS за тот же период,
    что и сам индекс (та же свечная база - data-api.binance.vision,
    1h-свечи) - в отличие от opinion_generator.calc_theme_stats("market"),
    который считает за фиксированные 2 дня, здесь период гибкий и
    совпадает с period_hours индекса, чтобы сравнение было корректным
    "за то же самое время", а не за разные окна.

    Возвращает None, если не удалось получить данные ни по одному
    активу корзины - остальной пост публикуется без этого сравнения."""
    pcts = []
    for ticker in MARKET_BENCHMARK_TICKERS:
        pct = _fetch_symbol_change_pct(f"{ticker}USDT", period_hours)
        if pct is not None:
            pcts.append(pct)

    if not pcts:
        return None
    return round(sum(pcts) / len(pcts), 2)


def find_coin_by_ticker(ticker: str) -> Optional[tuple[str, dict]]:
    """Ищет монету в корзине по тикеру - проверяет и основной тикер, и
    fallback (например, если сигнал сгенерирован по MATIC, а в корзине
    основной тикер - POL с fallback=MATIC, всё равно найдёт). Нужно
    index_signal_generator.py, чтобы получить тир/вес монеты для поста
    об управлении долей, без хрупкого парсинга строк.

    Возвращает (tier_key, coin_dict) либо None, если тикер не из корзины."""
    ticker = ticker.upper()
    for tier_key, coins in BASKET.items():
        for coin in coins:
            if coin["ticker"].upper() == ticker or coin.get("fallback", "").upper() == ticker:
                return tier_key, coin
    return None


def compute_breadth(result: TreasuryIndexResult) -> dict:
    """Считает, сколько монет корзины сейчас в плюсе/минусе/нуле -
    быстрый "пульс" состояния корзины отдельно от взвешенного %. Это не
    дублирует total_pct: индекс может быть в плюсе даже если большинство
    монет красные - если пара тяжеловесов сильно выросла. Ширина
    показывает, насколько движение "общее" по корзине, а не тянется
    парой монет."""
    green = red = flat = 0
    for tier in result.tiers:
        for c in tier.coins:
            if c.pct is None:
                continue
            if c.pct > 0:
                green += 1
            elif c.pct < 0:
                red += 1
            else:
                flat += 1
    return {"green": green, "red": red, "flat": flat, "total": green + red + flat}


def format_breadth_line(breadth: dict) -> str:
    """Пустая строка, если по корзине вообще нет данных (полный сбой API)."""
    if breadth["total"] == 0:
        return ""
    parts = [f"🟢 {breadth['green']}"]
    if breadth["flat"]:
        parts.append(f"⚪ {breadth['flat']}")
    parts.append(f"🔴 {breadth['red']}")
    return f"Ширина: {' / '.join(parts)} из {breadth['total']} монет в плюсе/минусе"


def leading_tier(result: TreasuryIndexResult) -> Optional[TierResult]:
    """Тир с наибольшим % изменения (по модулю роста, не волатильности) -
    пригодится для короткой рефлексии в духе 'риск-аппетит возвращается',
    когда обгоняет Tier 3, и т.п. Возвращает None, если данных нет вообще."""
    ok = [t for t in result.tiers if t.pct is not None]
    if not ok:
        return None
    return max(ok, key=lambda t: t.pct)


if __name__ == "__main__":
    # Ручной прогон с реальными данными Binance (не работает в песочнице
    # без сетевого доступа к data-api.binance.vision - запускать локально).
    logging.basicConfig(level=logging.INFO)
    res = compute_index(period_hours=12)
    print(format_index_block(res))
    lt = leading_tier(res)
    if lt:
        print(f"\nЛидирует: {lt.label} ({_sign(lt.pct)}{lt.pct}%)")