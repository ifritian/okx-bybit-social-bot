#!/usr/bin/env python3
"""
Тесты treasury_heatmap.py и treasury_composition_chart.py - только
проверка того, что построение не падает и создаёт непустой файл
(визуальное качество не тестируется автоматически). Реальных сетевых
запросов нет - composition chart рисует по статичному BASKET, heatmap -
по переданным (сконструированным вручную) данным.

Отдельно (класс TestSquarify ниже) - тесты на сам алгоритм раскладки
treemap (treasury_heatmap._squarify) в изоляции от matplotlib/рендеринга -
геометрия (площадь, отсутствие наложений, попадание в границы холста)
проверяется числами, а не глазами по картинке.
"""
import math
from pathlib import Path

from treasury_index import TreasuryIndexResult, TierResult, CoinChange, BASKET
import treasury_composition_chart
import treasury_heatmap


def _make_result(coin_pcts: dict) -> TreasuryIndexResult:
    tiers = []
    for tier_key, coins in BASKET.items():
        changes = [
            CoinChange(
                ticker=c["ticker"], weight=c["weight"],
                pct=coin_pcts.get(c["ticker"]), symbol_used=f"{c['ticker']}USDT",
                suspicious=False,
            )
            for c in coins
        ]
        tiers.append(TierResult(key=tier_key, label=tier_key, pct=1.0, coins=changes))
    return TreasuryIndexResult(total_pct=1.5, period_hours=12.0, tiers=tiers, missing=[])


def test_generate_treasury_heatmap_returns_none_for_empty_tiers():
    empty_result = TreasuryIndexResult(total_pct=None, period_hours=12.0, tiers=[], missing=[])
    assert treasury_heatmap.generate_treasury_heatmap(empty_result) is None


def test_generate_treasury_heatmap_produces_file(tmp_path, monkeypatch):
    out_path = tmp_path / "heatmap.png"
    monkeypatch.setattr(treasury_heatmap, "_OUT_PATH", out_path)
    monkeypatch.setattr(treasury_heatmap, "_CHARTS_DIR", tmp_path)

    all_tickers = [c["ticker"] for coins in BASKET.values() for c in coins]
    coin_pcts = {t: (i - len(all_tickers) / 2) * 0.7 for i, t in enumerate(all_tickers)}
    result = _make_result(coin_pcts)

    path = treasury_heatmap.generate_treasury_heatmap(result)

    assert path == out_path
    assert path.exists()
    assert path.stat().st_size > 0


def test_generate_treasury_heatmap_handles_missing_and_suspicious_coins(tmp_path, monkeypatch):
    out_path = tmp_path / "heatmap.png"
    monkeypatch.setattr(treasury_heatmap, "_OUT_PATH", out_path)
    monkeypatch.setattr(treasury_heatmap, "_CHARTS_DIR", tmp_path)

    tier_key = "tier1"
    coins = BASKET[tier_key]
    changes = [
        CoinChange(ticker=coins[0]["ticker"], weight=coins[0]["weight"], pct=None, symbol_used=None),
        CoinChange(
            ticker=coins[1]["ticker"], weight=coins[1]["weight"], pct=55.0,
            symbol_used=f"{coins[1]['ticker']}USDT", suspicious=True,
        ),
    ]
    tier = TierResult(key=tier_key, label="Тест", pct=1.0, coins=changes)
    result = TreasuryIndexResult(total_pct=1.0, period_hours=12.0, tiers=[tier], missing=[])

    path = treasury_heatmap.generate_treasury_heatmap(result)

    assert path is not None
    assert path.exists()


def test_generate_composition_chart_produces_file(tmp_path, monkeypatch):
    out_path = tmp_path / "composition.png"
    monkeypatch.setattr(treasury_composition_chart, "_OUT_PATH", out_path)
    monkeypatch.setattr(treasury_composition_chart, "_CHARTS_DIR", tmp_path)

    path = treasury_composition_chart.generate_composition_chart()

    assert path == out_path
    assert path.exists()
    assert path.stat().st_size > 0


# ============================================================
# _squarify - геометрия раскладки treemap, в изоляции от рендеринга
# ============================================================

def test_squarify_preserves_total_area():
    """Сумма площадей всех прямоугольников должна точно совпасть с
    площадью холста - иначе где-то остаются "дыры" или прямоугольники
    налезают друг на друга."""
    sizes = treasury_heatmap._normalize_sizes([20, 15, 10, 10, 8, 7, 7, 5, 5, 3, 2.5, 2.5, 2, 1.5, 1.5], 100.0, 50.0)
    rects = treasury_heatmap._squarify(sizes, 0.0, 0.0, 100.0, 50.0)

    assert len(rects) == len(sizes)
    total_area = sum(w * h for _, _, w, h in rects)
    assert math.isclose(total_area, 100.0 * 50.0, rel_tol=1e-6)


def test_squarify_rects_stay_within_canvas_bounds():
    sizes = treasury_heatmap._normalize_sizes([20, 15, 10, 10, 8, 7, 7, 5, 5, 3, 2.5, 2.5, 2, 1.5, 1.5], 100.0, 50.0)
    rects = treasury_heatmap._squarify(sizes, 0.0, 0.0, 100.0, 50.0)

    for x, y, w, h in rects:
        assert w > 0 and h > 0
        assert x >= -1e-6 and y >= -1e-6
        assert x + w <= 100.0 + 1e-6
        assert y + h <= 50.0 + 1e-6


def test_squarify_larger_weight_gets_larger_area():
    """Не буквальная геометрия, а сам смысл задачи - монета с бОльшим
    весом должна получить площадь строго больше, чем монета с меньшим
    весом (нет причин для инверсии при разумной раскладке)."""
    sizes = treasury_heatmap._normalize_sizes([20, 15, 10, 10, 8, 7, 7, 5, 5, 3, 2.5, 2.5, 2, 1.5, 1.5], 100.0, 50.0)
    rects = treasury_heatmap._squarify(sizes, 0.0, 0.0, 100.0, 50.0)
    areas = [w * h for _, _, w, h in rects]

    assert areas[0] > areas[1] > areas[2]  # SOL (20) > AVAX (15) > NEAR/ARB (10)
    assert areas[0] > areas[-1]  # SOL (20) намного больше PENDLE (1.5)


def test_squarify_single_size_fills_whole_canvas():
    rects = treasury_heatmap._squarify([100.0 * 50.0], 0.0, 0.0, 100.0, 50.0)
    assert rects == [(0.0, 0.0, 100.0, 50.0)]


def test_squarify_empty_sizes_returns_empty_list():
    assert treasury_heatmap._squarify([], 0.0, 0.0, 100.0, 50.0) == []


def test_normalize_sizes_matches_canvas_area():
    normalized = treasury_heatmap._normalize_sizes([1, 1, 2], dx=10.0, dy=10.0)
    assert math.isclose(sum(normalized), 100.0, rel_tol=1e-9)
    assert normalized == [25.0, 25.0, 50.0]


def test_flatten_coins_sorted_by_weight_descending():
    result = _make_result({c["ticker"]: 1.0 for coins in BASKET.values() for c in coins})
    flat = treasury_heatmap._flatten_coins(result)

    weights = [c["weight"] for c in flat]
    assert weights == sorted(weights, reverse=True)
    assert flat[0]["ticker"] == "SOL"  # самый большой вес в корзине (20.0)
    assert {c["ticker"] for c in flat} == {c["ticker"] for coins in BASKET.values() for c in coins}


if __name__ == "__main__":
    import sys
    import tempfile
    import types

    class _MiniMonkeypatch:
        def __init__(self):
            self._restore = []

        def setattr(self, obj, name, value):
            self._restore.append((obj, name, getattr(obj, name)))
            setattr(obj, name, value)

        def undo(self):
            for obj, name, old in reversed(self._restore):
                setattr(obj, name, old)

    passed, failed = 0, 0
    module = sys.modules[__name__]
    for name in dir(module):
        if not name.startswith("test_"):
            continue
        fn = getattr(module, name)
        if not isinstance(fn, types.FunctionType):
            continue
        params = fn.__code__.co_varnames[: fn.__code__.co_argcount]
        mp = _MiniMonkeypatch()
        try:
            kwargs = {}
            tmp_dir = None
            if "tmp_path" in params:
                tmp_dir = tempfile.TemporaryDirectory()
                kwargs["tmp_path"] = Path(tmp_dir.name)
            if "monkeypatch" in params:
                kwargs["monkeypatch"] = mp
            fn(**kwargs)
            print(f"OK   {name}")
            passed += 1
        except AssertionError as e:
            print(f"FAIL {name}: {e}")
            failed += 1
        finally:
            mp.undo()
            if tmp_dir is not None:
                tmp_dir.cleanup()

    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
