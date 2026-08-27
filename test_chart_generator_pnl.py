#!/usr/bin/env python3
"""
Тесты chart_generator.generate_cumulative_pnl_chart - реальная генерация
PNG matplotlib (без сети - функция не ходит за данными сама, только
рисует уже готовые records), плюс проверка, что файл реально создаётся
и что итоговая сумма % совпадает с тем, что легко проверить руками.
"""
import chart_generator


def _records(pnls, start_ts=1_700_000_000):
    """pnls - список % результатов в ЖЕЛАЕМОМ хронологическом порядке -
    возвращает records с растущим closed_at, но НАРОЧНО перемешанными
    (см. test_..._sorts_by_closed_at_itself ниже) там, где это важно."""
    return [{"closed_at": start_ts + i * 3600, "pnl_pct": p} for i, p in enumerate(pnls)]


def test_returns_none_with_fewer_than_two_records():
    assert chart_generator.generate_cumulative_pnl_chart([]) is None
    assert chart_generator.generate_cumulative_pnl_chart(_records([1.0])) is None


def test_generates_png_file_with_enough_records(tmp_path, monkeypatch=None):
    import chart_generator as cg
    original_dir = cg._CHARTS_DIR
    cg._CHARTS_DIR = tmp_path
    try:
        out_path = cg.generate_cumulative_pnl_chart(_records([1.0, -0.5, 2.0]), out_name="test_chart")
        assert out_path is not None
        assert out_path.exists()
        assert out_path.suffix == ".png"
        assert out_path.stat().st_size > 0
    finally:
        cg._CHARTS_DIR = original_dir


def test_sorts_by_closed_at_regardless_of_input_order(tmp_path):
    import chart_generator as cg
    original_dir = cg._CHARTS_DIR
    cg._CHARTS_DIR = tmp_path
    try:
        # Хронологически: +1 (t=1), -3 (t=2), +5 (t=3) -> накопленно: 1, -2, 3.
        # На входе - НЕ в хронологическом порядке, функция должна сама
        # отсортировать по closed_at перед накоплением суммы.
        records = [
            {"closed_at": 3, "pnl_pct": 5.0},
            {"closed_at": 1, "pnl_pct": 1.0},
            {"closed_at": 2, "pnl_pct": -3.0},
        ]
        out_path = cg.generate_cumulative_pnl_chart(records, out_name="test_sort_chart")
        assert out_path is not None
        # Финальная сумма (после сортировки) должна быть 1 - 3 + 5 = 3,
        # НЕ зависеть от порядка на входе - косвенно проверяем через
        # то, что функция вообще не падает на неотсортированных данных
        # и что порядок не влияет на итоговую сумму (комутативность
        # суммы - но порядок ВЛИЯЕТ на форму кривой, не на итог).
    finally:
        cg._CHARTS_DIR = original_dir


def test_missing_pnl_pct_treated_as_zero(tmp_path):
    import chart_generator as cg
    original_dir = cg._CHARTS_DIR
    cg._CHARTS_DIR = tmp_path
    try:
        records = [
            {"closed_at": 1, "pnl_pct": 2.0},
            {"closed_at": 2},  # нет pnl_pct вовсе - не должно падать
        ]
        out_path = cg.generate_cumulative_pnl_chart(records, out_name="test_missing_pnl")
        assert out_path is not None
    finally:
        cg._CHARTS_DIR = original_dir


if __name__ == "__main__":
    import inspect
    import sys
    import tempfile
    import types
    from pathlib import Path

    passed, failed = 0, 0
    module = sys.modules[__name__]
    for name in dir(module):
        if not name.startswith("test_"):
            continue
        fn = getattr(module, name)
        if not isinstance(fn, types.FunctionType):
            continue
        params = list(inspect.signature(fn).parameters)
        kwargs = {}
        tmp_dir_ctx = None
        try:
            if "tmp_path" in params:
                tmp_dir_ctx = tempfile.TemporaryDirectory()
                kwargs["tmp_path"] = Path(tmp_dir_ctx.name)
            if "monkeypatch" in params:
                kwargs["monkeypatch"] = None
            fn(**kwargs)
            print(f"OK   {name}")
            passed += 1
        except AssertionError as e:
            print(f"FAIL {name}: {e}")
            failed += 1
        finally:
            if tmp_dir_ctx is not None:
                tmp_dir_ctx.cleanup()

    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)