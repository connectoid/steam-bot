"""
Конфигурация. Все значения берутся из переменных окружения (.env).
Скопируйте .env.example в .env и заполните реальными значениями.
"""
import os

from dotenv import load_dotenv

load_dotenv()


def _required(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"Не задана обязательная переменная окружения: {name}")
    return value


def _int_list(value: str) -> list[int]:
    return [int(x) for x in value.replace(" ", "").split(",") if x]


# Токен Telegram-бота (от @BotFather)
BOT_TOKEN = _required("BOT_TOKEN")

# Токен из кабинета партнёра Payli (Authorization: Bearer <API_TOKEN>).
# Для создания заказов токену нужно право orders_create_acquirer,
# для команды /refund — право orders_refund.
PAYLI_API_TOKEN = os.environ.get("PAYLI_API_TOKEN", "")
PAYLI_BASE_URL = os.environ.get("PAYLI_BASE_URL", "https://payli.ru")

# Секрет для подписи вебхуков (X-Payli-Signature). Любая случайная строка до 512 символов,
# например: openssl rand -hex 32. Без секрета вебхуки не подписываются — так делать не стоит.
PAYLI_WEBHOOK_SECRET = os.environ.get("PAYLI_WEBHOOK_SECRET", "")

# Ваша наценка в процентах от суммы платежа (partner_commission_percent).
# Итоговая комиссия = базовая комиссия Payli + наценка, удерживается ИЗ платежа.
MARKUP_PERCENT = float(os.environ.get("MARKUP_PERCENT", "2.0"))

# Публичный HTTPS-адрес для вебхуков Payli: только https, порт 443 или 8443, публичный хост.
WEBHOOK_PUBLIC_URL = _required("WEBHOOK_PUBLIC_URL")
WEBHOOK_PATH = "/payli/webhook"

# Где бот слушает вебхуки локально (за nginx).
WEBHOOK_LISTEN_HOST = os.environ.get("WEBHOOK_LISTEN_HOST", "127.0.0.1")
WEBHOOK_LISTEN_PORT = int(os.environ.get("WEBHOOK_LISTEN_PORT", "8091"))

# Куда вернуть покупателя после оплаты (необязательно), например https://t.me/steam_charger_bot
REDIRECT_URL = os.environ.get("REDIRECT_URL", "")

# Telegram ID администраторов через запятую — им приходят алерты и доступны /balance, /order, /refund.
# Свой ID можно узнать командой /id в боте.
ADMIN_IDS = _int_list(os.environ.get("ADMIN_IDS", ""))

# Контакт поддержки для пользователей, например @your_username
SUPPORT_CONTACT = os.environ.get("SUPPORT_CONTACT", "")

# Режим «скоро запуск»: бот принимает логины, но заказы не создаёт.
# Включается сам, пока не задан PAYLI_API_TOKEN, или вручную: MAINTENANCE=1
MAINTENANCE = os.environ.get("MAINTENANCE", "").strip().lower() in ("1", "true", "yes", "on") or not PAYLI_API_TOKEN

# Канал с новостями, например @steam_charger (показывается в режиме «скоро запуск»)
NEWS_CHANNEL = os.environ.get("NEWS_CHANNEL", "")

# Как часто (сек) сверять незавершённые заказы через GET /orders/{id} — резерв, если вебхук не дошёл.
POLL_INTERVAL_SEC = int(os.environ.get("POLL_INTERVAL_SEC", "120"))

DB_PATH = os.environ.get("DB_PATH", "steam_bot.sqlite3")
