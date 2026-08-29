"""
Все настройки читаются из .env файла (см. .env.example). Никаких
ключей в коде - только через переменные окружения.

Это ОТДЕЛЬНЫЙ проект от binance-square-bot - свой .env, свои GitHub
Secrets, свой bot_state.db. Общий только Telegram-бот-токен (см.
.env.example) - разные chat_id разделяют черновики по проектам.
"""
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).parent

# --- Telegram (тот же бот-токен, что и у binance-square-bot, другой chat_id) ---
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")

# --- Groq (LLM) ---
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
GROQ_MODEL = os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b")
GROQ_MODEL_SECONDARY = os.environ.get("GROQ_MODEL_SECONDARY", "openai/gpt-oss-20b")

# --- OKX Orbit (см. okx_orbit_generator.py / okx_draft_publisher.py) ---
# У OKX Orbit нет публичного API для постинга - бот только ГОТОВИТ
# черновик и присылает его в личный Telegram-чат владельца
# (OKX_ORBIT_DRAFT_CHAT_ID), публикация - вручную.
OKX_ORBIT_ENABLED = os.environ.get("OKX_ORBIT_ENABLED", "false").lower() == "true"
OKX_ORBIT_DRAFT_CHAT_ID = os.environ.get("OKX_ORBIT_DRAFT_CHAT_ID", "")
OKX_ORBIT_INTERVAL_HOURS = float(os.environ.get("OKX_ORBIT_INTERVAL_HOURS", "8"))
OKX_ORBIT_JITTER_HOURS = float(os.environ.get("OKX_ORBIT_JITTER_HOURS", "2"))

# --- Bybit ByX ---
BYBIT_BYX_ENABLED = os.environ.get("BYBIT_BYX_ENABLED", "false").lower() == "true"
BYBIT_BYX_DRAFT_CHAT_ID = os.environ.get("BYBIT_BYX_DRAFT_CHAT_ID", "")
BYBIT_BYX_INTERVAL_HOURS = float(os.environ.get("BYBIT_BYX_INTERVAL_HOURS", "8"))
BYBIT_BYX_JITTER_HOURS = float(os.environ.get("BYBIT_BYX_JITTER_HOURS", "2"))

# --- Новостной формат (news_take) - см. news_channel_reader.py ---

# --- Новостной формат (см. news_channel_reader.py / news_opinion_generator.py) ---
# Читает публичный канал через t.me/s/<канал> (без токенов/аккаунта -
# см. README.md), формирует авторское мнение по последней новости.
# Интервал в часах, но по смыслу это "пару раз в неделю": 84ч (3.5 дня)
# +- 24ч джиттера. Источник канала общий для обеих бирж, но у каждой
# свой черновик, свой кулдаун и своя память "что уже разбирал" - см. main.py.
NEWS_SOURCE_CHANNEL = os.environ.get("NEWS_SOURCE_CHANNEL", "forklog")
OKX_NEWS_ENABLED = os.environ.get("OKX_NEWS_ENABLED", "false").lower() == "true"
OKX_NEWS_INTERVAL_HOURS = float(os.environ.get("OKX_NEWS_INTERVAL_HOURS", "84"))
OKX_NEWS_JITTER_HOURS = float(os.environ.get("OKX_NEWS_JITTER_HOURS", "24"))
BYBIT_NEWS_ENABLED = os.environ.get("BYBIT_NEWS_ENABLED", "false").lower() == "true"
BYBIT_NEWS_INTERVAL_HOURS = float(os.environ.get("BYBIT_NEWS_INTERVAL_HOURS", "84"))
BYBIT_NEWS_JITTER_HOURS = float(os.environ.get("BYBIT_NEWS_JITTER_HOURS", "24"))

DB_PATH = BASE_DIR / "bot_state.db"
LOG_PATH = BASE_DIR / "bot.log"
