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
GROQ_MODEL_SECONDARY = os.environ.get("GROQ_MODEL_SECONDARY", "llama-3.1-8b-instant")

# --- OKX Orbit (см. okx_orbit_generator.py / okx_draft_publisher.py) ---
# У OKX Orbit нет публичного API для постинга - бот только ГОТОВИТ
# черновик и присылает его в личный Telegram-чат владельца
# (OKX_ORBIT_DRAFT_CHAT_ID), публикация - вручную.
OKX_ORBIT_ENABLED = os.environ.get("OKX_ORBIT_ENABLED", "false").lower() == "true"
OKX_ORBIT_DRAFT_CHAT_ID = os.environ.get("OKX_ORBIT_DRAFT_CHAT_ID", "")
OKX_ORBIT_INTERVAL_HOURS = float(os.environ.get("OKX_ORBIT_INTERVAL_HOURS", "8"))
OKX_ORBIT_JITTER_HOURS = float(os.environ.get("OKX_ORBIT_JITTER_HOURS", "2"))

# --- Bybit ByX (заготовка - см. README.md, следующий этап) ---
BYBIT_BYX_ENABLED = os.environ.get("BYBIT_BYX_ENABLED", "false").lower() == "true"
BYBIT_BYX_DRAFT_CHAT_ID = os.environ.get("BYBIT_BYX_DRAFT_CHAT_ID", "")
BYBIT_BYX_INTERVAL_HOURS = float(os.environ.get("BYBIT_BYX_INTERVAL_HOURS", "8"))
BYBIT_BYX_JITTER_HOURS = float(os.environ.get("BYBIT_BYX_JITTER_HOURS", "2"))

DB_PATH = BASE_DIR / "bot_state.db"
LOG_PATH = BASE_DIR / "bot.log"
