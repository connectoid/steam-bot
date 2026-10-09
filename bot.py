"""
Steam Charger — Telegram-бот пополнения Steam по логину через Payli (оплата по СБП).

Сценарий:
  1. /start -> если логин Steam не сохранён, бот просит его прислать.
  2. Пользователь присылает сумму, которая должна зачислиться на Steam -> бот считает сумму
     к оплате (базовая комиссия Payli + ваша наценка удерживаются ИЗ платежа), создаёт заказ
     и присылает ссылку на оплату по СБП + QR.
  3. Payli шлёт вебхук о смене статуса -> бот уведомляет пользователя (и админа о проблемах).
     Резерв: раз в POLL_INTERVAL_SEC бот сам сверяет незавершённые заказы через GET /orders/{id}.

Запуск: python bot.py  (long polling Telegram + aiohttp-сервер для вебхуков Payli)
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

import qrcode
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BotCommand,
    BufferedInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from aiohttp import web

import db
from config import (
    ADMIN_IDS,
    BOT_TOKEN,
    MARKUP_PERCENT,
    PAYLI_WEBHOOK_SECRET,
    POLL_INTERVAL_SEC,
    REDIRECT_URL,
    SUPPORT_CONTACT,
    WEBHOOK_LISTEN_HOST,
    WEBHOOK_LISTEN_PORT,
    WEBHOOK_PATH,
    WEBHOOK_PUBLIC_URL,
)
from payli_client import PayliClient, PayliError

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("steam_charger")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
router = Router()
dp.include_router(router)
payli = PayliClient()

# Логин Steam (Account Name): латиница, цифры, «_». Окончательно логин проверяет Payli (400 steam_login_invalid).
STEAM_LOGIN_RE = re.compile(r"^[A-Za-z0-9_.\-]{2,64}$")
AMOUNT_RE = r"^\s*\d{1,7}([.,]\d{1,2})?\s*$"

SUPPORT_TEXT = SUPPORT_CONTACT or "поддержку (/support)"

# Ссылки на фоновые задачи, чтобы их не собрал GC.
_background_tasks: set[asyncio.Task] = set()


class Form(StatesGroup):
    waiting_login = State()


# ---------- утилиты ----------

def rub(value: float) -> str:
    """1063.8 -> '1 063,80 ₽'; целые суммы без копеек."""
    if abs(value - round(value)) < 0.005:
        s = f"{round(value):,}".replace(",", " ")
    else:
        s = f"{value:,.2f}".replace(",", " ").replace(".", ",")
    return f"{s} ₽"


def calc_amount_pay(credit_rub: float, base_commission_percent: float, markup_percent: float) -> float:
    """
    Комиссия Payli удерживается ИЗ платежа: credited = pay × (1 − total/100).
    Обратная задача: pay = credited / (1 − total/100), округляем ВВЕРХ до копейки,
    чтобы на Steam пришло не меньше запрошенного.
    """
    total = base_commission_percent + markup_percent
    if not 0 <= total < 100:
        raise ValueError(f"Некорректная суммарная комиссия: {total}%")
    raw = credit_rub / (1 - total / 100)
    return math.ceil(round(raw * 100, 6)) / 100


def make_qr_png(data: str) -> bytes:
    img = qrcode.make(data)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def spawn(coro) -> None:
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


async def notify_admins(text: str) -> None:
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(admin_id, text)
        except Exception:
            log.exception("Не удалось отправить сообщение админу %s", admin_id)


def error_text(e: PayliError, login: str) -> str:
    if e.code == "steam_login_invalid":
        return (
            f"Steam не нашёл аккаунт с логином «{login}».\n"
            "Проверьте, что это логин для входа в Steam, а не ник в профиле, "
            "и сохраните правильный командой /login."
        )
    if e.code == "invalid_amount":
        return "Эта сумма вне допустимого диапазона. Попробуйте другую сумму."
    if e.code in ("service_unavailable", "steam_service_not_found"):
        return "Пополнение Steam сейчас временно недоступно. Попробуйте чуть позже."
    if e.retryable:
        return "Платёжный сервис сейчас не отвечает. Попробуйте ещё раз через пару минут."
    return f"Не получилось создать заказ (код: {e.code}). Попробуйте позже или напишите в {SUPPORT_TEXT}."


# ---------- тексты ----------

HELP_TEXT = (
    "Как пополнить Steam:\n"
    "1. Один раз пришлите логин Steam — имя, под которым вы входите в клиент "
    "(не ник в профиле). Сменить: /login\n"
    "2. Пришлите сумму в рублях, которая должна прийти на баланс, например: 500\n"
    "3. Оплатите по ссылке или QR через СБП в приложении своего банка.\n\n"
    "Сумма к оплате с учётом всех комиссий показывается до оплаты. "
    "Ссылка действует 30 минут. Когда деньги зачислятся, я пришлю уведомление.\n\n"
    "Пароль и коды Steam Guard не нужны никогда — не сообщайте их никому.\n\n"
    f"Вопросы: {SUPPORT_TEXT}"
)

# Уведомления пользователю по статусам заказа.
USER_STATUS_TEXT = {
    "payment_received": "💳 Оплата по заказу {short} получена. Зачисляем {credit} на Steam-аккаунт {login}…",
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
# processing_payout отдельным вебхуком не приходит; если сверка увидела его раньше payment_received,
# сообщаем как об оплате.
USER_STATUS_TEXT["processing_payout"] = USER_STATUS_TEXT["payment_received"]
_PAID_STATUSES = {"payment_received", "processing_payout"}

ADMIN_ALERT_STATUSES = {"failed", "chargeback", "creation_failed", "refunded"}


# ---------- обработка смены статуса ----------

async def handle_status(public_id: str, status: str, source: str) -> None:
    order = await db.apply_status(public_id, status)
    if order is None:
        return  # не наш заказ, статус не изменился или запоздавший вебхук
    old = order["old_status"]
    log.info("Заказ %s: %s -> %s (%s)", public_id, old, status, source)

    # «Оплата получена» шлём один раз — при выходе из pending_payment/canceled.
    send_user = status in USER_STATUS_TEXT and not (status in _PAID_STATUSES and old in _PAID_STATUSES)
    if send_user:
        text = USER_STATUS_TEXT[status].format(
            short=public_id[:8],
            credit=rub(order["requested_credit_rub"]),
            paid=rub(order["amount_pay_rub"]),
            login=order["steam_login"],
            support=SUPPORT_TEXT,
        )
        try:
            await bot.send_message(order["telegram_id"], text)
        except Exception:
            log.exception("Не удалось отправить уведомление пользователю %s", order["telegram_id"])

    if status in ADMIN_ALERT_STATUSES:
        hint = f"\nВернуть оплату по СБП: /refund {public_id}" if status == "failed" else ""
        await notify_admins(
            f"⚠️ Заказ {public_id}\n{old} → {status}\n"
            f"Пользователь: {order['telegram_id']}, логин: {order['steam_login']}\n"
            f"Зачисление: {rub(order['requested_credit_rub'])}, оплата: {rub(order['amount_pay_rub'])}{hint}"
        )


# ---------- Telegram: пользовательские команды ----------

@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    login = await db.get_user_login(message.from_user.id)
    if login:
        await state.clear()
        await message.answer(
            f"С возвращением! Ваш логин Steam: {login}\n\n"
            "Пришлите сумму в рублях, которая должна прийти на баланс Steam (например: 500), "
            "и я пришлю ссылку на оплату по СБП.\n\nСменить логин: /login · Помощь: /help"
        )
        return
    await state.set_state(Form.waiting_login)
    await message.answer(
        "Привет! Я пополняю баланс Steam через СБП.\n\n"
        "Пришлите ваш логин Steam — имя, под которым вы входите в клиент Steam "
        "(не ник в профиле). Пароль не нужен."
    )


@router.message(Command("login"))
async def cmd_change_login(message: Message, state: FSMContext) -> None:
    await state.set_state(Form.waiting_login)
    await message.answer("Пришлите логин Steam (имя для входа, не ник).")


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(HELP_TEXT)


@router.message(Command("support"))
async def cmd_support(message: Message) -> None:
    if SUPPORT_CONTACT:
        await message.answer(f"Напишите в поддержку: {SUPPORT_CONTACT}\nЕсли вопрос по заказу — укажите его номер.")
    else:
        await message.answer("Поддержка скоро появится. Пока можно написать администратору канала.")


@router.message(Command("id"))
async def cmd_id(message: Message) -> None:
    await message.answer(f"Ваш Telegram ID: {message.from_user.id}")


@router.message(Form.waiting_login, F.text)
async def process_login(message: Message, state: FSMContext) -> None:
    login = (message.text or "").strip()
    if login.startswith("/"):
        await message.answer("Сначала пришлите логин Steam (или /start, чтобы начать заново).")
        return
    if not STEAM_LOGIN_RE.match(login):
        await message.answer(
            "Это не похоже на логин Steam: допустимы латинские буквы, цифры и «_», "
            "от 2 до 64 символов. Попробуйте ещё раз."
        )
        return
    await db.set_user_login(message.from_user.id, login)
    await state.clear()
    await message.answer(
        f"Логин сохранён: {login}\n\n"
        "Теперь пришлите сумму в рублях, например 500 — и я пришлю оплату по СБП."
    )


@router.message(F.text.regexp(AMOUNT_RE))
async def process_amount(message: Message) -> None:
    login = await db.get_user_login(message.from_user.id)
    if not login:
        await message.answer("Сначала пришлите логин Steam — нажмите /start.")
        return

    credit = round(float(message.text.strip().replace(",", ".")), 2)
    if credit <= 0:
        await message.answer("Сумма должна быть больше нуля.")
        return

    processing_msg = await message.answer("Считаю сумму к оплате…")
    try:
        service = await payli.get_steam_service()
        min_rub, max_rub = float(service["min_rub"]), float(service["max_rub"])
        if not (min_rub <= credit <= max_rub):
            await processing_msg.edit_text(
                f"Пополнить можно на сумму от {rub(min_rub)} до {rub(max_rub)}. Пришлите другую сумму."
            )
            return
        base_commission = float(service["base_commission_percent"])
        amount_pay = calc_amount_pay(credit, base_commission, MARKUP_PERCENT)
        resp = await payli.create_steam_order(
            account=login,
            amount_pay_rub=amount_pay,
            partner_commission_percent=MARKUP_PERCENT,
            idempotency_key=str(uuid.uuid4()),
            webhook_url=WEBHOOK_PUBLIC_URL,
            webhook_secret=PAYLI_WEBHOOK_SECRET,
            redirect_url=REDIRECT_URL,
        )
    except PayliError as e:
        log.warning("Не удалось создать заказ для %s: %s", message.from_user.id, e)
        await processing_msg.edit_text(error_text(e, login))
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
                telegram_id=message.from_user.id,
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
        await processing_msg.edit_text(f"Не удалось получить ссылку на оплату. Попробуйте ещё раз или напишите в {SUPPORT_TEXT}.")
        return

    keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Оплатить по СБП", url=pay_url)]])
    await processing_msg.delete()
    await message.answer_photo(
        photo=BufferedInputFile(make_qr_png(pay_url), filename="payment_qr.png"),
        caption=(
            f"Логин Steam: {login}\n"
            f"Зачислится: {rub(credit)}\n"
            f"К оплате: {rub(amount_pay)} — все комиссии включены\n\n"
            "Нажмите «Оплатить по СБП» или отсканируйте QR с телефона. "
            "Ссылка действует 30 минут, о зачислении я сообщу.\n"
            f"Заказ: {public_id[:8]}"
        ),
        reply_markup=keyboard,
    )


# ---------- Telegram: команды администратора ----------

admin_router = Router()
admin_router.message.filter(F.from_user.id.in_(set(ADMIN_IDS)))
dp.include_router(admin_router)


@admin_router.message(Command("balance"))
async def cmd_balance(message: Message) -> None:
    try:
        data = await payli.get_balance(limit=10)
    except PayliError as e:
        await message.answer(f"Ошибка: {e.code} {e.message}")
        return
    lines = [f"Баланс партнёра: {rub(float(data.get('balance_rub', 0)))}", "", "Последние операции:"]
    for tx in data.get("transactions", [])[:10]:
        lines.append(f"{tx.get('created_at', '')[:16]}  {tx.get('tx_type')}  {tx.get('amount_rub')} ₽")
    await message.answer("\n".join(lines))


@admin_router.message(Command("order"))
async def cmd_order(message: Message, command: CommandObject) -> None:
    public_id = (command.args or "").strip()
    if not public_id:
        await message.answer("Использование: /order <public_id>")
        return
    try:
        data = await payli.get_order(public_id)
    except PayliError as e:
        await message.answer(f"Ошибка: {e.code} {e.message}")
        return
    order = data.get("order", {})
    if order.get("status"):
        await handle_status(public_id, order["status"], "admin")
    await message.answer(json.dumps(order, ensure_ascii=False, indent=1)[:4000])


@admin_router.message(Command("refund"))
async def cmd_refund(message: Message, command: CommandObject) -> None:
    public_id = (command.args or "").strip()
    if not public_id:
        await message.answer("Использование: /refund <public_id> — только для заказов в статусе failed (СБП).")
        return
    try:
        data = await payli.refund_order(public_id)
    except PayliError as e:
        await message.answer(f"Возврат не выполнен: {e.code} {e.message}")
        return
    await db.log_event(public_id, "refund", data)
    fee = data.get("fee") or {}
    await message.answer(f"Возврат выполнен: {data.get('order', {}).get('status')}. Комиссия: {fee.get('rub', 0)} ₽")
    status = data.get("order", {}).get("status")
    if status:
        await handle_status(public_id, status, "refund")


# ---------- прочие сообщения (регистрируется последним) ----------

fallback_router = Router()
dp.include_router(fallback_router)


@fallback_router.message()
async def fallback(message: Message) -> None:
    await message.answer("Пришлите сумму в рублях для пополнения Steam (например: 500) или /help.")


# ---------- приём вебхуков Payli ----------

def verify_signature(raw_body: bytes, signature_header: str) -> bool:
    if not PAYLI_WEBHOOK_SECRET:
        return True  # секрет не задан — Payli не подписывает вебхуки
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
        # order.message — только для ручных заказов (type: manual), здесь не используется.
    except Exception:
        log.exception("Ошибка обработки вебхука %s", payload)


async def payli_webhook_handler(request: web.Request) -> web.Response:
    raw_body = await request.read()
    # Подпись проверяем до разбора тела.
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

    # Payli ждёт 2xx в течение 8 секунд — обрабатываем в фоне и отвечаем сразу.
    spawn(process_webhook(payload))
    return web.json_response({"ok": True})


async def health_handler(request: web.Request) -> web.Response:
    return web.json_response({"ok": True})


def build_webhook_app() -> web.Application:
    app = web.Application(client_max_size=1024 * 1024)
    app.router.add_post(WEBHOOK_PATH, payli_webhook_handler)
    app.router.add_get(WEBHOOK_PATH, health_handler)
    return app


# ---------- резервная сверка статусов ----------

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
    if not PAYLI_WEBHOOK_SECRET:
        log.warning("PAYLI_WEBHOOK_SECRET не задан — вебхуки принимаются без проверки подписи!")
    if not ADMIN_IDS:
        log.warning("ADMIN_IDS не задан — алерты о проблемных заказах никому не уйдут.")

    await db.init_db()
    await payli.start()

    runner = web.AppRunner(build_webhook_app())
    await runner.setup()
    await web.TCPSite(runner, WEBHOOK_LISTEN_HOST, WEBHOOK_LISTEN_PORT).start()
    log.info("Webhook-сервер слушает на %s:%s%s", WEBHOOK_LISTEN_HOST, WEBHOOK_LISTEN_PORT, WEBHOOK_PATH)

    try:
        await bot.set_my_commands([
            BotCommand(command="start", description="Начать / пополнить баланс"),
            BotCommand(command="login", description="Изменить логин Steam"),
            BotCommand(command="help", description="Как пользоваться"),
            BotCommand(command="support", description="Связаться с поддержкой"),
        ])
    except Exception:
        log.exception("Не удалось установить меню команд")

    poller = asyncio.create_task(poll_unfinished_orders())
    try:
        await dp.start_polling(bot)
    finally:
        poller.cancel()
        await runner.cleanup()
        await payli.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
