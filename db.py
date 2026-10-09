"""
Простое хранилище на SQLite. Для MVP достаточно; при росте нагрузки
можно заменить на Postgres без изменения интерфейса функций ниже.
"""
import asyncio
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Optional

from config import DB_PATH

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    telegram_id INTEGER PRIMARY KEY,
    steam_login TEXT NOT NULL,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS orders (
    public_id TEXT PRIMARY KEY,
    telegram_id INTEGER NOT NULL,
    steam_login TEXT NOT NULL,
    requested_credit_rub REAL NOT NULL,
    amount_pay_rub REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending_payment',
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS processed_webhook_events (
    event_id TEXT PRIMARY KEY,
    received_at TEXT DEFAULT CURRENT_TIMESTAMP
);
"""


@contextmanager
def _conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db_sync() -> None:
    with _conn() as conn:
        conn.executescript(_SCHEMA)


async def init_db() -> None:
    await asyncio.to_thread(init_db_sync)


# ---------- users ----------

def _get_user_login_sync(telegram_id: int) -> Optional[str]:
    with _conn() as conn:
        row = conn.execute(
            "SELECT steam_login FROM users WHERE telegram_id = ?", (telegram_id,)
        ).fetchone()
        return row["steam_login"] if row else None


async def get_user_login(telegram_id: int) -> Optional[str]:
    return await asyncio.to_thread(_get_user_login_sync, telegram_id)


def _set_user_login_sync(telegram_id: int, steam_login: str) -> None:
    with _conn() as conn:
        conn.execute(
            """
            INSERT INTO users (telegram_id, steam_login) VALUES (?, ?)
            ON CONFLICT(telegram_id) DO UPDATE SET steam_login = excluded.steam_login
            """,
            (telegram_id, steam_login),
        )


async def set_user_login(telegram_id: int, steam_login: str) -> None:
    await asyncio.to_thread(_set_user_login_sync, telegram_id, steam_login)


# ---------- orders ----------

@dataclass
class OrderRecord:
    public_id: str
    telegram_id: int
    steam_login: str
    requested_credit_rub: float
    amount_pay_rub: float
    status: str


def _create_order_sync(order: OrderRecord) -> None:
    with _conn() as conn:
        conn.execute(
            """
            INSERT INTO orders
                (public_id, telegram_id, steam_login, requested_credit_rub, amount_pay_rub, status)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                order.public_id,
                order.telegram_id,
                order.steam_login,
                order.requested_credit_rub,
                order.amount_pay_rub,
                order.status,
            ),
        )


async def create_order(order: OrderRecord) -> None:
    await asyncio.to_thread(_create_order_sync, order)


def _update_order_status_sync(public_id: str, status: str) -> Optional[int]:
    """Возвращает telegram_id владельца заказа, если заказ найден и обновлён."""
    with _conn() as conn:
        row = conn.execute(
            "SELECT telegram_id FROM orders WHERE public_id = ?", (public_id,)
        ).fetchone()
        if row is None:
            return None
        conn.execute(
            "UPDATE orders SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE public_id = ?",
            (status, public_id),
        )
        return row["telegram_id"]


async def update_order_status(public_id: str, status: str) -> Optional[int]:
    return await asyncio.to_thread(_update_order_status_sync, public_id, status)


def _get_order_sync(public_id: str) -> Optional[sqlite3.Row]:
    with _conn() as conn:
        return conn.execute(
            "SELECT * FROM orders WHERE public_id = ?", (public_id,)
        ).fetchone()


async def get_order(public_id: str) -> Optional[sqlite3.Row]:
    return await asyncio.to_thread(_get_order_sync, public_id)


# ---------- webhook idempotency ----------

def _mark_event_processed_sync(event_id: str) -> bool:
    """True, если событие новое и теперь помечено обработанным; False, если уже было."""
    with _conn() as conn:
        try:
            conn.execute(
                "INSERT INTO processed_webhook_events (event_id) VALUES (?)", (event_id,)
            )
            return True
        except sqlite3.IntegrityError:
            return False


async def mark_event_processed(event_id: str) -> bool:
    return await asyncio.to_thread(_mark_event_processed_sync, event_id)
