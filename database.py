"""
database.py
Persistent PostgreSQL database layer for the Telegram shop bot.

This is a separate database module. main.py will be connected to it in the
next step. Keep DATABASE_URL in Render Environment Variables, not in GitHub.
"""

import os
import json
import logging
from contextlib import contextmanager

try:
    import psycopg
    from psycopg.rows import dict_row
except ImportError:
    psycopg = None
    dict_row = None

logger = logging.getLogger(__name__)

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()


class Database:
    def __init__(self, database_url=None):
        self.database_url = (database_url or DATABASE_URL).strip()

        if not self.database_url:
            raise RuntimeError(
                "DATABASE_URL is not set. Add the PostgreSQL connection URL "
                "as a Render Environment Variable before connecting main.py."
            )

        if psycopg is None:
            raise RuntimeError(
                "PostgreSQL driver missing. Install psycopg[binary] in dependencies."
            )

        if self.database_url.startswith("postgres://"):
            self.database_url = "postgresql://" + self.database_url[len("postgres://"):]

    @contextmanager
    def connection(self):
        conn = psycopg.connect(
            self.database_url,
            connect_timeout=15,
            row_factory=dict_row,
        )
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def execute(self, query, params=None):
        with self.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(query, params or ())
                return cur.rowcount

    def fetchone(self, query, params=None):
        with self.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(query, params or ())
                return cur.fetchone()

    def fetchall(self, query, params=None):
        with self.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(query, params or ())
                return cur.fetchall()

    def init_db(self):
        statements = [
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY,
                full_name TEXT,
                username TEXT,
                joined_date TEXT,
                orders_count INTEGER NOT NULL DEFAULT 0,
                wallet_balance DOUBLE PRECISION NOT NULL DEFAULT 0,
                total_spent DOUBLE PRECISION NOT NULL DEFAULT 0,
                total_referrals INTEGER NOT NULL DEFAULT 0,
                referral_earnings DOUBLE PRECISION NOT NULL DEFAULT 0,
                account_type TEXT NOT NULL DEFAULT 'Normal'
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS order_history (
                id BIGSERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                prod_name TEXT,
                plan TEXT,
                key_delivered TEXT,
                amount DOUBLE PRECISION NOT NULL DEFAULT 0,
                utr TEXT,
                timestamp TEXT
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS products (
                prod_key TEXT PRIMARY KEY,
                name TEXT,
                category TEXT,
                prices TEXT,
                download_link TEXT,
                icon TEXT,
                maintenance INTEGER NOT NULL DEFAULT 0,
                stock_out INTEGER NOT NULL DEFAULT 0
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS keys_inventory (
                id BIGSERIAL PRIMARY KEY,
                prod_key TEXT NOT NULL,
                plan TEXT,
                item_key TEXT,
                is_used INTEGER NOT NULL DEFAULT 0
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS upi_settings (
                id INTEGER PRIMARY KEY,
                paytm_token TEXT,
                paytm_qr TEXT,
                fampay_token TEXT,
                fampay_qr TEXT
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS store_settings (
                id INTEGER PRIMARY KEY,
                support_username TEXT,
                how_to_use_link TEXT,
                welcome_message TEXT
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS payment_records (
                id BIGSERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                order_id TEXT UNIQUE,
                amount DOUBLE PRECISION NOT NULL DEFAULT 0,
                method TEXT,
                upi_id TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                utr TEXT,
                created_at TEXT,
                verified_at TEXT
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_order_history_user ON order_history(user_id)",
            "CREATE INDEX IF NOT EXISTS idx_keys_product_plan ON keys_inventory(prod_key, plan, is_used)",
            "CREATE INDEX IF NOT EXISTS idx_payments_user_status ON payment_records(user_id, status)",
        ]

        with self.connection() as conn:
            with conn.cursor() as cur:
                for statement in statements:
                    cur.execute(statement)

        logger.info("Persistent PostgreSQL database initialized.")

    # Users
    def add_or_update_user(self, user_id, full_name, username, joined_date):
        self.execute(
            """
            INSERT INTO users (user_id, full_name, username, joined_date)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (user_id) DO UPDATE SET
                full_name = EXCLUDED.full_name,
                username = EXCLUDED.username
            """,
            (user_id, full_name, username, joined_date),
        )

    def get_user(self, user_id):
        return self.fetchone("SELECT * FROM users WHERE user_id = %s", (user_id,))

    def update_balance(self, user_id, amount):
        self.execute(
            "UPDATE users SET wallet_balance = wallet_balance + %s WHERE user_id = %s",
            (amount, user_id),
        )

    def set_reseller(self, user_id):
        self.execute(
            "UPDATE users SET account_type = 'Reseller' WHERE user_id = %s",
            (user_id,),
        )

    # Orders
    def add_order(self, user_id, prod_name, plan, key_delivered, amount, utr, timestamp):
        self.execute(
            """
            INSERT INTO order_history
            (user_id, prod_name, plan, key_delivered, amount, utr, timestamp)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (user_id, prod_name, plan, key_delivered, amount, utr, timestamp),
        )

    def get_user_history(self, user_id, limit=20):
        return self.fetchall(
            """
            SELECT * FROM order_history
            WHERE user_id = %s
            ORDER BY id DESC
            LIMIT %s
            """,
            (user_id, limit),
        )

    # Product keys
    def pop_auto_key(self, prod_key, plan):
        with self.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, item_key FROM keys_inventory
                    WHERE prod_key = %s AND plan = %s AND is_used = 0
                    ORDER BY id
                    FOR UPDATE SKIP LOCKED
                    LIMIT 1
                    """,
                    (prod_key, plan),
                )
                row = cur.fetchone()
                if not row:
                    return None

                cur.execute(
                    "UPDATE keys_inventory SET is_used = 1 WHERE id = %s",
                    (row["id"],),
                )
                return row["item_key"]

    # Products
    def get_products_by_category(self, category):
        rows = self.fetchall(
            "SELECT * FROM products WHERE category = %s ORDER BY prod_key",
            (category,),
        )
        for row in rows:
            try:
                row["prices"] = json.loads(row["prices"] or "{}")
            except (TypeError, json.JSONDecodeError):
                row["prices"] = {}
        return rows

    def get_product_by_key(self, prod_key):
        row = self.fetchone(
            "SELECT * FROM products WHERE prod_key = %s",
            (prod_key,),
        )
        if row:
            try:
                row["prices"] = json.loads(row["prices"] or "{}")
            except (TypeError, json.JSONDecodeError):
                row["prices"] = {}
        return row

    # Payments
    def create_payment(self, user_id, order_id, amount, method, upi_id, created_at):
        self.execute(
            """
            INSERT INTO payment_records
            (user_id, order_id, amount, method, upi_id, created_at)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (order_id) DO NOTHING
            """,
            (user_id, order_id, amount, method, upi_id, created_at),
        )

    def get_payment(self, order_id):
        return self.fetchone(
            "SELECT * FROM payment_records WHERE order_id = %s",
            (order_id,),
        )

    def update_payment_status(self, order_id, status, utr=None, verified_at=None):
        self.execute(
            """
            UPDATE payment_records
            SET status = %s,
                utr = COALESCE(%s, utr),
                verified_at = COALESCE(%s, verified_at)
            WHERE order_id = %s
            """,
            (status, utr, verified_at, order_id),
        )


db = Database(DATABASE_URL) if DATABASE_URL else None


def init_db():
    if db is None:
        raise RuntimeError(
            "DATABASE_URL is not configured. Set it in Render Environment Variables."
        )
    db.init_db()
    return db
