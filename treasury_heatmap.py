"""
treasury_heatmap.py - "Тепловая карта" (визуал, каждый пост Treasury Index).

Визуальная замена трёх текстовых строк с процентами по каждой монете
(см. treasury_index.format_index_block) - вместо чтения 15 чисел
подряд, одна картинка в стиле привычных крипто-хитмапов (CoinMarketCap
и т.п.): площадь плашки = вес монеты в индексе (не все монеты равны -
SOL с весом 20% занимает в 13 раз больше места, чем PENDLE с весом
1.5%), а цвет/интенсивность = сила и направление движения за период.
Читается за пару секунд - сразу видно, что здесь БОЛЬШОЕ и что двигалось
СИЛЬНЕЕ, а не только "что выросло, а что упало".

Раньше (до этой версии) карта была равномерной сеткой - все 15 плашек
одного размера, сгруппированные по тирам строками. Технически понятно,
но визуально скучно и не показывает, что вес монет в индексе сильно
разный. Теперь используется squarified treemap (Bruls/Huizing/van Wijk,
2000) - стандартный алгоритм для "плиточных" визуализаций с площадью
пропорциональной значению, без сторонней библиотеки (реализация ниже,
~40 строк, только stdlib/matplotlib).

Публикуется КАЖДЫЙ пост Treasury Index (см. main.try_publish_treasury_post
через treasury_generator.generate_treasury_post) - в отличие от
treasury_composition_chart.py (диаграмма состава), которая появляется
периодически, не каждый раз.

Стиль - тот же тёмный, что и у остальных графиков бота (chart_generator.py,
treasury_chart.py), для визуальной консистентности.
"""
import logging
from pathlib import Path
from typing import Optional

import matplotlib
matplotlib.use("Agg")
import matplotlib.colors as mcolors
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch, Circle
from matplotlib.transforms import Bbox

import config

logger = logging.getLogger(__name__)

_CHARTS_DIR = config.BASE_DIR / "charts"
_OUT_PATH = _CHARTS_DIR / "treasury_heatmap.png"

_BG_COLOR = "#0B0E11"
_UP_COLOR = "#29ABE2"
_DOWN_COLOR = "#E4007A"
_MISSING_COLOR = "#2B2F36"
_TEXT_COLOR = "#FFFFFF"
_MUTED_TEXT_COLOR = "#848E9C"

_TIER_ACCENT = {"tier1": "#29ABE2", "tier2": "#F0B90B", "tier3": "#E4007A"}

# Холст в условных единицах - ширина/высота выбраны под соотношение
# сторон ~2:1 (близко к типичным крипто-хитмапам, удобно для ленты
# Binance Square/Telegram). Сумма весов всех 15 монет всегда = 100
# (см. treasury_index.py, проверяется assert'ом при импорте), поэтому
# каждая единица веса = _CANVAS_W * _CANVAS_H / 100 площади холста.
_CANVAS_W = 100.0
_CANVAS_H = 50.0

# Движение сильнее этого % насыщает цвет плашки полностью - без этого
# один резкий выброс (см. "suspicious" в CoinChange) обесцветил бы всю
# остальную карту рядом с собой на фоне бледных обычных плашек.
_COLOR_SATURATION_PCT = 8.0

# Тонкий зазор между плашками (в тех же условных единицах холста) -
# создаёт эффект сетки, как в примере, вместо плашек впритык друг к другу.
_TILE_GAP = 0.45


# ============================================================
# Squarified treemap - раскладка прямоугольников площадью,
# пропорциональной значениям, с раскладкой, стремящейся к квадратным
# (не вытянутым в полоску) плашкам - см. Bruls M., Huizing K.,
# van Wijk J.J. "Squarified Treemaps" (2000).
# ============================================================

def _normalize_sizes(sizes: list, dx: float, dy: float) -> list:
    """Масштабирует sizes так, чтобы их сумма точно совпала с площадью
    холста dx*dy - без этого раскладка "не дотянет" до краёв или
    вылезет за них."""
    total = sum(sizes)
    area = dx * dy
    return [s * area / total for s in sizes]


def _layout_row(sizes: list, x: float, y: float, dx: float, dy: float) -> list:
    """Раскладывает sizes в один ряд вдоль КОРОТКОЙ стороны текущей
    свободной области - если сторона по x короче (dx < dy), ряд идёт
    вертикальной полосой (постоянная ширина, монеты друг под другом),
    иначе горизонтальной полосой (постоянная высота, монеты друг
    за другом).

    Критично: сумма размеров вдоль оси, по которой двигаемся между
    монетами ряда, должна В ТОЧНОСТИ покрыть dy (для dx>=dy) или dx
    (для dx<dy) - иначе _leftover ниже (который просто вычитает
    занятую полосу из dx/dy) посчитает свободной область, часть
    которой на самом деле уже занята текущим рядом, и следующий ряд
    ляжет поверх него. Это ровно то, что раньше приводило к
    перекрывающимся плашкам на карте: монеты внутри ряда двигались
    по x (общей "ширине"), а не по y (каждая своей высотой) - сумма
    высот НЕ давала dy, и часть области оставалась невидимо занятой
    сразу двумя рядами."""
    covered = sum(sizes)
    if dx >= dy:
        # свободная область "широкая" - ряд занимает фиксированную
        # ШИРИНУ (=covered/dy) и тянется вертикальной полосой на всю
        # высоту dy: монеты стоят друг под другом, каждая своей
        # высотой (сумма высот по построению точно равна dy).
        width = covered / dy
        rects, cy = [], y
        for s in sizes:
            h = s / width
            rects.append((x, cy, width, h))
            cy += h
        return rects
    # свободная область "высокая" - ряд занимает фиксированную ВЫСОТУ
    # (=covered/dx) и тянется горизонтальной полосой на всю ширину dx:
    # монеты стоят друг за другом, каждая своей шириной (сумма ширин
    # по построению точно равна dx).
    height = covered / dx
    rects, cx = [], x
    for s in sizes:
        w = s / height
        rects.append((cx, y, w, height))
        cx += w
    return rects


def _leftover(sizes: list, x: float, y: float, dx: float, dy: float) -> tuple:
    """Область, оставшаяся свободной ПОСЛЕ того, как sizes уложены
    текущим рядом (см. _layout_row) - именно в неё укладывается
    следующий ряд при рекурсии."""
    covered = sum(sizes)
    if dx >= dy:
        width = covered / dy
        return (x + width, y, dx - width, dy)
    height = covered / dx
    return (x, y + height, dx, dy - height)


def _worst_ratio(sizes: list, x: float, y: float, dx: float, dy: float) -> float:
    """Худшее (максимальное) отношение сторон среди прямоугольников,
    которые получатся, если уложить sizes текущим рядом - чем ближе к
    1.0, тем "квадратнее" плашки. Squarify на каждом шаге сравнивает
    это значение до/после добавления следующего элемента в ряд, чтобы
    решить, стоит ли его добавлять (см. squarify ниже)."""
    rects = _layout_row(sizes, x, y, dx, dy)
    return max(max(w / h, h / w) for _, _, w, h in rects)


def _squarify(sizes: list, x: float, y: float, dx: float, dy: float) -> list:
    """Возвращает список (x, y, w, h) в ТОМ ЖЕ порядке, что и sizes.
    sizes должны быть отсортированы по убыванию (не обязательно строго,
    но так раскладка получается заметно аккуратнее) и в сумме давать
    dx*dy (см. _normalize_sizes)."""
    sizes = [s for s in sizes if s > 0]
    if not sizes:
        return []
    if len(sizes) == 1:
        return _layout_row(sizes, x, y, dx, dy)

    # Наращиваем текущий ряд, пока добавление следующего элемента
    # улучшает (уменьшает) худшее соотношение сторон - как только
    # начинает ухудшаться, закрываем ряд и уходим в оставшуюся область.
    i = 1
    while i < len(sizes) and _worst_ratio(sizes[:i], x, y, dx, dy) >= _worst_ratio(sizes[:i + 1], x, y, dx, dy):
        i += 1

    current, remaining = sizes[:i], sizes[i:]
    rects = _layout_row(current, x, y, dx, dy)
    nx, ny, ndx, ndy = _leftover(current, x, y, dx, dy)
    rects.extend(_squarify(remaining, nx, ny, ndx, ndy))
    return rects


# Минимальный читаемый размер шрифта (в pt) - ниже него подпись скорее
# мешает, чем помогает, лучше вообще её не показывать (см. _place_label).
_MIN_READABLE_FONTSIZE = 5.0

# На сколько "ужимаем" реальные габариты плашки перед тем, как в них
# что-то вписывать - без этого текст мог бы формально "влезать" впритык
# к рамке плашки, что выглядит так же тесно/криво, как и лёгкий overflow.
_TEXT_PADDING_FACTOR = 0.86


def _clip_to_tile(artist, ax, rx: float, ry: float, rw: float, rh: float) -> None:
    """Жёстко обрезает artist (текст) по границам ЕГО СОБСТВЕННОЙ плашки
    в display-координатах (пиксели финального рендера) - страховка на
    случай, если измерение текста (см. _fit_text_in_box) немного
    ошиблось: лучше едва заметно обрезанная буква с краю, чем текст,
    заезжающий на соседний остров, как было в старой версии карты."""
    corners = ax.transData.transform([(rx, ry), (rx + rw, ry + rh)])
    x0, x1 = sorted((corners[0, 0], corners[1, 0]))
    y0, y1 = sorted((corners[0, 1], corners[1, 1]))
    artist.set_clip_on(True)
    artist.set_clip_box(Bbox.from_extents(x0, y0, x1, y1))


def _fit_text_in_box(
    ax, renderer, text_str: str, cx: float, cy: float,
    max_width_data: float, max_height_data: float,
    max_fontsize: float, **text_kwargs,
):
    """Создаёт текстовый объект и подбирает для него РЕАЛЬНЫЙ (измеренный
    рендерером, а не оценённый на глаз по short_side, как раньше)
    размер шрифта так, чтобы отрендеренный текст помещался в прямоугольник
    max_width_data x max_height_data (в тех же условных единицах холста,
    что и сама плашка). Если текст не помещается даже на минимальном
    читаемом размере - текст удаляется, вызывающий код показывает
    что-то более короткое (или не показывает вообще, если совсем нечего).

    Возвращает (text_or_None, fontsize)."""
    max_w_px, max_h_px = ax.transData.transform(
        (max_width_data, max_height_data)
    ) - ax.transData.transform((0, 0))
    max_w_px, max_h_px = abs(max_w_px), abs(max_h_px)

    fontsize = max_fontsize
    txt = ax.text(cx, cy, text_str, fontsize=fontsize, **text_kwargs)
    while True:
        bbox = txt.get_window_extent(renderer=renderer)
        if bbox.width <= max_w_px and bbox.height <= max_h_px:
            return txt, fontsize
        if fontsize <= _MIN_READABLE_FONTSIZE:
            txt.remove()
            return None, 0.0
        fontsize = max(fontsize * 0.88, _MIN_READABLE_FONTSIZE)
        txt.set_fontsize(fontsize)


def _draw_warning_badge(ax, rx: float, ry: float, rw: float, rh: float) -> None:
    """Рисует значок аномалии в правом верхнем углу плашки - тёмный
    кружок с золотым "!" ФИКСИРОВАННОГО, всегда контрастного вида,
    независимо от цвета самой плашки и от того, как ужался авто-подгон
    текста тикера/%. Раньше "⚠️" был просто приклеен к строке процента
    (см. старую версию) - на маленьких плашках или при длинном тикере
    он сливался с текстом и был еле различим. Теперь это отдельный
    элемент, который не зависит от auto-fit текста и всегда одного и
    того же вида - его видно и на самой мелкой, и на самой крупной
    плашке одинаково однозначно."""
    short_side = min(rw, rh)
    if short_side < 2.2:
        # Плашка совсем крошечная - золотая рамка тайла (см. edgecolor
        # в generate_treasury_heatmap) уже сигнализирует про аномалию,
        # значок только замусорил бы и без того тесное место.
        return

    radius = max(min(short_side * 0.16, 1.7), 0.55)
    bx = rx + rw - radius * 1.35
    by = ry + radius * 1.35
    badge = Circle(
        (bx, by), radius,
        facecolor="#3A2E00", edgecolor="#FFD700", linewidth=1.2, zorder=5,
    )
    ax.add_patch(badge)
    ax.text(
        bx, by, "!", ha="center", va="center",
        fontsize=max(radius * 11, 7), fontweight="bold", color="#FFD700", zorder=6,
    )


def _tile_color(pct: Optional[float]) -> tuple:
    if pct is None:
        return mcolors.to_rgb(_MISSING_COLOR)

    clipped = max(min(pct, _COLOR_SATURATION_PCT), -_COLOR_SATURATION_PCT)
    intensity = abs(clipped) / _COLOR_SATURATION_PCT  # 0..1
    base = mcolors.to_rgb(_UP_COLOR if pct >= 0 else _DOWN_COLOR)
    bg = mcolors.to_rgb(_BG_COLOR)
    # Слабое движение - тайл почти сливается с фоном (мало насыщенности,
    # смысл в том, что взгляд сразу цепляется за сильные движения, а не
    # за все 15 плашек одинаково ярко).
    return tuple(bg[i] + (base[i] - bg[i]) * (0.15 + 0.85 * intensity) for i in range(3))


def _flatten_coins(result) -> list:
    """Собирает все монеты всех тиров в один плоский список (тикер,
    вес, %, tier_key, suspicious), отсортированный по весу по убыванию -
    и порядок для эстетики squarify (см. _squarify), и он же естественно
    ставит самые весомые монеты индекса (SOL, AVAX...) в левый верхний
    угол карты, на самые заметные плашки - ровно то же место, которое в
    привычных крипто-хитмапах занимает BTC/ETH."""
    flat = []
    for tier in result.tiers:
        for coin in tier.coins:
            flat.append({
                "ticker": coin.ticker,
                "weight": coin.weight,
                "pct": coin.pct,
                "tier_key": tier.key,
                "suspicious": coin.suspicious,
            })
    flat.sort(key=lambda c: c["weight"], reverse=True)
    return flat


def generate_treasury_heatmap(result) -> Optional[Path]:
    """Рисует тепловую карту-treemap по всем монетам result.tiers и
    сохраняет в PNG, перезаписывая предыдущий файл. Возвращает None при
    ошибке построения - вызывающий код публикует пост без картинки, не
    блокируется."""
    coins = _flatten_coins(result)
    if not coins:
        return None

    try:
        sizes = _normalize_sizes([c["weight"] for c in coins], _CANVAS_W, _CANVAS_H)
        rects = _squarify(sizes, 0.0, 0.0, _CANVAS_W, _CANVAS_H)

        fig, ax = plt.subplots(figsize=(13, 6.8), dpi=150)
        fig.patch.set_facecolor(_BG_COLOR)
        ax.set_facecolor(_BG_COLOR)

        # Оси/заголовок/шапку с процентами по тирам фиксируем ДО того,
        # как будем мерить и вписывать подписи монет - tight_layout ниже
        # меняет итоговое положение осей на холсте (под заголовок и
        # шапку), а нам для честного измерения текста (см.
        # _fit_text_in_box/_clip_to_tile) нужны уже ФИНАЛЬНЫЕ
        # display-координаты плашек, а не те, что были бы до подгонки
        # layout'а - иначе поймали бы ту же рассинхронизацию, из-за
        # которой подписи и вылезали за края плашек раньше.
        ax.set_xlim(0, _CANVAS_W)
        ax.set_ylim(0, _CANVAS_H)
        ax.invert_yaxis()  # самые крупные монеты - сверху, как в примере, а не снизу
        ax.axis("off")

        total_str = f"{result.total_pct:+.2f}%" if result.total_pct is not None else "н/д"
        ax.set_title(
            f"Treasury Index за {result.period_hours:g}ч: {total_str}",
            color=_TEXT_COLOR, fontsize=14, fontweight="bold", pad=12, loc="left",
        )

        tier_bits = []
        for tier in result.tiers:
            label = tier.label.split(" ", 1)[-1] if " " in tier.label else tier.label
            tier_pct_str = f"{tier.pct:+.2f}%" if tier.pct is not None else "н/д"
            tier_bits.append(f"{label} {tier_pct_str}")
        if tier_bits:
            fig.text(
                0.01, 0.955, "  ·  ".join(tier_bits),
                color=_MUTED_TEXT_COLOR, fontsize=10, ha="left", va="top",
            )

        _CHARTS_DIR.mkdir(exist_ok=True)
        # Нижний отступ резервируем только если он реально нужен (есть
        # хотя бы одна аномальная монета) - иначе обычный пост без
        # предупреждений не терял бы впустую место под карту.
        bottom_margin = 0.06 if result.suspicious else 0.0
        fig.tight_layout(rect=(0, bottom_margin, 1, 0.93))
        # Финализирует расположение осей на холсте и даёт renderer,
        # которым _fit_text_in_box реально измеряет отрисованный текст -
        # без этого шага get_window_extent() вернул бы координаты по
        # ещё не готовому layout'у.
        renderer = fig.canvas.get_renderer()

        for coin, (x, y, w, h) in zip(coins, rects):
            # Небольшой зазор со всех сторон вместо плашек впритык -
            # но не больше половины меньшей стороны, иначе совсем
            # маленькие плашки (низкий вес) схлопнутся в точку.
            gap = min(_TILE_GAP, w * 0.15, h * 0.15)
            rx, ry, rw, rh = x + gap, y + gap, w - 2 * gap, h - 2 * gap
            if rw <= 0 or rh <= 0:
                continue

            color = _tile_color(coin["pct"])
            rounding = max(min(rw, rh) * 0.06, 0.15)
            rect = FancyBboxPatch(
                (rx, ry), rw, rh,
                boxstyle=f"round,pad=0,rounding_size={rounding}",
                linewidth=1.3 if coin["suspicious"] else 0.8,
                edgecolor="#FFD700" if coin["suspicious"] else _TIER_ACCENT.get(coin["tier_key"], _BG_COLOR),
                facecolor=color,
            )
            ax.add_patch(rect)

            # Верхняя граница размера шрифта, от которой стартует
            # подгонка (см. _fit_text_in_box) - зависит от МЕНЬШЕЙ
            # стороны плашки, как и раньше, но теперь это только
            # ОТПРАВНАЯ точка: реальный размер ниже подбирается по
            # фактически измеренному тексту, а не берётся как есть, так
            # что длинные тикеры (AVAX, PENDLE, DYDX...) в узких плашках
            # больше не вылезают за её границы.
            short_side = min(rw, rh)
            cx, cy = rx + rw / 2, ry + rh / 2
            pct_str = f"{coin['pct']:+.1f}%" if coin["pct"] is not None else "н/д"

            pad_w, pad_h = rw * _TEXT_PADDING_FACTOR, rh * _TEXT_PADDING_FACTOR

            if short_side >= 4:
                # Тикер сверху, % снизу - каждая строка получает свою
                # половину высоты плашки под измерение, чтобы линии
                # гарантированно не наезжали друг на друга по вертикали.
                ticker_max_fs = min(15 + short_side * 0.9, 34) if short_side >= 9 else max(short_side * 1.15, 7)
                pct_max_fs = min(9 + short_side * 0.35, 16) if short_side >= 9 else max(short_side * 0.9, 6)

                ticker_txt, ticker_fs = _fit_text_in_box(
                    ax, renderer, f"${coin['ticker']}", cx, cy + rh * 0.22,
                    pad_w, pad_h * 0.42, ticker_max_fs,
                    ha="center", va="center", fontweight="bold", color=_TEXT_COLOR,
                )
                if ticker_txt is not None:
                    _clip_to_tile(ticker_txt, ax, rx, ry, rw, rh)

                pct_txt, _ = _fit_text_in_box(
                    ax, renderer, pct_str, cx, cy - rh * 0.22,
                    pad_w, pad_h * 0.34, pct_max_fs,
                    ha="center", va="center", color=_TEXT_COLOR,
                )
                if pct_txt is not None:
                    _clip_to_tile(pct_txt, ax, rx, ry, rw, rh)

                # Если даже тикер (более короткая строка) не влез хотя
                # бы на минимальном читаемом размере - плашка слишком
                # тесная для двух строк, пробуем один только тикер по
                # центру (см. ветку ниже), а не оставляем пустоту.
                if ticker_txt is None and short_side >= 1.8:
                    solo_txt, _ = _fit_text_in_box(
                        ax, renderer, coin["ticker"], cx, cy,
                        pad_w, pad_h, max(short_side * 1.4, 5.5),
                        ha="center", va="center", fontweight="bold", color=_TEXT_COLOR,
                    )
                    if solo_txt is not None:
                        _clip_to_tile(solo_txt, ax, rx, ry, rw, rh)
            elif short_side >= 1.8:
                # Совсем маленькая плашка (низковесные монеты tier3) -
                # только тикер, без % (не влезет читаемо в обе строки).
                solo_txt, _ = _fit_text_in_box(
                    ax, renderer, coin["ticker"], cx, cy,
                    pad_w, pad_h, max(short_side * 1.4, 5.5),
                    ha="center", va="center", fontweight="bold", color=_TEXT_COLOR,
                )
                if solo_txt is not None:
                    _clip_to_tile(solo_txt, ax, rx, ry, rw, rh)
            # Ещё меньше - оставляем плашку голой (только цвет), подпись
            # была бы нечитаемой кашей - площадь и цвет уже несут сигнал.

            if coin["suspicious"]:
                _draw_warning_badge(ax, rx, ry, rw, rh)

        if result.suspicious:
            fig.text(
                0.01, 0.01,
                "⚠ выброс/аномалия - исключено из расчёта среднего тира",
                color="#FFD700", fontsize=9, ha="left", va="bottom",
            )

        fig.savefig(_OUT_PATH, facecolor=fig.get_facecolor())
        plt.close(fig)
    except Exception:
        logger.exception("Не удалось построить тепловую карту Treasury Index")
        return None

    logger.info("Сгенерирована тепловая карта Treasury Index (treemap)")
    return _OUT_PATH
