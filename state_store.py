"""
state_store.py - состояние бота (время последней публикации, джиттер,
retry-backoff, ротация темы/формата) в SQLite.

Это НАМЕРЕННО маленький модуль, а не queue_manager.py целиком из
binance-square-bot (там 1223 строки, из которых 95% - фьючерсы, ребаланс
портфеля, риск-гварды и другие вещи, не имеющие отношения к этому
проекту). Здесь только то, что реально нужно циклу "черновик по
расписанию": последнее время публикации, случайный джиттер окна,
back-off после сбоя, ротация темы/формата - параметризовано строкой
post_type ("okx_orbit" / "bybit_byx"), чтобы не плодить по 4 функции на
каждую биржу.

SQLite выбран по той же причине, что и в binance-square-bot: ничего не
нужно поднимать отдельно, файл bot_state.db просто лежит рядом со
скриптом и переживает перезапуски GitHub Actions джобы (коммитится
обратно в репозиторий, см. .github/workflows/bot.yml).
"""
import json
import random
import sqlite3
import time
from contextlib import contextmanager
from typing import Optional

import config

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


# --- Время последней публикации, отдельно по каждому post_type ---

def get_last_post_time(post_type: str) -> float:
    return _get(f"last_post_time:{post_type}", 0)


def set_last_post_time(post_type: str, ts: Optional[float] = None) -> None:
    _set(f"last_post_time:{post_type}", ts if ts is not None else time.time())


def seconds_since_last_post(post_type: str) -> float:
    last = get_last_post_time(post_type)
    if last == 0:
        return float("inf")
    return time.time() - last


# --- Случайный разброс окна публикации (решается один раз после каждой
# публикации, а не на каждом тике - иначе порог "плавал" бы туда-сюда) ---

def get_jitter_seconds(post_type: str) -> float:
    return _get(f"jitter_seconds:{post_type}", 0)


def roll_new_jitter(post_type: str, max_jitter_seconds: float) -> float:
    value = random.uniform(-max_jitter_seconds, max_jitter_seconds)
    _set(f"jitter_seconds:{post_type}", value)
    return value


# --- Back-off после сбоя (rate limit, сетевая ошибка и т.п.) - чтобы не
# долбить API на каждом тике сразу после неудачи ---

def should_retry_now(post_type: str) -> bool:
    retry_after = _get(f"retry_after:{post_type}", None)
    return retry_after is None or time.time() >= retry_after


def set_retry_backoff(post_type: str, hours: float) -> None:
    _set(f"retry_after:{post_type}", time.time() + hours * 3600)


# --- Ротация темы (BTC/ETH/market) и формата (market_take/trading_insight),
# независимо по каждому post_type ---

def get_last_theme(post_type: str) -> Optional[str]:
    return _get(f"last_theme:{post_type}", None)


def set_last_theme(post_type: str, theme: str) -> None:
    _set(f"last_theme:{post_type}", theme)


def get_last_format(post_type: str) -> Optional[str]:
    return _get(f"last_format:{post_type}", None)


def set_last_format(post_type: str, format_type: str) -> None:
    _set(f"last_format:{post_type}", format_type)
