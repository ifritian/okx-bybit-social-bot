"""
treasury_chart.py - формат "График динамики" (визуал, Этап 4, направление C).

Рисует equity curve Treasury Index против BTC/ETH/топ-4 рынка с момента
запуска (база 100 у всех линий) - использует накопленные снимки из
queue_manager.get_treasury_snapshots()/append_treasury_snapshot (по
одному на каждый пост Treasury Index, см. treasury_generator.
generate_treasury_post).

Раньше "с запуска" был просто числом в тексте (+X% индекс vs +Y% BTC) -
график даёт визуальную "историю", за которой можно следить неделями, а
не разовый снимок, и заметно увеличивает "расшариваемость" поста и на
Bluesky, и в Telegram.

Стиль - тот же тёмный, что и у chart_generator.py (свечные графики по
тикерам), для визуальной консистентности бота.
"""
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

import matplotlib
matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt

import config

logger = logging.getLogger(__name__)

_CHARTS_DIR = config.BASE_DIR / "charts"
_OUT_PATH = _CHARTS_DIR / "treasury_index_chart.png"

_BG_COLOR = "#0B0E11"
_GRID_COLOR = "#1E2329"
_AXIS_TEXT_COLOR = "#848E9C"

# Порядок в этом словаре = порядок отрисовки (фоновые линии первыми,
# индекс - последним, поверх остальных, чтобы визуально доминировать).
_LINE_STYLE = {
    "market_value": {"color": "#848E9C", "label": "Рынок (топ-4)", "width": 1.4, "alpha": 0.7},
    "eth_value": {"color": "#7B61FF", "label": "ETH", "width": 1.4, "alpha": 0.8},
    "btc_value": {"color": "#F0B90B", "label": "BTC", "width": 1.6, "alpha": 0.9},
    "index_value": {"color": "#29ABE2", "label": "Treasury Index", "width": 2.8, "alpha": 1.0},
}

# Меньше точек - график ещё не несёт пользы (пара точек выглядит как
# заглушка, а не "история") - ждём накопления данных, не рисуем раньше.
MIN_SNAPSHOTS_FOR_CHART = 3


def generate_treasury_chart(snapshots: list) -> Optional[Path]:
    """Рисует equity curve по накопленным снимкам и сохраняет в PNG,
    перезаписывая предыдущий файл (одно и то же имя - предыдущая версия
    графика никому не нужна, актуальна только последняя).

    Возвращает None, если снимков недостаточно (см. MIN_SNAPSHOTS_FOR_CHART)
    или если построение по какой-то причине не удалось - вызывающий код
    (main.py) в этом случае просто публикует пост без картинки, как и
    раньше, публикация не блокируется."""
    if len(snapshots) < MIN_SNAPSHOTS_FOR_CHART:
        logger.info(
            "Недостаточно снимков для графика Treasury Index (%d < %d) - пропускаю",
            len(snapshots), MIN_SNAPSHOTS_FOR_CHART,
        )
        return None

    try:
        dates = [datetime.fromtimestamp(s["timestamp"]) for s in snapshots]

        fig, ax = plt.subplots(figsize=(10, 5.5), dpi=150)
        fig.patch.set_facecolor(_BG_COLOR)
        ax.set_facecolor(_BG_COLOR)

        for key, style in _LINE_STYLE.items():
            values = [s.get(key, 100.0) for s in snapshots]
            ax.plot(
                dates, values, color=style["color"], label=style["label"],
                linewidth=style["width"], alpha=style["alpha"],
            )

        ax.axhline(100.0, color=_GRID_COLOR, linewidth=1.0, linestyle="--")

        ax.set_title("Treasury Index vs BTC/ETH/Рынок - с момента запуска", color="#FFFFFF", fontsize=13, pad=14)
        ax.grid(True, color=_GRID_COLOR, linewidth=0.6)
        ax.tick_params(colors=_AXIS_TEXT_COLOR)
        for spine in ax.spines.values():
            spine.set_color(_GRID_COLOR)

        ax.xaxis.set_major_formatter(mdates.DateFormatter("%d.%m"))
        fig.autofmt_xdate()

        ax.legend(loc="upper left", facecolor=_BG_COLOR, edgecolor=_GRID_COLOR, labelcolor=_AXIS_TEXT_COLOR)

        _CHARTS_DIR.mkdir(exist_ok=True)
        fig.tight_layout()
        fig.savefig(_OUT_PATH, facecolor=fig.get_facecolor())
        plt.close(fig)
    except Exception:
        logger.exception("Не удалось построить график динамики Treasury Index")
        return None

    logger.info("Сгенерирован график динамики Treasury Index (%d точек)", len(snapshots))
    return _OUT_PATH
