"""
Steam Charger для мессенджера MAX — копия Telegram-бота (bot.py) на MAX Bot API.

Сценарий тот же:
  1. «Начать» / /start -> бот просит логин Steam (с инструкцией, где его посмотреть).
  2. Пользователь присылает сумму зачисления -> бот показывает расчёт и кнопки
     «Оплатить» / «Изменить логин».
  3. «Оплатить» -> заказ в Payli (Payli проверяет логин) -> ссылка на оплату по СБП + QR.
  4. Вебхук Payli (свой путь /payli/webhook-max) или резервная сверка -> уведомление в MAX.

Обновления от MAX получаем через long polling (GET /updates): вебхук MAX требует порт 443,
а он на сервере занят VPN.

Использует общие модули: config.py (Payli, наценка, поддержка), db.py, payli_client.py.
База — отдельная (MAX_DB_PATH); в колонке users/orders.telegram_id хранится user_id из MAX.

Запуск: python max_bot.py
"""
import asyncio
import hashlib
import hmac
import io
import json
import logging
import math
import re
import uuid
from typing import Optional

import aiohttp
import qrcode
from aiohttp import web

import db
from config import (
    ADMIN_IDS as TELEGRAM_ADMIN_IDS,
    BOT_TOKEN as TELEGRAM_BOT_TOKEN,
    MAINTENANCE,
    MARKUP_PERCENT,
    NEWS_CHANNEL,
    PAYLI_WEBHOOK_SECRET,
    POLL_INTERVAL_SEC,
    SUPPORT_CONTACT,
)
from max_api import MaxApi, MaxError, callback_button, keyboard, link_button
from max_config import (
    MAX_ADMIN_IDS,
    MAX_API_URL,
    MAX_BOT_TOKEN,
    MAX_CA_FILE,
    MAX_DB_PATH,
    MAX_REDIRECT_URL,
    MAX_WEBHOOK_LISTEN_HOST,
    MAX_WEBHOOK_LISTEN_PORT,
    MAX_WEBHOOK_PATH,
    MAX_WEBHOOK_PUBLIC_URL,
)
from payli_client import PayliClient, PayliError

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("steam_charger_max")

# Своя база для MAX (db.py читает db.DB_PATH при каждом подключении).
db.DB_PATH = MAX_DB_PATH

api = MaxApi(MAX_BOT_TOKEN, MAX_API_URL, MAX_CA_FILE)
payli = PayliClient()

STEAM_LOGIN_RE = re.compile(r"^[A-Za-z0-9_.\-]{2,64}$")
AMOUNT_RE = re.compile(r"^\s*\d{1,7}([.,]\d{1,2})?\s*$")

SUPPORT_TEXT = SUPPORT_CONTACT or "поддержку (/support)"

UPDATE_TYPES = ["message_created", "message_callback", "bot_started"]

# Пользователи, от которых ждём логин (аналог FSM-состояния в Telegram-боте).
_waiting_login: set[int] = set()
# Защита от двойного нажатия «Оплатить».
_creating_orders: set[int] = set()
# Ссылки на фоновые задачи, чтобы их не собрал GC.
_background_tasks: set[asyncio.Task] = set()


# ---------- тексты ----------

COMING_SOON_TEXT = (
    "🚀 Пополнение Steam скоро заработает — мы завершаем подключение платёжного сервиса."
    + (f"\nО запуске сообщим в канале {NEWS_CHANNEL}." if NEWS_CHANNEL else "")
)

LOGIN_HELP_TEXT = (
    "Где посмотреть логин Steam:\n"
    "• В приложении Steam на ПК: нажмите на свой ник справа вверху → «Об аккаунте». "
    "Логин написан в заголовке этой страницы.\n"
    "• В браузере: откройте store.steampowered.com/account — логин вверху страницы.\n"
    "• Это имя, которое вы вводите при входе в Steam вместе с паролем.\n\n"
    "Логин ≠ ник в профиле. Пароль и коды Steam Guard не нужны — никому их не сообщайте."
)

CREDIT_DELAY_TEXT = (
    "После оплаты деньги поступают на Steam не мгновенно — обычно в течение 20–30 минут, "
    "иногда дольше. Это нормально: как только они зачислятся, я пришлю уведомление сюда."
)

HELP_TEXT = (
    "Как пополнить Steam:\n"
    "1. Один раз пришлите логин Steam — имя, под которым вы входите в клиент "
    "(не ник в профиле). Сменить: /login\n"
    "2. Пришлите сумму в рублях, которая должна прийти на баланс, например: 500\n"
    "3. Проверьте логин и сумму, нажмите «Оплатить» и оплатите по ссылке или QR через СБП.\n\n"
    "Сумма к оплате с учётом всех комиссий показывается до оплаты. Ссылка действует 30 минут. "
    "Зачисление обычно занимает 20–30 минут после оплаты — когда деньги поступят, я пришлю уведомление.\n\n"
    "Пароль и коды Steam Guard не нужны никогда — не сообщайте их никому.\n\n"
    f"Вопросы: {SUPPORT_TEXT}"
)

USER_STATUS_TEXT = {
    "payment_received": (
        "💳 Оплата по заказу {short} получена, спасибо!\n\n"
        "Зачисляем {credit} на Steam-аккаунт {login}. Обычно это занимает 20–30 минут, иногда дольше — "
        "ничего делать не нужно. Как только деньги поступят, я пришлю уведомление."
    ),
    "completed": "✅ Готово! На Steam-аккаунт {login} зачислено {credit}.",
    "failed": (
        "❌ Не удалось зачислить деньги по заказу {short}. Мы уже разбираемся и вернём оплату "
        "или завершим пополнение. Если есть вопросы — {support}."
    ),
    "canceled": "⌛ Заказ {short} отменён: оплата не поступила за 30 минут. Чтобы пополнить, пришлите сумму ещё раз.",
    "refunded": "↩️ Оплата по заказу {short} ({paid}) возвращена на ваш счёт.",
    "chargeback": "⚠️ По заказу {short} платёж оспорен в банке. Если это ошибка — напишите в {support}.",
    "creation_failed": "❌ Не удалось создать оплату по заказу {short}. Пришлите сумму ещё раз.",
}
USER_STATUS_TEXT["processing_payout"] = USER_STATUS_TEXT["payment_received"]
_PAID_STATUSES = {"payment_received", "processing_payout"}
ADMIN_ALERT_STATUSES = {"failed", "chargeback", "creation_failed", "refunded"}


# ---------- утилиты ----------

def rub(value: float) -> str:
    if abs(value - round(value)) < 0.005:
        s = f"{round(value):,}".replace(",", " ")
    else:
        s = f"{value:,.2f}".replace(",", " ").replace(".", ",")
    return f"{s} ₽"


def calc_amount_pay(credit_rub: float, base_commission_percent: float, markup_percent: float) -> float:
    """pay = credited / (1 − total/100), вверх до копейки — см. bot.py."""
    total = base_commission_percent + markup_percent
    if not 0 <= total < 100:
        raise ValueError(f"Некорректная суммарная комиссия: {total}%")
    return math.ceil(round(credit_rub / (1 - total / 100) * 100, 6)) / 100


def make_qr_png(data: str) -> bytes:
    img = qrcode.make(data)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def spawn(coro) -> None:
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


def error_text(e: PayliError, login: str) -> str:
    if e.code == "steam_login_invalid":
        return (
            f"Steam не нашёл аккаунт с логином «{login}».\n"
            "Проверьте, что это логин для входа в Steam, а не ник в профиле, "
            "и сохраните правильный. Оплата не создана, деньги не списаны."
        )
    if e.code == "invalid_amount":
        return "Эта сумма вне допустимого диапазона. Попробуйте другую сумму."
    if e.code in ("service_unavailable", "steam_service_not_found"):
        return "Пополнение Steam сейчас временно недоступно. Попробуйте чуть позже."
    if e.retryable:
        return "Платёжный сервис сейчас не отвечает. Попробуйте ещё раз через пару минут."
    return f"Не получилось создать заказ (код: {e.code}). Попробуйте позже или напишите в {SUPPORT_TEXT}."


def change_login_kb() -> list[dict]:
    return [keyboard([[callback_button("✏️ Изменить логин", "chlogin")]])]


async def send(user_id: int, text: str, attachments: Optional[list[dict]] = None) -> Optional[dict]:
    try:
        return await api.send_message(user_id, text, attachments)
    except MaxError:
        log.exception("Не удалось отправить сообщение пользователю MAX %s", user_id)
        return None


async def notify_admins(text: str) -> None:
    text = "[MAX] " + text
    if MAX_ADMIN_IDS:
        for admin_id in MAX_ADMIN_IDS:
            await send(admin_id, text)
        return
    # Запасной канал — Telegram-админы через Telegram Bot API.
    if not TELEGRAM_ADMIN_IDS:
        return
    async with aiohttp.ClientSession() as s:
        for admin_id in TELEGRAM_ADMIN_IDS:
            try:
                await s.post(
                    f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                    json={"chat_id": admin_id, "text": text},
                    timeout=aiohttp.ClientTimeout(total=15),
                )
            except Exception:
                log.exception("Не удалось отправить алерт в Telegram админу %s", admin_id)


def message_id_of(msg: Optional[dict]) -> Optional[str]:
    if not msg:
        return None
    return (msg.get("body") or {}).get("mid")


# ---------- смена статуса заказа ----------

async def handle_status(public_id: str, status: str, source: str) -> None:
    order = await db.apply_status(public_id, status)
    if order is None:
        return
    old = order["old_status"]
    log.info("Заказ %s: %s -> %s (%s)", public_id, old, status, source)

    send_user = status in USER_STATUS_TEXT and not (status in _PAID_STATUSES and old in _PAID_STATUSES)
    if send_user:
        await send(
            order["telegram_id"],
            USER_STATUS_TEXT[status].format(
                short=public_id[:8],
                credit=rub(order["requested_credit_rub"]),
                paid=rub(order["amount_pay_rub"]),
                login=order["steam_login"],
                support=SUPPORT_TEXT,
            ),
        )

    if status in ADMIN_ALERT_STATUSES:
        hint = f"\nВернуть оплату по СБП: /refund {public_id}" if status == "failed" else ""
        await notify_admins(
            f"⚠️ Заказ {public_id}\n{old} → {status}\n"
            f"Пользователь MAX: {order['telegram_id']}, логин: {order['steam_login']}\n"
            f"Зачисление: {rub(order['requested_credit_rub'])}, оплата: {rub(order['amount_pay_rub'])}{hint}"
        )


# ---------- сценарий пользователя ----------

async def ask_login(user_id: int, intro: str) -> None:
    _waiting_login.add(user_id)
    await send(user_id, f"{intro}\n\n{LOGIN_HELP_TEXT}")


async def cmd_start(user_id: int) -> None:
    login = await db.get_user_login(user_id)
    if login:
        _waiting_login.discard(user_id)
        await send(
            user_id,
            f"С возвращением! Ваш логин Steam: {login}\n\n"
            "Пришлите сумму в рублях, которая должна прийти на баланс Steam (например: 500), "
            "и я пришлю ссылку на оплату по СБП.\n\nСменить логин: /login · Помощь: /help"
            + (f"\n\n{COMING_SOON_TEXT}" if MAINTENANCE else ""),
        )
        return
    await ask_login(user_id, "Привет! Я пополняю баланс Steam через СБП.\n\nПришлите ваш логин Steam.")


async def cmd_login(user_id: int) -> None:
    current = await db.get_user_login(user_id)
    intro = f"Сейчас сохранён логин: {current}\nПришлите новый логин Steam." if current else "Пришлите логин Steam."
    await ask_login(user_id, intro)


async def process_login(user_id: int, text: str) -> None:
    login = text.strip()
    if not STEAM_LOGIN_RE.match(login):
        await send(
            user_id,
            "Это не похоже на логин Steam: допустимы латинские буквы, цифры и «_», "
            f"от 2 до 64 символов, без пробелов. Попробуйте ещё раз.\n\n{LOGIN_HELP_TEXT}",
        )
        return
    await db.set_user_login(user_id, login)
    _waiting_login.discard(user_id)
    await send(
        user_id,
        f"Логин сохранён: {login}\n"
        "Проверьте, что это именно логин для входа, а не ник в профиле. "
        "Существует ли такой аккаунт, Steam проверит при создании заказа — до оплаты, "
        "так что заплатить за неверный логин не получится.\n\n"
        + (
            f"{COMING_SOON_TEXT}\nКак только запустимся — просто пришлите сумму."
            if MAINTENANCE
            else "Теперь пришлите сумму в рублях, например 500."
        ),
        change_login_kb(),
    )


async def process_amount(user_id: int, text: str) -> None:
    if MAINTENANCE:
        await send(user_id, COMING_SOON_TEXT)
        return
    login = await db.get_user_login(user_id)
    if not login:
        await send(user_id, "Сначала пришлите логин Steam — отправьте /start.")
        return
    credit = round(float(text.strip().replace(",", ".")), 2)
    if credit <= 0:
        await send(user_id, "Сумма должна быть больше нуля.")
        return
    try:
        service = await payli.get_steam_service()
    except PayliError as e:
        log.warning("Не удалось получить условия Steam: %s", e)
        await send(user_id, error_text(e, login))
        return
    min_rub, max_rub = float(service["min_rub"]), float(service["max_rub"])
    if not (min_rub <= credit <= max_rub):
        await send(user_id, f"Пополнить можно на сумму от {rub(min_rub)} до {rub(max_rub)}. Пришлите другую сумму.")
        return
    amount_pay = calc_amount_pay(credit, float(service["base_commission_percent"]), MARKUP_PERCENT)
    await send(
        user_id,
        "Проверьте заказ:\n\n"
        f"Логин Steam: {login}\n"
        f"Зачислится: {rub(credit)}\n"
        f"К оплате: {rub(amount_pay)} — все комиссии включены\n\n"
        "Если логин верный — нажмите «Оплатить».",
        [keyboard([
            [callback_button(f"✅ Оплатить {rub(amount_pay)}", f"pay:{credit:.2f}")],
            [callback_button("✏️ Изменить логин", "chlogin")],
        ])],
    )


async def process_pay(user_id: int, credit_raw: str, source_mid: Optional[str]) -> None:
    if MAINTENANCE:
        await send(user_id, COMING_SOON_TEXT)
        return
    if user_id in _creating_orders:
        return
    try:
        credit = round(float(credit_raw), 2)
    except ValueError:
        return
    login = await db.get_user_login(user_id)
    if not login:
        await send(user_id, "Сначала укажите логин Steam: /start")
        return

    _creating_orders.add(user_id)
    try:
        # Убираем кнопки у сообщения с расчётом, чтобы не нажали второй раз.
        if source_mid:
            try:
                await api.edit_message(source_mid, f"Создаю заказ и проверяю логин {login}…", [])
            except MaxError:
                log.warning("Не удалось отредактировать сообщение %s", source_mid)
        try:
            service = await payli.get_steam_service()
            base_commission = float(service["base_commission_percent"])
            amount_pay = calc_amount_pay(credit, base_commission, MARKUP_PERCENT)
            resp = await payli.create_steam_order(
                account=login,
                amount_pay_rub=amount_pay,
                partner_commission_percent=MARKUP_PERCENT,
                idempotency_key=str(uuid.uuid4()),
                webhook_url=MAX_WEBHOOK_PUBLIC_URL,
                webhook_secret=PAYLI_WEBHOOK_SECRET,
                redirect_url=MAX_REDIRECT_URL,
            )
        except PayliError as e:
            log.warning("Не удалось создать заказ для MAX %s: %s", user_id, e)
            await send(user_id, error_text(e, login),
                       change_login_kb() if e.code == "steam_login_invalid" else None)
            if e.code in ("invalid_partner_commission_percent", "invalid_total_commission_percent",
                          "unauthorized", "insufficient_scope", "forbidden", "invalid_webhook_url"):
                await notify_admins(f"🛠 Ошибка настройки при создании заказа: {e.code} {e.message}")
            return

        order = resp.get("order", {})
        public_id = order.get("public_id")
        pay_url = (resp.get("payment") or {}).get("pay_url")
        amount_pay = float(order.get("amount_pay_rub", amount_pay))

        if public_id:
            await db.create_order(
                db.OrderRecord(
                    public_id=public_id,
                    telegram_id=user_id,  # здесь — user_id из MAX
                    steam_login=login,
                    requested_credit_rub=credit,
                    amount_pay_rub=amount_pay,
                    status=order.get("status", "pending_payment"),
                    base_commission_percent=base_commission,
                    partner_commission_percent=order.get("partner_commission_percent", MARKUP_PERCENT),
                    partner_commission_rub=order.get("partner_commission_rub"),
                    partner_fee_rub=order.get("partner_fee_rub"),
                )
            )
            await db.log_event(public_id, "create", resp)

        if not public_id or not pay_url:
            log.error("Payli вернул заказ без public_id/pay_url: %s", resp)
            await send(user_id, f"Не удалось получить ссылку на оплату. Попробуйте ещё раз или напишите в {SUPPORT_TEXT}.")
            return

        attachments: list[dict] = []
        try:
            attachments.append(await api.upload_image(make_qr_png(pay_url), "payment_qr.png"))
        except Exception:
            log.exception("Не удалось загрузить QR — отправляю без картинки")
        attachments.append(keyboard([[link_button("Оплатить по СБП", pay_url)]]))

        text = (
            f"Логин Steam: {login} ✓\n"
            f"Зачислится: {rub(credit)}\n"
            f"К оплате: {rub(amount_pay)} — все комиссии включены\n\n"
            "Нажмите «Оплатить по СБП» или отсканируйте QR с телефона. Ссылка действует 30 минут.\n\n"
            f"⏳ {CREDIT_DELAY_TEXT}\n"
            f"Заказ: {public_id[:8]}"
        )
        sent = await send(user_id, text, attachments)
        if sent is None and len(attachments) > 1:
            await send(user_id, text, attachments[-1:])  # повтор без картинки
        if source_mid:
            try:
                await api.delete_message(source_mid)
            except MaxError:
                pass
    finally:
        _creating_orders.discard(user_id)


# ---------- команды администратора ----------

async def admin_command(user_id: int, cmd: str, arg: str) -> bool:
    if user_id not in MAX_ADMIN_IDS:
        return False
    try:
        if cmd == "/balance":
            data = await payli.get_balance(limit=10)
            lines = [f"Баланс партнёра: {rub(float(data.get('balance_rub', 0)))}", "", "Последние операции:"]
            for tx in data.get("transactions", [])[:10]:
                lines.append(f"{tx.get('created_at', '')[:16]}  {tx.get('tx_type')}  {tx.get('amount_rub')} ₽")
            await send(user_id, "\n".join(lines))
        elif cmd == "/order":
            if not arg:
                await send(user_id, "Использование: /order <public_id>")
                return True
            order = (await payli.get_order(arg)).get("order", {})
            if order.get("status"):
                await handle_status(arg, order["status"], "admin")
            await send(user_id, json.dumps(order, ensure_ascii=False, indent=1)[:3900])
        elif cmd == "/refund":
            if not arg:
                await send(user_id, "Использование: /refund <public_id> — только для заказов в статусе failed (СБП).")
                return True
            data = await payli.refund_order(arg)
            await db.log_event(arg, "refund", data)
            status = data.get("order", {}).get("status")
            await send(user_id, f"Возврат выполнен: {status}. Комиссия: {(data.get('fee') or {}).get('rub', 0)} ₽")
            if status:
                await handle_status(arg, status, "refund")
        else:
            return False
    except PayliError as e:
        await send(user_id, f"Ошибка: {e.code} {e.message}")
    return True


# ---------- разбор обновлений MAX ----------

async def on_message(update: dict) -> None:
    msg = update.get("message") or {}
    sender = msg.get("sender") or {}
    recipient = msg.get("recipient") or {}
    if sender.get("is_bot") or recipient.get("chat_type") not in (None, "dialog"):
        return  # отвечаем только в личном диалоге с человеком
    user_id = sender.get("user_id")
    text = ((msg.get("body") or {}).get("text") or "").strip()
    if not user_id or not text:
        return

    if text.startswith("/"):
        parts = text.split(maxsplit=1)
        cmd = parts[0].split("@")[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""
        if cmd == "/start":
            await cmd_start(user_id)
        elif cmd == "/login":
            await cmd_login(user_id)
        elif cmd == "/help":
            await send(user_id, HELP_TEXT)
        elif cmd == "/support":
            await send(user_id, f"Напишите в поддержку: {SUPPORT_CONTACT}\nЕсли вопрос по заказу — укажите его номер."
                       if SUPPORT_CONTACT else "Поддержка скоро появится.")
        elif cmd == "/id":
            await send(user_id, f"Ваш ID в MAX: {user_id}")
        elif not await admin_command(user_id, cmd, arg):
            await send(user_id, "Неизвестная команда. Помощь: /help")
        return

    if user_id in _waiting_login:
        await process_login(user_id, text)
    elif AMOUNT_RE.match(text):
        await process_amount(user_id, text)
    elif not await db.get_user_login(user_id):
        # Новый пользователь сразу прислал логин, не нажав «Начать».
        _waiting_login.add(user_id)
        await process_login(user_id, text)
    else:
        await send(user_id, "Пришлите сумму в рублях для пополнения Steam (например: 500) или /help.")


async def on_callback(update: dict) -> None:
    cb = update.get("callback") or {}
    user_id = (cb.get("user") or {}).get("user_id")
    payload = cb.get("payload") or ""
    callback_id = cb.get("callback_id")
    source_mid = message_id_of(update.get("message"))
    if callback_id:
        try:
            await api.answer_callback(callback_id, "Создаю заказ…" if payload.startswith("pay:") else "Ок")
        except MaxError:
            log.warning("Не удалось ответить на callback %s", callback_id)
    if not user_id:
        return
    if payload == "chlogin":
        await ask_login(user_id, "Пришлите правильный логин Steam.")
    elif payload.startswith("pay:"):
        await process_pay(user_id, payload.split(":", 1)[1], source_mid)


async def handle_update(update: dict) -> None:
    try:
        kind = update.get("update_type")
        if kind == "message_created":
            await on_message(update)
        elif kind == "message_callback":
            await on_callback(update)
        elif kind == "bot_started":
            user_id = (update.get("user") or {}).get("user_id")
            if user_id:
                await cmd_start(user_id)
    except Exception:
        log.exception("Ошибка обработки обновления MAX: %s", update)


async def poll_max_updates() -> None:
    marker: Optional[int] = None
    while True:
        try:
            data = await api.get_updates(marker, timeout=30, types=UPDATE_TYPES)
        except asyncio.CancelledError:
            raise
        except MaxError as e:
            log.warning("MAX /updates: %s", e)
            await asyncio.sleep(5)
            continue
        for update in data.get("updates") or []:
            spawn(handle_update(update))
        if data.get("marker") is not None:
            marker = data["marker"]


# ---------- вебхуки Payli ----------

def verify_signature(raw_body: bytes, signature_header: str) -> bool:
    if not PAYLI_WEBHOOK_SECRET:
        return True
    if not signature_header.startswith("sha256="):
        return False
    expected = hmac.new(PAYLI_WEBHOOK_SECRET.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header.removeprefix("sha256=").lower())


async def process_webhook(payload: dict) -> None:
    public_id = payload.get("order_public_id") or payload.get("public_id")
    try:
        await db.log_event(public_id, "webhook", payload)
        if payload.get("event") == "order.status" and public_id and payload.get("status"):
            await handle_status(public_id, payload["status"], "webhook")
    except Exception:
        log.exception("Ошибка обработки вебхука %s", payload)


async def payli_webhook_handler(request: web.Request) -> web.Response:
    raw_body = await request.read()
    if not verify_signature(raw_body, request.headers.get("X-Payli-Signature", "")):
        log.warning("Payli webhook: неверная подпись")
        return web.json_response({"ok": False}, status=401)
    try:
        payload = json.loads(raw_body)
    except ValueError:
        return web.json_response({"ok": False}, status=400)
    event_id = request.headers.get("X-Payli-Event-Id", "")
    if event_id and not await db.mark_event_processed(event_id):
        return web.json_response({"ok": True, "duplicate": True})
    spawn(process_webhook(payload))
    return web.json_response({"ok": True})


async def health_handler(request: web.Request) -> web.Response:
    return web.json_response({"ok": True, "bot": "max"})


async def poll_unfinished_orders() -> None:
    while True:
        await asyncio.sleep(POLL_INTERVAL_SEC)
        try:
            for public_id in await db.list_unfinished_orders():
                try:
                    data = await payli.get_order(public_id)
                except PayliError as e:
                    log.warning("Сверка заказа %s: %s", public_id, e)
                    continue
                status = (data.get("order") or {}).get("status")
                if status:
                    await handle_status(public_id, status, "poll")
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Ошибка периодической сверки заказов")


# ---------- запуск ----------

async def main() -> None:
    if not MAX_WEBHOOK_PUBLIC_URL and not MAINTENANCE:
        log.warning("MAX_WEBHOOK_PUBLIC_URL не задан — статусы заказов будут приходить только через сверку.")
    await db.init_db()
    await api.start()
    await payli.start()

    me = await api.get_me()  # заодно проверяем токен и сертификат
    log.info("MAX-бот: %s (id %s)", me.get("name") or me.get("first_name"), me.get("user_id"))
    try:
        await api.set_commands([
            ("start", "Начать / пополнить баланс"),
            ("login", "Изменить логин Steam"),
            ("help", "Как пользоваться"),
            ("support", "Связаться с поддержкой"),
        ])
    except MaxError as e:
        log.warning("Не удалось установить меню команд: %s", e)

    app = web.Application(client_max_size=1024 * 1024)
    app.router.add_post(MAX_WEBHOOK_PATH, payli_webhook_handler)
    app.router.add_get(MAX_WEBHOOK_PATH, health_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, MAX_WEBHOOK_LISTEN_HOST, MAX_WEBHOOK_LISTEN_PORT).start()
    log.info("Webhook-сервер Payli (MAX) на %s:%s%s", MAX_WEBHOOK_LISTEN_HOST, MAX_WEBHOOK_LISTEN_PORT, MAX_WEBHOOK_PATH)

    if MAINTENANCE:
        log.warning("Режим «скоро запуск»: заказы не создаются (нет PAYLI_API_TOKEN или MAINTENANCE=1).")
    tasks = [asyncio.create_task(poll_max_updates())]
    if not MAINTENANCE:
        tasks.append(asyncio.create_task(poll_unfinished_orders()))
    try:
        await asyncio.gather(*tasks)
    finally:
        for t in tasks:
            t.cancel()
        await runner.cleanup()
        await payli.close()
        await api.close()


if __name__ == "__main__":
    asyncio.run(main())
