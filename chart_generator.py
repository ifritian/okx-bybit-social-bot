"""
Генерация графика цены для тикера - японские свечи + MA-линии + объём
снизу, в стиле, максимально похожем на сам Binance (карточка торговой
пары + водяной знак-ромб + плашка с ценой справа).

Данные берутся из того же источника, по которому сканер (scanner.py)
и нашёл сигнал: data-api.binance.vision - публичное зеркало рыночных
данных Binance без авторизации и без гео-ограничений (в отличие от
обычного api.binance.com). Раз сканер увидел этот тикер - значит у
Binance по определению есть свечи по этой паре, так что тикер не
может "не найтись" или внезапно оказаться другой монетой с тем же
символом (раньше график рисовался через CoinGecko, где это бывало).
"""
import logging
from datetime import datetime, timezone
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle, FancyBboxPatch
import requests

import config

logger = logging.getLogger(__name__)

_BASE_URL = "https://data-api.binance.vision/api/v3"
_CHARTS_DIR = config.BASE_DIR / "charts"

# Неоновая палитра (по референсу пользователя - коктейль на тёмно-синем
# фоне): циан для роста, маджента для падения - вместо стандартных
# Binance-цветов (#0ECB81/#F6465D).
_UP_COLOR = "#29ABE2"
_DOWN_COLOR = "#E4007A"
_BG_COLOR = "#0B0E11"
_GRID_COLOR = "#1E2329"
_AXIS_TEXT_COLOR = "#848E9C"
_WATERMARK_COLOR = "#FFFFFF"

# Цвета MA-линий. MA25 намеренно НЕ маджента/розовый (как раньше) -
# на фоне такого же по тону down-цвета свечей (#E4007A) линия бы
# сливалась с телами свечей и переставала читаться. Светлый
# нейтральный цвет держит контраст с обоими цветами свечей сразу.
_MA_COLORS = {7: "#F0B90B", 25: "#E8E8E8", 99: "#7B61FF"}

# Страховка на случай ошибок форматирования тикера и т.п. - на практике
# не должна срабатывать, раз график и сигнал берут данные из одного и
# того же места и по одному и тому же символу.
MAX_PRICE_MISMATCH_RATIO = 3.0

# (interval, limit, формат подписи времени по оси X) для каждого периода.
# Лимит klines у Binance - 1000 свечей за запрос, так что запас большой.
_INTERVAL_BY_DAYS = {
    2: ("1h", 48, "%H:%M"),
    7: ("4h", 42, "%d.%m"),
}


def symbol_exists(ticker: str) -> bool:
    """Быстрая проверка (без скачивания свечей - один короткий запрос
    цены), торгуется ли SYMBOLUSDT на Binance вообще.

    Нужна, чтобы отсеивать сигналы для тикеров НЕ с Binance ещё на
    этапе парсинга (main.check_for_new_signals), а не только когда
    generate_chart_image уже упадёт с 400 в момент публикации. Такие
    сигналы иногда приходят от стороннего бота (@syndicateproobot) -
    например, однобуквенный тикер вроде \"O\", который на самом деле
    NYSE-акция, а не крипто-пара, и на Binance его, конечно, нет.

    При сетевой ошибке ИЛИ неожиданном статусе - возвращает True (не
    блокируем сигнал из-за временного сбоя самой проверки, а не
    реального отсутствия пары); False - только при точном 400/404 от
    Binance, означающем \"такого символа не существует\"."""
    clean_ticker = ticker.replace("USDT", "").upper()
    symbol = f"{clean_ticker}USDT"

    try:
        resp = requests.get(f"{_BASE_URL}/ticker/price", params={"symbol": symbol}, timeout=10)
    except requests.RequestException as e:
        logger.warning("Не удалось проверить существование пары %s: %s - пропускаю проверку, разрешаю", symbol, e)
        return True

    if resp.status_code in (400, 404):
        logger.info("Пара %s не торгуется на Binance (статус %s) - тикер будет отсеян", symbol, resp.status_code)
        return False

    if not resp.ok:
        logger.warning("Неожиданный статус %s при проверке пары %s - пропускаю проверку, разрешаю", resp.status_code, symbol)
        return True

    return True


def fetch_klines(ticker: str, days: int = 2) -> list[dict]:
    """Возвращает свечи (open_time, open, high, low, close, volume) с Binance.
    ticker - без USDT (например, "PHB" или "BTC")."""
    clean_ticker = ticker.replace("USDT", "").upper()
    symbol = f"{clean_ticker}USDT"
    interval, limit, _ = _INTERVAL_BY_DAYS.get(days, ("1h", days * 24, "%H:%M"))

    try:
        resp = requests.get(
            f"{_BASE_URL}/klines",
            params={"symbol": symbol, "interval": interval, "limit": limit},
            timeout=15,
        )
        resp.raise_for_status()
        rows = resp.json()
    except requests.RequestException as e:
        logger.warning("Не удалось получить свечи Binance для графика %s: %s", symbol, e)
        return []

    if not isinstance(rows, list):
        # Binance отвечает {"code": ..., "msg": ...} для несуществующего символа
        logger.warning("Binance вернул неожиданный ответ для графика %s: %s", symbol, rows)
        return []

    try:
        return [
            {
                "open_time": int(r[0]),
                "open": float(r[1]),
                "high": float(r[2]),
                "low": float(r[3]),
                "close": float(r[4]),
                "volume": float(r[5]),
            }
            for r in rows
        ]
    except (IndexError, ValueError, TypeError) as e:
        logger.warning("Не удалось разобрать свечи Binance для графика %s: %s", symbol, e)
        return []


def _format_price(price: float) -> str:
    if price >= 100:
        return f"{price:,.2f}"
    if price >= 1:
        return f"{price:.4f}"
    return f"{price:.6f}".rstrip("0").rstrip(".")


def _moving_average(closes: list[float], period: int) -> list[float | None]:
    """MA как на Binance: точка появляется только когда накопилось
    достаточно свечей для периода (первые period-1 точек - None)."""
    out: list[float | None] = []
    for i in range(len(closes)):
        if i + 1 < period:
            out.append(None)
        else:
            out.append(sum(closes[i + 1 - period:i + 1]) / period)
    return out


def _draw_candles(ax, candles: list[dict]) -> None:
    highs = [c["high"] for c in candles]
    lows = [c["low"] for c in candles]
    price_span = max(highs) - min(lows) or 1.0
    min_body_height = price_span * 0.0015  # видимая тонкая линия вместо нулевой свечи-доджи

    width = 0.6
    for i, c in enumerate(candles):
        color = _UP_COLOR if c["close"] >= c["open"] else _DOWN_COLOR
        ax.plot([i, i], [c["low"], c["high"]], color=color, linewidth=1, solid_capstyle="round")
        body_low = min(c["open"], c["close"])
        body_height = max(abs(c["close"] - c["open"]), min_body_height)
        ax.add_patch(Rectangle((i - width / 2, body_low), width, body_height, color=color, linewidth=0))


def _draw_moving_averages(ax, candles: list[dict]) -> None:
    closes = [c["close"] for c in candles]
    for period, color in _MA_COLORS.items():
        if len(closes) < period:
            continue  # недостаточно данных для этого периода - просто не рисуем линию
        ma = _moving_average(closes, period)
        xs = [i for i, v in enumerate(ma) if v is not None]
        ys = [v for v in ma if v is not None]
        ax.plot(xs, ys, color=color, linewidth=1.1, alpha=0.9, zorder=3)


def _draw_volume(ax, candles: list[dict]) -> None:
    """Объём снизу - столбики цветом по свече (рост/падение), с отступом
    сверху (чтобы самый высокий бар не упирался в границу панели) и
    тонкой горизонтальной сеткой на паре уровней в цвет сетки основного
    графика - без этого плоские полупрозрачные бруски выглядели как
    отдельная, "приклеенная" диаграмма, а не органичное продолжение
    свечного графика, как на настоящих биржевых терминалах."""
    width = 0.6
    volumes = [c["volume"] for c in candles]
    max_vol = max(volumes) if volumes else 1.0

    for i, c in enumerate(candles):
        color = _UP_COLOR if c["close"] >= c["open"] else _DOWN_COLOR
        ax.add_patch(Rectangle((i - width / 2, 0), width, c["volume"], color=color, alpha=0.55, linewidth=0))

    # Отступ сверху ~25% - бары "дышат", не упираются в край панели
    ax.set_ylim(0, max_vol * 1.25)

    # Лёгкая горизонтальная сетка на 2 уровнях - визуально связывает
    # панель объёма с основным графиком (та же сетка, тот же цвет).
    for frac in (0.25, 0.75):
        ax.axhline(max_vol * frac, color=_GRID_COLOR, linewidth=0.5, zorder=0)


def _draw_watermark(ax, text: str | None = "BINANCE") -> None:
    """Полупрозрачный ромб + надпись по центру графика, как водяной
    знак на скриншотах с самой площадки. text=None - не рисовать
    вообще (используется для графиков под другие площадки, см.
    okx_orbit_generator.generate_chart_for_post - водяной знак BINANCE
    там неуместен)."""
    if text is None:
        return
    ax.text(
        0.5, 0.52, "◆", transform=ax.transAxes, ha="center", va="center",
        fontsize=46, color=_WATERMARK_COLOR, alpha=0.05, zorder=0,
    )
    ax.text(
        0.5, 0.46, text, transform=ax.transAxes, ha="center", va="center",
        fontsize=20, color=_WATERMARK_COLOR, alpha=0.05, fontweight="bold",
        family="monospace", zorder=0,
    )


def _draw_price_tag(ax, price: float, color: str) -> None:
    """Плашка с текущей ценой у правого края графика, на уровне
    последнего закрытия - как "Last Price" бирка на самом Binance."""
    ax.annotate(
        _format_price(price),
        xy=(1.0, price), xycoords=("axes fraction", "data"),
        xytext=(8, 0), textcoords="offset points",
        ha="left", va="center", fontsize=8.5, color="#0B0E11", fontweight="bold",
        bbox=dict(boxstyle="round,pad=0.3", facecolor=color, edgecolor="none"),
        annotation_clip=False, zorder=5,
    )


def _style_axis(ax, show_xticks: bool = False) -> None:
    ax.set_facecolor(_BG_COLOR)
    ax.tick_params(colors=_AXIS_TEXT_COLOR, labelsize=8)
    for spine in ax.spines.values():
        spine.set_color(_GRID_COLOR)
    ax.grid(color=_GRID_COLOR, linewidth=0.6, axis="y")
    ax.yaxis.tick_right()
    if not show_xticks:
        ax.set_xticks([])


def _draw_percent_tag(ax, value: float, color: str) -> None:
    """Плашка со значением в % у правого края графика - тот же стиль,
    что и _draw_price_tag (цена), но для % вместо абсолютной цены (см.
    generate_cumulative_pnl_chart)."""
    ax.annotate(
        f"{value:+.2f}%",
        xy=(1.0, value), xycoords=("axes fraction", "data"),
        xytext=(8, 0), textcoords="offset points",
        ha="left", va="center", fontsize=8.5, color="#0B0E11", fontweight="bold",
        bbox=dict(boxstyle="round,pad=0.3", facecolor=color, edgecolor="none"),
        annotation_clip=False, zorder=5,
    )


def generate_cumulative_pnl_chart(records: list[dict], out_name: str = "weekly_digest_pnl") -> Path | None:
    """Линейный график накопленного результата по закрытым сигналам
    (C1 в роадмапе: "визуальное подтверждение результата воспринимается
    убедительнее текстовых цифр"), в том же визуальном стиле, что и
    generate_chart_image (тёмный фон, водяной знак, плашка со значением
    справа) - чтобы смотрелось частью той же серии графиков, а не
    отдельным, чужеродным типом картинки.

    records - список словарей с полями "closed_at" (unix-время закрытия)
    и "pnl_pct" (% результат конкретного сигнала) - обычно
    queue_manager.get_closed_outcomes(), уже отфильтрованный по периоду
    вызывающим кодом (см. accuracy_report_generator.generate_accuracy_report_post).
    Сортируются по closed_at здесь же - порядок на входе не важен.

    ВАЖНО про смысл кривой: это ПРОСТАЯ СУММА pnl_pct по сигналам одного
    за другим ("что было бы, если бы на каждый сигнал ставилась примерно
    одна и та же доля капитала, БЕЗ реинвестирования прибыли") - та же
    намеренно простая договорённость, что уже используется в
    outcome_tracker.get_accuracy_stats (avg_pnl_pct - обычное среднее, не
    взвешенное и не сложный процент). Это НЕ график реального баланса
    счёта - подпись на графике явно говорит "сумма % результата", а не
    "доходность депозита", чтобы не создавать ложное впечатление
    точности там, где её нет.

    Возвращает None, если точек меньше 2 (линию из одной точки рисовать
    не за чем - как и generate_chart_image, который тоже требует
    минимум 2 свечи)."""
    if len(records) < 2:
        logger.info("Недостаточно точек (%d) для кумулятивного графика PnL - пропускаю", len(records))
        return None

    sorted_records = sorted(records, key=lambda r: r.get("closed_at", 0))
    cumulative: list[float] = []
    running = 0.0
    for r in sorted_records:
        running += r.get("pnl_pct", 0) or 0.0
        cumulative.append(running)

    xs = list(range(len(cumulative)))
    final = cumulative[-1]
    line_color = _UP_COLOR if final >= 0 else _DOWN_COLOR

    _CHARTS_DIR.mkdir(exist_ok=True)
    out_path = _CHARTS_DIR / f"{out_name}.png"

    fig = plt.figure(figsize=(8, 4.5), dpi=150)
    fig.patch.set_facecolor(_BG_COLOR)
    ax = fig.add_axes((0.05, 0.12, 0.87, 0.68))

    _draw_watermark(ax)
    ax.axhline(0, color=_GRID_COLOR, linewidth=0.8, zorder=1)
    ax.fill_between(xs, cumulative, 0, color=line_color, alpha=0.12, zorder=2)
    ax.plot(xs, cumulative, color=line_color, linewidth=1.8, zorder=3)
    _style_axis(ax, show_xticks=False)
    ax.set_xlim(0, len(cumulative) - 1)
    _draw_percent_tag(ax, final, line_color)

    fig.text(0.05, 0.94, "Накопленный результат сигналов", color="white", fontsize=15, fontweight="bold", va="top")
    fig.text(
        0.05, 0.895,
        f"{len(cumulative)} закрытых сигналов подряд, сумма % результата: {final:+.2f}%",
        color=line_color, fontsize=10, fontweight="bold", va="top",
    )

    fig.savefig(out_path, facecolor=fig.get_facecolor())
    plt.close(fig)

    logger.info("Кумулятивный график PnL сохранён (%d точек, итог %+.2f%%): %s", len(cumulative), final, out_path)
    return out_path


def generate_chart_image(ticker: str, days: int = 2, expected_price: float | None = None,
                          watermark_text: str | None = "BINANCE", filename_suffix: str = "") -> Path | None:
    """
    Возвращает путь к PNG со свечным графиком тикера (MA7/25/99 +
    объём снизу + водяной знак, в стиле самого Binance), либо None.

    watermark_text - см. _draw_watermark (None - без водяного знака,
    для графиков под другие площадки, см. okx_orbit_generator.py).
    filename_suffix - добавляется к имени файла (например "_okx"),
    чтобы графики под разные площадки для одного тикера не
    перезаписывали друг друга при параллельной генерации.
    """
    try:
        candles = fetch_klines(ticker, days)
    except Exception as e:
        logger.warning("Ошибка при получении данных для графика %s: %s", ticker, e)
        return None

    if len(candles) < 2:
        logger.warning("Недостаточно данных для графика %s", ticker)
        return None

    last_close = candles[-1]["close"]

    if expected_price is not None and expected_price > 0 and last_close > 0:
        ratio = max(last_close, expected_price) / min(last_close, expected_price)
        if ratio > MAX_PRICE_MISMATCH_RATIO:
            logger.warning(
                "График %s отбракован: последняя цена с Binance (%.10g) сильно "
                "отличается от цены сигнала (%.10g) - %.1fx. Публикация без графика.",
                ticker, last_close, expected_price, ratio,
            )
            return None

    first_open = candles[0]["open"]
    change_pct = (last_close - first_open) / first_open * 100 if first_open else 0.0
    header_color = _UP_COLOR if change_pct >= 0 else _DOWN_COLOR
    arrow = "▲" if change_pct >= 0 else "▼"

    _, _, time_fmt = _INTERVAL_BY_DAYS.get(days, ("1h", days * 24, "%H:%M"))

    _CHARTS_DIR.mkdir(exist_ok=True)
    out_path = _CHARTS_DIR / f"{ticker}{filename_suffix}_chart.png"

    fig = plt.figure(figsize=(8, 5), dpi=150)
    fig.patch.set_facecolor(_BG_COLOR)
    gs = fig.add_gridspec(2, 1, height_ratios=(3.2, 1), hspace=0.05, left=0.04, right=0.90, top=0.83, bottom=0.08)
    ax_price = fig.add_subplot(gs[0])
    ax_vol = fig.add_subplot(gs[1], sharex=ax_price)

    _draw_watermark(ax_price, watermark_text)
    _draw_candles(ax_price, candles)
    _draw_moving_averages(ax_price, candles)
    _style_axis(ax_price, show_xticks=False)
    ax_price.set_xlim(-1, len(candles))
    _draw_price_tag(ax_price, last_close, header_color)

    _draw_volume(ax_vol, candles)
    _style_axis(ax_vol, show_xticks=True)
    ax_vol.set_xlim(-1, len(candles))
    ax_vol.set_yticks([])

    tick_count = min(5, len(candles))
    tick_positions = [int(i * (len(candles) - 1) / (tick_count - 1)) for i in range(tick_count)] if tick_count > 1 else [0]
    tick_labels = [
        datetime.fromtimestamp(candles[i]["open_time"] / 1000, tz=timezone.utc).strftime(time_fmt)
        for i in tick_positions
    ]
    ax_vol.set_xticks(tick_positions)
    ax_vol.set_xticklabels(tick_labels)

    # Заголовок в духе карточки монеты на Binance: тикер + текущая цена +
    # изменение за период, цветом по направлению.
    fig.text(0.04, 0.96, f"{ticker}/USDT", color="white", fontsize=15, fontweight="bold", va="top")
    fig.text(
        0.04, 0.915,
        f"{_format_price(last_close)}  {arrow} {change_pct:+.2f}%",
        color=header_color, fontsize=11, fontweight="bold", va="top",
    )
    # Легенда MA-линий, как в шапке графика на Binance.
    last_ma7 = _moving_average([c["close"] for c in candles], 7)[-1] or last_close
    fig.text(
        0.40, 0.918,
        f"MA(7) {_format_price(last_ma7)}",
        color=_MA_COLORS[7], fontsize=8, va="top",
    )
    fig.text(0.04, 0.875, f"{days}d", color=_AXIS_TEXT_COLOR, fontsize=8, va="top")

    fig.savefig(out_path, facecolor=fig.get_facecolor())
    plt.close(fig)

    logger.info("График сохранён: %s", out_path)
    return out_path