"""Настройки бота. Секреты берутся из переменных окружения (локально — из файла .env)."""

import hashlib
import os

from dotenv import load_dotenv

load_dotenv()


def _require(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Не задана переменная окружения {name} (см. .env.example)")
    return value


def _async_db_url(url: str) -> str:
    # Supabase/Render выдают postgresql://..., а SQLAlchemy нужен явный асинхронный драйвер.
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            url = "postgresql+asyncpg://" + url[len(prefix):]
            break
    return url.replace("sslmode=", "ssl=")  # asyncpg понимает ssl=, а не sslmode=


BOT_TOKEN = _require("BOT_TOKEN")
GROQ_API_KEY = _require("GROQ_API_KEY")

# Локально — файл SQLite, на сервере — PostgreSQL (Supabase) из DATABASE_URL.
DATABASE_URL = _async_db_url(os.getenv("DATABASE_URL", "sqlite+aiosqlite:///safechat.db"))

# Модели Groq: быстрый отсев + «консилиум» из моделей разных компаний.
SCREEN_MODEL = os.getenv("SCREEN_MODEL", "openai/gpt-oss-20b")
JUDGE_MODELS = [m.strip() for m in os.getenv("JUDGE_MODELS", "openai/gpt-oss-120b,qwen/qwen3.8-27b").split(",") if m.strip()]
ADVICE_MODEL = os.getenv("ADVICE_MODEL", "openai/gpt-oss-120b")

SCREEN_THRESHOLD = int(os.getenv("SCREEN_THRESHOLD", "4"))  # с какого напряжения звать консилиум
ALERT_THRESHOLD = float(os.getenv("ALERT_THRESHOLD", "6"))  # с какого уровня уведомлять медиатора

DEBOUNCE_SECONDS = 15  # ждём паузу в переписке, чтобы анализировать пачкой
BATCH_SIZE = 10  # ...но не дольше, чем столько новых сообщений
SCREEN_WINDOW = 15  # сообщений для отсева
JUDGE_WINDOW = 30  # сообщений для консилиума
ALERT_COOLDOWN_MINUTES = 15  # не дёргать медиатора чаще, если конфликт не усилился
HISTORY_LIMIT = 300  # сколько последних сообщений хранить на чат

# Webhook включается, если известен публичный адрес (Render задаёт RENDER_EXTERNAL_URL сам).
WEBHOOK_BASE_URL = os.getenv("WEBHOOK_BASE_URL") or os.getenv("RENDER_EXTERNAL_URL")
WEBHOOK_PATH = "webhook"
WEBHOOK_SECRET = hashlib.sha256(BOT_TOKEN.encode()).hexdigest()[:32]
PORT = int(os.getenv("PORT", "8080"))
