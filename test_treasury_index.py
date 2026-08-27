"""
Offline-тест treasury_index.py на синтетических данных - без реальных
запросов к Binance (в песочнице/CI сеть к data-api.binance.vision может
быть недоступна). Проверяет: расчёт весов, renormalization при
пропущенных монетах, fallback MATIC->POL, форматирование.

Запуск: python test_treasury_index.py
"""
from treasury_index import (
    CoinChange, TierResult, compute_breadth, compute_index, format_breadth_line, format_index_block, leading_tier,
)

# ticker -> % изменения (синтетика). POL отсутствует специально, чтобы
# проверить fallback на MATIC.
FAKE_PCT = {
    "SOL": 2.1, "AVAX": 0.8, "NEAR": -0.5, "ARB": 1.9, "OP": 0.2,
    "AAVE": 5.1, "UNI": 2.3, "MATIC": 1.0,  # POL нет - должен сработать fallback
    "JUP": -1.2, "DYDX": 0.4,
    "SUI": 7.2, "APT": -2.0, "STRK": 3.3, "MANTA": 0.0, "PENDLE": 3.1,
}


def fake_fetch(coin: dict, period_hours: float) -> CoinChange:
    from treasury_index import OUTLIER_THRESHOLD_PCT

    primary = coin["ticker"]
    if primary in FAKE_PCT:
        pct = FAKE_PCT[primary]
        return CoinChange(
            ticker=primary, weight=coin["weight"], pct=pct, symbol_used=f"{primary}USDT",
            suspicious=abs(pct) > OUTLIER_THRESHOLD_PCT,
        )
    fallback = coin.get("fallback")
    if fallback and fallback in FAKE_PCT:
        pct = FAKE_PCT[fallback]
        return CoinChange(
            ticker=primary, weight=coin["weight"], pct=pct, symbol_used=f"{fallback}USDT",
            suspicious=abs(pct) > OUTLIER_THRESHOLD_PCT,
        )
    return CoinChange(ticker=primary, weight=coin["weight"], pct=None, symbol_used=None)


def test_basic_calc():
    result = compute_index(period_hours=12, fetch_fn=fake_fetch)

    assert result.total_pct is not None
    assert not result.missing, f"Не должно быть пропусков, а есть: {result.missing}"

    tier1 = next(t for t in result.tiers if t.key == "tier1")
    expected_tier1 = round(
        (2.1 * 20 + 0.8 * 15 + -0.5 * 10 + 1.9 * 10 + 0.2 * 5) / 60, 2
    )
    assert tier1.pct == expected_tier1, f"tier1 pct {tier1.pct} != {expected_tier1}"

    # POL должен был подхватить fallback MATIC
    pol_coin = next(c for c in next(t for t in result.tiers if t.key == "tier2").coins if c.ticker == "POL")
    assert pol_coin.pct == 1.0
    assert pol_coin.symbol_used == "MATICUSDT"

    print("test_basic_calc OK - total:", result.total_pct)


def test_missing_coin_renormalizes():
    """Если одна монета не получена - вес перераспределяется, а не теряется."""
    def fetch_missing_avax(coin, period_hours):
        if coin["ticker"] == "AVAX":
            return CoinChange(ticker="AVAX", weight=coin["weight"], pct=None, symbol_used=None)
        return fake_fetch(coin, period_hours)

    result = compute_index(period_hours=12, fetch_fn=fetch_missing_avax)
    assert "AVAX" in result.missing

    tier1 = next(t for t in result.tiers if t.key == "tier1")
    # вес AVAX (15) убран из знаменателя, остальные веса делятся на 45 вместо 60
    expected = round((2.1 * 20 + -0.5 * 10 + 1.9 * 10 + 0.2 * 5) / 45, 2)
    assert tier1.pct == expected, f"{tier1.pct} != {expected}"
    print("test_missing_coin_renormalizes OK - tier1:", tier1.pct)


def test_whole_tier_missing():
    """Если весь тир не получен - индекс всё равно считается по оставшимся тирам."""
    def fetch_nothing_in_tier3(coin, period_hours):
        if coin["ticker"] in ("SUI", "APT", "STRK", "MANTA", "PENDLE"):
            return CoinChange(ticker=coin["ticker"], weight=coin["weight"], pct=None, symbol_used=None)
        return fake_fetch(coin, period_hours)

    result = compute_index(period_hours=12, fetch_fn=fetch_nothing_in_tier3)
    tier3 = next(t for t in result.tiers if t.key == "tier3")
    assert tier3.pct is None
    assert result.total_pct is not None, "Индекс должен считаться по tier1+tier2, даже если tier3 пуст"
    print("test_whole_tier_missing OK - total:", result.total_pct)


def test_outlier_excluded_from_average_but_shown():
    """Монета с аномальным % (как DYDX +36% в реальном прогоне) не должна
    тянуть среднее тира, но должна остаться видна в тексте поста с пометкой."""
    def fetch_with_outlier(coin, period_hours):
        if coin["ticker"] == "DYDX":
            return CoinChange(ticker="DYDX", weight=coin["weight"], pct=55.0, symbol_used="DYDXUSDT", suspicious=True)
        return fake_fetch(coin, period_hours)

    result = compute_index(period_hours=12, fetch_fn=fetch_with_outlier)
    assert "DYDX" in result.suspicious
    assert "DYDX" not in result.missing  # это не "нет данных", а "данные есть, но подозрительны"

    tier2 = next(t for t in result.tiers if t.key == "tier2")
    # DYDX (вес 3) исключён из среднего - знаменатель tier2 = 27 вместо 30
    expected = round((8.0 * 5.1 + 7.0 * 2.3 + 7.0 * 1.0 + 5.0 * -1.2) / 27, 2)
    assert tier2.pct == expected, f"{tier2.pct} != {expected}"

    text = format_index_block(result)
    assert "DYDX +55.0% ⚠️" in text
    assert "аномальное движение" in text
    print("test_outlier_excluded_from_average_but_shown OK - tier2:", tier2.pct)
    print(text[:200], "...\n")


def test_format_and_leading_tier():
    result = compute_index(period_hours=12, fetch_fn=fake_fetch)
    text = format_index_block(result)
    assert "📊 Treasury Index:" in text
    assert "🔵 Фундамент" in text and "🟡 Рост" in text and "🔴 Риск" in text
    assert "POL" in text  # используем тикер аудитории, не MATIC, даже если сработал fallback

    lt = leading_tier(result)
    assert lt.key == "tier3", f"Ожидался лидер tier3 (SUI/STRK/PENDLE высокие), получили {lt.key}"

    print("test_format_and_leading_tier OK\n")
    print(text)


def test_breadth_counts_green_red_flat():
    result = compute_index(period_hours=12, fetch_fn=fake_fetch)
    breadth = compute_breadth(result)
    # По FAKE_PCT: положительных (>0) - SOL,AVAX,ARB,OP,AAVE,UNI,MATIC,DYDX,SUI,STRK,PENDLE = 11
    # отрицательных (<0) - NEAR,JUP,APT = 3; ровно 0 - MANTA = 1
    assert breadth["green"] == 11, breadth
    assert breadth["red"] == 3, breadth
    assert breadth["flat"] == 1, breadth
    assert breadth["total"] == 15, breadth

    line = format_breadth_line(breadth)
    assert "🟢 11" in line and "🔴 3" in line and "⚪ 1" in line
    print("test_breadth_counts_green_red_flat OK -", line, "\n")


def test_breadth_empty_when_no_data():
    breadth = {"green": 0, "red": 0, "flat": 0, "total": 0}
    assert format_breadth_line(breadth) == ""


if __name__ == "__main__":
    test_basic_calc()
    test_missing_coin_renormalizes()
    test_whole_tier_missing()
    test_outlier_excluded_from_average_but_shown()
    test_format_and_leading_tier()
    test_breadth_counts_green_red_flat()
    test_breadth_empty_when_no_data()
    print("\nВсе тесты прошли.")