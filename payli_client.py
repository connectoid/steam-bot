"""
Обёртка над Payli Partner API v1 (https://payli.ru/docs).
Используемые методы:
  GET  /api/partner/v1/services               — базовая комиссия и границы суммы Steam
  POST /api/partner/v1/orders                 — создать заказ (type: steam)
  GET  /api/partner/v1/orders/{public_id}     — статус заказа
  POST /api/partner/v1/orders/{public_id}/refund — возврат СБП-оплаты по заказу в статусе failed
  GET  /api/partner/v1/balance                — баланс партнёра и операции
"""
import asyncio
import logging
from typing import Optional

import aiohttp

from config import PAYLI_API_TOKEN, PAYLI_BASE_URL

log = logging.getLogger("payli")

API = "/api/partner/v1"

# Ошибки, при которых документация разрешает повтор (для POST /orders — с тем же idempotency_key).
_RETRYABLE_CODES = {
    "provider_bad_response",
    "payment_url_missing",
    "payment_init_recovery_pending",
    "request_in_progress",
    "rate_limited",
    "too_many_concurrent_requests",
    "server_busy",
    "server_error",
    "idempotency_unavailable",
    "commission_unavailable",
    "fx_rate_unavailable",
    "network_error",
}


class PayliError(RuntimeError):
    def __init__(self, status: int, code: str, message: str = "", retry_after: Optional[float] = None):
        self.status = status
        self.code = code
        self.message = message
        self.retry_after = retry_after
        super().__init__(f"Payli API error {status} {code}: {message}")

    @property
    def retryable(self) -> bool:
        return self.code in _RETRYABLE_CODES or (self.code.startswith("http_") and self.status >= 500)


def _parse_retry_after(value: Optional[str]) -> Optional[float]:
    try:
        return float(value) if value else None
    except ValueError:
        return None


class PayliClient:
    def __init__(self, token: str = PAYLI_API_TOKEN, base_url: str = PAYLI_BASE_URL, max_attempts: int = 3):
        self._token = token
        self._base_url = base_url
        self._max_attempts = max_attempts
        self._session: Optional[aiohttp.ClientSession] = None

    async def start(self) -> None:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                base_url=self._base_url,
                headers={"Authorization": f"Bearer {self._token}", "Content-Type": "application/json"},
                timeout=aiohttp.ClientTimeout(total=15),
            )

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def __aenter__(self) -> "PayliClient":
        await self.start()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    async def _request_once(self, method: str, path: str, **kwargs) -> dict:
        await self.start()
        try:
            async with self._session.request(method, path, **kwargs) as resp:
                try:
                    data = await resp.json(content_type=None)
                except (aiohttp.ContentTypeError, ValueError):
                    data = None
                if resp.status >= 400 or not isinstance(data, dict):
                    if isinstance(data, dict):
                        code = data.get("error") or f"http_{resp.status}"
                        message = data.get("message", "")
                    else:
                        code, message = f"http_{resp.status}", "non-JSON response"
                    raise PayliError(resp.status, code, message, _parse_retry_after(resp.headers.get("Retry-After")))
                return data
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            raise PayliError(0, "network_error", repr(e)) from e

    async def _request(self, method: str, path: str, *, retry: bool = True, **kwargs) -> dict:
        attempt = 0
        while True:
            attempt += 1
            try:
                return await self._request_once(method, path, **kwargs)
            except PayliError as e:
                if not retry or not e.retryable or attempt >= self._max_attempts:
                    raise
                delay = min(e.retry_after or 2 * attempt, 10)
                log.warning("Payli %s %s: %s, повтор %d через %.0f с", method, path, e.code, attempt, delay)
                await asyncio.sleep(delay)

    # ---------- каталог ----------

    async def get_steam_service(self) -> dict:
        """{"code": "steam", "available": true, "min_rub": ..., "max_rub": ..., "base_commission_percent": ...}"""
        data = await self._request("GET", f"{API}/services")
        for service in data.get("services", []):
            if service.get("code") == "steam":
                if not service.get("available", True):
                    raise PayliError(503, "service_unavailable", "Steam временно недоступен")
                return service
        raise PayliError(0, "steam_service_not_found", "Steam отсутствует в каталоге услуг")

    # ---------- заказы ----------

    async def create_steam_order(
        self,
        *,
        account: str,
        amount_pay_rub: float,
        partner_commission_percent: float,
        idempotency_key: str,
        webhook_url: str = "",
        webhook_secret: str = "",
        redirect_url: str = "",
    ) -> dict:
        # Неизвестные поля в POST /orders запрещены (400 invalid_json) — шлём только документированные.
        payload = {
            "type": "steam",
            "account": account,
            "amount_pay_rub": round(amount_pay_rub, 2),
            "partner_commission_percent": partner_commission_percent,
            "payment_method": "sbp",
            "idempotency_key": idempotency_key,
        }
        if webhook_url:
            payload["webhook_url"] = webhook_url
        if webhook_secret:
            payload["webhook_secret"] = webhook_secret
        if redirect_url:
            payload["redirect_url"] = redirect_url
        # Повторы безопасны: тот же idempotency_key вернёт уже созданный заказ.
        return await self._request("POST", f"{API}/orders", json=payload)

    async def get_order(self, public_id: str) -> dict:
        return await self._request("GET", f"{API}/orders/{public_id}")

    async def refund_order(self, public_id: str, reason: str = "manual_refund") -> dict:
        # Возврат не идемпотентен (параллельный повтор → 409 refund_already_attempted) — без автоповторов.
        return await self._request("POST", f"{API}/orders/{public_id}/refund", retry=False, json={"reason": reason})

    async def get_balance(self, limit: int = 10) -> dict:
        return await self._request("GET", f"{API}/balance", params={"limit": limit})
