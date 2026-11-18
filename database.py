"""
database.py
Persistent PostgreSQL database layer for the Telegram shop bot + web admin panel.

Keep DATABASE_URL in Render Environment Variables, not in GitHub.
"""

import os
import json
import logging
import secrets
from contextlib import contextmanager

try:
    import psycopg
    from psycopg.rows import dict_row
except ImportError:
    psycopg = None
    dict_row = None

logger = logging.getLogger(__name__)

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()


def parse_prices(raw):
    """Return prices as a list of [plan_name, price] pairs.

    Accepts a JSON string, a list of pairs, or a {plan: price} dict.
    """
    if isinstance(raw, (str, bytes)):
        try:
            raw = json.loads(raw or "[]")
        except (TypeError, ValueError):
            return []
    if isinstance(raw, dict):
        raw = list(raw.items())
    result = []
    if isinstance(raw, (list, tuple)):
        for item in raw:
            try:
                name, price = item
                result.append([str(name), float(price)])
            except (TypeError, ValueError):
                continue
    return result


class Database:
    def __init__(self, database_url=None):
        self.database_url = (database_url or DATABASE_URL).strip()

        if not self.database_url:
            raise RuntimeError(
                "DATABASE_URL is not set. Add the PostgreSQL connection URL "
                "as a Render Environment Variable."
            )

        if psycopg is None:
            raise RuntimeError(
                "PostgreSQL driver missing. Add psycopg[binary] to requirements.txt."
            )

        if self.database_url.startswith("postgres://"):
            self.database_url = "postgresql://" + self.database_url[len("postgres://"):]

    # ------------------------------------------------------------------
    # Low-level helpers
    # ------------------------------------------------------------------
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

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------
    def init_db(self):
        statements = [
            # Prevent two instances (e.g. during a redeploy) from racing on schema creation.
            "SELECT pg_advisory_xact_lock(727274)",
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
                account_type TEXT NOT NULL DEFAULT 'Regular',
                last_spin_at TIMESTAMPTZ
            )
            """,
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS last_spin_at TIMESTAMPTZ",
            "ALTER TABLE users ALTER COLUMN account_type SET DEFAULT 'Regular'",
            "UPDATE users SET account_type = 'Regular' WHERE account_type = 'Normal'",
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
                verified_at TEXT,
                prod_key TEXT,
                plan TEXT,
                is_deficit INTEGER NOT NULL DEFAULT 0
            )
            """,
            "ALTER TABLE payment_records ADD COLUMN IF NOT EXISTS prod_key TEXT",
            "ALTER TABLE payment_records ADD COLUMN IF NOT EXISTS plan TEXT",
            "ALTER TABLE payment_records ADD COLUMN IF NOT EXISTS is_deficit INTEGER NOT NULL DEFAULT 0",
            # --- Tables used by the web admin panel ---
            """
            CREATE TABLE IF NOT EXISTS admin_auth (
                id INTEGER PRIMARY KEY,
                password_hash TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS coupons (
                id BIGSERIAL PRIMARY KEY,
                code TEXT UNIQUE,
                discount_type TEXT,
                discount_val DOUBLE PRECISION,
                usage_limit INTEGER,
                used_count INTEGER NOT NULL DEFAULT 0,
                is_active INTEGER NOT NULL DEFAULT 1
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS id_accounts (
                id BIGSERIAL PRIMARY KEY,
                category_name TEXT,
                account_data TEXT,
                price DOUBLE PRECISION,
                is_sold INTEGER NOT NULL DEFAULT 0
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS gateway_config (
                id INTEGER PRIMARY KEY,
                active_gateway TEXT NOT NULL DEFAULT 'fampay',
                fampay_api_key TEXT NOT NULL DEFAULT '',
                fampay_upi_id TEXT NOT NULL DEFAULT '9544113089@fam',
                fampay_base_url TEXT NOT NULL DEFAULT 'https://xyzcheats.com/gateway',
                paytm_base_url TEXT NOT NULL DEFAULT 'https://xyzcheats.com',
                paytm_upi_id TEXT NOT NULL DEFAULT '',
                paytm_merchant_id TEXT NOT NULL DEFAULT ''
            )
            """,
            "ALTER TABLE gateway_config ADD COLUMN IF NOT EXISTS upi_id TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE gateway_config ADD COLUMN IF NOT EXISTS payee_name TEXT NOT NULL DEFAULT 'Elite Hackers Store'",
            "CREATE INDEX IF NOT EXISTS idx_order_history_user ON order_history(user_id)",
            "CREATE INDEX IF NOT EXISTS idx_keys_product_plan ON keys_inventory(prod_key, plan, is_used)",
            "CREATE INDEX IF NOT EXISTS idx_payments_user_status ON payment_records(user_id, status)",
            # --- Default rows ---
            """
            INSERT INTO store_settings (id, support_username, how_to_use_link, welcome_message)
            VALUES (1, '@Athulsudin', 'https://t.me/chatelitehackers', '')
            ON CONFLICT (id) DO NOTHING
            """,
            """
            INSERT INTO upi_settings (id, paytm_token, paytm_qr, fampay_token, fampay_qr)
            VALUES (1, '', '', '9544113089@fam', '')
            ON CONFLICT (id) DO NOTHING
            """,
            "INSERT INTO gateway_config (id) VALUES (1) ON CONFLICT (id) DO NOTHING",
        ]

        with self.connection() as conn:
            with conn.cursor() as cur:
                for statement in statements:
                    cur.execute(statement)

        self._seed_admin_password()
        logger.info("Persistent PostgreSQL database initialized.")

    # ------------------------------------------------------------------
    # Admin password (stored as a hash, never in plain text)
    # ------------------------------------------------------------------
    def _seed_admin_password(self):
        from werkzeug.security import generate_password_hash

        env_pwd = os.getenv("ADMIN_PASSWORD", "").strip()
        if env_pwd:
            # ADMIN_PASSWORD in Render is the source of truth: change it there and
            # the login password changes after the next deploy/restart.
            self.execute(
                """
                INSERT INTO admin_auth (id, password_hash) VALUES (1, %s)
                ON CONFLICT (id) DO UPDATE SET password_hash = EXCLUDED.password_hash
                """,
                (generate_password_hash(env_pwd),),
            )
            return

        if self.fetchone("SELECT id FROM admin_auth WHERE id = 1"):
            return

        pwd = secrets.token_urlsafe(9)
        generated = True

        self.execute(
            "INSERT INTO admin_auth (id, password_hash) VALUES (1, %s) ON CONFLICT (id) DO NOTHING",
            (generate_password_hash(pwd),),
        )
        if generated:
            logger.warning(
                "ADMIN_PASSWORD env var is not set. Generated admin password: %s "
                "(set ADMIN_PASSWORD in Render, or change it from the admin panel).",
                pwd,
            )

    def check_admin_password(self, password):
        from werkzeug.security import check_password_hash

        row = self.fetchone("SELECT password_hash FROM admin_auth WHERE id = 1")
        if not row or not password:
            return False
        return check_password_hash(row["password_hash"], password)

    def set_admin_password(self, new_password):
        from werkzeug.security import generate_password_hash

        self.execute(
            """
            INSERT INTO admin_auth (id, password_hash) VALUES (1, %s)
            ON CONFLICT (id) DO UPDATE SET password_hash = EXCLUDED.password_hash
            """,
            (generate_password_hash(new_password),),
        )

    # ------------------------------------------------------------------
    # Users & wallet
    # ------------------------------------------------------------------
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
        """Add `amount` (can be negative) to the wallet."""
        self.execute(
            "UPDATE users SET wallet_balance = wallet_balance + %s WHERE user_id = %s",
            (amount, user_id),
        )

    def try_deduct(self, user_id, amount):
        """Atomically subtract `amount` if the wallet has enough money.

        Returns the new balance, or None if the balance was too low.
        """
        row = self.fetchone(
            """
            UPDATE users SET wallet_balance = wallet_balance - %s
            WHERE user_id = %s AND wallet_balance >= %s
            RETURNING wallet_balance
            """,
            (amount, user_id, amount),
        )
        return float(row["wallet_balance"]) if row else None

    def set_reseller(self, user_id):
        self.execute(
            "UPDATE users SET account_type = 'Reseller' WHERE user_id = %s",
            (user_id,),
        )

    def set_account_type(self, user_id, role):
        if role not in ("Regular", "Reseller"):
            raise ValueError("Invalid role")
        self.execute(
            "UPDATE users SET account_type = %s WHERE user_id = %s",
            (role, user_id),
        )

    # Lucky spin cooldown (enforced in the database so it cannot be bypassed)
    def claim_spin(self, user_id, cooldown_hours=24):
        """Atomically start a spin. Returns True only if the cooldown has passed."""
        count = self.execute(
            """
            UPDATE users SET last_spin_at = now()
            WHERE user_id = %s
              AND (last_spin_at IS NULL OR last_spin_at <= now() - (%s * INTERVAL '1 hour'))
            """,
            (user_id, cooldown_hours),
        )
        return count > 0

    def spin_remaining_seconds(self, user_id, cooldown_hours=24):
        row = self.fetchone(
            """
            SELECT EXTRACT(EPOCH FROM (last_spin_at + (%s * INTERVAL '1 hour') - now())) AS secs
            FROM users WHERE user_id = %s
            """,
            (cooldown_hours, user_id),
        )
        if not row or row["secs"] is None:
            return 0
        return max(0, int(float(row["secs"])))

    # ------------------------------------------------------------------
    # Orders
    # ------------------------------------------------------------------
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

    # ------------------------------------------------------------------
    # Product keys
    # ------------------------------------------------------------------
    def pop_auto_key(self, prod_key, plan):
        with self.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, item_key FROM keys_inventory
                    WHERE prod_key = %s AND plan = %s AND is_used = 0
                    ORDER BY id
                    LIMIT 1
                    FOR UPDATE SKIP LOCKED
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

    # ------------------------------------------------------------------
    # Products
    # ------------------------------------------------------------------
    def get_products_by_category(self, category):
        rows = self.fetchall(
            "SELECT * FROM products WHERE category = %s ORDER BY prod_key",
            (category,),
        )
        for row in rows:
            row["prices"] = parse_prices(row["prices"])
        return rows

    def get_product_by_key(self, prod_key):
        row = self.fetchone(
            "SELECT * FROM products WHERE prod_key = %s",
            (prod_key,),
        )
        if row:
            row["prices"] = parse_prices(row["prices"])
        return row

    # ------------------------------------------------------------------
    # Payments
    # ------------------------------------------------------------------
    def get_payment_settings(self):
        """UPI ID + payee name shown on the payment QR (editable live from the admin panel)."""
        row = self.fetchone("SELECT upi_id, payee_name FROM gateway_config WHERE id = 1")
        return row or {"upi_id": "", "payee_name": "Elite Hackers Store"}

    def set_payment_settings(self, upi_id, payee_name):
        self.execute(
            "UPDATE gateway_config SET upi_id = %s, payee_name = %s WHERE id = 1",
            (upi_id, payee_name),
        )

    def create_payment(self, user_id, order_id, amount, method, upi_id, created_at,
                       prod_key=None, plan=None, is_deficit=False):
        self.execute(
            """
            INSERT INTO payment_records
            (user_id, order_id, amount, method, upi_id, created_at, prod_key, plan, is_deficit)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (order_id) DO NOTHING
            """,
            (user_id, order_id, amount, method, upi_id, created_at,
             prod_key, plan, 1 if is_deficit else 0),
        )

    def get_payment(self, order_id):
        return self.fetchone(
            "SELECT * FROM payment_records WHERE order_id = %s",
            (order_id,),
        )

    def claim_payment(self, order_id, from_status, to_status, utr=None, verified_at=None):
        """Atomically move a payment from one status to another.

        Returns the updated row, or None if the payment was not in `from_status`
        (e.g. it was already approved), so a payment can never be processed twice.
        """
        return self.fetchone(
            """
            UPDATE payment_records
            SET status = %s,
                utr = COALESCE(%s, utr),
                verified_at = COALESCE(%s, verified_at)
            WHERE order_id = %s AND status = %s
            RETURNING *
            """,
            (to_status, utr, verified_at, order_id, from_status),
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




