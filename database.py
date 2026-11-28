"""
database.py
Persistent PostgreSQL database layer for the Telegram shop bot + web admin panel.

Keep DATABASE_URL in Render Environment Variables, not in GitHub.
"""

import os
import re
import json
import time
import threading
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

try:
    from psycopg_pool import ConnectionPool
except ImportError:  # optional: without it the old "new connection per query" mode is used
    ConnectionPool = None

logger = logging.getLogger(__name__)

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()


# Every bot button whose color can be chosen in the admin panel, grouped by screen.
# (key, label shown in the admin panel, default color). Colors: primary = blue,
# success = green, danger = red, "" = Telegram's normal grey.
BUTTON_GROUPS = [
    ("🏠 Main Menu", [
        ("shop_key", "🛒 Shop Key", "success"),
        ("add_balance", "💰 Add Balance", "success"),
        ("my_profile", "👤 My Profile", "primary"),
        ("all_history", "🧾 All History", "primary"),
        ("tutorial", "▶️ Tutorial watch", "primary"),
        ("selling_proof", "💼 Selling Proof", "primary"),
        ("lucky_spin", "🎰 Lucky Spin", "danger"),
        ("referral", "👥 Referral", "danger"),
        ("top_sellers", "👑 Top 5 Sellers Monthly", "primary"),
        ("reseller", "👑 Reseller", "success"),
        ("support", "💬 Support", "success"),
        ("check_updates", "📢 Check All Updates", "primary"),
        ("language", "🌐 Language", "primary"),
        ("web_admin", "👑 Web Admin Panel (owner only)", "success"),
    ]),
    ("🌐 Language Screen", [
        ("lang_option", "Language choices (English / हिन्दी / తెలుగు / தமிழ் / മലയാളം)", ""),
    ]),
    ("🛒 Shop & Products", [
        ("game_ff", "🔥 FREE FIRE", "primary"),
        ("game_carrom", "🎱 CARROM POOL", "primary"),
        ("game_pool8", "🎱 8BALL POOL", "primary"),
        ("cat_non_root", "☎️ Non-Root Mobile", "primary"),
        ("cat_root", "🔓 Root Mobile", "primary"),
        ("cat_ios", "🍏 iOS Panels", "primary"),
        ("cat_pc", "💻 PC Panels", "primary"),
        ("cat_likes", "🆔 8 Level ID", "primary"),
        ("product_item", "Product buttons (every product in a category)", ""),
        ("plan_item", "Plan / duration buttons (🕒 1 Day | ₹50)", "success"),
        ("preview_video", "🎥 Preview Video", "primary"),
        ("open_shop", "🛒 Open Shop", "success"),
    ]),
    ("💳 Add Balance & Payment", [
        ("pay_upi", "💳 Pay via UPI", "success"),
        ("preset_amount", "₹100 / ₹500 / ₹1000 / ₹2000 buttons", "primary"),
        ("custom_amount", "✏️ Custom Amount", "primary"),
        ("np_digit", "Number pad digits 0-9", ""),
        ("np_clear", "Number pad: C (clear)", "danger"),
        ("np_backspace", "Number pad: ⌫ (backspace)", ""),
        ("np_confirm", "Number pad: ✅ Confirm", "success"),
        ("paid", "✅ I have paid", "success"),
        ("cancel_payment", "❌ Cancel Payment", "danger"),
        ("paid_not_credited", "💬 I paid but wasn't credited", "primary"),
    ]),
    ("👤 Profile, Reseller & Support", [
        ("profile_upgrade", "👑 Upgrade To Reseller", "success"),
        ("reseller_buy", "Buy Reseller Plan", "success"),
        ("reseller_add_balance", "💰 Add Balance (reseller screen)", "success"),
        ("view_profile", "👤 View Profile", "primary"),
        ("support_contact", "💬 Contact Support", "success"),
    ]),
    ("🎰 Lucky Spin", [
        ("spin_now", "🍯 Spin Now! (FREE)", "success"),
        ("spin_shop", "🛒 Shop Now — Use ₹X", "primary"),
    ]),
    ("🔑 After Purchase (key card)", [
        ("update_file", "📥 Update File", "primary"),
    ]),
    ("↩️ Navigation", [
        ("back", "🔜 Back (all Back / Back to Plans buttons)", ""),
        ("back_menu", "➡️ Back to Menu", ""),
    ]),
    ("🛡️ Admin alerts (owner only)", [
        ("admin_approve", "✅ Approve payment", "success"),
        ("admin_reject", "❌ Reject payment", "danger"),
    ]),
]
MENU_BUTTONS = {k: (label, default) for _g, items in BUTTON_GROUPS for k, label, default in items}
BUTTON_COLORS = ("primary", "success", "danger")


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


_pool_lock = threading.Lock()


class _SimplePool:
    """Small thread-safe pool of ready database connections (used when psycopg_pool is not installed).
    Reusing connections instead of opening a new one for every query is the biggest speed-up."""

    def __init__(self, url, size):
        self.url, self.size = url, max(2, int(size))
        self.idle = []                       # stack of (connection, last_used)
        self.cond = threading.Condition()
        self.open = 0

    def _connect(self):
        return psycopg.connect(self.url, connect_timeout=15, row_factory=dict_row)

    def _drop(self, conn):
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
        with self.cond:
            self.open -= 1
            self.cond.notify()

    def get(self):
        deadline = time.time() + 20
        while True:
            conn, ts, fresh = None, 0.0, False
            with self.cond:
                while True:
                    if self.idle:
                        conn, ts = self.idle.pop()
                        break
                    if self.open < self.size:
                        self.open += 1
                        fresh = True
                        break
                    left = deadline - time.time()
                    if left <= 0:
                        raise RuntimeError("Database busy: no free connection")
                    self.cond.wait(left)
            if fresh:
                try:
                    return self._connect()
                except Exception:
                    self._drop(None)
                    raise
            if conn.closed or getattr(conn, "broken", False) or time.time() - ts > 240:
                self._drop(conn)
                continue
            if time.time() - ts > 15:        # idle for a while: make sure the server still answers
                try:
                    conn.execute("SELECT 1")
                    conn.rollback()
                except Exception:
                    self._drop(conn)
                    continue
            return conn

    def put(self, conn):
        try:
            if conn.closed or getattr(conn, "broken", False):
                return self._drop(conn)
            if conn.info.transaction_status != psycopg.pq.TransactionStatus.IDLE:
                conn.rollback()
        except Exception:
            return self._drop(conn)
        with self.cond:
            self.idle.append((conn, time.time()))
            self.cond.notify()


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
    def _get_pool(self):
        """One shared pool of ready connections (psycopg_pool if installed, otherwise our own)."""
        pool = getattr(self, "_pool", None)
        if pool is not None:
            return pool
        with _pool_lock:
            pool = getattr(self, "_pool", None)
            if pool is None:
                if ConnectionPool is not None:
                    pool = ConnectionPool(
                        self.database_url,
                        min_size=1,
                        max_size=int(os.getenv("DB_POOL_MAX", "6")),
                        kwargs={"connect_timeout": 15, "row_factory": dict_row, "prepare_threshold": None},
                        check=ConnectionPool.check_connection,
                        max_idle=240,
                        max_lifetime=1800,
                        timeout=20,
                        open=True,
                    )
                else:
                    pool = _SimplePool(self.database_url, int(os.getenv("DB_POOL_MAX", "8")))
                self._pool = pool
        return pool

    @contextmanager
    def connection(self):
        pool = self._get_pool()
        if isinstance(pool, _SimplePool):
            conn = pool.get()
            try:
                yield conn
                conn.commit()
            except BaseException:
                try:
                    conn.rollback()
                except Exception:
                    pass
                raise
            finally:
                pool.put(conn)
            return
        with pool.connection() as conn:       # psycopg_pool: commits on success, rolls back on error
            yield conn

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
            "ALTER TABLE gateway_config ADD COLUMN IF NOT EXISTS gmail_user TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE gateway_config ADD COLUMN IF NOT EXISTS gmail_app_password TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE gateway_config ADD COLUMN IF NOT EXISTS bank_domain TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE gateway_config ADD COLUMN IF NOT EXISTS payee_name TEXT NOT NULL DEFAULT 'Elite Hackers Store'",
            "ALTER TABLE gateway_config ADD COLUMN IF NOT EXISTS auto_verify INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE gateway_config ADD COLUMN IF NOT EXISTS webhook_secret TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE gateway_config ADD COLUMN IF NOT EXISTS supplier_url TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE gateway_config ADD COLUMN IF NOT EXISTS supplier_key TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE payment_records ADD COLUMN IF NOT EXISTS pay_amount DOUBLE PRECISION",
            "ALTER TABLE payment_records ADD COLUMN IF NOT EXISTS coupon_code TEXT",
            "ALTER TABLE payment_records ADD COLUMN IF NOT EXISTS discount DOUBLE PRECISION NOT NULL DEFAULT 0",
            "ALTER TABLE gateway_config ADD COLUMN IF NOT EXISTS binance_enabled INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE gateway_config ADD COLUMN IF NOT EXISTS binance_pay_id TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE gateway_config ADD COLUMN IF NOT EXISTS binance_rate DOUBLE PRECISION NOT NULL DEFAULT 90",
            "ALTER TABLE gateway_config ADD COLUMN IF NOT EXISTS binance_note TEXT NOT NULL DEFAULT ''",
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
            # --- Button colors chosen in the admin panel ---
            """
            CREATE TABLE IF NOT EXISTS button_styles (
                btn_key TEXT PRIMARY KEY,
                style TEXT NOT NULL DEFAULT ''
            )
            """,
            "ALTER TABLE button_styles ADD COLUMN IF NOT EXISTS emoji_id TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS lang TEXT NOT NULL DEFAULT 'en'",
            # --- Admin panel upgrades: store settings, spin-reward expiry, session control ---
            "ALTER TABLE store_settings ADD COLUMN IF NOT EXISTS shop_name TEXT",
            "ALTER TABLE store_settings ADD COLUMN IF NOT EXISTS tagline TEXT",
            "ALTER TABLE store_settings ADD COLUMN IF NOT EXISTS update_link TEXT",
            "ALTER TABLE store_settings ADD COLUMN IF NOT EXISTS min_deposit DOUBLE PRECISION",
            "ALTER TABLE store_settings ADD COLUMN IF NOT EXISTS max_deposit DOUBLE PRECISION",
            "ALTER TABLE store_settings ADD COLUMN IF NOT EXISTS qr_expiry_minutes INTEGER",
            "ALTER TABLE store_settings ADD COLUMN IF NOT EXISTS referral_bonus DOUBLE PRECISION",
            "ALTER TABLE store_settings ADD COLUMN IF NOT EXISTS spin_min DOUBLE PRECISION",
            "ALTER TABLE store_settings ADD COLUMN IF NOT EXISTS spin_max DOUBLE PRECISION",
            "ALTER TABLE store_settings ADD COLUMN IF NOT EXISTS spin_cooldown_hours DOUBLE PRECISION",
            "ALTER TABLE store_settings ADD COLUMN IF NOT EXISTS spin_expiry_hours DOUBLE PRECISION",
            "ALTER TABLE admin_auth ADD COLUMN IF NOT EXISTS session_version INTEGER NOT NULL DEFAULT 1",
            "ALTER TABLE order_history ADD COLUMN IF NOT EXISTS created_ts TIMESTAMPTZ",
            "ALTER TABLE order_history ALTER COLUMN created_ts SET DEFAULT now()",
            """
            CREATE TABLE IF NOT EXISTS spin_rewards (
                id BIGSERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                amount DOUBLE PRECISION NOT NULL DEFAULT 0,
                created_ts TIMESTAMPTZ NOT NULL DEFAULT now(),
                expires_ts TIMESTAMPTZ NOT NULL,
                status TEXT NOT NULL DEFAULT 'active'
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_spin_rewards_open ON spin_rewards(status, expires_ts)",
            """
            CREATE TABLE IF NOT EXISTS broadcast_history (
                id BIGSERIAL PRIMARY KEY,
                created_ts TIMESTAMPTZ NOT NULL DEFAULT now(),
                audience TEXT,
                media_type TEXT,
                preview TEXT,
                total INTEGER NOT NULL DEFAULT 0,
                sent INTEGER NOT NULL DEFAULT 0,
                failed INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'running'
            )
            """,
            # --- Admin panel live wallpaper (video + song) ---
            """
            CREATE TABLE IF NOT EXISTS ui_settings (
                skey TEXT PRIMARY KEY,
                svalue TEXT NOT NULL DEFAULT ''
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS ui_media (
                kind TEXT PRIMARY KEY,
                filename TEXT,
                mime TEXT,
                data BYTEA NOT NULL,
                updated_ts TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_order_history_created ON order_history(created_ts)",
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
            """
            CREATE TABLE IF NOT EXISTS supplier_map (
                prod_key TEXT NOT NULL,
                plan TEXT NOT NULL,
                pid INTEGER NOT NULL,
                PRIMARY KEY (prod_key, plan)
            )
            """,
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
        self.bump_session_version()

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

    def activate_reseller(self, user_id, min_balance, fee):
        """Make the user a Reseller and take the fee in ONE transaction (row locked), so tapping the
        button twice - or at the same time - can never charge twice.
        Returns (status, balance): 'ok' | 'already' | 'low' | 'no_user'."""
        required = max(float(min_balance), float(fee))
        with self.connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT account_type, wallet_balance FROM users WHERE user_id = %s FOR UPDATE", (user_id,))
                row = cur.fetchone()
                if not row:
                    return "no_user", 0.0
                balance = float(row["wallet_balance"] or 0)
                if row["account_type"] == "Reseller":
                    return "already", balance
                if balance < required:
                    return "low", balance
                cur.execute(
                    "UPDATE users SET wallet_balance = wallet_balance - %s, account_type = 'Reseller' "
                    "WHERE user_id = %s RETURNING wallet_balance", (float(fee), user_id))
                return "ok", float(cur.fetchone()["wallet_balance"])

    _lang_cache = {}
    LANGS = ("en", "hi", "te", "ta", "ml")

    def get_user_lang(self, user_id):
        """'en' | 'hi' | 'te' | 'ta' | 'ml' (cached; 'en' when unknown or on any error)."""
        try:
            uid = int(user_id)
        except (TypeError, ValueError):
            return "en"
        if uid <= 0:
            return "en"
        lang = self._lang_cache.get(uid)
        if lang is None:
            try:
                row = self.fetchone("SELECT lang FROM users WHERE user_id = %s", (uid,))
                lang = (row or {}).get("lang") or "en"
            except Exception:
                return "en"
            if lang not in self.LANGS:
                lang = "en"
            if len(self._lang_cache) > 50000:
                self._lang_cache.clear()
            self._lang_cache[uid] = lang
        return lang

    def set_user_lang(self, user_id, lang):
        if lang not in self.LANGS:
            raise ValueError("Unknown language")
        self.execute("UPDATE users SET lang = %s WHERE user_id = %s", (lang, int(user_id)))
        self._lang_cache[int(user_id)] = lang

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
    # Store settings (editable from the admin panel, read live by the bot)
    # ------------------------------------------------------------------
    SETTING_DEFAULTS = {
        "shop_name": "Elite Hackers Store",
        "tagline": "",
        "update_link": "",
        "min_deposit": 10.0,
        "max_deposit": 50000.0,
        "qr_expiry_minutes": 5,
        "referral_bonus": 0.0,
        "spin_min": 2.0,
        "spin_max": 10.0,
        "spin_cooldown_hours": 24.0,
        "spin_expiry_hours": 1.0,   # 0 = spin cash never expires
    }

    def get_settings(self):
        """All store settings with defaults filled in for anything not saved yet."""
        row = self.fetchone(
            """
            SELECT support_username, how_to_use_link, shop_name, tagline, update_link,
                   min_deposit, max_deposit, qr_expiry_minutes, referral_bonus,
                   spin_min, spin_max, spin_cooldown_hours, spin_expiry_hours
            FROM store_settings WHERE id = 1
            """
        ) or {}
        cfg = dict(self.SETTING_DEFAULTS)
        cfg["support_username"] = row.get("support_username") or ""
        cfg["how_to_use_link"] = row.get("how_to_use_link") or ""
        for key in self.SETTING_DEFAULTS:
            value = row.get(key)
            if value is None or (isinstance(value, str) and value == "" and key not in ("tagline", "update_link")):
                continue
            cfg[key] = value
        return cfg

    def save_branding(self, shop_name, tagline, support_username, update_link, how_to_use_link):
        self.execute(
            """
            UPDATE store_settings SET shop_name = %s, tagline = %s, support_username = %s,
                update_link = %s, how_to_use_link = %s
            WHERE id = 1
            """,
            (shop_name, tagline, support_username, update_link, how_to_use_link),
        )

    def save_limits(self, min_deposit, max_deposit, qr_expiry_minutes, referral_bonus,
                    spin_min, spin_max, spin_cooldown_hours, spin_expiry_hours):
        self.execute(
            """
            UPDATE store_settings SET min_deposit = %s, max_deposit = %s, qr_expiry_minutes = %s,
                referral_bonus = %s, spin_min = %s, spin_max = %s,
                spin_cooldown_hours = %s, spin_expiry_hours = %s
            WHERE id = 1
            """,
            (min_deposit, max_deposit, qr_expiry_minutes, referral_bonus,
             spin_min, spin_max, spin_cooldown_hours, spin_expiry_hours),
        )

    # ------------------------------------------------------------------
    # Spin-reward expiry: the cash won must be used for a purchase within N hours
    # ------------------------------------------------------------------
    def add_spin_reward(self, user_id, amount, expiry_hours):
        self.execute(
            """
            INSERT INTO spin_rewards (user_id, amount, expires_ts)
            VALUES (%s, %s, now() + (%s * INTERVAL '1 hour'))
            """,
            (user_id, amount, expiry_hours),
        )

    def mark_spin_rewards_used(self, user_id):
        """The customer bought something in time: their active spin cash is kept for good."""
        return self.execute(
            "UPDATE spin_rewards SET status = 'used' WHERE user_id = %s AND status = 'active'",
            (user_id,),
        )

    def claim_expired_spin_rewards(self, limit=50):
        """Atomically close every spin reward whose time ran out and return them."""
        return self.fetchall(
            """
            UPDATE spin_rewards SET status = 'expired'
            WHERE id IN (
                SELECT id FROM spin_rewards
                WHERE status = 'active' AND expires_ts <= now()
                ORDER BY id LIMIT %s
                FOR UPDATE SKIP LOCKED
            )
            RETURNING id, user_id, amount
            """,
            (limit,),
        )

    def take_back_spin_cash(self, user_id, amount):
        """Remove expired spin cash from the wallet (never more than the balance). Returns the amount removed."""
        row = self.fetchone(
            """
            WITH cur AS (SELECT wallet_balance AS bal FROM users WHERE user_id = %s FOR UPDATE)
            UPDATE users SET wallet_balance = users.wallet_balance - LEAST(%s::double precision, cur.bal)
            FROM cur WHERE users.user_id = %s
            RETURNING LEAST(%s::double precision, cur.bal) AS taken, users.wallet_balance AS new_balance
            """,
            (user_id, amount, user_id, amount),
        )
        return (float(row["taken"]), float(row["new_balance"])) if row else (0.0, 0.0)

    def active_spin_reward(self, user_id):
        return self.fetchone(
            """
            SELECT amount, EXTRACT(EPOCH FROM (expires_ts - now())) AS secs
            FROM spin_rewards WHERE user_id = %s AND status = 'active' AND expires_ts > now()
            ORDER BY id DESC LIMIT 1
            """,
            (user_id,),
        )

    # ------------------------------------------------------------------
    # Admin sessions ("Logout all devices")
    # ------------------------------------------------------------------
    def get_session_version(self):
        row = self.fetchone("SELECT session_version FROM admin_auth WHERE id = 1")
        return int(row["session_version"]) if row and row["session_version"] is not None else 1

    def bump_session_version(self):
        row = self.fetchone(
            "UPDATE admin_auth SET session_version = session_version + 1 WHERE id = 1 RETURNING session_version"
        )
        return int(row["session_version"]) if row else 1

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
    # Button colors (set from the admin panel)
    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    # Supplier (other reseller panel) PID mapping: our product+plan -> their PID
    # ------------------------------------------------------------------
    def get_supplier_config(self):
        return self.fetchone("SELECT supplier_url, supplier_key FROM gateway_config WHERE id = 1") or {}

    def set_supplier_config(self, url, key=None):
        """key=None keeps the saved key."""
        if key is None:
            self.execute("UPDATE gateway_config SET supplier_url = %s WHERE id = 1", (url,))
        else:
            self.execute("UPDATE gateway_config SET supplier_url = %s, supplier_key = %s WHERE id = 1", (url, key))

    def get_supplier_pid(self, prod_key, plan):
        row = self.fetchone("SELECT pid FROM supplier_map WHERE prod_key = %s AND plan = %s", (prod_key, plan))
        return int(row["pid"]) if row else None

    def set_supplier_pid(self, prod_key, plan, pid):
        self.execute(
            "INSERT INTO supplier_map (prod_key, plan, pid) VALUES (%s, %s, %s) "
            "ON CONFLICT (prod_key, plan) DO UPDATE SET pid = EXCLUDED.pid",
            (prod_key, plan, int(pid)),
        )

    def delete_supplier_pid(self, prod_key, plan):
        self.execute("DELETE FROM supplier_map WHERE prod_key = %s AND plan = %s", (prod_key, plan))

    def list_supplier_map(self):
        return self.fetchall("SELECT prod_key, plan, pid FROM supplier_map ORDER BY prod_key, plan")

    def get_button_styles(self):
        """{btn_key: style} where style is 'primary' | 'success' | 'danger' | 'none'."""
        rows = self.fetchall("SELECT btn_key, style FROM button_styles")
        return {r["btn_key"]: r["style"] for r in rows}

    def get_button_looks(self):
        """{btn_key: (style, emoji_id)} - color and premium (custom) emoji chosen in the admin panel."""
        rows = self.fetchall("SELECT btn_key, style, emoji_id FROM button_styles")
        return {r["btn_key"]: (r["style"] or "", r["emoji_id"] or "") for r in rows}

    def set_button_look(self, btn_key, style=None, emoji_id=None):
        """Save color and/or premium emoji id of one button (None = keep what is saved)."""
        self.execute(
            """
            INSERT INTO button_styles (btn_key, style, emoji_id) VALUES (%s, COALESCE(%s, ''), COALESCE(%s, ''))
            ON CONFLICT (btn_key) DO UPDATE SET
                style = COALESCE(%s, button_styles.style),
                emoji_id = COALESCE(%s, button_styles.emoji_id)
            """,
            (btn_key, style, emoji_id, style, emoji_id),
        )

    def set_button_style(self, btn_key, style):
        self.execute(
            """
            INSERT INTO button_styles (btn_key, style) VALUES (%s, %s)
            ON CONFLICT (btn_key) DO UPDATE SET style = EXCLUDED.style
            """,
            (btn_key, style),
        )


    # ------------------------------------------------------------------
    # Top 5 sellers of the month (customers who bought the most keys, Indian time)
    # ------------------------------------------------------------------
    @staticmethod
    def _month_where():
        """SQL (+ params) selecting delivered orders of the current month in IST.
        Old rows without created_ts are matched by their IST text timestamp."""
        from datetime import datetime, timezone, timedelta
        now_ist = datetime.now(timezone(timedelta(hours=5, minutes=30)))
        like = "% " + now_ist.strftime("%b %Y") + ",%"
        sql = (
            "LEFT(COALESCE(o.key_delivered, ''), 7) <> 'PENDING' AND o.amount > 0 AND ("
            "(o.created_ts IS NOT NULL AND (o.created_ts AT TIME ZONE 'Asia/Kolkata') >= "
            "date_trunc('month', now() AT TIME ZONE 'Asia/Kolkata')) "
            "OR (o.created_ts IS NULL AND o.timestamp LIKE %s))"
        )
        return sql, [like]

    def get_top_sellers_monthly(self, limit=5):
        where, params = self._month_where()
        return self.fetchall(
            f"""
            SELECT u.user_id, u.full_name, u.account_type,
                   COUNT(*) AS keys_count, COALESCE(SUM(o.amount), 0) AS total
            FROM order_history o JOIN users u ON u.user_id = o.user_id
            WHERE {where}
            GROUP BY u.user_id, u.full_name, u.account_type
            ORDER BY total DESC, keys_count DESC, u.user_id
            LIMIT %s
            """,
            params + [limit],
        )

    def get_user_monthly_rank(self, user_id):
        """(rank, keys, total) of one customer this month, or None if they bought nothing yet."""
        where, params = self._month_where()
        return self.fetchone(
            f"""
            SELECT * FROM (
                SELECT user_id, keys_count, total, RANK() OVER (ORDER BY total DESC) AS rnk FROM (
                    SELECT o.user_id, COUNT(*) AS keys_count, COALESCE(SUM(o.amount), 0) AS total
                    FROM order_history o WHERE {where} GROUP BY o.user_id
                ) m
            ) r WHERE user_id = %s
            """,
            params + [user_id],
        )

    # ------------------------------------------------------------------
    # Admin panel live wallpaper (background video + looping song)
    # ------------------------------------------------------------------
    BG_DEFAULTS = {"video_url": "", "audio_url": "", "volume": "0.6", "dim": "0.45", "enabled": "1"}

    def get_ui_settings(self):
        rows = self.fetchall("SELECT skey, svalue FROM ui_settings")
        cfg = dict(self.BG_DEFAULTS)
        cfg.update({r["skey"]: r["svalue"] for r in rows})
        return cfg

    def set_ui_settings(self, values):
        with self.connection() as conn:
            with conn.cursor() as cur:
                for k, v in values.items():
                    cur.execute(
                        """
                        INSERT INTO ui_settings (skey, svalue) VALUES (%s, %s)
                        ON CONFLICT (skey) DO UPDATE SET svalue = EXCLUDED.svalue
                        """,
                        (k, str(v)),
                    )

    def save_media(self, kind, filename, mime, data):
        self.execute(
            """
            INSERT INTO ui_media (kind, filename, mime, data, updated_ts) VALUES (%s, %s, %s, %s, now())
            ON CONFLICT (kind) DO UPDATE SET filename = EXCLUDED.filename, mime = EXCLUDED.mime,
                data = EXCLUDED.data, updated_ts = now()
            """,
            (kind, filename, mime, data),
        )

    def media_meta(self, kind):
        return self.fetchone(
            "SELECT filename, mime, octet_length(data) AS size, EXTRACT(EPOCH FROM updated_ts)::bigint AS ver "
            "FROM ui_media WHERE kind = %s",
            (kind,),
        )

    def media_chunk(self, kind, start, length):
        """Bytes [start, start+length) of the stored file (so videos can seek without loading it all)."""
        row = self.fetchone(
            "SELECT substring(data FROM %s FOR %s) AS chunk FROM ui_media WHERE kind = %s",
            (start + 1, length, kind),
        )
        return bytes(row["chunk"]) if row and row["chunk"] is not None else b""

    def delete_media(self, kind):
        self.execute("DELETE FROM ui_media WHERE kind = %s", (kind,))

    # ------------------------------------------------------------------
    # Payments
    # ------------------------------------------------------------------
    def get_gmail_config(self):
        """Gmail address, App Password and the bank's sender domain (set in the admin panel)."""
        row = self.fetchone("SELECT gmail_user, gmail_app_password, bank_domain FROM gateway_config WHERE id = 1")
        return row or {"gmail_user": "", "gmail_app_password": "", "bank_domain": ""}

    def set_gmail_config(self, gmail_user, app_password, bank_domain):
        """app_password=None keeps the saved one."""
        self.execute(
            """
            UPDATE gateway_config
            SET gmail_user = %s,
                gmail_app_password = COALESCE(%s, gmail_app_password),
                bank_domain = %s
            WHERE id = 1
            """,
            (gmail_user, app_password, bank_domain),
        )

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

    def get_binance_settings(self):
        row = self.fetchone(
            "SELECT binance_enabled, binance_pay_id, binance_rate, binance_note FROM gateway_config WHERE id = 1"
        ) or {}
        return {
            "enabled": bool(row.get("binance_enabled")),
            "pay_id": (row.get("binance_pay_id") or "").strip(),
            "rate": float(row.get("binance_rate") or 90),
            "note": (row.get("binance_note") or "").strip(),
        }

    def set_binance_settings(self, enabled, pay_id, rate, note):
        self.execute(
            "UPDATE gateway_config SET binance_enabled = %s, binance_pay_id = %s, binance_rate = %s, binance_note = %s WHERE id = 1",
            (1 if enabled else 0, pay_id, rate, note),
        )

    def get_coupon(self, code):
        return self.fetchone(
            "SELECT * FROM coupons WHERE UPPER(code) = UPPER(%s) AND is_active = 1 "
            "AND (usage_limit IS NULL OR used_count < usage_limit)", (code,))

    def use_coupon(self, code):
        self.execute("UPDATE coupons SET used_count = used_count + 1 WHERE UPPER(code) = UPPER(%s)", (code,))

    def set_payment_coupon(self, order_id, code, discount):
        self.execute("UPDATE payment_records SET coupon_code = %s, discount = %s WHERE order_id = %s",
                     (code, discount, order_id))

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
              AND COALESCE(method, '') <> 'binance'
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


# ----------------------------------------------------------------------
# 🔎 Automatic button discovery
# Every button written in main.py as styled_button("Label", "key", ...) / tg_button(...) is picked up
# here (at start-up and again every 5 seconds if main.py changed), and shows up in the admin panel's
# "Button Colors" page by itself. BUTTON_GROUPS and MENU_BUTTONS are updated IN PLACE, so web_admin.py
# and main.py (which imported them) see the new buttons with no change to those files.
# ----------------------------------------------------------------------
AUTO_GROUP_TITLE = "🆕 Other buttons (found automatically)"
_STATIC_KEYS = set(MENU_BUTTONS)
_BTN_RE = re.compile(r"""(?:styled_button|tg_button)\(\s*f?(["'])(.+?)\1\s*,\s*(["'])([A-Za-z0-9_]+)\3""")
_btn_state = {"mtime": None}

def _clean_label(label):
    label = re.sub(r"\{[^}]*\}", "X", label)          # f-string parts like {amount}
    return " ".join(label.replace("\\n", " ").split())[:60] or "Button"

def refresh_buttons(path=None):
    """Re-read main.py when it changed and register any button key that is not listed above."""
    path = path or os.path.join(os.path.dirname(os.path.abspath(__file__)), "main.py")
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return False
    if _btn_state["mtime"] == mtime:
        return False
    try:
        with open(path, encoding="utf-8", errors="ignore") as fh:
            text = fh.read()
    except OSError:
        return False
    found, seen = [], set()
    for m in _BTN_RE.finditer(text):
        key = m.group(4)
        if key in _STATIC_KEYS or key in seen:
            continue
        seen.add(key)
        found.append((key, _clean_label(m.group(2)), ""))
    # drop the previous automatic group, then add the fresh one
    for key in [k for k in MENU_BUTTONS if k not in _STATIC_KEYS]:
        MENU_BUTTONS.pop(key, None)
    BUTTON_GROUPS[:] = [g for g in BUTTON_GROUPS if g[0] != AUTO_GROUP_TITLE]
    if found:
        BUTTON_GROUPS.append((AUTO_GROUP_TITLE, found))
        for key, label, default in found:
            MENU_BUTTONS[key] = (label, default)
    _btn_state["mtime"] = mtime
    return True

def _button_watcher():
    while True:
        try:
            refresh_buttons()
        except Exception:
            logger.exception("Button discovery failed")
        time.sleep(5)

refresh_buttons()
threading.Thread(target=_button_watcher, name="button-watcher", daemon=True).start()


db = Database(DATABASE_URL) if DATABASE_URL else None


def init_db():
    if db is None:
        raise RuntimeError(
            "DATABASE_URL is not configured. Set it in Render Environment Variables."
        )
    db.init_db()
    try:
        _x_start()
        _x_ensure_triggers()      # tables exist now (first deploy): attach the change hooks
    except Exception:
        logger.exception("Change hooks could not be installed")
    return db


# ======================================================================
# LIVE EXTENSIONS - everything below works from this one file
#   1. Change hook: every database write bumps a counter; each process checks it every 5 s
#   2. Bot maintenance mode (switch in the admin panel, enforced inside the Telegram bot)
#   3. Theme studio (crown button) + maintenance button injected into the web admin page
#   4. Speed: gzip for every admin page / API answer
# ======================================================================
import sys
import gzip

_X_DEFAULT_TEXT = "🛠 The bot is under maintenance. Please try again later."
_X = {"rev": 0, "maint": False, "notice": False, "text": _X_DEFAULT_TEXT, "theme": {}, "started": False}
_x_hooks = []
_x_notice_seen = {}

_X_THEME_KEYS = {
    "p": {"default", "ocean", "emerald", "sunset", "crimson", "gold", "neon", "cyber", "royal", "amoled", "forest"},
    "b": {"gradient", "solid", "outline", "neon", "glass", "pill"},
    "c": {"glass", "solid", "neon", "soft", "sharp"},
    "bg": {"default", "aurora", "stars", "matrix", "bubbles", "pulse", "plain"},
    "m": {"off", "robot", "rocket"},
    "r": {"s", "m", "l"},
    "f": {"s", "n", "l"},
}


def on_db_change(fn):
    """Register fn(rev) - called (within ~5 s) whenever ANY process changed the database."""
    _x_hooks.append(fn)


def bot_maintenance_on():
    return bool(_X["maint"])


# ---------------------------------------------------------------- storage
def _x_ensure_schema():
    db.execute("CREATE TABLE IF NOT EXISTS x_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL DEFAULT '')")
    db.execute("CREATE SEQUENCE IF NOT EXISTS x_rev_seq")
    try:
        db.execute(
            "CREATE OR REPLACE FUNCTION x_bump_rev() RETURNS trigger AS $$ "
            "BEGIN PERFORM nextval('x_rev_seq'); RETURN NULL; END; $$ LANGUAGE plpgsql"
        )
    except Exception:
        logger.debug("x_bump_rev function already being created by another process")


def _x_ensure_triggers():
    """One tiny statement-level trigger per table: any insert/update/delete moves the change counter."""
    rows = db.fetchall(
        "SELECT c.relname AS t FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = 'public' AND c.relkind = 'r'"
    )
    have = {r["t"] for r in db.fetchall(
        "SELECT c.relname AS t FROM pg_trigger g JOIN pg_class c ON c.oid = g.tgrelid "
        "WHERE g.tgname = 'x_rev_trg' AND NOT g.tgisinternal"
    )}
    for r in rows:
        t = r["t"]
        if t == "x_settings" or t in have:
            continue
        ident = 'public."' + t.replace('"', '""') + '"'
        for proc in ("FUNCTION", "PROCEDURE"):
            try:
                db.execute(f"CREATE TRIGGER x_rev_trg AFTER INSERT OR UPDATE OR DELETE ON {ident} "
                           f"FOR EACH STATEMENT EXECUTE {proc} x_bump_rev()")
                break
            except Exception as e:
                logger.debug("trigger on %s (%s) failed: %s", t, proc, e)


def _x_set(key, value):
    db.execute(
        "INSERT INTO x_settings (key, value) VALUES (%s, %s) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", (key, value))


def _x_load():
    kv = {r["key"]: r["value"] for r in db.fetchall("SELECT key, value FROM x_settings")}
    row = db.fetchone("SELECT last_value AS v FROM x_rev_seq") or {}
    _X["maint"] = kv.get("maintenance", "0") == "1"
    _X["notice"] = kv.get("maint_notice", "0") == "1"
    _X["text"] = (kv.get("maint_text") or _X_DEFAULT_TEXT)[:200]
    try:
        th = json.loads(kv.get("theme") or "{}")
        _X["theme"] = th if isinstance(th, dict) else {}
    except ValueError:
        _X["theme"] = {}
    new_rev = int(row.get("v") or 0)
    changed = new_rev != _X["rev"]
    _X["rev"] = new_rev
    return changed


def _x_loop():
    n = 0
    while True:
        time.sleep(5)
        try:
            if _x_load():
                for fn in list(_x_hooks):
                    try:
                        fn(_X["rev"])
                    except Exception:
                        logger.exception("db change hook failed")
            n += 1
            if n % 24 == 0:          # every ~2 minutes: pick up tables created later
                _x_ensure_triggers()
        except Exception as e:
            logger.debug("x-sync poll failed: %s", e)


def _x_start():
    if db is None or _X["started"]:
        return
    _X["started"] = True
    try:
        _x_ensure_schema()
        _x_load()
    except Exception:
        logger.exception("Could not prepare live settings")
    try:
        _x_ensure_triggers()
    except Exception:
        logger.exception("Could not install change hooks")
    threading.Thread(target=_x_loop, name="x-sync", daemon=True).start()


def set_bot_maintenance(on, notice=None, text=None):
    on = bool(on)
    _x_set("maintenance", "1" if on else "0")
    _X["maint"] = on
    if notice is not None:
        _x_set("maint_notice", "1" if notice else "0")
        _X["notice"] = bool(notice)
    if text is not None:
        text = (str(text).strip() or _X_DEFAULT_TEXT)[:200]
        _x_set("maint_text", text)
        _X["text"] = text


def _x_clean_theme(d):
    out = {}
    for k, allowed in _X_THEME_KEYS.items():
        v = d.get(k)
        if isinstance(v, str) and v in allowed:
            out[k] = v
    ac = d.get("ac")
    if isinstance(ac, str) and re.fullmatch(r"#[0-9a-fA-F]{6}", ac):
        out["ac"] = ac.lower()
    return out


# ---------------------------------------------------------------- Telegram bot: maintenance guard
def _patch_telegram():
    try:
        from telegram.ext import Application
    except Exception:
        return
    if getattr(Application, "_x_patched", False):
        return
    original = Application.process_update

    async def guarded(self, update):
        if _X["maint"]:
            await _x_maint_reply(update)
            return                      # nothing else runs: no menu, no button, no purchase, no command
        return await original(self, update)

    Application.process_update = guarded
    Application._x_patched = True


async def _x_maint_reply(update):
    """The bot never does any work while maintenance is ON. At most one short notice (only if enabled)."""
    try:
        text = _X["text"] if _X["notice"] else None
        cq = getattr(update, "callback_query", None)
        if cq is not None:
            await cq.answer(text=text, show_alert=bool(text))   # stops the button spinner; shows a popup only if enabled
            return
        if not text:
            return
        chat = getattr(update, "effective_chat", None)
        msg = getattr(update, "effective_message", None)
        user = getattr(update, "effective_user", None)
        if chat is None or msg is None or getattr(chat, "type", "") != "private":
            return
        uid = getattr(user, "id", 0)
        now = time.time()
        if now - _x_notice_seen.get(uid, 0) < 60:
            return
        _x_notice_seen[uid] = now
        if len(_x_notice_seen) > 5000:
            _x_notice_seen.clear()
        await msg.reply_text(text)
    except Exception:
        pass


# ---------------------------------------------------------------- Web admin: routes + page injection
def _x_admin_ok():
    mod = sys.modules.get("web_admin") or sys.modules.get("__main__")
    fn = getattr(mod, "admin_required", None)
    try:
        return bool(fn and fn())
    except Exception:
        return False


def _x_install(app):
    from flask import request, jsonify

    def deny():
        return jsonify({"message": "Unauthorized"}), 401

    def maint_view():
        if not _x_admin_ok():
            return deny()
        if request.method == "POST":
            d = request.get_json(silent=True) or {}
            set_bot_maintenance(bool(d.get("on")), bool(d.get("notice")), d.get("text"))
        return jsonify({"on": _X["maint"], "notice": _X["notice"], "text": _X["text"]})

    def theme_view():
        if request.method == "POST":
            if not _x_admin_ok():
                return deny()
            d = request.get_json(silent=True) or {}
            clean = {} if d.get("reset") else _x_clean_theme(d)
            _x_set("theme", json.dumps(clean))
            _X["theme"] = clean
        return jsonify(_X["theme"])

    def rev_view():
        if not _x_admin_ok():
            return deny()
        return jsonify({"rev": _X["rev"], "maint": 1 if _X["maint"] else 0})

    app.add_url_rule("/_x/maintenance", "x_maintenance", maint_view, methods=["GET", "POST"])
    app.add_url_rule("/_x/theme", "x_theme", theme_view, methods=["GET", "POST"])
    app.add_url_rule("/_x/rev", "x_rev", rev_view, methods=["GET"])

    def after(resp):
        try:
            if resp.status_code != 200 or resp.direct_passthrough:
                return resp
            if resp.mimetype == "text/html" and request.path != "/health":
                dest = request.headers.get("Sec-Fetch-Dest", "document")
                if dest in ("document", "iframe"):
                    body = resp.get_data(as_text=True)
                    if "</head>" in body:
                        resp.set_data(body.replace("</head>", _x_snippet() + "</head>", 1))
            if ("gzip" in request.headers.get("Accept-Encoding", "")
                    and "Content-Encoding" not in resp.headers
                    and resp.mimetype in ("text/html", "text/css", "application/json", "application/javascript", "text/javascript")):
                data = resp.get_data()
                if len(data) > 1200:
                    packed = gzip.compress(data, 5)
                    resp.set_data(packed)
                    resp.headers["Content-Encoding"] = "gzip"
                    resp.headers["Content-Length"] = str(len(packed))
                    resp.headers.add("Vary", "Accept-Encoding")
        except Exception:
            logger.exception("admin page extension failed")
        return resp

    app.after_request(after)


def _x_snippet():
    cfg = {"theme": _X["theme"]}
    if _x_admin_ok():
        cfg["maint"] = {"on": _X["maint"], "notice": _X["notice"], "text": _X["text"]}
    data = json.dumps(cfg).replace("</", "<\\/")
    return "<script>window.__XCFG=" + data + ";</script><script>" + _X_JS + "</script>"


def _patch_flask():
    try:
        import flask
    except Exception:
        return
    if getattr(flask.Flask, "_x_patched", False):
        return
    original = flask.Flask.__init__

    def init(self, *args, **kwargs):
        original(self, *args, **kwargs)
        try:
            name = str(getattr(self, "import_name", "")).split(".")[0]
            if name == "web_admin" or (name == "__main__" and os.path.basename(sys.argv[0]) == "web_admin.py"):
                _x_install(self)
        except Exception:
            logger.exception("Could not attach admin extensions")

    flask.Flask.__init__ = init
    flask.Flask._x_patched = True


_X_JS = r'''(function () {
  if (window.__xtLoaded) return;
  window.__xtLoaded = 1;
  var CFG = window.__XCFG || {};

  /* ---------- palettes ---------- */
  var PRESETS = {
    ocean:   {n: 'Ocean',   a: '#38bdf8', b: '#6366f1', bg: ['#04121f', '#071d33', '#020a14'], card: '#0a2236', txt: '#e8f6ff', soft: '#a9c4d9'},
    emerald: {n: 'Emerald', a: '#10b981', b: '#14b8a6', bg: ['#03140f', '#06251c', '#020a07'], card: '#0a2a20', txt: '#eafff6', soft: '#a7d4c3'},
    sunset:  {n: 'Sunset',  a: '#f97316', b: '#ec4899', bg: ['#1c0a06', '#331208', '#0d0503'], card: '#2c120c', txt: '#fff1ea', soft: '#e0b9a8'},
    crimson: {n: 'Crimson', a: '#ef4444', b: '#f43f5e', bg: ['#1a0507', '#2e0a0f', '#0b0203'], card: '#2a0c10', txt: '#ffecee', soft: '#dba9ae'},
    gold:    {n: 'Gold',    a: '#f59e0b', b: '#eab308', bg: ['#171004', '#2a1d08', '#0a0702'], card: '#261b08', txt: '#fff8e6', soft: '#d9c79a'},
    neon:    {n: 'Neon pink', a: '#ec4899', b: '#a855f7', bg: ['#1a0516', '#2d0a28', '#0a020a'], card: '#280a24', txt: '#ffeefa', soft: '#dbaed0'},
    cyber:   {n: 'Cyber',   a: '#22d3ee', b: '#a3e635', bg: ['#021417', '#052a30', '#010809'], card: '#062a30', txt: '#e6fdff', soft: '#9fd3d9'},
    royal:   {n: 'Royal',   a: '#3b82f6', b: '#8b5cf6', bg: ['#060b24', '#0c1745', '#03051a'], card: '#0d1840', txt: '#eaf0ff', soft: '#adb9de'},
    amoled:  {n: 'AMOLED black', a: '#a78bfa', b: '#60a5fa', bg: ['#000000', '#050505', '#000000'], card: '#0b0b0b', txt: '#f4f4f5', soft: '#a1a1aa'},
    forest:  {n: 'Forest',  a: '#84cc16', b: '#22c55e', bg: ['#07120a', '#0d2412', '#030a05'], card: '#0e2615', txt: '#f0ffe8', soft: '#b3d4a6'}
  };
  var OPT = {
    p:  [['default', 'Default']].concat(Object.keys(PRESETS).map(function (k) { return [k, PRESETS[k].n]; })),
    b:  [['gradient', 'Gradient'], ['solid', 'Solid'], ['outline', 'Outline'], ['neon', 'Neon glow'], ['glass', 'Glass'], ['pill', 'Pill']],
    c:  [['glass', 'Glass'], ['solid', 'Solid'], ['neon', 'Neon edge'], ['soft', 'Soft'], ['sharp', 'Sharp']],
    bg: [['default', 'Default grid'], ['aurora', 'Aurora'], ['stars', 'Stars'], ['matrix', 'Matrix rain'], ['bubbles', 'Bubbles'], ['pulse', 'Moving grid'], ['plain', 'Plain']],
    m:  [['off', 'None'], ['robot', 'Robot buddy'], ['rocket', 'Rocket']],
    r:  [['s', 'Small'], ['m', 'Normal'], ['l', 'Round']],
    f:  [['s', 'Small'], ['n', 'Normal'], ['l', 'Large']]
  };
  window.__xtOpt = OPT;
  var DEF = {p: 'default', ac: '', b: 'gradient', c: 'glass', bg: 'default', m: 'off', r: 'm', f: 'n'};
  var T = Object.assign({}, DEF, CFG.theme || {});
  var saved = Object.assign({}, T);

  function rgb(h) { h = h.replace('#', ''); return [parseInt(h.substr(0, 2), 16), parseInt(h.substr(2, 2), 16), parseInt(h.substr(4, 2), 16)]; }
  function rgba(h, a) { var c = rgb(h); return 'rgba(' + c[0] + ',' + c[1] + ',' + c[2] + ',' + a + ')'; }
  function shade(h, k) { var c = rgb(h).map(function (v) { return Math.max(0, Math.min(255, Math.round(v * k))); }); return '#' + c.map(function (v) { return ('0' + v.toString(16)).slice(-2); }).join(''); }
  function light(h) { var c = rgb(h).map(function (v) { return Math.round(v + (255 - v) * 0.45); }); return '#' + c.map(function (v) { return ('0' + v.toString(16)).slice(-2); }).join(''); }

  var BTN = '.btn-grad,.qa,.wp-btn';
  var CARD = '.card,.gcard,.stat-card,.quick-box,.hero-card,.ins-box,.mc,.modal-card';

  function buildCss(t) {
    var P = PRESETS[t.p];
    var a = t.ac || (P && P.a) || '#8b5cf6';
    var b = t.ac ? shade(t.ac, 0.65) : ((P && P.b) || '#6366f1');
    var o = [':root{--x-a:' + a + ';--x-b:' + b + ';--x-r:' + (t.r === 's' ? '8px' : (t.r === 'l' ? '34px' : '22px')) + ';}'];
    if (P) {
      o.push(':root{--bg-deep:' + P.bg[0] + ';--card-glass:' + rgba(P.card, 0.95) + ';--neon-border:' + rgba(a, 0.38) + ';--txt:' + P.txt + ';--txt-soft:' + P.soft + ';--bs-body-bg:' + P.bg[0] + ';--bs-body-color:' + P.txt + ';--bs-border-color:' + rgba(a, 0.38) + ';}');
      o.push('body:not(.bg-live){background:radial-gradient(circle at top center,' + P.bg[1] + ' 0%,' + P.bg[0] + ' 60%,' + P.bg[2] + ' 100%) fixed!important}');
      o.push('.sidebar{background:linear-gradient(180deg,' + P.bg[1] + ',' + P.bg[0] + ')!important}');
      o.push('.hero-card{background:linear-gradient(135deg,' + rgba(a, 0.42) + ',' + P.bg[0] + ')!important}');
      o.push('.form-control,.form-select{background-color:' + P.bg[0] + '!important}');
    } else if (t.ac) {
      o.push(':root{--neon-border:' + rgba(a, 0.38) + ';--bs-border-color:' + rgba(a, 0.38) + ';}');
    }
    if (P || t.ac) {
      o.push('.qa{background:linear-gradient(135deg,' + rgba(a, 0.34) + ',' + rgba(b, 0.3) + ')!important;border-color:' + rgba(a, 0.6) + '!important}');
      o.push('.btn-grad{background:linear-gradient(135deg,' + a + ',' + b + ')!important;box-shadow:0 6px 20px ' + rgba(a, 0.4) + '!important}');
      o.push('.stat-card{border-left-color:' + a + '!important}.quick-box{border-top-color:' + a + '!important}.quick-box h5{color:' + light(a) + '!important}');
      o.push('.gcard{--ac:' + a + '!important;border-top-color:' + a + '!important}');
      o.push('.crown-ring{background:linear-gradient(135deg,' + a + ',' + b + ')!important}');
      o.push('body::before{background-image:linear-gradient(' + rgba(a, 0.06) + ' 1px,transparent 1px),linear-gradient(90deg,' + rgba(a, 0.06) + ' 1px,transparent 1px)!important}');
      o.push('.form-control:focus,.form-select:focus{border-color:' + a + '!important;box-shadow:0 0 0 3px ' + rgba(a, 0.2) + '!important}');
    }
    /* cards */
    if (t.r !== 'm') o.push(CARD + '{border-radius:var(--x-r)!important}' + BTN + '{border-radius:calc(var(--x-r) * .6)!important}');
    if (t.c === 'solid') o.push(CARD + '{background:' + (P ? P.card : '#150b32') + '!important;backdrop-filter:none!important}');
    if (t.c === 'neon') o.push(CARD + '{border:1px solid ' + a + '!important;box-shadow:0 0 18px ' + rgba(a, 0.35) + ',inset 0 0 12px ' + rgba(a, 0.12) + '!important}');
    if (t.c === 'soft') o.push(CARD + '{border-color:transparent!important;box-shadow:0 12px 34px rgba(0,0,0,.45)!important}');
    if (t.c === 'sharp') o.push(CARD + '{border-radius:6px!important}');
    /* buttons */
    if (t.b === 'solid') o.push(BTN + '{background:' + a + '!important;box-shadow:none!important;border-color:' + a + '!important}');
    if (t.b === 'outline') o.push(BTN + '{background:transparent!important;box-shadow:none!important;border:2px solid ' + a + '!important}');
    if (t.b === 'neon') o.push(BTN + '{background:' + rgba(a, 0.14) + '!important;border:1px solid ' + a + '!important;box-shadow:0 0 14px ' + rgba(a, 0.7) + ',inset 0 0 8px ' + rgba(a, 0.3) + '!important}');
    if (t.b === 'glass') o.push(BTN + '{background:rgba(255,255,255,.09)!important;backdrop-filter:blur(8px);border:1px solid rgba(255,255,255,.28)!important;box-shadow:none!important}');
    if (t.b === 'pill') o.push(BTN + '{border-radius:999px!important}');
    if (t.f === 's') o.push('html{font-size:14px!important}');
    if (t.f === 'l') o.push('html{font-size:18px!important}');
    if (t.bg === 'plain') o.push('body::before{display:none!important}');
    return o.join('\n');
  }

  var STATIC = [
    '@keyframes xtFloat{0%,100%{transform:translateY(0)}50%{transform:translateY(-7px)}}',
    '@keyframes xtBlink{0%,90%,100%{transform:scaleY(1)}94%{transform:scaleY(.1)}}',
    '@keyframes xtGlow{0%,100%{opacity:.45}50%{opacity:1}}',
    '@keyframes xtWave{0%,100%{transform:rotate(0)}50%{transform:rotate(-28deg)}}',
    '@keyframes xtFlame{0%,100%{transform:scaleY(1)}50%{transform:scaleY(1.45)}}',
    '@keyframes xtRise{0%{transform:translateY(0) scale(.6);opacity:0}15%{opacity:.5}100%{transform:translateY(-110vh) scale(1.1);opacity:0}}',
    '@keyframes xtDrift{0%{transform:translate(-8%,-6%) scale(1)}50%{transform:translate(10%,8%) scale(1.25)}100%{transform:translate(-8%,-6%) scale(1)}}',
    '@keyframes xtGrid{from{background-position:0 0}to{background-position:0 54px}}',
    '#xt-mascot{position:fixed;right:10px;bottom:12px;z-index:30;pointer-events:none;opacity:.95;filter:drop-shadow(0 6px 14px rgba(0,0,0,.5))}',
    '#xt-mascot .fl{animation:xtFloat 3.2s ease-in-out infinite}',
    '#xt-mascot .eye{transform-box:fill-box;transform-origin:center;animation:xtBlink 4.5s infinite}',
    '#xt-mascot .gl{animation:xtGlow 1.6s ease-in-out infinite}',
    '#xt-mascot .arm{transform-box:fill-box;transform-origin:left center;animation:xtWave 1.8s ease-in-out infinite}',
    '#xt-mascot .fire{transform-box:fill-box;transform-origin:top center;animation:xtFlame .35s ease-in-out infinite}',
    '.xt-bub{position:absolute;bottom:-60px;border-radius:50%;border:1px solid var(--x-a);background:radial-gradient(circle at 30% 30%,rgba(255,255,255,.22),transparent 60%);animation:xtRise linear infinite}',
    '.xt-blob{position:absolute;width:55vmax;height:55vmax;border-radius:50%;filter:blur(70px);opacity:.38;animation:xtDrift 18s ease-in-out infinite}',
    '#xt-ov,#xm-ov{position:fixed;inset:0;z-index:5000;background:rgba(3,1,12,.72);display:none;align-items:center;justify-content:center;padding:14px}',
    '.xt-box{width:100%;max-width:520px;max-height:90vh;overflow:auto;background:#120a2c;border:1px solid var(--x-a);border-radius:22px;padding:18px 16px;color:#eef2ff;box-shadow:0 20px 60px rgba(0,0,0,.6)}',
    '.xt-box h5{font-weight:800;margin:0 0 4px}.xt-sub{font-size:.8rem;color:#b4bdd9;margin-bottom:10px}',
    '.xt-g{margin-top:12px}.xt-g>b{display:block;font-size:.78rem;letter-spacing:.05em;text-transform:uppercase;color:#c4b5fd;margin-bottom:6px}',
    '.xt-chips{display:flex;flex-wrap:wrap;gap:6px}',
    '.xt-chip{border:1px solid rgba(255,255,255,.2);background:rgba(255,255,255,.05);color:#fff;border-radius:999px;padding:6px 12px;font-size:.82rem;font-weight:600;display:inline-flex;align-items:center;gap:6px}',
    '.xt-chip.on{border-color:var(--x-a);background:rgba(255,255,255,.16);box-shadow:0 0 0 2px var(--x-a) inset}',
    '.xt-dot{width:14px;height:14px;border-radius:50%;display:inline-block}',
    '.xt-row{display:flex;gap:8px;margin-top:16px;flex-wrap:wrap}.xt-row button{flex:1;min-width:120px}',
    '.xt-btn{border:0;border-radius:14px;padding:11px 14px;font-weight:700;color:#fff;background:linear-gradient(135deg,var(--x-a),var(--x-b))}',
    '.xt-btn.alt{background:rgba(255,255,255,.1);border:1px solid rgba(255,255,255,.25)}',
    '.xm-sw{position:relative;display:inline-block;width:64px;height:34px;flex:none}.xm-sw input{opacity:0;width:0;height:0}',
    '.xm-sw span{position:absolute;inset:0;background:#3b3b55;border-radius:34px;transition:.2s}',
    '.xm-sw span:before{content:"";position:absolute;height:26px;width:26px;left:4px;top:4px;background:#fff;border-radius:50%;transition:.2s}',
    '.xm-sw input:checked+span{background:#ef4444}.xm-sw input:checked+span:before{transform:translateX(30px)}',
    '#xm-banner{position:fixed;top:0;left:0;right:0;z-index:4000;background:#b91c1c;color:#fff;text-align:center;font-weight:800;font-size:.8rem;padding:6px 8px;display:none}',
    'body.xm-on{padding-top:30px}body.xm-on #xm-banner{display:block}',
    '.xm-badge{margin-left:auto;font-size:.72rem;font-weight:800;border-radius:999px;padding:3px 10px;background:#16a34a;color:#fff}.xm-badge.on{background:#dc2626}'
  ].join('\n');

  function el(tag, attrs, html) { var e = document.createElement(tag); if (attrs) for (var k in attrs) e.setAttribute(k, attrs[k]); if (html != null) e.innerHTML = html; return e; }
  function toastMsg(m, ok) { try { if (typeof window.toast === 'function') { window.toast(m, ok !== false); return; } } catch (e) {} alert(m); }
  function post(url, body) {
    return fetch(url, {method: 'POST', credentials: 'same-origin', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)})
      .then(function (r) { return r.json(); }).catch(function () { return {message: 'Network error'}; });
  }

  /* ---------- theme apply ---------- */
  var styleEl = null;
  function applyCss() {
    if (!styleEl) { styleEl = el('style', {id: 'xt-style'}); document.head.appendChild(styleEl); }
    styleEl.textContent = buildCss(T);
  }
  var bgEl = null, raf = 0, resizeFn = null;
  function clearBg() {
    if (raf) cancelAnimationFrame(raf); raf = 0;
    if (resizeFn) { window.removeEventListener('resize', resizeFn); resizeFn = null; }
    if (bgEl) { bgEl.remove(); bgEl = null; }
  }
  function canvasBg(draw) {
    var cv = el('canvas'); bgEl.appendChild(cv);
    var ctx = cv.getContext('2d'), W = 0, H = 0;
    resizeFn = function () { W = cv.width = window.innerWidth; H = cv.height = window.innerHeight; };
    resizeFn(); window.addEventListener('resize', resizeFn);
    var st = {}, last = 0;
    (function loop(ts) { raf = requestAnimationFrame(loop); if (ts - last < 45) return; last = ts; draw(ctx, W, H, st, ts); })(0);
  }
  function applyBg() {
    clearBg();
    var k = T.bg;
    if (k === 'default' || k === 'plain') return;
    bgEl = el('div', {id: 'xt-bg'});
    bgEl.style.cssText = 'position:fixed;inset:0;z-index:0;pointer-events:none;overflow:hidden';
    document.body.appendChild(bgEl);
    var a = getComputedStyle(document.documentElement).getPropertyValue('--x-a').trim() || '#8b5cf6';
    var b = getComputedStyle(document.documentElement).getPropertyValue('--x-b').trim() || '#6366f1';
    if (k === 'aurora') {
      bgEl.innerHTML = '<div class="xt-blob" style="left:-10vmax;top:-12vmax;background:' + a + '"></div><div class="xt-blob" style="right:-14vmax;top:20vh;background:' + b + ';animation-delay:-6s"></div><div class="xt-blob" style="left:12vw;bottom:-22vmax;background:#22d3ee;animation-delay:-11s;opacity:.25"></div>';
    } else if (k === 'bubbles') {
      var h = '';
      for (var i = 0; i < 16; i++) { var s = 14 + Math.random() * 46; h += '<i class="xt-bub" style="left:' + (Math.random() * 100).toFixed(1) + '%;width:' + s.toFixed(0) + 'px;height:' + s.toFixed(0) + 'px;animation-duration:' + (9 + Math.random() * 12).toFixed(1) + 's;animation-delay:-' + (Math.random() * 14).toFixed(1) + 's"></i>'; }
      bgEl.innerHTML = h;
    } else if (k === 'pulse') {
      bgEl.style.backgroundImage = 'linear-gradient(' + rgba(a, 0.22) + ' 2px,transparent 2px),linear-gradient(90deg,' + rgba(a, 0.22) + ' 2px,transparent 2px)';
      bgEl.style.backgroundSize = '54px 54px'; bgEl.style.animation = 'xtGrid 2.4s linear infinite';
    } else if (k === 'stars') {
      canvasBg(function (c, W, H, s, ts) {
        if (!s.p) { s.p = []; for (var i = 0; i < 90; i++) s.p.push({x: Math.random(), y: Math.random(), r: 0.6 + Math.random() * 1.6, o: Math.random() * 6.28, v: 0.8 + Math.random() * 2}); }
        c.clearRect(0, 0, W, H);
        s.p.forEach(function (p) { c.globalAlpha = 0.25 + 0.75 * Math.abs(Math.sin(p.o + ts / 1000 * p.v)); c.fillStyle = '#fff'; c.beginPath(); c.arc(p.x * W, p.y * H, p.r, 0, 6.28); c.fill(); });
        c.globalAlpha = 1;
      });
    } else if (k === 'matrix') {
      canvasBg(function (c, W, H, s) {
        var fs = 15, cols = Math.ceil(W / fs);
        if (!s.d || s.d.length !== cols) { s.d = []; for (var i = 0; i < cols; i++) s.d.push(Math.random() * H / fs); }
        c.globalCompositeOperation = 'destination-out'; c.fillStyle = 'rgba(0,0,0,.14)'; c.fillRect(0, 0, W, H);
        c.globalCompositeOperation = 'source-over'; c.fillStyle = a; c.font = fs + 'px monospace';
        for (var j = 0; j < cols; j++) {
          c.fillText(String.fromCharCode(0x30A0 + Math.floor(Math.random() * 60)), j * fs, s.d[j] * fs);
          if (s.d[j] * fs > H && Math.random() > 0.97) s.d[j] = 0; else s.d[j] += 1;
        }
      });
    }
  }
  var ROBOT = '<svg viewBox="0 0 100 124" width="86" height="106"><g class="fl"><line x1="50" y1="9" x2="50" y2="22" stroke="var(--x-a)" stroke-width="3"/><circle class="gl" cx="50" cy="7" r="5" fill="var(--x-b)"/><rect x="22" y="22" width="56" height="44" rx="14" fill="#1b1340" stroke="var(--x-a)" stroke-width="3"/><g class="eye"><circle cx="39" cy="43" r="7" fill="#fff"/><circle cx="61" cy="43" r="7" fill="#fff"/><circle cx="40" cy="44" r="3" fill="#111"/><circle cx="62" cy="44" r="3" fill="#111"/></g><rect x="40" y="56" width="20" height="4" rx="2" fill="var(--x-a)"/><rect x="28" y="70" width="44" height="34" rx="12" fill="#1b1340" stroke="var(--x-b)" stroke-width="3"/><circle class="gl" cx="50" cy="87" r="6" fill="var(--x-a)"/><rect class="arm" x="72" y="74" width="16" height="8" rx="4" fill="var(--x-b)"/><rect x="12" y="74" width="16" height="8" rx="4" fill="var(--x-b)"/><rect x="34" y="106" width="11" height="9" rx="3" fill="var(--x-a)"/><rect x="55" y="106" width="11" height="9" rx="3" fill="var(--x-a)"/></g></svg>';
  var ROCKET = '<svg viewBox="0 0 80 124" width="62" height="106"><g class="fl"><path d="M40 6 C58 24 60 52 52 78 L28 78 C20 52 22 24 40 6Z" fill="#e5e7ff" stroke="var(--x-a)" stroke-width="3"/><circle cx="40" cy="42" r="9" fill="var(--x-b)" stroke="#fff" stroke-width="2"/><path d="M28 62 L12 84 L28 80Z M52 62 L68 84 L52 80Z" fill="var(--x-a)"/><path class="fire" d="M32 80 L40 112 L48 80Z" fill="#f59e0b"/><path class="fire" d="M36 80 L40 100 L44 80Z" fill="#fde047"/></g></svg>';
  function applyMascot() {
    var old = document.getElementById('xt-mascot'); if (old) old.remove();
    if (T.m === 'off') return;
    document.body.appendChild(el('div', {id: 'xt-mascot'}, T.m === 'rocket' ? ROCKET : ROBOT));
  }
  function applyAll() { applyCss(); applyBg(); applyMascot(); }

  /* ---------- theme panel (crown) ---------- */
  var ov = null;
  function chips(key) {
    return OPT[key].map(function (o) {
      var dot = '';
      if (key === 'p') { var P = PRESETS[o[0]]; dot = '<span class="xt-dot" style="background:' + (P ? 'linear-gradient(135deg,' + P.a + ',' + P.b + ')' : 'linear-gradient(135deg,#8b5cf6,#6366f1)') + '"></span>'; }
      return '<button type="button" class="xt-chip' + (T[key] === o[0] ? ' on' : '') + '" data-k="' + key + '" data-v="' + o[0] + '">' + dot + o[1] + '</button>';
    }).join('');
  }
  function renderPanel() {
    var G = [['p', '🎨 Colour theme'], ['b', '🔘 Buttons'], ['c', '🗂 Cards'], ['bg', '✨ Animated background'], ['m', '🤖 Animated mascot'], ['r', '◻ Roundness'], ['f', '🔤 Text size']];
    var h = '<div class="xt-box"><h5>🎨 Theme studio</h5><div class="xt-sub">Tap anything to preview it instantly. Press <b>Save</b> to keep it on every device.</div>';
    G.forEach(function (g) { h += '<div class="xt-g"><b>' + g[1] + '</b><div class="xt-chips">' + chips(g[0]) + '</div></div>'; });
    h += '<div class="xt-g"><b>🌈 Your own accent colour</b><div class="xt-chips"><input type="color" id="xt-ac" value="' + (T.ac || '#8b5cf6') + '" style="width:56px;height:38px;border:0;background:none"><button type="button" class="xt-chip" id="xt-acx">Clear custom colour</button></div></div>';
    h += '<div class="xt-row"><button type="button" class="xt-btn" id="xt-save">💾 Save</button><button type="button" class="xt-btn alt" id="xt-reset">↺ Reset</button><button type="button" class="xt-btn alt" id="xt-close">Close</button></div></div>';
    ov.innerHTML = h;
  }
  function openTheme() {
    if (!ov) {
      ov = el('div', {id: 'xt-ov'}); document.body.appendChild(ov);
      ov.addEventListener('click', function (e) {
        var t = e.target;
        if (t === ov || t.id === 'xt-close') { T = Object.assign({}, saved); applyAll(); ov.style.display = 'none'; return; }
        var c = t.closest ? t.closest('.xt-chip[data-k]') : null;
        if (c) { T[c.getAttribute('data-k')] = c.getAttribute('data-v'); applyAll(); var sc = ov.firstChild.scrollTop; renderPanel(); ov.firstChild.scrollTop = sc; return; }
        if (t.id === 'xt-acx') { T.ac = ''; applyAll(); renderPanel(); return; }
        if (t.id === 'xt-save') { post('/_x/theme', T).then(function (d) { saved = Object.assign({}, T); toastMsg(d.message && d.message === 'Unauthorized' ? 'Please log in again' : 'Theme saved ✔', d.message !== 'Unauthorized'); ov.style.display = 'none'; }); return; }
        if (t.id === 'xt-reset') { T = Object.assign({}, DEF); applyAll(); post('/_x/theme', {reset: true}).then(function () { saved = Object.assign({}, DEF); toastMsg('Theme reset to default', true); }); renderPanel(); return; }
      });
      ov.addEventListener('input', function (e) { if (e.target.id === 'xt-ac') { T.ac = e.target.value; applyAll(); } });
    }
    renderPanel(); ov.style.display = 'flex';
  }

  /* ---------- maintenance ---------- */
  var M = CFG.maint || null, mov = null;
  function setMaint(on) {
    if (!M) return;
    M.on = !!on;
    document.body.classList.toggle('xm-on', M.on);
    var b = document.getElementById('xm-badge'); if (b) { b.textContent = M.on ? 'ON' : 'OFF'; b.className = 'xm-badge' + (M.on ? ' on' : ''); }
  }
  function renderMaint() {
    mov.innerHTML = '<div class="xt-box"><h5>🛠 Bot maintenance</h5><div class="xt-sub">While ON the Telegram bot does <b>nothing</b> for anyone: no menu, no buttons, no purchases, no replies. Turn it OFF here when your update is finished.</div>'
      + '<div style="display:flex;align-items:center;gap:14px;margin:14px 0"><label class="xm-sw"><input type="checkbox" id="xm-on"' + (M.on ? ' checked' : '') + '><span></span></label><b id="xm-lbl">' + (M.on ? 'Maintenance is ON' : 'Maintenance is OFF') + '</b></div>'
      + '<label style="display:flex;gap:8px;align-items:center;font-size:.9rem"><input type="checkbox" id="xm-notice"' + (M.notice ? ' checked' : '') + '> Also send users one short notice (off = bot stays completely silent)</label>'
      + '<input type="text" id="xm-text" maxlength="200" class="form-control" style="margin-top:8px" value="' + String(M.text || '').replace(/"/g, '&quot;') + '">'
      + '<div class="xt-row"><button type="button" class="xt-btn" id="xm-save">Save</button><button type="button" class="xt-btn alt" id="xm-close">Close</button></div></div>';
  }
  function openMaint() {
    if (!M) return;
    if (!mov) {
      mov = el('div', {id: 'xm-ov'}); document.body.appendChild(mov);
      mov.addEventListener('click', function (e) {
        var t = e.target;
        if (t === mov || t.id === 'xm-close') { mov.style.display = 'none'; return; }
        if (t.id === 'xm-save') {
          var on = document.getElementById('xm-on').checked;
          if (on && !confirm('Turn maintenance ON? The bot will stop answering EVERYONE until you turn it off.')) return;
          post('/_x/maintenance', {on: on, notice: document.getElementById('xm-notice').checked, text: document.getElementById('xm-text').value}).then(function (d) {
            if (d.on === undefined) { toastMsg(d.message || 'Could not save', false); return; }
            M.notice = d.notice; M.text = d.text; setMaint(d.on); mov.style.display = 'none';
            toastMsg(d.on ? 'Bot maintenance is ON - the bot is silent' : 'Bot maintenance OFF - bot is live again', true);
          });
        }
      });
      mov.addEventListener('change', function (e) { if (e.target.id === 'xm-on') document.getElementById('xm-lbl').textContent = e.target.checked ? 'Maintenance will be ON' : 'Maintenance will be OFF'; });
    }
    renderMaint(); mov.style.display = 'flex';
  }

  /* ---------- live sync (5 s) ---------- */
  function startSync() {
    var origRefresh = typeof window.liveRefresh === 'function' ? window.liveRefresh : null;
    var lastRef = Date.now(), dirty = false, lastRev = null;
    if (origRefresh) {
      window.liveRefresh = function (force) {
        if (force) { lastRef = Date.now(); return origRefresh(force); }
        if (dirty || Date.now() - lastRef > 60000) { dirty = false; lastRef = Date.now(); return origRefresh(false); }
      };
      var chip = document.getElementById('liveChip');
      if (chip) chip.innerHTML = '<i></i> Live &middot; synced every 5s';
    }
    function poll() {
      fetch('/_x/rev', {credentials: 'same-origin', cache: 'no-store'}).then(function (r) { return r.ok ? r.json() : null; }).then(function (d) {
        if (!d) return;
        setMaint(d.maint === 1);
        if (lastRev === null) { lastRev = d.rev; return; }
        if (d.rev !== lastRev) {
          lastRev = d.rev;
          if (origRefresh && !document.hidden && Date.now() - lastRef > 8000) { lastRef = Date.now(); origRefresh(false); } else { dirty = true; }
        }
      }).catch(function () {});
    }
    setInterval(poll, 5000); poll();
  }

  /* ---------- boot ---------- */
  function boot() {
    document.head.appendChild(el('style', {id: 'xt-static'})).textContent = STATIC;
    applyAll();
    var crown = document.querySelector('.crown-ring');
    if (!crown) return;                                  /* login page: theme only */
    crown.setAttribute('title', 'Theme studio');
    document.addEventListener('click', function (e) {
      var c = e.target.closest ? e.target.closest('.crown-ring') : null;
      if (c) { e.preventDefault(); e.stopPropagation(); openTheme(); }
    }, true);
    if (M) {
      document.body.appendChild(el('div', {id: 'xm-banner'}, '🛠 BOT MAINTENANCE IS ON - the bot is not answering anyone. Tap to manage')).addEventListener('click', openMaint);
      var box = document.querySelector('.quick-box');
      if (box) {
        var btn = el('button', {'class': 'qa', id: 'xm-btn', type: 'button'}, '<span class="qi">🛠</span> Bot Maintenance <span class="xm-badge" id="xm-badge">OFF</span>');
        btn.addEventListener('click', openMaint);
        var anchor = box.querySelector('.bot-link');
        if (anchor) box.insertBefore(btn, anchor); else box.appendChild(btn);
      }
      setMaint(M.on);
      startSync();
    }
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot); else boot();
})();
'''

_patch_telegram()
_patch_flask()
_x_start()






