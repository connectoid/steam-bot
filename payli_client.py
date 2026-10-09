"""
Тонкая обёртка над Payli Partner API (https://payli.ru/docs).
Используются только методы, нужные для сценария "Steam по логину":
  GET  /api/partner/v1/services        — узнать текущую базовую комиссию и границы суммы
  POST /api/partner/v1/orders          — создать заказ (type: steam)
  GET  /api/partner/v1/orders/{id}     — получить статус заказа
"""
import aiohttp

from config import PAYLI_API_TOKEN, PAYLI_BASE_URL


class PayliError(RuntimeError):
    def __init__(self, status: int, code: str, message: str = ""):
        self.status = status
        self.code = code
        self.message = message
        super().__init__(f"Payli API error {status} {code}: {message}")


class PayliClient:
    def __init__(self) -> None:
        self._session: aiohttp.ClientSession | None = None

    async def __aenter__(self) -> "PayliClient":
        self._session = aiohttp.ClientSession(
            base_url=PAYLI_BASE_URL,
            headers={
                "Authorization": f"Bearer {PAYLI_API_TOKEN}",
                "Content-Type": "application/json",
            },
            timeout=aiohttp.ClientTimeout(total=20),
        )
        return self

    async def __aexit__(self, *exc) -> None:
        if self._session:
            await self._session.close()

    async def _request(self, method: str, path: str, **kwargs) -> dict:
        assert self._session is not None, "используйте PayliClient как async context manager"
        async with self._session.request(method, path, **kwargs) as resp:
            data = await resp.json()
            if resp.status >= 400:
                raise PayliError(
                    resp.status,
                    data.get("error", "unknown_error"),
                    data.get("message", ""),
                )
            return data

    async def get_steam_service_info(self) -> dict:
        """Возвращает {"min_rub":..., "max_rub":..., "base_commission_percent":...} для Steam."""
        data = await self._request("GET", "/api/partner/v1/services")
        for service in data.get("services", []):
            if service.get("code") == "steam":
                return service
        raise PayliError(0, "steam_service_not_found", "Steam отсутствует в каталоге услуг")

    async def create_steam_order(
        self,
        *,
        account: str,
        amount_pay_rub: float,
        partner_commission_percent: float,
        webhook_url: str,
        webhook_secret: str,
        idempotency_key: str,
    ) -> dict:
        payload = {
            "type": "steam",
            "account": account,
            "amount_pay_rub": round(amount_pay_rub, 2),
            "partner_commission_percent": partner_commission_percent,
            "webhook_url": webhook_url,
            "idempotency_key": idempotency_key,
        }
        if webhook_secret:
            payload["webhook_secret"] = webhook_secret
        return await self._request("POST", "/api/partner/v1/orders", json=payload)

    async def get_order(self, public_id: str) -> dict:
        return await self._request("GET", f"/api/partner/v1/orders/{public_id}")
