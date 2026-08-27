"""
Состояние бота в SQLite: id последнего просмотренного поста канала,
время последней публикации, и "отложенный" дайджест, который ждёт
своего окна публикации (>4ч с прошлого поста).

SQLite выбран по той же причине, что и в проекте: ничего не нужно
поднимать отдельно, файл bot_state.db просто лежит рядом со скриптом
и переживает перезапуски.
"""
import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict
from typing import Optional

import config
from image_analyzer import ImageInsight
from signal_parser import RsiSignal

_SCHEMA = """
CREATE TABLE IF NOT EXISTS state (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


@contextmanager
def _connect():
    conn = sqlite3.connect(config.DB_PATH)
    try:
        conn.execute(_SCHEMA)
        yield conn
        conn.commit()
    finally:
        conn.close()


def _get(key: str, default=None):
    with _connect() as conn:
        row = conn.execute("SELECT value FROM state WHERE key = ?", (key,)).fetchone()
        return json.loads(row[0]) if row else default


def _set(key: str, value) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT INTO state (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, json.dumps(value)),
        )


# --- id последнего просмотренного поста в канале ---

def get_telegram_update_offset() -> int:
    return _get("telegram_update_offset", 0)


def set_telegram_update_offset(update_id: int) -> None:
    _set("telegram_update_offset", update_id)


# --- Время последней публикации, отдельно по каждому формату поста ---
# "currency" - пост про валюту (раз в 4ч), "opinion" - личное мнение
# (раз в 2 дня), "article" - статья (раз в неделю). Форматы независимы
# друг от друга - могут публиковаться в один день, если так совпало.

def get_last_post_time(post_type: str = "currency") -> float:
    return _get(f"last_post_time:{post_type}", 0)


def set_last_post_time(post_type: str = "currency", ts: Optional[float] = None) -> None:
    _set(f"last_post_time:{post_type}", ts if ts is not None else time.time())


def seconds_since_last_post(post_type: str = "currency") -> float:
    last = get_last_post_time(post_type)
    if last == 0:
        return float("inf")
    return time.time() - last


# --- Случайный разброс окна публикации ---
# Решается ОДИН РАЗ после каждой публикации (не на каждом тике, иначе
# порог "плавал" бы туда-сюда и было бы непредсказуемо). Хранится до
# следующей публикации этого формата, потом пересчитывается заново.

def get_jitter_seconds(post_type: str) -> float:
    return _get(f"jitter_seconds:{post_type}", 0)


def roll_new_jitter(post_type: str, max_jitter_seconds: float) -> float:
    """Бросает новый случайный разброс в диапазоне [-max, +max] и
    сохраняет его для следующего окна публикации этого формата."""
    import random

    value = random.uniform(-max_jitter_seconds, max_jitter_seconds)
    _set(f"jitter_seconds:{post_type}", value)
    return value


# --- История дайджестов за последние дни - для еженедельной статьи ---
# Храним отдельно от "отложенного поста" (pending_post) - это лог ВСЕХ
# увиденных дайджестов, а не только последнего, чтобы статья могла
# подвести итог за неделю.

_HISTORY_MAX_AGE_SECONDS = 9 * 24 * 3600  # держим чуть больше недели "на всякий"
_HISTORY_MAX_ENTRIES = 200  # защитный потолок - не даём промпту статьи разрастись, что бы ни писало в историю


def log_signal_history(signal: RsiSignal) -> None:
    history = _get("digest_history", [])
    history.append({
        "ticker": signal.ticker,
        "timeframe": signal.timeframe,
        "direction": signal.direction,
        "strategy": signal.strategy,
        "change_pct": signal.change_24h,
        "score": signal.score,
        "ts": time.time(),
    })
    cutoff = time.time() - _HISTORY_MAX_AGE_SECONDS
    history = [h for h in history if h["ts"] >= cutoff]
    if len(history) > _HISTORY_MAX_ENTRIES:
        history = history[-_HISTORY_MAX_ENTRIES:]
    _set("digest_history", history)


def get_digest_history(since_seconds_ago: float) -> list[dict]:
    """Возвращает записи истории не старше since_seconds_ago секунд назад,
    не больше _HISTORY_MAX_ENTRIES штук (на случай, если в базе уже
    накопилось больше из-за прошлых версий кода)."""
    history = _get("digest_history", [])
    cutoff = time.time() - since_seconds_ago
    recent = [h for h in history if h["ts"] >= cutoff]
    return recent[-_HISTORY_MAX_ENTRIES:]


# --- Недавно опубликованные тикеры - для разнообразия (избегаем повторов) ---

_RECENT_TICKERS_LIMIT = 5


def get_recent_tickers() -> list[str]:
    return _get("recent_tickers", [])


def log_posted_ticker(ticker: str) -> None:
    history = get_recent_tickers()
    history.append(ticker.upper())
    history = history[-_RECENT_TICKERS_LIMIT:]
    _set("recent_tickers", history)


# --- Кэш сопоставления тикер -> CoinGecko id ---
# Чтобы не дёргать /search на CoinGecko повторно для уже встречавшихся
# тикеров - результат поиска сохраняется один раз и переживает перезапуски.

def get_cached_coingecko_id(ticker: str) -> Optional[str]:
    return _get(f"coingecko_id:{ticker.upper()}", None)


def set_cached_coingecko_id(ticker: str, coingecko_id: str) -> None:
    _set(f"coingecko_id:{ticker.upper()}", coingecko_id)


# --- Кэш беты монеты к BTC (см. risk_guard._get_symbol_beta, P3.7) ---
# В отличие от coingecko_id выше - ЭТОТ кэш имеет TTL (бета пересчитана
# на несвежих данных вводит в заблуждение, а не просто неоптимальна), но
# TTL проверяется НА СТОРОНЕ risk_guard (см. get_cached_symbol_beta -
# отдаёт (beta, computed_at), а не только beta), а не здесь - здесь
# только хранение, без знания о том, какой TTL сейчас актуален.

def get_cached_symbol_beta(symbol: str) -> Optional[tuple[float, float]]:
    """(beta, computed_at) или None, если для символа ещё ничего не
    посчитано."""
    return _get(f"symbol_beta:{symbol.upper()}", None)


def set_cached_symbol_beta(symbol: str, beta: float) -> None:
    _set(f"symbol_beta:{symbol.upper()}", (beta, time.time()))


# --- Отложенный пост, ждущий своего окна публикации ---
# Может быть двух видов: "digest" (текстовый дайджест с числами)
# или "image" (качественный инсайт по картинке, без чисел).

# --- Последняя использованная тема поста-мнения - для ротации ---

def get_last_opinion_theme() -> Optional[str]:
    return _get("last_opinion_theme", None)


def set_last_opinion_theme(theme: str) -> None:
    _set("last_opinion_theme", theme)


def get_last_hot_take_theme() -> Optional[str]:
    """Отдельно от get_last_opinion_theme - хот-тейк публикуется в своё
    расписание (см. config.HOT_TAKE_INTERVAL_HOURS), тема ротируется
    независимо, чтобы не быть всегда синхронной с постом-мнением."""
    return _get("last_hot_take_theme", None)


def set_last_hot_take_theme(theme: str) -> None:
    _set("last_hot_take_theme", theme)


def get_last_okx_orbit_theme() -> Optional[str]:
    """Отдельно от get_last_opinion_theme/get_last_hot_take_theme -
    формат OKX Orbit публикуется по своему расписанию (см.
    config.OKX_ORBIT_INTERVAL_HOURS), тема ротируется независимо."""
    return _get("last_okx_orbit_theme", None)


def set_last_okx_orbit_theme(theme: str) -> None:
    _set("last_okx_orbit_theme", theme)


def get_last_okx_orbit_format() -> Optional[str]:
    """Ротация между форматами okx_orbit_generator.FORMATS
    ('market_take'/'trading_insight') - независимо от темы."""
    return _get("last_okx_orbit_format", None)


def set_last_okx_orbit_format(format_type: str) -> None:
    _set("last_okx_orbit_format", format_type)


def get_last_win_celebration_angle() -> Optional[str]:
    """Ротация эмоционального фокуса поста 'Забрали профит!' (см.
    win_celebration_generator._ANGLES) - отдельно от остальных ротаций,
    публикуется НЕ по расписанию, а сразу при закрытии сделки в плюс."""
    return _get("last_win_celebration_angle", None)


def set_last_win_celebration_angle(angle: str) -> None:
    _set("last_win_celebration_angle", angle)


def get_last_binance_promo_theme() -> Optional[str]:
    """Ротация фокуса промо-поста Binance Square (комиссии/фичи Square/
    удобство площадки) - своя, отдельная от opinion/hot_take/mini_lesson."""
    return _get("last_binance_promo_theme", None)


def set_last_binance_promo_theme(theme: str) -> None:
    _set("last_binance_promo_theme", theme)


def get_last_mini_lesson_topic() -> Optional[str]:
    """Ротация темы мини-урока - своя, отдельная от opinion/hot_take."""
    return _get("last_mini_lesson_topic", None)


def set_last_mini_lesson_topic(topic: str) -> None:
    _set("last_mini_lesson_topic", topic)


def get_last_audience_question() -> Optional[str]:
    """Ротация вопроса аудитории - своя, отдельная от остальных форматов."""
    return _get("last_audience_question", None)


def set_last_audience_question(question: str) -> None:
    _set("last_audience_question", question)


def get_glossary_index() -> int:
    """Порядковый номер следующей темы глоссария (Telegram) - в отличие
    от остальных ротаций (случайных, без повторов подряд) - здесь строго
    последовательный проход по telegram_glossary.TOPICS, с переходом на
    начало после последней темы (см. telegram_glossary.get_topic)."""
    return _get("glossary_index", 0)


def set_glossary_index(index: int) -> None:
    _set("glossary_index", index)


def get_last_telegram_poll() -> Optional[str]:
    """Ротация опроса (Telegram) - своя, отдельная от остальных форматов."""
    return _get("last_telegram_poll", None)


def set_last_telegram_poll(question: str) -> None:
    _set("last_telegram_poll", question)


def get_last_telegram_ama_prompt() -> Optional[str]:
    """Ротация приглашения на AMA (Telegram) - своя, отдельная от опроса."""
    return _get("last_telegram_ama_prompt", None)


def set_last_telegram_ama_prompt(prompt: str) -> None:
    _set("last_telegram_ama_prompt", prompt)


# --- Последний использованный режим тона хука - для ротации ---

def get_last_hook_mode() -> Optional[str]:
    return _get("last_hook_mode", None)


def set_last_hook_mode(mode: str) -> None:
    _set("last_hook_mode", mode)


# --- Кумулятивная история Treasury Index (с момента первого запуска) ---
# База 100 в момент первой публикации - и для индекса, и для BTC (для
# честного сравнения "если бы вы просто держали BTC вместо этого").
# Хранится здесь же, в bot_state.db, который и так коммитится обратно
# в репозиторий каждым прогоном GitHub Actions - значит переживает
# перезапуски точно так же, как очередь и остальное состояние.

def get_treasury_history() -> Optional[dict]:
    """{"launch_at": unix ts, "index_value": float, "btc_value": float} -
    None, если Treasury Index ещё ни разу не публиковался успешно."""
    return _get("treasury_history", None)


def update_treasury_history(
    index_pct: float, btc_pct: float,
    eth_pct: Optional[float] = None, market_pct: Optional[float] = None,
) -> dict:
    """Применяет очередное изменение (% за период) к кумулятивным
    значениям индекса, BTC и, если удалось получить, ETH и
    равновзвешенной корзины топ-4 (см. treasury_index.
    fetch_market_benchmark_pct). Первый вызов инициализирует базу 100
    для всех значений и текущий момент как launch_at (запоминается один
    раз, дальше не трогается).

    eth_pct/market_pct - ОПЦИОНАЛЬНЫ: если в конкретный тик их не
    удалось получить (сетевой сбой), соответствующее кумулятивное
    значение просто не обновляется в этот раз - не сбрасывается и не
    роняет публикацию, как и остальной индекс при частичных данных.

    Обратная совместимость: history, сохранённая ДО появления полей
    eth_value/market_value, дополняется ими на лету (база 100, отсчёт
    "с этого момента", а не ретроактивно - реальных данных за прошлые
    периоды для них просто нет)."""
    history = get_treasury_history()
    if history is None:
        history = {
            "launch_at": time.time(), "index_value": 100.0, "btc_value": 100.0,
            "eth_value": 100.0, "market_value": 100.0,
        }
    else:
        history.setdefault("eth_value", 100.0)
        history.setdefault("market_value", 100.0)

    history["index_value"] = round(history["index_value"] * (1 + index_pct / 100), 4)
    history["btc_value"] = round(history["btc_value"] * (1 + btc_pct / 100), 4)
    if eth_pct is not None:
        history["eth_value"] = round(history["eth_value"] * (1 + eth_pct / 100), 4)
    if market_pct is not None:
        history["market_value"] = round(history["market_value"] * (1 + market_pct / 100), 4)

    _set("treasury_history", history)
    return history


# --- Снимки истории индекса (для графика динамики, см. treasury_chart.py) ---
# update_treasury_history хранит только ТЕКУЩЕЕ кумулятивное состояние -
# для графика "с запуска" нужен весь путь, а не только конечная точка,
# поэтому здесь отдельно копим снимки {timestamp, index_value, btc_value,
# eth_value, market_value} по одному на каждый пост Treasury Index.
_TREASURY_SNAPSHOTS_MAX = 500  # тот же запас, что и у treasury_returns_history


def get_treasury_snapshots() -> list:
    return _get("treasury_snapshots", [])


def append_treasury_snapshot(history: dict) -> list:
    snapshots = get_treasury_snapshots()
    snapshots.append({
        "timestamp": time.time(),
        "index_value": history["index_value"],
        "btc_value": history["btc_value"],
        "eth_value": history.get("eth_value", 100.0),
        "market_value": history.get("market_value", 100.0),
    })
    if len(snapshots) > _TREASURY_SNAPSHOTS_MAX:
        snapshots = snapshots[-_TREASURY_SNAPSHOTS_MAX:]
    _set("treasury_snapshots", snapshots)
    return snapshots


# --- История % по каждой монете корзины (для rebalance_advisor.py) ---
# Треугольный ряд: {ticker: [pct, pct, None, ...]} - одно значение на
# каждый успешный расчёт индекса (None, если в тот раз данные по
# монете не получены - см. CoinChange.pct). Нужен, чтобы отличить
# разовую просадку монеты от СИСТЕМАТИЧЕСКОГО отставания от тира -
# без истории по каждой монете отдельно это в принципе не посчитать.
_COIN_HISTORY_MAX = 120  # ~60 дней при цикле в 12ч - разумное окно для "хронического" отставания


def get_coin_pct_history() -> dict:
    return _get("coin_pct_history", {})


def append_coin_periods(tiers: list) -> dict:
    """Добавляет по одному значению pct на каждую монету из всех тиров
    результата compute_index() - вызывается из treasury_generator.
    generate_treasury_post на каждый успешный расчёт индекса
    (независимо от того, удалось ли посчитать сравнение с BTC/ETH -
    это отдельная, самостоятельная история)."""
    history = get_coin_pct_history()
    for tier in tiers:
        for coin in tier.coins:
            history.setdefault(coin.ticker, [])
            history[coin.ticker].append(coin.pct)
            if len(history[coin.ticker]) > _COIN_HISTORY_MAX:
                history[coin.ticker] = history[coin.ticker][-_COIN_HISTORY_MAX:]
    _set("coin_pct_history", history)
    return history


def get_treasury_post_count() -> int:
    """Счётчик постов Treasury Index - используется, чтобы диаграмма
    состава (treasury_composition_chart.py) появлялась не на каждый
    пост, а раз в config.TREASURY_COMPOSITION_INTERVAL_POSTS постов
    (см. treasury_generator.generate_treasury_post)."""
    return _get("treasury_post_count", 0)


def increment_treasury_post_count() -> int:
    count = get_treasury_post_count() + 1
    _set("treasury_post_count", count)
    return count


# --- Память о недавних зачинах постов (см. post_memory.py) ---
_RECENT_OPENERS_MAX = 12


def get_recent_post_openers() -> list:
    return _get("recent_post_openers", [])


def append_recent_post_opener(opener: str) -> list:
    openers = get_recent_post_openers()
    openers.append(opener)
    if len(openers) > _RECENT_OPENERS_MAX:
        openers = openers[-_RECENT_OPENERS_MAX:]
    _set("recent_post_openers", openers)
    return openers


def get_recent_openers() -> list:
    """Последние N зачинов (первое предложение) опубликованных постов -
    по ВСЕМ форматам сразу (currency-сигнал/opinion/hot_take), не
    отдельно по каждому. См. voice_memory.py - используется, чтобы
    новый пост не начинался так же, как недавние, даже если это разные
    форматы."""
    return _get("recent_openers", [])


def set_recent_openers(openers: list) -> None:
    _set("recent_openers", openers)


def get_theme_post_history() -> dict:
    """{theme: {"pct", "stance_summary", "timestamp"}} - последняя
    реальная точка данных по каждой теме (BTC/ETH/market), после
    публикации поста-мнения/хот-тейка на эту тему. См. voice_memory.py -
    используется для честной преемственности ("как я говорил раньше")
    без выдумывания истории, которой не было."""
    return _get("theme_post_history", {})


def set_theme_post_history(history: dict) -> None:
    _set("theme_post_history", history)


# --- Ряд доходностей по периодам (для index_volatility.py) ---
# update_treasury_history хранит только ТЕКУЩЕЕ кумулятивное значение -
# этого достаточно для "с запуска +X%", но недостаточно для волатильности
# и просадки (нужен весь путь, а не только конечная точка). Здесь -
# отдельный список % за каждый период (обычно раз в TREASURY_INTERVAL_HOURS),
# из которого можно восстановить кумулятивный ряд и посчитать std/max
# drawdown задним числом.
_TREASURY_RETURNS_MAX = 500  # с запасом на ~7 месяцев при 12ч периоде - более чем достаточно


def get_treasury_returns_history() -> list[float]:
    return _get("treasury_returns_history", [])


def append_treasury_return(pct: float) -> list[float]:
    history = get_treasury_returns_history()
    history.append(pct)
    if len(history) > _TREASURY_RETURNS_MAX:
        history = history[-_TREASURY_RETURNS_MAX:]
    _set("treasury_returns_history", history)
    return history


# --- Очередь отложенных постов, ждущих своего окна публикации ---
# ВАЖНО: это настоящая FIFO-очередь, а не одно перезаписываемое
# значение. Раньше "отложенный пост" был ОДНИМ слотом - если за тик
# в канале набегало несколько сигналов, каждый следующий просто
# перетирал предыдущий, и публиковался только последний из пачки,
# а остальные терялись безо всякого лога. Теперь каждый новый сигнал
# или картинка добавляется в конец списка и ждёт своей очереди.
#
# У каждой записи есть счётчик попыток публикации (attempts) - если
# конкретный пост не публикуется несколько раз подряд (например,
# для тикера так и не нашёлся график), он сбрасывается из очереди,
# чтобы не блокировать навечно всё, что скопилось за ним.

_MAX_QUEUE_LENGTH = 30   # на случай аномального наплыва сигналов
MAX_PUBLISH_ATTEMPTS = 3


def _get_queue() -> list[dict]:
    return _get("post_queue", [])


def _set_queue(queue: list[dict]) -> None:
    _set("post_queue", queue)


def _push_pending(kind: str, payload: dict) -> None:
    queue = _get_queue()
    queue.append({"kind": kind, "payload": payload, "attempts": 0, "queued_at": time.time()})
    if len(queue) > _MAX_QUEUE_LENGTH:
        dropped = queue.pop(0)
        import logging
        logging.getLogger("queue_manager").warning(
            "Очередь переполнена (>%d) - старейшая запись (%s) выброшена без публикации",
            _MAX_QUEUE_LENGTH, dropped.get("kind"),
        )
    _set_queue(queue)


def prune_expired_entries(max_age_hours: float) -> int:
    """Удаляет из очереди записи старше max_age_hours - устаревший RSI-
    сигнал (RSI мог уже давно выйти из зоны перекупленности/перепроданности
    к моменту публикации) не должен ждать своей очереди часами только
    потому, что у него был высокий score в момент обнаружения. Это
    дополняет лимит по количеству (_MAX_QUEUE_LENGTH) - тот срабатывает
    только когда очередь физически переполнена, а это чистит по времени
    вне зависимости от текущей длины очереди.

    Записи без queued_at (уже лежавшие в очереди до этого изменения)
    считаются устаревшими сразу - их возраст всё равно неизвестен,
    лучше почистить, чем оставить висеть бессрочно.

    Возвращает число удалённых записей."""
    queue = _get_queue()
    if not queue:
        return 0

    cutoff = time.time() - max_age_hours * 3600
    kept = [item for item in queue if item.get("queued_at", 0) >= cutoff]
    dropped_count = len(queue) - len(kept)

    if dropped_count:
        import logging
        logging.getLogger("queue_manager").info(
            "Очистка очереди по возрасту (>%.1fч): удалено %d устаревших записей, осталось %d",
            max_age_hours, dropped_count, len(kept),
        )
        _set_queue(kept)

    return dropped_count


def push_pending_signal(signal: RsiSignal) -> None:
    _push_pending("signal", asdict(signal))


def push_pending_image(insight: ImageInsight) -> None:
    _push_pending("image", asdict(insight))


def pending_queue_length() -> int:
    return len(_get_queue())


def pending_queue_summary() -> list[str]:
    """Короткое описание очереди для диагностики (check_state.py)."""
    out = []
    for item in _get_queue():
        ticker = item["payload"].get("ticker", "?")
        out.append(f"{item['kind']}:{ticker} (попыток={item['attempts']})")
    return out


def get_pending_post(min_score: int = 0) -> Optional[tuple[int, str, object]]:
    """Возвращает (индекс_в_очереди, kind, payload) ЛУЧШЕГО подходящего
    поста, или None, если ничего не подходит.

    "Лучший" = сигнал (kind=signal) с максимальным score СРЕДИ ТЕХ, у
    кого score > min_score И чей тикер не входит в get_recent_tickers()
    (последние _RECENT_TICKERS_LIMIT опубликованных тикеров). Это нужно
    для разнообразия: один и тот же тикер (например, PHB) может неделями
    держать самый высокий score в очереди просто потому, что RSI там
    стабильно экстремальный - без этой проверки бот публиковал бы по
    нему пост за постом, игнорируя всё остальное. Если ничего, кроме
    недавних тикеров, не проходит порог - публикуем недавний тикер
    всё равно (лучше повтор, чем пропуск окна публикации целиком).

    Если ни один сигнал не проходит порог - рассматривается самый старый
    пост типа "image" (для картинок score не считается, порог на них не
    действует - это отдельный, более редкий путь публикации).

    Если ничего не подходит вообще - очередь НЕ трогаем, просто ждём
    следующего тика (новый сигнал может появиться, либо существующий
    станет неактуальным и выпадет по лимиту попыток/переполнению)."""
    queue = _get_queue()
    if not queue:
        return None

    recent_tickers = {t.upper() for t in get_recent_tickers()}

    best_idx, best_score = None, None
    best_recent_idx, best_recent_score = None, None  # запасной вариант среди недавних тикеров
    fallback_image_idx = None

    for idx, item in enumerate(queue):
        if item["kind"] == "signal":
            try:
                score = int(item["payload"].get("score", 0))
            except (TypeError, ValueError):
                score = 0
            if score <= min_score:
                continue
            ticker = str(item["payload"].get("ticker", "")).upper()
            if ticker in recent_tickers:
                if best_recent_score is None or score > best_recent_score:
                    best_recent_idx, best_recent_score = idx, score
            else:
                if best_score is None or score > best_score:
                    best_idx, best_score = idx, score
        elif item["kind"] == "image" and fallback_image_idx is None:
            fallback_image_idx = idx

    if best_idx is not None:
        chosen_idx = best_idx
    elif best_recent_idx is not None:
        chosen_idx = best_recent_idx
    else:
        chosen_idx = fallback_image_idx

    if chosen_idx is None:
        return None

    item = queue[chosen_idx]
    kind = item["kind"]
    payload = item["payload"]

    if kind == "signal":
        return chosen_idx, kind, RsiSignal(**payload)
    return chosen_idx, kind, ImageInsight(**payload)


def clear_pending_post(index: int) -> None:
    """Убирает конкретный пост из очереди (по индексу) - вызывать после успешной публикации."""
    queue = _get_queue()
    if 0 <= index < len(queue):
        queue.pop(index)
        _set_queue(queue)


def register_failed_attempt(index: int) -> bool:
    """Увеличивает счётчик попыток у конкретного поста в очереди (по
    индексу). Если попыток стало больше лимита - выбрасывает его из
    очереди и возвращает True. Иначе возвращает False (попробуем снова
    на следующем тике)."""
    queue = _get_queue()
    if not (0 <= index < len(queue)):
        return False

    queue[index]["attempts"] += 1
    dropped = queue[index]["attempts"] > MAX_PUBLISH_ATTEMPTS
    if dropped:
        queue.pop(index)
    _set_queue(queue)
    return dropped


# --- Отдельная очередь для сигналов по монетам Treasury Index ---
# (index_signal_scanner.py) - специально ОТДЕЛЬНО от основной очереди
# currency (та выбирает лучший сигнал по всему рынку). Здесь вселенная
# всего 15 тикеров индекса, и публикуется это отдельным форматом
# ("удобный момент купить/продать монету из индекса"), со своим окном
# по расписанию (config.INDEX_SIGNAL_INTERVAL_HOURS) - смешивать с
# общей очередью значило бы либо утопить индекс-сигналы среди сотен
# рыночных, либо наоборот вытеснять ими интересные рыночные сетапы.

_INDEX_SIGNAL_QUEUE_MAX = 15  # по числу монет в корзине - больше и не нужно


def _get_index_signal_queue() -> list[dict]:
    return _get("index_signal_queue", [])


def _set_index_signal_queue(queue: list[dict]) -> None:
    _set("index_signal_queue", queue)


def push_pending_index_signal(signal: RsiSignal) -> None:
    queue = _get_index_signal_queue()
    # Не дублируем сигнал по тому же тикеру - обновляем на более свежий,
    # а не копим (в отличие от основной очереди, здесь вселенная маленькая
    # и повторный сигнал по той же монете почти наверняка про то же самое).
    queue = [item for item in queue if item["payload"].get("ticker") != signal.ticker]
    queue.append({"kind": "index_signal", "payload": asdict(signal), "attempts": 0, "queued_at": time.time()})
    if len(queue) > _INDEX_SIGNAL_QUEUE_MAX:
        queue = queue[-_INDEX_SIGNAL_QUEUE_MAX:]
    _set_index_signal_queue(queue)


def prune_expired_index_signals(max_age_hours: float) -> int:
    queue = _get_index_signal_queue()
    if not queue:
        return 0
    cutoff = time.time() - max_age_hours * 3600
    kept = [item for item in queue if item.get("queued_at", 0) >= cutoff]
    dropped = len(queue) - len(kept)
    if dropped:
        _set_index_signal_queue(kept)
    return dropped


def get_pending_index_signal(min_score: int = 0) -> Optional[tuple[int, RsiSignal]]:
    """Лучший (по score) сигнал в очереди индекса, выше min_score, или None."""
    queue = _get_index_signal_queue()
    best_idx, best_score = None, None
    for idx, item in enumerate(queue):
        try:
            score = int(item["payload"].get("score", 0))
        except (TypeError, ValueError):
            score = 0
        if score > min_score and (best_score is None or score > best_score):
            best_idx, best_score = idx, score
    if best_idx is None:
        return None
    return best_idx, RsiSignal(**queue[best_idx]["payload"])


def clear_pending_index_signal(index: int) -> None:
    queue = _get_index_signal_queue()
    if 0 <= index < len(queue):
        queue.pop(index)
        _set_index_signal_queue(queue)


def register_failed_index_attempt(index: int) -> bool:
    """Как register_failed_attempt для основной очереди - возвращает
    True, если запись выброшена по лимиту попыток."""
    queue = _get_index_signal_queue()
    if not (0 <= index < len(queue)):
        return False
    queue[index]["attempts"] += 1
    dropped = queue[index]["attempts"] > MAX_PUBLISH_ATTEMPTS
    if dropped:
        queue.pop(index)
    _set_index_signal_queue(queue)
    return dropped


def pending_index_queue_summary() -> list[str]:
    return [f"{item['payload'].get('ticker', '?')} (попыток={item['attempts']})" for item in _get_index_signal_queue()]


# --- Cooldown для собственного сканера сигналов (scanner.py) ---
# Без этого, пока RSI пары держится за пределами 70/30 (а это может
# длиться часами), сканер заносил бы в очередь практически идентичный
# сигнал на каждом тике (раз в 10 минут).

def was_recently_alerted(ticker: str, direction_key: str, cooldown_hours: float) -> bool:
    key = f"scanner_alert:{ticker.upper()}:{direction_key}"
    last_ts = _get(key, None)
    if last_ts is None:
        return False
    return (time.time() - last_ts) < cooldown_hours * 3600


def mark_alerted(ticker: str, direction_key: str) -> None:
    key = f"scanner_alert:{ticker.upper()}:{direction_key}"
    _set(key, time.time())


# --- Cooldown по символу после стоп-аута реальной позиции (см.
# config.FUTURES_SYMBOL_COOLDOWN_HOURS, futures_position_monitor,
# futures_signal_bridge.execute_signal) - персистентно, а не in-memory,
# чтобы переживать рестарт/деплой бота: без этого cooldown "сбрасывался"
# бы при каждом деплое и не защищал бы от входа сразу после рестарта.

def was_recently_stopped_out(symbol: str, cooldown_hours: float) -> bool:
    key = f"futures_stop_cooldown:{symbol.upper()}"
    last_ts = _get(key, None)
    if last_ts is None:
        return False
    return (time.time() - last_ts) < cooldown_hours * 3600


def mark_stopped_out(symbol: str) -> None:
    key = f"futures_stop_cooldown:{symbol.upper()}"
    _set(key, time.time())


# --- Отступ при сбое генерации/публикации (opinion, article) ---
# Без этого временный сбой (например, 429 от Groq) приводил бы к
# попытке заново на КАЖДОМ тике (раз в ~10 минут) до победного конца,
# без всякой паузы - это и не экономично для лимитов API, и просто
# бессмысленный busy-loop. should_retry_now() проверяется в начале
# попытки публикации, set_retry_backoff() выставляется на любом сбое.

def should_retry_now(post_type: str) -> bool:
    retry_after = _get(f"retry_after:{post_type}", None)
    return retry_after is None or time.time() >= retry_after


def set_retry_backoff(post_type: str, hours: float) -> None:
    _set(f"retry_after:{post_type}", time.time() + hours * 3600)


# --- Трекинг результатов опубликованных сигналов (outcome_tracker.py) ---
# Отдельно от post_queue (очередь на ПУБЛИКАЦИЮ) - здесь сигналы,
# которые УЖЕ опубликованы и ждут, пока цена дойдёт до тейка/стопа.
# open_outcomes - ещё не решённые (see outcome_tracker.check_open_outcomes),
# closed_outcomes - решённые (win/loss/timeout), на них считается
# статистика (outcome_tracker.get_accuracy_stats).

_OPEN_OUTCOMES_MAX = 300     # защитный потолок, чтобы список не рос бесконечно при сбоях трекинга
_CLOSED_OUTCOMES_MAX = 1000  # ~несколько месяцев истории для статистики - этого достаточно


def get_open_outcomes() -> list[dict]:
    return _get("open_outcomes", [])


def add_open_outcome(record: dict) -> None:
    items = get_open_outcomes()
    items.append(record)
    if len(items) > _OPEN_OUTCOMES_MAX:
        import logging
        logging.getLogger("queue_manager").warning(
            "open_outcomes переполнен (>%d) - старейшие записи выброшены без результата",
            _OPEN_OUTCOMES_MAX,
        )
        items = items[-_OPEN_OUTCOMES_MAX:]
    _set("open_outcomes", items)


def replace_open_outcomes(items: list[dict]) -> None:
    """Перезаписывает open_outcomes целиком - вызывается после каждого
    прохода check_open_outcomes() с тем, что осталось нерешённым."""
    _set("open_outcomes", items)


def attach_bluesky_ref_to_outcome(ticker: str, bluesky_ref: dict) -> bool:
    """Дозаписывает bluesky_ref в уже созданную (открытую) запись
    трекинга результата - нужна, потому что кроспост в Bluesky теперь
    ОТЛОЖЕН (см. main._schedule_crossposts), а запись в open_outcomes
    создаётся сразу после публикации на Binance Square, ещё без ссылки
    на Bluesky-пост.

    Ищет среди открытых записей по данному тикеру ту, у которой
    bluesky_ref ещё не проставлен, и берёт САМУЮ СВЕЖУЮ (по
    published_at) - на случай, если по одному тикеру одновременно
    открыто несколько сделок. Возвращает True, если нашлась и
    обновилась подходящая запись, иначе False (например, сделка уже
    успела закрыться раньше, чем кроспост состоялся - тогда 'До/После'
    в Bluesky для неё просто не будет, публикация на Square этим не
    затрагивается)."""
    items = get_open_outcomes()
    candidates = [
        (idx, item) for idx, item in enumerate(items)
        if item.get("ticker") == ticker and not item.get("bluesky_ref")
    ]
    if not candidates:
        return False

    idx, _ = max(candidates, key=lambda pair: pair[1].get("published_at", 0))
    items[idx]["bluesky_ref"] = bluesky_ref
    _set("open_outcomes", items)
    return True


def get_closed_outcomes() -> list[dict]:
    return _get("closed_outcomes", [])


def append_closed_outcomes(new_items: list[dict]) -> None:
    items = get_closed_outcomes()
    items.extend(new_items)
    if len(items) > _CLOSED_OUTCOMES_MAX:
        items = items[-_CLOSED_OUTCOMES_MAX:]
    _set("closed_outcomes", items)


# --- P3.9: теневые (shadow) вердикты будущих фильтров (shadow_filters.py) ---
# Отдельно от open/closed_outcomes выше - это НЕ трекинг результата
# сделки, а лог "заблокировал бы фильтр X этот сигнал или нет", записанный
# ДО публикации, чтобы позже (см. shadow_filters.get_shadow_stats)
# сопоставить его с РЕАЛЬНЫМ исходом из closed_outcomes и сравнить
# win-rate "заблокировано" vs "пропущено" - без того, чтобы фильтр
# реально резал сигналы вслепую первые 1-2 недели.

_SHADOW_VERDICTS_MAX = 5000  # много разных фильтров * много сигналов - выше потолок, чем у outcomes


def get_shadow_verdicts() -> list[dict]:
    return _get("shadow_filter_verdicts", [])


def add_shadow_verdict(record: dict) -> None:
    items = get_shadow_verdicts()
    items.append(record)
    if len(items) > _SHADOW_VERDICTS_MAX:
        items = items[-_SHADOW_VERDICTS_MAX:]
    _set("shadow_filter_verdicts", items)


def get_recent_post_openers() -> list:
    """Опенеры (первые символы) последних опубликованных постов across
    ВСЕХ форматов - см. post_memory.py (anti-repetition guard)."""
    return _get("recent_post_openers", [])


def set_recent_post_openers(openers: list) -> None:
    _set("recent_post_openers", openers)


# --- Троттлинг алертов владельцу (alerting.py) ---
# Отдельно от retry_after (тот - пауза перед следующей ПОПЫТКОЙ действия,
# этот - пауза перед следующим УВЕДОМЛЕНИЕМ об одном и том же по сути
# сбое, чтобы повторяющаяся ошибка не заваливала личку одинаковыми
# сообщениями на каждом тике).

def get_last_alert_sent(alert_key: str) -> float:
    return _get(f"alert_sent:{alert_key}", 0)


def set_last_alert_sent(alert_key: str) -> None:
    _set(f"alert_sent:{alert_key}", time.time())


# --- Автокоррекция порога публикации по стратегиям (strategy_tuner.py) ---
def get_strategy_adjustments() -> dict:
    return _get("strategy_adjustments", {})


def set_strategy_adjustments(adjustments: dict) -> None:
    _set("strategy_adjustments", adjustments)


# --- История использования hook_mode + ручной ввод заработка Write to
# Earn (engagement_tracker.py) - для косвенного A/B хуков. Официальный
# Binance Square API не отдаёт статистику существующих постов (ни
# просмотры/лайки, ни заработок) - это подтверждено документацией
# square-post skill ("this skill only creates new posts"), поэтому
# заработок вносится вручную раз в неделю (см. log_earnings.py), а вот
# КАКОЙ hook_mode использовался в какую неделю - бот считает сам.

_HOOK_MODE_HISTORY_MAX = 500   # с запасом на несколько месяцев ежедневных постов
_EARNINGS_HISTORY_MAX = 104    # 2 года еженедельных записей - более чем достаточно


def log_hook_mode_usage(mode: str) -> None:
    history = _get("hook_mode_history", [])
    history.append({"mode": mode, "timestamp": time.time()})
    if len(history) > _HOOK_MODE_HISTORY_MAX:
        history = history[-_HOOK_MODE_HISTORY_MAX:]
    _set("hook_mode_history", history)


def get_hook_mode_history() -> list[dict]:
    return _get("hook_mode_history", [])


def log_weekly_earnings(amount: float, currency: str, week_ending_ts: float) -> None:
    history = get_earnings_history()
    history.append({"amount": amount, "currency": currency, "week_ending": week_ending_ts, "logged_at": time.time()})
    history.sort(key=lambda e: e["week_ending"])
    if len(history) > _EARNINGS_HISTORY_MAX:
        history = history[-_EARNINGS_HISTORY_MAX:]
    _set("earnings_history", history)


def get_earnings_history() -> list[dict]:
    return _get("earnings_history", [])


# --- Мониторинг здоровья монет Treasury Index (index_health_monitor.py) ---
def get_coin_miss_streaks() -> dict:
    return _get("coin_miss_streaks", {})


def set_coin_miss_streaks(streaks: dict) -> None:
    _set("coin_miss_streaks", streaks)


def get_retry_backoff_remaining_seconds(post_type: str) -> Optional[float]:
    """Сколько секунд осталось до конца паузы после сбоя, или None,
    если бэкофф не активен (можно пробовать сейчас)."""
    retry_after = _get(f"retry_after:{post_type}", None)
    if retry_after is None:
        return None
    remaining = retry_after - time.time()
    return remaining if remaining > 0 else None

# --- Отложенный кроспостинг (разведение Telegram/Bluesky по времени) ---
# Раньше кроспост в Telegram и Bluesky публиковался СРАЗУ ЖЕ после
# основного поста на Binance Square, синхронно, в одном тике. Теперь
# main._schedule_crossposts кладёт сюда запись с "временем публикации"
# в будущем (см. config.CROSSPOST_DELAY_MIN_MINUTES/MAX_MINUTES) - сам
# GitHub Actions тик крутится раз в 10 минут (см. .github/workflows/
# bot.yml), поэтому запись переживает несколько запусков подряд, пока её
# время не наступит - именно поэтому картинка хранится тут же в виде
# base64 (не путём к файлу - файл на диске не переживёт следующий запуск
# Actions, там свежий checkout репозитория).
_CROSSPOST_QUEUE_MAX = 20


def _get_crosspost_queue() -> list[dict]:
    return _get("crosspost_queue", [])


def _set_crosspost_queue(queue: list[dict]) -> None:
    _set("crosspost_queue", queue)


def push_pending_crosspost(platform: str, due_ts: float, data: dict) -> str:
    """Кладёт отложенный кроспост в очередь, возвращает его id.

    platform - "telegram" или "bluesky". data - всё, что нужно
    main._process_pending_crossposts для реальной публикации (текст,
    картинка в base64, тикер, hook, сигнал как dict и т.п.) - должно
    быть JSON-сериализуемо."""
    queue = _get_crosspost_queue()
    entry_id = uuid.uuid4().hex
    queue.append({
        "id": entry_id,
        "platform": platform,
        "due_ts": due_ts,
        "queued_at": time.time(),
        "attempts": 0,
        "data": data,
    })
    if len(queue) > _CROSSPOST_QUEUE_MAX:
        dropped = queue.pop(0)
        import logging
        logging.getLogger("queue_manager").warning(
            "Очередь отложенных кроспостов переполнена (>%d) - старейшая запись (%s/%s) выброшена",
            _CROSSPOST_QUEUE_MAX, dropped.get("platform"), dropped.get("data", {}).get("ticker"),
        )
    _set_crosspost_queue(queue)
    return entry_id


def get_due_crossposts() -> list[dict]:
    """Записи, чьё время публикации уже наступило (due_ts <= сейчас).
    Порядок площадок между собой не гарантирован - каждой при постановке
    в очередь выставляется своя случайная задержка (см.
    main._schedule_crossposts), поэтому какая раньше "созреет" - зависит
    от розыгрыша, а не от фиксированного порядка кода."""
    now = time.time()
    return [item for item in _get_crosspost_queue() if item["due_ts"] <= now]


def remove_crosspost(entry_id: str) -> None:
    queue = _get_crosspost_queue()
    queue = [item for item in queue if item.get("id") != entry_id]
    _set_crosspost_queue(queue)


def register_failed_crosspost(entry_id: str, max_attempts: int = 3) -> bool:
    """Как register_failed_attempt для основной очереди - увеличивает
    счётчик попыток, выбрасывает запись из очереди при превышении
    лимита. Возвращает True, если запись была выброшена."""
    queue = _get_crosspost_queue()
    for item in queue:
        if item.get("id") == entry_id:
            item["attempts"] = item.get("attempts", 0) + 1
            dropped = item["attempts"] > max_attempts
            if dropped:
                queue = [i for i in queue if i.get("id") != entry_id]
            _set_crosspost_queue(queue)
            return dropped
    return False


def prune_stale_crossposts(max_age_hours: float) -> int:
    """Удаляет зависшие записи (например, площадка была настроена в
    момент постановки в очередь, но перестала быть настроена/токен
    протух) старше max_age_hours - защита от бесконечного накопления в
    bot_state.db. Возвращает число удалённых записей."""
    queue = _get_crosspost_queue()
    if not queue:
        return 0

    cutoff = time.time() - max_age_hours * 3600
    kept = [item for item in queue if item.get("queued_at", 0) >= cutoff]
    dropped_count = len(queue) - len(kept)
    if dropped_count:
        _set_crosspost_queue(kept)
    return dropped_count


# --- Предохранители риска фьючерсов (risk_guard.py) ---
# Тот же key-value store bot_state.db, что и весь остальной модуль -
# отдельная таблица тут не нужна (см. _SCHEMA наверху файла).

def get_risk_daily_baseline(day_key: str) -> Optional[float]:
    """Баланс кошелька, зафиксированный при ПЕРВОЙ проверке за
    UTC-день day_key (например '2026-07-29') - точка отсчёта для
    дневного лимита убытка (см. risk_guard._daily_loss_pct). None,
    если сегодня ещё не проверяли ни разу."""
    return _get(f"risk_daily_baseline:{day_key}", None)


def set_risk_daily_baseline(day_key: str, balance: float) -> None:
    _set(f"risk_daily_baseline:{day_key}", balance)


def get_kill_switch() -> Optional[dict]:
    """{'reason': str, 'tripped_at': timestamp}, если предохранитель
    (дневной лимит убытка или серия убытков подряд - см. risk_guard.py)
    сработал, иначе None. Сознательно НЕ привязан к дню/сессии - раз
    взведённый kill switch остаётся взведённым, пока его явно не снимут
    (см. clear_kill_switch) или (если настроено, см.
    config.BINANCE_FUTURES_KILL_SWITCH_AUTO_RESET_HOURS) не пройдёт
    заданный таймаут - даже после наступления нового UTC-дня."""
    return _get("risk_kill_switch", None)


def set_kill_switch(reason: str) -> None:
    _set("risk_kill_switch", {"reason": reason, "tripped_at": time.time()})


def clear_kill_switch() -> None:
    """Снимает kill switch - и вручную (risk_guard_cli.py reset), и
    автоматически по таймауту (см. risk_guard.check_new_position_allowed)
    - ОБА пути идут через эту функцию. Заодно сдвигает "точку отсчёта"
    серии убытков на текущий момент (get/set_risk_streak_ignore_before) -
    без этого убытки, из-за которых switch взвёлся, никуда не делись бы
    из истории биржи и немедленно взвели бы его заново на первой же
    следующей проверке (см. risk_guard._consecutive_losses,
    параметр since_ts) - что на практике и происходило до этого
    изменения, когда снятие приходилось делать вручную ПОСЛЕ каждой
    неудачной серии, а не один раз."""
    _set("risk_kill_switch", None)
    set_risk_streak_ignore_before(time.time())


def get_risk_streak_ignore_before() -> Optional[float]:
    """Unix-время (секунды): сделки ДО этого момента не учитываются при
    подсчёте серии убытков подряд (risk_guard._consecutive_losses). None,
    если отметки ещё не было (учитывать всю доступную историю, как
    раньше)."""
    return _get("risk_streak_ignore_before", None)


def set_risk_streak_ignore_before(ts: float) -> None:
    _set("risk_streak_ignore_before", ts)


def pending_crosspost_summary() -> list[str]:
    """Короткое описание очереди для диагностики (check_state.py)."""
    out = []
    for item in _get_crosspost_queue():
        eta_min = max((item["due_ts"] - time.time()) / 60, 0)
        ticker = item.get("data", {}).get("ticker", "?")
        out.append(f"{item['platform']}:{ticker} (через ~{eta_min:.0f} мин, попыток={item.get('attempts', 0)})")
    return out


# --- Трекинг реально открытых позиций на фьючерсах (futures_position_monitor.py) ---
# Отдельно от open_outcomes/closed_outcomes выше - те трекают ЦЕНУ
# опубликованного сигнала (для accuracy_report), а это - РЕАЛЬНО
# открытую на бирже позицию (futures_signal_bridge.execute_signal), с
# quantity/order id и т.п. Без этого open_protected_position открывает
# позицию, и о ней тут же забывают - см. docstring futures_position_monitor.py.
_OPEN_FUTURES_POSITIONS_MAX = 50
_CLOSED_FUTURES_POSITIONS_MAX = 500


def get_open_futures_positions() -> list[dict]:
    return _get("open_futures_positions", [])


def add_open_futures_position(record: dict) -> None:
    items = get_open_futures_positions()
    items.append(record)
    if len(items) > _OPEN_FUTURES_POSITIONS_MAX:
        import logging
        logging.getLogger("queue_manager").warning(
            "open_futures_positions переполнен (>%d) - похоже, старые записи не "
            "закрываются (мониторинг не запускается?) - старейшие выброшены без трекинга",
            _OPEN_FUTURES_POSITIONS_MAX,
        )
        items = items[-_OPEN_FUTURES_POSITIONS_MAX:]
    _set("open_futures_positions", items)


def replace_open_futures_positions(items: list[dict]) -> None:
    """Перезаписывает open_futures_positions целиком - вызывается после
    каждого прохода futures_position_monitor.check_open_positions() с
    тем, что осталось реально открытым на бирже."""
    _set("open_futures_positions", items)


def get_closed_futures_positions() -> list[dict]:
    return _get("closed_futures_positions", [])


def append_closed_futures_positions(new_items: list[dict]) -> None:
    items = get_closed_futures_positions()
    items.extend(new_items)
    if len(items) > _CLOSED_FUTURES_POSITIONS_MAX:
        items = items[-_CLOSED_FUTURES_POSITIONS_MAX:]
    _set("closed_futures_positions", items)

