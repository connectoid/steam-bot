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


# Токен вашего Telegram-бота (от @BotFather)
BOT_TOKEN = _required("BOT_TOKEN")

# Токен из кабинета партнёра Payli (Authorization: Bearer <API_TOKEN>)
PAYLI_API_TOKEN = _required("PAYLI_API_TOKEN")

# Секрет для проверки подписи вебхуков Payli (X-Payli-Signature).
# Можно придумать любую случайную строку — тот же секрет передаётся в webhook_secret при создании заказа.
PAYLI_WEBHOOK_SECRET = os.environ.get("PAYLI_WEBHOOK_SECRET", "")

# Ваша наценка сверх базовой комиссии Payli, в процентах.
# Пример из документации: база 4%, наценка 2% -> итоговая комиссия 6%.
MARKUP_PERCENT = float(os.environ.get("MARKUP_PERCENT", "2.0"))

# Публичный HTTPS-адрес, на который Payli будет слать вебхуки о статусе заказа.
# Требования из документации: только https, порт 443 или 8443, публичный хост.
# Например: https://your-domain.example/payli/webhook
WEBHOOK_PUBLIC_URL = _required("WEBHOOK_PUBLIC_URL")

# На каком хосте/порту локально слушать входящие вебхуки (за них должен отвечать реверс-прокси
# на WEBHOOK_PUBLIC_URL, если порт отличается от 443/8443 — см. README).
WEBHOOK_LISTEN_HOST = os.environ.get("WEBHOOK_LISTEN_HOST", "0.0.0.0")
WEBHOOK_LISTEN_PORT = int(os.environ.get("WEBHOOK_LISTEN_PORT", "8443"))

DB_PATH = os.environ.get("DB_PATH", "steam_bot.sqlite3")

PAYLI_BASE_URL = "https://payli.ru"
