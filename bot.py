"""
Telegram-бот пополнения Steam по логину через Payli (оплата СБП).

Сценарий:
  1. /start -> если логин Steam не сохранён, бот просит его прислать.
  2. Пользователь в любой момент присылает число (сумму, которая должна
     зачислиться на Steam) -> бот считает итоговую сумму к оплате с учётом
     базовой комиссии Payli + вашей наценки, создаёт заказ и присылает
     ссылку на оплату по СБП + QR-код для открытия ссылки с телефона.
  3. Payli присылает вебхук о смене статуса заказа -> бот уведомляет
     пользователя о результате.

Запуск: python bot.py
(long polling для Telegram + отдельный aiohttp-сервер для приёма вебхуков Payli)
"""
import asyncio
import hashlib
import hmac
import io
import logging
import uuid

import qrcode
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BufferedInputFile, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiohttp import web

import db
from config import (
    BOT_TOKEN,
    MARKUP_PERCENT,
    PAYLI_WEBHOOK_SECRET,
    WEBHOOK_LISTEN_HOST,
    WEBHOOK_LISTEN_PORT,
    WEBHOOK_PUBLIC_URL,
)
from payli_client import PayliClient, PayliError

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("steam_topup_bot")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
router = Router()
dp.include_router(router)

# Простая валидация логина Steam: буквы, цифры, подчёркивание/дефис, 2-32 символа.
STEAM_LOGIN_RE = __import__("re").compile(r"^[A-Za-z0-9_\-]{2,32}$")


class Form(StatesGroup):
    waiting_login = State()


# ---------- Telegram-хендлеры ----------

@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    login = await db.get_user_login(message.from_user.id)
    if login:
        await message.answer(
            f"С возвращением! Ваш привязанный Steam-логин: {login}\n\n"
            "Пришлите сумму в рублях, которая должна зачислиться на баланс Steam "
            "(например: 500) — я пришлю ссылку и QR для оплаты по СБП."
        )
        return
    await state.set_state(Form.waiting_login)
    await message.answer(
        "Привет! Чтобы пополнять Steam, сначала пришлите ваш логин Steam "
        "(Account Name — тот же, под которым вы заходите в клиент Steam)."
    )


@router.message(Command("login"))
async def cmd_change_login(message: Message, state: FSMContext) -> None:
    await state.set_state(Form.waiting_login)
    await message.answer("Пришлите новый логин Steam.")


@router.message(Form.waiting_login)
async def process_login(message: Message, state: FSMContext) -> None:
    login = (message.text or "").strip()
    if not STEAM_LOGIN_RE.match(login):
        await message.answer(
            "Это не похоже на логин Steam. Разрешены буквы, цифры, «_» и «-», "
            "от 2 до 32 символов. Попробуйте ещё раз."
        )
        return
    await db.set_user_login(message.from_user.id, login)
    await state.clear()
    await message.answer(
        f"Логин сохранён: {login}\n\n"
        "Теперь просто пришлите сумму в рублях — например 500 — и я пришлю оплату по СБП."
    )


@router.message(F.text.regexp(r"^\d+([.,]\d{1,2})?$"))
async def process_amount(message: Message, state: FSMContext) -> None:
    # На всякий случай: если мы ещё ждём логин, цифры не трактуем как сумму.
    if await state.get_state() == Form.waiting_login.state:
        return

    login = await db.get_user_login(message.from_user.id)
    if not login:
        await message.answer("Сначала пришлите логин Steam командой /start.")
        return

    requested_credit = float((message.text or "0").replace(",", "."))
    if requested_credit <= 0:
        await message.answer("Сумма должна быть больше нуля.")
        return

    processing_msg = await message.answer("Считаю сумму к оплате…")

    try:
        async with PayliClient() as client:
            service = await client.get_steam_service_info()
            min_rub = float(service["min_rub"])
            max_rub = float(service["max_rub"])

            if not (min_rub <= requested_credit <= max_rub):
                await processing_msg.edit_text(
                    f"Сумма зачисления должна быть от {min_rub:.0f} до {max_rub:.0f} ₽. "
                    "Пришлите другую сумму."
                )
                return

            base_commission = float(service["base_commission_percent"])
            total_commission = base_commission + MARKUP_PERCENT

            # credited = amount_pay * (1 - total_commission/100)  =>  amount_pay = credited / (1 - total/100)
            amount_pay_rub = round(requested_credit / (1 - total_commission / 100), 2)

            idempotency_key = str(uuid.uuid4())
            order = await client.create_steam_order(
                account=login,
                amount_pay_rub=amount_pay_rub,
                partner_commission_percent=MARKUP_PERCENT,
                webhook_url=WEBHOOK_PUBLIC_URL,
                webhook_secret=PAYLI_WEBHOOK_SECRET,
                idempotency_key=idempotency_key,
            )
    except PayliError as e:
        log.exception("Payli create order failed")
        await processing_msg.edit_text(
            "Не получилось создать заказ. "
            f"Причина: {e.code}. Попробуйте ещё раз чуть позже."
        )
        return

    public_id = order["order"]["public_id"]
    pay_url = order["payment"]["pay_url"]

    await db.create_order(
        db.OrderRecord(
            public_id=public_id,
            telegram_id=message.from_user.id,
            steam_login=login,
            requested_credit_rub=requested_credit,
            amount_pay_rub=amount_pay_rub,
            status=order["order"]["status"],
        )
    )

    qr_bytes = _make_qr_png(pay_url)
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Оплатить по СБП", url=pay_url)]]
    )

    await processing_msg.delete()
    await message.answer_photo(
        photo=BufferedInputFile(qr_bytes, filename="payment_qr.png"),
        caption=(
            f"Зачислится на Steam: {requested_credit:.0f} ₽\n"
            f"К оплате: {amount_pay_rub:.2f} ₽ (включая все комиссии)\n\n"
            "Нажмите кнопку ниже или отсканируйте QR, чтобы открыть страницу оплаты."
        ),
        reply_markup=keyboard,
    )


@router.message()
async def fallback(message: Message) -> None:
    await message.answer(
        "Не понял. Пришлите /start, чтобы начать, или сумму в рублях для пополнения Steam."
    )


def _make_qr_png(data: str) -> bytes:
    img = qrcode.make(data)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


# ---------- приём вебхуков Payli ----------

# Статусы, при которых заказ окончательно решён и стоит уведомить пользователя.
_FINAL_STATUSES = {
    "completed": "✅ Зачислено {amount:.0f} ₽ на ваш Steam-аккаунт.",
    "failed": "❌ Пополнение не удалось. Деньги будут возвращены, либо обратитесь в поддержку.",
    "canceled": "⚠️ Заказ отменён (не оплачен вовремя).",
    "refunded": "↩️ Платёж возвращён.",
    "chargeback": "⚠️ По платежу был chargeback — уточните детали у администратора.",
    "creation_failed": "❌ Не удалось создать оплату. Попробуйте ещё раз.",
}


def _verify_signature(raw_body: bytes, signature_header: str) -> bool:
    if not PAYLI_WEBHOOK_SECRET:
        # Секрет не задан — подписи не будет и в заголовке. Пропускаем проверку осознанно.
        return True
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = hmac.new(
        PAYLI_WEBHOOK_SECRET.encode(), raw_body, hashlib.sha256
    ).hexdigest()
    got = signature_header.removeprefix("sha256=")
    return hmac.compare_digest(expected, got)


async def payli_webhook_handler(request: web.Request) -> web.Response:
    raw_body = await request.read()
    signature = request.headers.get("X-Payli-Signature", "")
    if not _verify_signature(raw_body, signature):
        log.warning("Payli webhook: неверная подпись")
        return web.json_response({"ok": False}, status=401)

    payload = await request.json()
    event_id = request.headers.get("X-Payli-Event-Id", "")

    # Дедупликация повторных доставок.
    if event_id and not await db.mark_event_processed(event_id):
        return web.json_response({"ok": True, "duplicate": True})

    if payload.get("event") != "order.status":
        # order.message нас пока не интересует (используется только для type: manual).
        return web.json_response({"ok": True})

    public_id = payload.get("order_public_id") or payload.get("public_id")
    status = payload.get("status")
    if not public_id or not status:
        return web.json_response({"ok": True})

    telegram_id = await db.update_order_status(public_id, status)
    if telegram_id and status in _FINAL_STATUSES:
        order = await db.get_order(public_id)
        amount = order["requested_credit_rub"] if order else 0.0
        text = _FINAL_STATUSES[status].format(amount=amount)
        try:
            await bot.send_message(telegram_id, text)
        except Exception:
            log.exception("Не удалось отправить уведомление пользователю %s", telegram_id)

    return web.json_response({"ok": True})


def build_webhook_app() -> web.Application:
    app = web.Application()
    app.router.add_post("/payli/webhook", payli_webhook_handler)
    return app


# ---------- запуск ----------

async def main() -> None:
    await db.init_db()

    webhook_app = build_webhook_app()
    runner = web.AppRunner(webhook_app)
    await runner.setup()
    site = web.TCPSite(runner, WEBHOOK_LISTEN_HOST, WEBHOOK_LISTEN_PORT)
    await site.start()
    log.info("Webhook-сервер слушает на %s:%s", WEBHOOK_LISTEN_HOST, WEBHOOK_LISTEN_PORT)

    try:
        await dp.start_polling(bot)
    finally:
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
