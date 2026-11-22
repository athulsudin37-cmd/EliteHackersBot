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
            "ALTER TABLE payment_records ADD COLUMN IF NOT EXISTS pay_amount DOUBLE PRECISION",
            "ALTER TABLE payment_records ADD COLUMN IF NOT EXISTS created_ts TIMESTAMPTZ",
            "ALTER TABLE payment_records ADD COLUMN IF NOT EXISTS chat_id BIGINT",
            "ALTER TABLE payment_records ADD COLUMN IF NOT EXISTS qr_msg_id BIGINT",
            "UPDATE payment_records SET pay_amount = amount WHERE pay_amount IS NULL",
            """
            CREATE TABLE IF NOT EXISTS payment_settings (
                id INTEGER PRIMARY KEY,
                upi_id TEXT NOT NULL DEFAULT '',
                payee_name TEXT NOT NULL DEFAULT 'Elite Hackers Store',
                auto_verify INTEGER NOT NULL DEFAULT 0
            )
            """,
            """
            INSERT INTO payment_settings (id, upi_id, payee_name, auto_verify)
            VALUES (
                1,
                COALESCE((SELECT fampay_token FROM upi_settings WHERE id = 1), ''),
                'Elite Hackers Store',
                0
            )
            ON CONFLICT (id) DO NOTHING
            """,
            """
            CREATE TABLE IF NOT EXISTS gmail_settings (
                id INTEGER PRIMARY KEY,
                gmail_user TEXT NOT NULL DEFAULT '',
                gmail_app_password TEXT NOT NULL DEFAULT '',
                bank_domain TEXT NOT NULL DEFAULT ''
            )
            """,
            "INSERT INTO gmail_settings (id) VALUES (1) ON CONFLICT (id) DO NOTHING",
            """
            CREATE TABLE IF NOT EXISTS bank_credits (
                id BIGSERIAL PRIMARY KEY,
                fingerprint TEXT UNIQUE NOT NULL,
                amount DOUBLE PRECISION,
                utr TEXT,
                raw_text TEXT,
                status TEXT NOT NULL DEFAULT 'new',
                order_id TEXT,
                received_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_bank_credits_received ON bank_credits(received_at DESC)",
            """
            CREATE TABLE IF NOT EXISTS processed_mails (
                message_key TEXT PRIMARY KEY,
                created_ts TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """,
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
    def create_payment(self, user_id, order_id, amount, method, upi_id, created_at,
                       prod_key=None, plan=None, is_deficit=False, unique_amount=False):
        """Create a pending payment and return the exact amount to show in the QR.

        Automatic verification gives each active order a distinct paise amount so
        an incoming FamPay/bank credit can be matched to one order.
        """
        base_amount = round(float(amount), 2)
        with self.connection() as conn:
            with conn.cursor() as cur:
                # Serialize amount assignment so two simultaneous orders cannot receive
                # the same unique paise amount.
                cur.execute("SELECT pg_advisory_xact_lock(727275)")
                pay_amount = base_amount
                if unique_amount:
                    cur.execute(
                        """
                        SELECT pay_amount
                        FROM payment_records
                        WHERE status IN ('pending', 'awaiting_approval', 'expired', 'cancelled')
                          AND (created_ts IS NULL OR created_ts >= now() - INTERVAL '90 minutes')
                        """
                    )
                    used = {round(float(r["pay_amount"]), 2) for r in cur.fetchall()
                            if r["pay_amount"] is not None}
                    for cents in range(1, 100):
                        candidate = round(base_amount + cents / 100.0, 2)
                        if candidate not in used:
                            pay_amount = candidate
                            break
                    else:
                        raise RuntimeError("No unique paise amount is available for this payment.")

                cur.execute(
                    """
                    INSERT INTO payment_records
                    (user_id, order_id, amount, pay_amount, method, upi_id, created_at,
                     created_ts, prod_key, plan, is_deficit, status)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, now(), %s, %s, %s, 'pending')
                    ON CONFLICT (order_id) DO NOTHING
                    RETURNING pay_amount
                    """,
                    (user_id, order_id, base_amount, pay_amount, method, upi_id, created_at,
                     prod_key, plan, 1 if is_deficit else 0),
                )
                row = cur.fetchone()
                if row:
                    return float(row["pay_amount"])
                existing = cur.execute(
                    "SELECT pay_amount FROM payment_records WHERE order_id = %s",
                    (order_id,),
                ).fetchone()
                if not existing:
                    raise RuntimeError("Payment record could not be created.")
                return float(existing["pay_amount"] or existing["amount"])

    def get_payment(self, order_id):
        return self.fetchone(
            "SELECT * FROM payment_records WHERE order_id = %s",
            (order_id,),
        )

    def claim_payment(self, order_id, from_status, to_status, utr=None, verified_at=None):
        """Atomically move a payment from one or more allowed statuses.

        A string means one status; a tuple/list/set means any of those statuses.
        This supports the bot's pending/expired button flow and multi-state
        automatic settlement without allowing the same payment to be processed twice.
        """
        if isinstance(from_status, (list, tuple, set)):
            statuses = list(from_status)
            if not statuses:
                return None
            return self.fetchone(
                """
                UPDATE payment_records
                SET status = %s,
                    utr = COALESCE(%s, utr),
                    verified_at = COALESCE(%s, verified_at)
                WHERE order_id = %s AND status = ANY(%s)
                RETURNING *
                """,
                (to_status, utr, verified_at, order_id, statuses),
            )

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

    def set_payment_message(self, order_id, chat_id, message_id):
        self.execute(
            "UPDATE payment_records SET chat_id = %s, qr_msg_id = %s WHERE order_id = %s",
            (chat_id, message_id, order_id),
        )

    # Payment settings used by the Telegram bot and web admin panel.
    def get_payment_settings(self):
        row = self.fetchone("SELECT upi_id, payee_name, auto_verify FROM payment_settings WHERE id = 1")
        return row or {"upi_id": "", "payee_name": "Elite Hackers Store", "auto_verify": 0}

    def set_payment_settings(self, upi_id, payee_name, auto_verify):
        self.execute(
            """
            INSERT INTO payment_settings (id, upi_id, payee_name, auto_verify)
            VALUES (1, %s, %s, %s)
            ON CONFLICT (id) DO UPDATE SET
                upi_id = EXCLUDED.upi_id,
                payee_name = EXCLUDED.payee_name,
                auto_verify = EXCLUDED.auto_verify
            """,
            (upi_id, payee_name, 1 if auto_verify else 0),
        )

    # Gmail credentials and trusted FamPay/bank sender domain.
    def get_gmail_config(self):
        row = self.fetchone(
            "SELECT gmail_user, gmail_app_password, bank_domain FROM gmail_settings WHERE id = 1"
        )
        return row or {"gmail_user": "", "gmail_app_password": "", "bank_domain": ""}

    def set_gmail_config(self, gmail_user, app_password, bank_domain):
        current = self.get_gmail_config()
        password = app_password if app_password is not None else current.get("gmail_app_password", "")
        self.execute(
            """
            INSERT INTO gmail_settings (id, gmail_user, gmail_app_password, bank_domain)
            VALUES (1, %s, %s, %s)
            ON CONFLICT (id) DO UPDATE SET
                gmail_user = EXCLUDED.gmail_user,
                gmail_app_password = EXCLUDED.gmail_app_password,
                bank_domain = EXCLUDED.bank_domain
            """,
            (gmail_user or "", password or "", bank_domain or ""),
        )

    # Incoming credit alerts are deduplicated before they can settle an order.
    def record_bank_credit(self, fingerprint, amount, utr, raw_text, status):
        row = self.fetchone(
            """
            INSERT INTO bank_credits (fingerprint, amount, utr, raw_text, status)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (fingerprint) DO NOTHING
            RETURNING id
            """,
            (fingerprint, amount, utr, raw_text, status),
        )
        return row["id"] if row else None

    def set_bank_credit_status(self, credit_id, status, order_id=None):
        self.execute(
            "UPDATE bank_credits SET status = %s, order_id = COALESCE(%s, order_id) WHERE id = %s",
            (status, order_id, credit_id),
        )

    def recent_bank_credits(self, limit=20):
        return self.fetchall(
            """
            SELECT id, amount, utr, raw_text, status, order_id, received_at
            FROM bank_credits ORDER BY received_at DESC, id DESC LIMIT %s
            """,
            (limit,),
        )

    def find_open_payment_by_amount(self, amount):
        """Find one recent unpaid order with the exact QR amount (including paise)."""
        return self.fetchone(
            """
            SELECT * FROM payment_records
            WHERE status IN ('pending', 'awaiting_approval', 'expired', 'cancelled')
              AND ABS(COALESCE(pay_amount, amount) - %s) < 0.005
              AND COALESCE(created_ts, now()) >= now() - INTERVAL '2 hours'
            ORDER BY created_ts DESC NULLS LAST, id DESC
            LIMIT 1
            """,
            (round(float(amount), 2),),
        )


db = Database(DATABASE_URL) if DATABASE_URL else None


def init_db():
    if db is None:
        raise RuntimeError(
            "DATABASE_URL is not configured. Set it in Render Environment Variables."
        )
    db.init_db()
    return db





