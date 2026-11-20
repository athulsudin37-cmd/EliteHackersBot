"""
database.py
Persistent PostgreSQL database layer for the Telegram shop bot + web admin panel.

Keep DATABASE_URL in Render Environment Variables, not in GitHub.
"""

import os
import json
import logging
import secrets
import random
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
            "ALTER TABLE gateway_config ADD COLUMN IF NOT EXISTS auto_verify INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE gateway_config ADD COLUMN IF NOT EXISTS webhook_secret TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE payment_records ADD COLUMN IF NOT EXISTS pay_amount DOUBLE PRECISION",
            "ALTER TABLE payment_records ADD COLUMN IF NOT EXISTS created_ts TIMESTAMPTZ NOT NULL DEFAULT now()",
            "ALTER TABLE payment_records ADD COLUMN IF NOT EXISTS chat_id BIGINT",
            "ALTER TABLE payment_records ADD COLUMN IF NOT EXISTS qr_msg_id BIGINT",
            """
            CREATE TABLE IF NOT EXISTS bank_credits (
                id BIGSERIAL PRIMARY KEY,
                fingerprint TEXT UNIQUE,
                amount DOUBLE PRECISION,
                utr TEXT,
                raw_text TEXT,
                status TEXT NOT NULL DEFAULT 'new',
                order_id TEXT,
                received_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """,
            "ALTER TABLE gateway_config ADD COLUMN IF NOT EXISTS rzp_key_id TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE gateway_config ADD COLUMN IF NOT EXISTS rzp_key_secret TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE gateway_config ADD COLUMN IF NOT EXISTS rzp_webhook_secret TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE payment_records ADD COLUMN IF NOT EXISTS gateway TEXT",
            "ALTER TABLE payment_records ADD COLUMN IF NOT EXISTS gateway_ref TEXT",
            "ALTER TABLE payment_records ADD COLUMN IF NOT EXISTS pay_url TEXT",
            "CREATE INDEX IF NOT EXISTS idx_payments_amount ON payment_records(pay_amount, created_ts)",
            "ALTER TABLE payment_records ADD COLUMN IF NOT EXISTS created_ts TIMESTAMPTZ NOT NULL DEFAULT now()",
            "ALTER TABLE payment_records ADD COLUMN IF NOT EXISTS base_amount DOUBLE PRECISION",
            "ALTER TABLE payment_records ADD COLUMN IF NOT EXISTS notified INTEGER NOT NULL DEFAULT 1",
            "ALTER TABLE payment_records ADD COLUMN IF NOT EXISTS qr_message_id BIGINT",
            "ALTER TABLE gateway_config ADD COLUMN IF NOT EXISTS webhook_secret TEXT NOT NULL DEFAULT ''",
            """
            CREATE TABLE IF NOT EXISTS bank_credits (
                id BIGSERIAL PRIMARY KEY,
                received_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                raw_text TEXT,
                amount DOUBLE PRECISION,
                utr TEXT,
                dedupe_key TEXT UNIQUE,
                status TEXT NOT NULL DEFAULT 'unmatched',
                matched_order_id TEXT
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_payments_open ON payment_records(status, created_ts)",
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
        self.execute(
            "UPDATE gateway_config SET webhook_secret = %s WHERE id = 1 AND webhook_secret = ''",
            (secrets.token_urlsafe(24),),
        )
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
        """Insert the order and update the buyer's order count / total spent in one transaction."""
        with self.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO order_history
                    (user_id, prod_name, plan, key_delivered, amount, utr, timestamp)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    """,
                    (user_id, prod_name, plan, key_delivered, amount, utr, timestamp),
                )
                cur.execute(
                    """
                    UPDATE users
                    SET orders_count = orders_count + 1,
                        total_spent = total_spent + %s
                    WHERE user_id = %s
                    """,
                    (amount, user_id),
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
        """UPI ID, payee name, auto-verify switch and webhook secret (all editable from the admin panel)."""
        row = self.fetchone(
            "SELECT upi_id, payee_name, auto_verify, webhook_secret FROM gateway_config WHERE id = 1"
        )
        return row or {"upi_id": "", "payee_name": "Elite Hackers Store", "auto_verify": 0, "webhook_secret": ""}

    def set_payment_settings(self, upi_id, payee_name, auto_verify=False):
        self.execute(
            "UPDATE gateway_config SET upi_id = %s, payee_name = %s, auto_verify = %s WHERE id = 1",
            (upi_id, payee_name, 1 if auto_verify else 0),
        )

    def rotate_webhook_secret(self):
        new_secret = secrets.token_urlsafe(24)
        self.execute("UPDATE gateway_config SET webhook_secret = %s WHERE id = 1", (new_secret,))
        return new_secret

    # ------------------------------------------------------------------
    # Automatic payment verification (bank credit SMS -> webhook)
    # ------------------------------------------------------------------
    OPEN_GRACE_MINUTES = 30  # a payment can still be matched this long after its QR expired

    def _seed_webhook_secret(self):
        self.execute(
            "UPDATE gateway_config SET webhook_secret = %s WHERE id = 1 AND webhook_secret = ''",
            (secrets.token_urlsafe(24),),
        )

    def get_webhook_secret(self):
        row = self.fetchone("SELECT webhook_secret FROM gateway_config WHERE id = 1")
        return (row or {}).get("webhook_secret") or ""

    def regenerate_webhook_secret(self):
        new_secret = secrets.token_urlsafe(24)
        self.execute("UPDATE gateway_config SET webhook_secret = %s WHERE id = 1", (new_secret,))
        return new_secret

    def create_payment_unique(self, user_id, order_id, base_amount, method, upi_id, created_at,
                              prod_key=None, plan=None, is_deficit=False):
        """Create a pending payment with an amount that no other open payment has.

        1-99 paise are added to the requested amount, so an incoming bank credit can be
        matched to exactly one order. Returns the final amount the customer must pay.
        """
        base_cents = int(round(float(base_amount) * 100))
        with self.connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_xact_lock(727275)")
                cur.execute(
                    """
                    SELECT ROUND(amount * 100)::bigint AS cents FROM payment_records
                    WHERE status IN ('pending', 'awaiting_approval')
                       OR (status = 'expired' AND created_ts > now() - (%s * INTERVAL '1 minute'))
                    """,
                    (self.OPEN_GRACE_MINUTES,),
                )
                taken = {r["cents"] for r in cur.fetchall()}
                offsets = list(range(1, 100))
                random.shuffle(offsets)
                final_cents = next((base_cents + k for k in offsets if base_cents + k not in taken), None)
                if final_cents is None:
                    raise RuntimeError("Too many open payments for this amount")
                final_amount = round(final_cents / 100.0, 2)
                cur.execute(
                    """
                    INSERT INTO payment_records
                    (user_id, order_id, amount, base_amount, method, upi_id, created_at,
                     prod_key, plan, is_deficit)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (user_id, order_id, final_amount, base_amount, method, upi_id, created_at,
                     prod_key, plan, 1 if is_deficit else 0),
                )
        return final_amount

    def record_bank_credit(self, raw_text, amount, utr, dedupe_key):
        """Store an incoming bank credit and, if it matches an open payment, verify it and
        credit the wallet in ONE transaction (so it can never be credited twice).

        Returns (status, payment_row) with status: duplicate | matched | unmatched.
        """
        cents = int(round(float(amount) * 100))
        with self.connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_xact_lock(727275)")
                cur.execute(
                    """
                    INSERT INTO bank_credits (raw_text, amount, utr, dedupe_key, status)
                    VALUES (%s, %s, %s, %s, 'unmatched')
                    ON CONFLICT (dedupe_key) DO NOTHING
                    RETURNING id
                    """,
                    ((raw_text or "")[:1000], amount, utr, dedupe_key),
                )
                credit = cur.fetchone()
                if not credit:
                    return "duplicate", None

                cur.execute(
                    """
                    SELECT * FROM payment_records
                    WHERE ROUND(amount * 100)::bigint = %s
                      AND (status IN ('pending', 'awaiting_approval')
                           OR (status = 'expired' AND created_ts > now() - (%s * INTERVAL '1 minute')))
                    ORDER BY created_ts DESC
                    LIMIT 1
                    FOR UPDATE
                    """,
                    (cents, self.OPEN_GRACE_MINUTES),
                )
                pay = cur.fetchone()
                if not pay:
                    return "unmatched", None

                cur.execute(
                    """
                    UPDATE payment_records
                    SET status = 'verified', utr = %s, notified = 0,
                        verified_at = to_char(now() AT TIME ZONE 'Asia/Kolkata', 'DD-MM-YYYY HH12:MI AM')
                    WHERE order_id = %s
                    """,
                    (utr, pay["order_id"]),
                )
                cur.execute(
                    "UPDATE users SET wallet_balance = wallet_balance + %s WHERE user_id = %s",
                    (amount, pay["user_id"]),
                )
                cur.execute(
                    "UPDATE bank_credits SET status = 'matched', matched_order_id = %s WHERE id = %s",
                    (pay["order_id"], credit["id"]),
                )
                return "matched", pay

    def get_unnotified_payments(self, limit=20):
        return self.fetchall(
            "SELECT order_id FROM payment_records WHERE status = 'verified' AND notified = 0 ORDER BY id LIMIT %s",
            (limit,),
        )

    def claim_notification(self, order_id):
        """Returns the payment row exactly once (so the customer is notified only once)."""
        return self.fetchone(
            """
            UPDATE payment_records SET notified = 1
            WHERE order_id = %s AND status = 'verified' AND notified = 0
            RETURNING *
            """,
            (order_id,),
        )

    def set_payment_qr_message(self, order_id, message_id):
        self.execute(
            "UPDATE payment_records SET qr_message_id = %s WHERE order_id = %s",
            (message_id, order_id),
        )

    def recent_bank_credits(self, limit=20):
        return self.fetchall(
            """
            SELECT received_at, amount, utr, status, matched_order_id
            FROM bank_credits ORDER BY id DESC LIMIT %s
            """,
            (limit,),
        )

    # ------------------------------------------------------------------
    # Razorpay (real payment gateway)
    # ------------------------------------------------------------------
    def get_razorpay_config(self):
        row = self.fetchone(
            "SELECT active_gateway, rzp_key_id, rzp_key_secret, rzp_webhook_secret FROM gateway_config WHERE id = 1"
        )
        return row or {"active_gateway": "upi", "rzp_key_id": "", "rzp_key_secret": "", "rzp_webhook_secret": ""}

    def set_razorpay_config(self, use_razorpay, key_id, key_secret=None, webhook_secret=None):
        """Save the Razorpay settings. A secret is only replaced when a new value is given."""
        self.execute(
            """
            UPDATE gateway_config SET
                active_gateway = %s,
                rzp_key_id = %s,
                rzp_key_secret = COALESCE(%s, rzp_key_secret),
                rzp_webhook_secret = COALESCE(%s, rzp_webhook_secret)
            WHERE id = 1
            """,
            ("razorpay" if use_razorpay else "upi", key_id, key_secret, webhook_secret),
        )

    def set_payment_gateway_ref(self, order_id, gateway, ref, url):
        self.execute(
            "UPDATE payment_records SET gateway = %s, gateway_ref = %s, pay_url = %s WHERE order_id = %s",
            (gateway, ref, url, order_id),
        )

    def open_gateway_payments(self, gateway="razorpay"):
        """Payments waiting for the gateway to report them as paid."""
        return self.fetchall(
            """
            SELECT order_id, gateway_ref FROM payment_records
            WHERE gateway = %s AND status = 'pending' AND gateway_ref IS NOT NULL
              AND created_ts > now() - INTERVAL '60 minutes'
            ORDER BY id
            LIMIT 50
            """,
            (gateway,),
        )

    def expire_old_gateway_payments(self, gateway="razorpay", minutes=25):
        return self.execute(
            """
            UPDATE payment_records SET status = 'expired'
            WHERE gateway = %s AND status = 'pending'
              AND created_ts < now() - (%s * INTERVAL '1 minute')
            """,
            (gateway, minutes),
        )

    def create_payment(self, user_id, order_id, amount, method, upi_id, created_at,
                       prod_key=None, plan=None, is_deficit=False, unique_amount=False):
        """Create a pending payment and return the exact amount the customer must pay.

        With unique_amount=True a few paise (1-99) are added so that no other payment from the
        last 60 minutes has the same payable amount. That makes every incoming bank credit match
        exactly one order. The wallet is still credited with `amount` (without the paise).
        """
        pay_amount = round(float(amount), 2)
        with self.connection() as conn:
            with conn.cursor() as cur:
                if unique_amount:
                    cur.execute("SELECT pg_advisory_xact_lock(727275)")
                    chosen = None
                    for paise in range(1, 100):
                        candidate = round(float(amount) + paise / 100.0, 2)
                        cur.execute(
                            """
                            SELECT 1 FROM payment_records
                            WHERE ROUND(pay_amount::numeric, 2) = ROUND(%s::numeric, 2)
                              AND created_ts > now() - INTERVAL '60 minutes'
                            LIMIT 1
                            """,
                            (candidate,),
                        )
                        if not cur.fetchone():
                            chosen = candidate
                            break
                    if chosen is None:
                        raise RuntimeError("Too many open payments for this amount. Try again shortly.")
                    pay_amount = chosen

                cur.execute(
                    """
                    INSERT INTO payment_records
                    (user_id, order_id, amount, pay_amount, method, upi_id, created_at,
                     prod_key, plan, is_deficit)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (order_id) DO NOTHING
                    """,
                    (user_id, order_id, amount, pay_amount, method, upi_id, created_at,
                     prod_key, plan, 1 if is_deficit else 0),
                )
        return pay_amount

    def set_payment_message(self, order_id, chat_id, message_id):
        """Remember the QR message so it can be removed when the payment is auto-confirmed."""
        self.execute(
            "UPDATE payment_records SET chat_id = %s, qr_msg_id = %s WHERE order_id = %s",
            (chat_id, message_id, order_id),
        )

    def find_open_payment_by_amount(self, amount):
        """The payment (last 60 min, not yet settled) whose payable amount equals a bank credit."""
        return self.fetchone(
            """
            SELECT * FROM payment_records
            WHERE ROUND(pay_amount::numeric, 2) = ROUND(%s::numeric, 2)
              AND status IN ('pending', 'awaiting_approval', 'expired', 'cancelled')
              AND created_ts > now() - INTERVAL '60 minutes'
            ORDER BY created_ts DESC
            LIMIT 1
            """,
            (amount,),
        )

    def record_bank_credit(self, fingerprint, amount, utr, raw_text, status):
        """Log an incoming bank alert. Returns its id, or None if the same alert was already logged."""
        row = self.fetchone(
            """
            INSERT INTO bank_credits (fingerprint, amount, utr, raw_text, status)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (fingerprint) DO NOTHING
            RETURNING id
            """,
            (fingerprint, amount, utr, (raw_text or "")[:600], status),
        )
        return row["id"] if row else None

    def set_bank_credit_status(self, credit_id, status, order_id=None):
        self.execute(
            "UPDATE bank_credits SET status = %s, order_id = %s WHERE id = %s",
            (status, order_id, credit_id),
        )

    def recent_bank_credits(self, limit=20):
        return self.fetchall(
            """
            SELECT amount, utr, status, order_id, raw_text, received_at
            FROM bank_credits ORDER BY id DESC LIMIT %s
            """,
            (limit,),
        )

    def get_payment(self, order_id):
        return self.fetchone(
            "SELECT * FROM payment_records WHERE order_id = %s",
            (order_id,),
        )

    def claim_payment(self, order_id, from_status, to_status, utr=None, verified_at=None):
        """Atomically move a payment to a new status.

        `from_status` is one status or a list of allowed statuses. Returns the updated row, or None
        if the payment was not in one of them (e.g. it was already settled), so a payment can
        never be processed twice.
        """
        statuses = [from_status] if isinstance(from_status, str) else list(from_status)
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




