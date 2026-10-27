"""
Минимальный асинхронный клиент MAX Bot API (https://dev.max.ru/docs-api).
Официальной Python-библиотеки нет, поэтому здесь только то, что нужно боту:
  GET    /me                 — проверка токена
  PATCH  /me                 — меню команд
  GET    /updates            — long polling
  POST   /messages?user_id=  — отправка сообщения
  PUT    /messages?message_id= — редактирование
  DELETE /messages?message_id= — удаление
  POST   /answers?callback_id= — ответ на нажатие кнопки
  POST   /uploads?type=image — загрузка картинки (QR)

Токен передаётся в заголовке «Authorization: <token>» (без Bearer).
Домен API — platform-api2.max.ru; для него в системе должен быть доверенным
корневой сертификат Минцифры (см. README) либо путь к нему в MAX_CA_FILE.
"""
import asyncio
import logging
import ssl
from typing import Any, Optional

import aiohttp

log = logging.getLogger("max_api")


class MaxError(RuntimeError):
    def __init__(self, status: int, code: str, message: str = ""):
        self.status = status
        self.code = code
        self.message = message
        super().__init__(f"MAX API error {status} {code}: {message}")

    @property
    def retryable(self) -> bool:
        return self.status in (0, 429) or self.status >= 500


# ---------- клавиатуры ----------

def keyboard(rows: list[list[dict]]) -> dict:
    return {"type": "inline_keyboard", "payload": {"buttons": rows}}


def callback_button(text: str, payload: str) -> dict:
    return {"type": "callback", "text": text, "payload": payload}


def link_button(text: str, url: str) -> dict:
    return {"type": "link", "text": text, "url": url}


# ---------- клиент ----------

class MaxApi:
    def __init__(self, token: str, base_url: str = "https://platform-api2.max.ru", ca_file: str = ""):
        self._token = token
        self._base_url = base_url.rstrip("/")
        self._ca_file = ca_file
        self._session: Optional[aiohttp.ClientSession] = None
        self._upload_session: Optional[aiohttp.ClientSession] = None

    def _ssl_context(self) -> ssl.SSLContext:
        ctx = ssl.create_default_context()  # системные корневые сертификаты
        if self._ca_file:
            ctx.load_verify_locations(self._ca_file)
        return ctx

    async def start(self) -> None:
        if self._session is None or self._session.closed:
            ctx = self._ssl_context()
            self._session = aiohttp.ClientSession(
                base_url=self._base_url,
                headers={"Authorization": self._token},
                connector=aiohttp.TCPConnector(ssl=ctx),
            )
            # Загрузка файлов идёт на отдельный хост — без токена в заголовках.
            self._upload_session = aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=ctx))

    async def close(self) -> None:
        for s in (self._session, self._upload_session):
            if s and not s.closed:
                await s.close()

    async def _request_once(self, method: str, path: str, *, timeout: float = 20, **kwargs) -> dict:
        await self.start()
        try:
            async with self._session.request(
                method, path, timeout=aiohttp.ClientTimeout(total=timeout), **kwargs
            ) as resp:
                try:
                    data = await resp.json(content_type=None)
                except (aiohttp.ContentTypeError, ValueError):
                    data = None
                if resp.status >= 400:
                    if isinstance(data, dict):
                        raise MaxError(resp.status, str(data.get("code") or f"http_{resp.status}"),
                                       str(data.get("message", "")))
                    raise MaxError(resp.status, f"http_{resp.status}", "non-JSON response")
                return data if isinstance(data, dict) else {}
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            raise MaxError(0, "network_error", repr(e)) from e

    async def _request(self, method: str, path: str, *, retry: bool = True, **kwargs) -> dict:
        attempt = 0
        while True:
            attempt += 1
            try:
                return await self._request_once(method, path, **kwargs)
            except MaxError as e:
                if not retry or not e.retryable or attempt >= 3:
                    raise
                delay = 2 * attempt
                log.warning("MAX %s %s: %s, повтор %d через %d с", method, path, e.code, attempt, delay)
                await asyncio.sleep(delay)

    # ---------- бот ----------

    async def get_me(self) -> dict:
        return await self._request("GET", "/me")

    async def set_commands(self, commands: list[tuple[str, str]]) -> dict:
        body = {"commands": [{"name": n, "description": d} for n, d in commands]}
        return await self._request("PATCH", "/me", json=body)

    async def get_updates(self, marker: Optional[int], timeout: int = 30, types: Optional[list[str]] = None) -> dict:
        params: dict[str, Any] = {"timeout": timeout, "limit": 100}
        if marker is not None:
            params["marker"] = marker
        if types:
            params["types"] = ",".join(types)
        # Сетевой таймаут больше серверного, иначе long polling будет обрываться сам.
        return await self._request("GET", "/updates", retry=False, params=params, timeout=timeout + 15)

    # ---------- сообщения ----------

    async def send_message(self, user_id: int, text: str, attachments: Optional[list[dict]] = None) -> dict:
        body: dict[str, Any] = {"text": text}
        if attachments:
            body["attachments"] = attachments
        # Сразу после загрузки картинка может быть ещё не готова (attachment.not.ready) — повторяем.
        for attempt in range(5):
            try:
                data = await self._request("POST", "/messages", params={"user_id": user_id}, json=body)
                return data.get("message", data)
            except MaxError as e:
                if "not.ready" in e.code or "not.ready" in e.message:
                    await asyncio.sleep(1 + attempt)
                    continue
                raise
        raise MaxError(400, "attachment.not.ready", "вложение так и не обработалось")

    async def edit_message(self, message_id: str, text: str, attachments: Optional[list[dict]] = None) -> dict:
        # attachments=[] удаляет все вложения, включая клавиатуру.
        body: dict[str, Any] = {"text": text, "attachments": attachments if attachments is not None else []}
        return await self._request("PUT", "/messages", params={"message_id": message_id}, json=body)

    async def delete_message(self, message_id: str) -> dict:
        return await self._request("DELETE", "/messages", params={"message_id": message_id})

    async def answer_callback(self, callback_id: str, notification: str = "") -> dict:
        body: dict[str, Any] = {}
        if notification:
            body["notification"] = notification
        return await self._request("POST", "/answers", params={"callback_id": callback_id}, json=body, retry=False)

    async def upload_image(self, data: bytes, filename: str = "image.png") -> dict:
        """Загружает картинку и возвращает готовое вложение {"type": "image", "payload": {...}}."""
        endpoint = await self._request("POST", "/uploads", params={"type": "image"})
        url = endpoint.get("url")
        if not url:
            raise MaxError(0, "upload_url_missing", str(endpoint))
        form = aiohttp.FormData()
        form.add_field("data", data, filename=filename, content_type="image/png")
        try:
            async with self._upload_session.post(url, data=form, timeout=aiohttp.ClientTimeout(total=60)) as resp:
                try:
                    result = await resp.json(content_type=None)
                except (aiohttp.ContentTypeError, ValueError):
                    result = None
                if resp.status >= 400:
                    raise MaxError(resp.status, f"upload_http_{resp.status}", str(result)[:200])
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            raise MaxError(0, "upload_network_error", repr(e)) from e

        # Для картинок сервер загрузки обычно отвечает {"photos": {...}}; на всякий случай поддерживаем и token.
        if isinstance(result, dict) and result.get("photos"):
            payload = {"photos": result["photos"]}
        elif isinstance(result, dict) and result.get("token"):
            payload = {"token": result["token"]}
        elif endpoint.get("token"):
            payload = {"token": endpoint["token"]}
        else:
            raise MaxError(0, "upload_token_missing", str(result)[:200])
        return {"type": "image", "payload": payload}
