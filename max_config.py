"""
Настройки MAX-версии бота. Общие параметры (Payli, наценка, поддержка, режим «скоро запуск»)
берутся из config.py — тот же .env, что и у Telegram-бота. Здесь только то, что относится к MAX.
"""
import os

from config import _int_list  # noqa: F401  (тот же парсер списков ID)
from dotenv import load_dotenv

load_dotenv()

# Токен бота из кабинета MAX для партнёров (раздел «Интеграция»)
MAX_BOT_TOKEN = os.environ.get("MAX_BOT_TOKEN", "").strip()
if not MAX_BOT_TOKEN:
    raise RuntimeError("Не задана обязательная переменная окружения: MAX_BOT_TOKEN")

MAX_API_URL = os.environ.get("MAX_API_URL", "https://platform-api2.max.ru")

# Путь к корневому сертификату Минцифры, если он не установлен в систему (необязательно)
MAX_CA_FILE = os.environ.get("MAX_CA_FILE", "")

# ID администраторов в MAX (узнать: команда /id в MAX-боте). Если пусто — алерты уходят
# Telegram-админам из ADMIN_IDS через Telegram-бота.
MAX_ADMIN_IDS = _int_list(os.environ.get("MAX_ADMIN_IDS", ""))

# Отдельная база: ID пользователей MAX и Telegram могут совпадать численно.
MAX_DB_PATH = os.environ.get("MAX_DB_PATH", "steam_bot_max.sqlite3")

# Вебхуки Payli для заказов из MAX — отдельный путь и порт, чтобы не смешивать с Telegram.
MAX_WEBHOOK_PATH = "/payli/webhook-max"
MAX_WEBHOOK_PUBLIC_URL = os.environ.get("MAX_WEBHOOK_PUBLIC_URL", "")
MAX_WEBHOOK_LISTEN_HOST = os.environ.get("MAX_WEBHOOK_LISTEN_HOST", "127.0.0.1")
MAX_WEBHOOK_LISTEN_PORT = int(os.environ.get("MAX_WEBHOOK_LISTEN_PORT", "8092"))

# Куда вернуть покупателя после оплаты, например https://max.ru/<ник_бота> (необязательно)
MAX_REDIRECT_URL = os.environ.get("MAX_REDIRECT_URL", "")
