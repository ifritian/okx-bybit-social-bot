"""
treasury_composition_chart.py - "Диаграмма состава" (визуал, дополнение
к Этапу 4).

Показывает, ИЗ ЧЕГО состоит Treasury Index: 15 монет, сгруппированные
по трём тирам, с реальными весами (см. treasury_index.BASKET/
TIER_WEIGHTS). В отличие от treasury_heatmap.py (каждый пост, "что
произошло СЕЙЧАС") - эта диаграмма про структуру, которая меняется
только при ручной ребалансировке (см. rebalance_advisor.py), поэтому
появляется РЕДКО (см. main.try_publish_treasury_post - генерируется не
на каждый пост, а раз в config.TREASURY_COMPOSITION_INTERVAL_POSTS
постов) - периодическое напоминание "из чего это состоит" для тех, кто
не видел объяснение раньше или забыл.

Цветовая группировка по тирам - та же палитра, что и в
treasury_heatmap.py (_TIER_ACCENT), визуальная консистентность между
двумя картинками одного поста.
"""
import logging
import math
from pathlib import Path
from typing import Optional

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

import config
from treasury_index import BASKET, TIER_WEIGHTS

logger = logging.getLogger(__name__)

_CHARTS_DIR = config.BASE_DIR / "charts"
_OUT_PATH = _CHARTS_DIR / "treasury_composition.png"

_BG_COLOR = "#0B0E11"
_TEXT_COLOR = "#FFFFFF"
_MUTED_TEXT_COLOR = "#848E9C"

# Та же палитра, что и _TIER_ACCENT в treasury_heatmap.py - визуальная
# консистентность между двумя картинками одного поста.
_TIER_ACCENT = {"tier1": "#29ABE2", "tier2": "#F0B90B", "tier3": "#E4007A"}
_TIER_LABELS = {"tier1": "Фундамент", "tier2": "Рост", "tier3": "Риск"}


def generate_composition_chart() -> Optional[Path]:
    """Рисует кольцевую диаграмму состава корзины (15 монет, цвет по
    тиру, размер сектора = реальный вес монеты из BASKET) и сохраняет в
    PNG. Состав статичен между ребалансировками, поэтому не принимает
    входных данных - всегда рисует ТЕКУЩИЙ BASKET из treasury_index.py.

    Возвращает None, если построение по какой-то причине не удалось -
    вызывающий код публикует пост без этой картинки, не блокируется."""
    try:
        labels = []
        sizes = []
        colors = []

        for tier_key in ("tier1", "tier2", "tier3"):
            for coin in BASKET[tier_key]:
                labels.append(coin["ticker"])
                sizes.append(coin["weight"])
                colors.append(_TIER_ACCENT[tier_key])

        fig, ax = plt.subplots(figsize=(8, 8), dpi=150)
        fig.patch.set_facecolor(_BG_COLOR)
        ax.set_facecolor(_BG_COLOR)

        wedges, _texts = ax.pie(
            sizes, colors=colors, startangle=90, counterclock=False,
            wedgeprops={"width": 0.38, "edgecolor": _BG_COLOR, "linewidth": 2},
        )

        # Подписи тикеров вынесены за пределы кольца (а не внутри узких
        # секторов риск-тира, где 2% веса физически не оставляют места
        # для текста) - линия-выноска на каждый сектор.
        for wedge, label, size in zip(wedges, labels, sizes):
            angle_deg = (wedge.theta2 + wedge.theta1) / 2
            angle_rad = math.radians(angle_deg)
            x_inner = 0.81 * math.cos(angle_rad)
            y_inner = 0.81 * math.sin(angle_rad)
            x_outer = 1.15 * math.cos(angle_rad)
            y_outer = 1.15 * math.sin(angle_rad)
            ha = "left" if math.cos(angle_rad) >= 0 else "right"

            ax.plot([x_inner, x_outer], [y_inner, y_outer], color=_MUTED_TEXT_COLOR, linewidth=0.8)
            ax.text(
                x_outer * 1.05, y_outer * 1.05, f"${label}\n{size:g}%",
                ha=ha, va="center", fontsize=9, color=_TEXT_COLOR, fontweight="bold",
            )

        # Легенда тиров и их совокупный вес - в центре кольца.
        center_lines = [
            f"{_TIER_LABELS[t]}: {TIER_WEIGHTS[t]:g}%" for t in ("tier1", "tier2", "tier3")
        ]
        ax.text(
            0, 0, "Treasury Index\n" + "\n".join(center_lines),
            ha="center", va="center", fontsize=11, color=_TEXT_COLOR, fontweight="bold", linespacing=1.8,
        )

        ax.set_xlim(-1.6, 1.6)
        ax.set_ylim(-1.6, 1.6)
        ax.set_aspect("equal")
        ax.axis("off")

        _CHARTS_DIR.mkdir(exist_ok=True)
        fig.tight_layout()
        fig.savefig(_OUT_PATH, facecolor=fig.get_facecolor())
        plt.close(fig)
    except Exception:
        logger.exception("Не удалось построить диаграмму состава Treasury Index")
        return None

    logger.info("Сгенерирована диаграмма состава Treasury Index")
    return _OUT_PATH
