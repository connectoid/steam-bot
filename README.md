# Steam Charger (@steam_charger_bot)

Telegram-бот пополнения Steam по логину через Payli (оплата по СБП).
Пользователь один раз вводит логин Steam, дальше присылает сумму — бот создаёт заказ
в Payli, присылает ссылку и QR для оплаты и сообщает о зачислении.

## Структура

- `config.py` — настройки из `.env` (шаблон — `.env.example`).
- `db.py` — SQLite: логины, заказы (с суммами комиссий), журнал событий Payli,
  дедупликация вебхуков. Схема мигрирует сама при запуске.
- `payli_client.py` — клиент Payli Partner API v1 с повторами по правилам документации.
- `bot.py` — хендлеры Telegram (aiogram 3), приём вебхуков (`/payli/webhook`),
  резервная сверка статусов.
- `deploy/steam-bot.service` — служба systemd; `deploy/nginx-payli-webhook.conf` — location для nginx.

## Как считается сумма к оплате

По документации Payli комиссия удерживается **из** платежа:

```
credited_rub = amount_pay_rub × (1 − (base_commission_percent + MARKUP_PERCENT) / 100)
```

Бот решает обратную задачу: по желаемой сумме зачисления считает `amount_pay_rub`
(округление вверх до копейки, чтобы на Steam пришло не меньше запрошенного).
`base_commission_percent` берётся из `GET /services` перед каждым заказом.

Пример: база 4%, наценка 2%, на Steam 1000 ₽ → к оплате 1063,83 ₽.

## Как вы зарабатываете

- Ваша наценка: `partner_commission_rub = amount_pay_rub × MARKUP_PERCENT / 100`.
- На баланс партнёра Payli приходит `partner_fee_rub = partner_commission_rub × partner_commission_share_percent / 100`
  (доля зависит от вашего аккаунта, видна в ответе на заказ).
- Оба значения сохраняются в таблице `orders`, сырые ответы — в `order_events`.
- Баланс и операции — команда `/balance` (для админов) или кабинет Payli; вывод — через кабинет.

Оплата заказа по ссылке **не** списывает деньги с партнёрского баланса. Баланс нужен для
комиссии при возврате (`/refund`, по умолчанию 3% от суммы платежа).

## Статусы и уведомления

- Вебхук `order.status` → пользователь получает сообщение (оплата получена, зачислено,
  ошибка, отменён и т.д.). Ответ на вебхук — сразу, обработка в фоне (Payli ждёт 2xx ≤ 8 с).
- Подпись `X-Payli-Signature` проверяется до разбора тела; повторы отсекаются по `X-Payli-Event-Id`.
- Вебхуки могут прийти не по порядку — «откат» статуса игнорируется, каждое уведомление уходит один раз.
- Резерв: раз в `POLL_INTERVAL_SEC` бот сверяет незавершённые заказы (до 3 суток) через `GET /orders/{id}`.
- Админам (`ADMIN_IDS`) приходят алерты по `failed`, `chargeback`, `creation_failed`, `refunded`.

## Команды

Пользователь: `/start`, `/login`, `/help`, `/support`, `/id` (меню команд бот ставит сам).

Админ: `/balance`, `/order <public_id>`, `/refund <public_id>`.
`failed` по СБП деньги автоматически **не** возвращает — верните `/refund` (нужно право токена
`orders_refund` и деньги на балансе на комиссию) или завершите вопрос через поддержку Payli.

## Деплой на VPS

```bash
cd /root/dev/bots/steam-bot
git pull
python3 -m venv venv && venv/bin/pip install -r requirements.txt
cp .env.example .env   # первый раз; заполнить значения
```

1. nginx: добавить location из `deploy/nginx-payli-webhook.conf` в server-блок домена,
   `sudo nginx -t && sudo systemctl reload nginx`.
   Проверка: `curl https://beley-n8n.duckdns.org/payli/webhook` → `{"ok": true}`.
2. systemd:
   ```bash
   sudo cp deploy/steam-bot.service /etc/systemd/system/
   sudo systemctl daemon-reload && sudo systemctl enable --now steam-bot
   sudo journalctl -u steam-bot -f
   ```
3. После обновления кода: `git pull && sudo systemctl restart steam-bot`.

Требования Payli к `webhook_url`: только https, порт 443 или 8443, публичный хост.

## Проверка перед запуском

1. `/start` → логин → сумма; заказ создаётся, приходит QR и кнопка оплаты.
2. Оплатить заказ на минимальную сумму, дождаться «Готово!».
3. В `journalctl` видны строки `Заказ … pending_payment -> payment_received (webhook)` и `-> completed`.
4. Неверный логин → Payli вернёт `steam_login_invalid`, бот предложит `/login`.
5. `/balance` показывает начисление наценки.

## Что можно добавить позже

- Историю заказов пользователя (`/orders`).
- Оплату картой/криптой (`payment_method: card | crypto`) — в API уже есть.
- Другие товары каталога Payli (`topup`, `voucher`, `esim`).
