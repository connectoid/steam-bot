"""
Хранилище на SQLite. Для MVP достаточно; при росте можно заменить на Postgres,
не меняя интерфейс функций ниже.
"""
import asyncio
import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Optional

from config import DB_PATH

# Конечные статусы заказа по документации Payli.
FINAL_STATUSES = {"completed", "failed", "canceled", "refunded", "chargeback", "creation_failed"}

# Порядок статусов: вебхуки могут прийти не по порядку, «откатываться» назад нельзя.
_STATUS_RANK = {
    "pending_payment": 0,
    "payment_received": 1,
    "processing_payout": 2,
    "awaiting_confirmation": 3,
    "dispute": 3,
}
_FINAL_RANK = 10


def status_rank(status: str) -> int:
    if status in FINAL_STATUSES:
        return _FINAL_RANK
    return _STATUS_RANK.get(status, 0)


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

-- Сырые ответы Payli и вебхуки по каждому заказу — для сверки и бухгалтерии.
CREATE TABLE IF NOT EXISTS order_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    public_id TEXT,
    source TEXT NOT NULL,
    payload TEXT NOT NULL,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_order_events_public_id ON order_events(public_id);
"""

# Колонки, добавленные позже: докатываем на существующую базу.
_ORDER_EXTRA_COLUMNS = {
    "base_commission_percent": "REAL",
    "partner_commission_percent": "REAL",
    "partner_commission_rub": "REAL",
    "partner_fee_rub": "REAL",
}


@contextmanager
def _conn():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db_sync() -> None:
    with _conn() as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(_SCHEMA)
        existing = {r["name"] for r in conn.execute("PRAGMA table_info(orders)")}
        for col, col_type in _ORDER_EXTRA_COLUMNS.items():
            if col not in existing:
                conn.execute(f"ALTER TABLE orders ADD COLUMN {col} {col_type}")


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
    base_commission_percent: Optional[float] = None
    partner_commission_percent: Optional[float] = None
    partner_commission_rub: Optional[float] = None
    partner_fee_rub: Optional[float] = None


def _create_order_sync(order: OrderRecord) -> None:
    with _conn() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO orders
                (public_id, telegram_id, steam_login, requested_credit_rub, amount_pay_rub, status,
                 base_commission_percent, partner_commission_percent,
                 partner_commission_rub, partner_fee_rub)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                order.public_id,
                order.telegram_id,
                order.steam_login,
                order.requested_credit_rub,
                order.amount_pay_rub,
                order.status,
                order.base_commission_percent,
                order.partner_commission_percent,
                order.partner_commission_rub,
                order.partner_fee_rub,
            ),
        )


async def create_order(order: OrderRecord) -> None:
    await asyncio.to_thread(_create_order_sync, order)


def _apply_status_sync(public_id: str, new_status: str) -> Optional[dict]:
    """
    Атомарно меняет статус заказа. Возвращает данные заказа + old_status, если статус
    действительно изменился; None — если заказ не найден, статус тот же или это «откат назад»
    (запоздавший вебхук). Благодаря этому уведомление по каждому статусу уходит один раз,
    даже если его одновременно увидели и вебхук, и периодическая сверка.
    """
    with _conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM orders WHERE public_id = ?", (public_id,)).fetchone()
        if row is None:
            return None
        old_status = row["status"]
        if old_status == new_status:
            return None
        # canceled -> payment_received возможен (поздняя оплата), остальные откаты игнорируем.
        if status_rank(new_status) < status_rank(old_status) and old_status != "canceled":
            return None
        conn.execute(
            "UPDATE orders SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE public_id = ?",
            (new_status, public_id),
        )
        result = dict(row)
        result["old_status"] = old_status
        result["status"] = new_status
        return result


async def apply_status(public_id: str, new_status: str) -> Optional[dict]:
    return await asyncio.to_thread(_apply_status_sync, public_id, new_status)


def _get_order_sync(public_id: str) -> Optional[dict]:
    with _conn() as conn:
        row = conn.execute("SELECT * FROM orders WHERE public_id = ?", (public_id,)).fetchone()
        return dict(row) if row else None


async def get_order(public_id: str) -> Optional[dict]:
    return await asyncio.to_thread(_get_order_sync, public_id)


def _list_unfinished_orders_sync() -> list[str]:
    """Незавершённые заказы старше минуты и моложе 3 суток — для резервной сверки статусов."""
    placeholders = ",".join("?" * len(FINAL_STATUSES))
    with _conn() as conn:
        rows = conn.execute(
            f"""
            SELECT public_id FROM orders
            WHERE status NOT IN ({placeholders})
              AND created_at <= datetime('now', '-60 seconds')
              AND created_at >= datetime('now', '-3 days')
            ORDER BY created_at
            """,
            tuple(FINAL_STATUSES),
        ).fetchall()
        return [r["public_id"] for r in rows]


async def list_unfinished_orders() -> list[str]:
    return await asyncio.to_thread(_list_unfinished_orders_sync)


# ---------- журнал событий ----------

def _log_event_sync(public_id: Optional[str], source: str, payload: dict) -> None:
    with _conn() as conn:
        conn.execute(
            "INSERT INTO order_events (public_id, source, payload) VALUES (?, ?, ?)",
            (public_id, source, json.dumps(payload, ensure_ascii=False)),
        )


async def log_event(public_id: Optional[str], source: str, payload: dict) -> None:
    await asyncio.to_thread(_log_event_sync, public_id, source, payload)


# ---------- идемпотентность вебхуков ----------

def _mark_event_processed_sync(event_id: str) -> bool:
    """True, если событие новое и теперь помечено обработанным; False, если уже было."""
    with _conn() as conn:
        try:
            conn.execute("INSERT INTO processed_webhook_events (event_id) VALUES (?)", (event_id,))
            return True
        except sqlite3.IntegrityError:
            return False


async def mark_event_processed(event_id: str) -> bool:
    return await asyncio.to_thread(_mark_event_processed_sync, event_id)
