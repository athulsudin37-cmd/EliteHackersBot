import logging
import asyncio
import os
import time
import json
import re
import random
import urllib.parse
import html
import io
import sys
from threading import Thread
from datetime import datetime, timedelta
import pytz
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand, MenuButtonCommands
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    TypeHandler,
    ContextTypes,
    filters
)
from telegram.error import BadRequest

# 🌐 വെബ് അഡ്മിൻ പാനൽ ബാക്ക്ഗ്രൗണ്ടിൽ റൺ ചെയ്യുന്നു
import web_admin

# 🗄️ Persistent PostgreSQL database layer
from database import db, init_db as init_persistent_db, MENU_BUTTONS, BUTTON_COLORS, parse_prices
import supplier_api

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)
# httpx logs full request URLs, which contain the bot token. Keep it out of the logs.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

# ==========================================
# ⚙️ CONFIGURATION & CONSTANTS
# ==========================================
# Remove ALL whitespace (a stray space inside the token makes Telegram reject it).
BOT_TOKEN = "".join(os.getenv("BOT_TOKEN", "").split())
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set. Add it in Render Environment Variables.")
ADMIN_ID = int(os.getenv("ADMIN_ID", "7616127905"))

STORE_CONFIG = {
    "support_username": "@Athulsudin",
    "how_to_use_link": "https://t.me/chatelitehackers"
}

IST = pytz.timezone('Asia/Kolkata')

def get_ist_time():
    return datetime.now(IST).strftime("%d %b %Y, %I:%M %p (IST)")

# ==========================================
# 🗄️ DATABASE HELPERS (PostgreSQL via database.py)
# ==========================================
_bg_tasks = set()

def spawn(coro):
    """Run a background task and keep a reference so it is not garbage-collected."""
    task = asyncio.create_task(coro)
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)
    return task

def ack(query):
    """Answer a button tap in the background, so the next screen does not wait for this extra Telegram call."""
    async def _go():
        try:
            await query.answer()
        except Exception:
            pass
    spawn(_go())

def init_db():
    return init_persistent_db()

_settings_cache = {"t": 0.0, "v": None}

def get_settings():
    """Live store settings from the admin panel (branding, deposit limits, referral, spin).
    Cached for 5 seconds (like the button colors), so a button tap does not hit the database for it.
    Falls back to the built-in defaults if the database cannot be read."""
    if _settings_cache["v"] is not None and time.time() - _settings_cache["t"] < 5:
        return dict(_settings_cache["v"])
    try:
        value = db.get_settings()
        _settings_cache["v"], _settings_cache["t"] = value, time.time()
        return dict(value)
    except Exception:
        logger.exception("Could not load store settings")
        return dict(db.SETTING_DEFAULTS, support_username="", how_to_use_link="")

def _num(value, default):
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)

# ----------------------------------------------------------------------
# 👑 RESELLER SETTINGS  (controlled from the web admin panel)
#
# HOOK FOR THE WEB ADMIN: all values live in the existing `ui_settings` table
# (key/value, keys start with "reseller_"). On every start the bot publishes
# RESELLER_SCHEMA into ui_settings["bot_schema_reseller"] (JSON, with a version) and
# seeds missing defaults. web_admin.py can read that schema to draw the form by
# itself, and save with db.set_ui_settings({...}). The bot picks changes up within 5 s.
# ----------------------------------------------------------------------
RESELLER_SCHEMA_VERSION = 1
RESELLER_SCHEMA = [
    # key, label, type, default  (every text is written in the admin panel; empty = a short neutral line)
    ("reseller_enabled", "Reseller plan enabled (1 = on, 0 = off)", "number", "1"),
    ("reseller_min_balance", "Minimum wallet balance (₹)", "number", "2500"),
    ("reseller_fee", "Activation fee (₹)", "number", "700"),
    ("reseller_factor", "Reseller price factor (0.6 = pay 60% of regular price)", "number", "0.6"),
    ("reseller_text", "Reseller plan screen text (HTML). Use {min_balance} {fee} {rest}", "textarea", ""),
    ("reseller_buy_label", "Buy button text", "text", "Buy Reseller Plan"),
    ("reseller_success_text", "Success message (HTML). Use {fee} {rest}", "textarea", ""),
    ("reseller_lowbal_text", "Not-enough-balance message (HTML). Use {min_balance} {balance}", "textarea", ""),
]
RESELLER_DEFAULTS = {k: d for k, _l, _t, d in RESELLER_SCHEMA}
_reseller_cache = {"t": 0.0, "v": None}


def publish_reseller_schema():
    """Tell the web admin which reseller settings exist (called on every bot start)."""
    try:
        current = db.get_ui_settings()
        values = {"bot_schema_reseller": json.dumps(
            {"version": RESELLER_SCHEMA_VERSION,
             "fields": [{"key": k, "label": l, "type": t, "default": d} for k, l, t, d in RESELLER_SCHEMA]},
            ensure_ascii=False)}
        for k, d in RESELLER_DEFAULTS.items():
            if k not in current:
                values[k] = d
        db.set_ui_settings(values)
    except Exception:
        logger.exception("Could not publish reseller schema")


def get_reseller_cfg():
    """Reseller settings from the admin panel (cached 5 s), defaults for anything missing."""
    if _reseller_cache["v"] is not None and time.time() - _reseller_cache["t"] < 5:
        return _reseller_cache["v"]
    cfg = dict(RESELLER_DEFAULTS)
    try:
        cfg.update({k: v for k, v in db.get_ui_settings().items() if k in RESELLER_DEFAULTS and v != ""})
    except Exception:
        logger.exception("Could not load reseller settings")
    cfg["min_balance"] = _num(cfg["reseller_min_balance"], 2500)
    cfg["fee"] = _num(cfg["reseller_fee"], 700)
    cfg["factor"] = _num(cfg["reseller_factor"], 0.6)
    cfg["enabled"] = str(cfg["reseller_enabled"]).strip() not in ("0", "", "false", "False")
    _reseller_cache["v"], _reseller_cache["t"] = cfg, time.time()
    return cfg


def _is_reseller_row(u_data):
    return bool(u_data and u_data[8] == "Reseller")


def _fill(template, **vals):
    """Fill {placeholders}; a typo in the admin-written text can never crash the bot."""
    out = str(template)
    for k, v in vals.items():
        out = out.replace("{" + k + "}", fmt_amount(v))
    return out


def _fill_text(template, **vals):
    out = str(template)
    for k, v in vals.items():
        out = out.replace("{" + k + "}", str(v))
    return out

_TG_TAGS = {"b", "strong", "i", "em", "u", "ins", "s", "strike", "del", "code", "pre", "a", "tg-spoiler", "blockquote"}
_TAG_RE = re.compile(r"<(/?)([a-zA-Z][\w-]*)([^<>]*)>")

def _safe_html(text):
    """Admin-written text must never break a message: only Telegram's own tags, properly closed
    (anything else, or a mistake in the tags, and the whole text is shown as plain text instead)."""
    text = str(text or "")
    stack, pos = [], 0
    for m in _TAG_RE.finditer(text):
        if "<" in text[pos:m.start()] or ">" in text[pos:m.start()]:
            return html.escape(text, quote=False)
        pos = m.end()
        closing, name, attrs = m.group(1), m.group(2).lower(), m.group(3)
        if name not in _TG_TAGS or (closing and attrs.strip()):
            return html.escape(text, quote=False)
        if closing:
            if not stack or stack.pop() != name:
                return html.escape(text, quote=False)
        else:
            if attrs.strip() and not (name == "a" and re.fullmatch(r'\s+href="[^"<>]*"\s*', attrs)):
                if not (name in ("pre", "code", "blockquote") and re.fullmatch(r'\s+[\w-]+(="[^"<>]*")?\s*', attrs)):
                    return html.escape(text, quote=False)
            stack.append(name)
    if stack or "<" in text[pos:] or ">" in text[pos:]:
        return html.escape(text, quote=False)
    return text


def fmt_amount(value):
    value = float(value)
    return f"{value:,.0f}" if value.is_integer() else f"{value:,.2f}"

def fmt_hours(hours):
    hours = float(hours)
    if hours < 1:
        return f"{int(round(hours * 60))} minutes"
    whole = f"{hours:g}"
    return f"{whole} hour" + ("" if hours == 1 else "s")

def get_store_config():
    """Support username / tutorial link / branding: values saved in the web admin, with fallback to defaults."""
    cfg = dict(STORE_CONFIG)
    st = get_settings()
    if st.get("support_username"):
        cfg["support_username"] = st["support_username"]
    if st.get("how_to_use_link"):
        cfg["how_to_use_link"] = st["how_to_use_link"]
    cfg["shop_name"] = (st.get("shop_name") or "Elite Hackers Store").strip()
    cfg["tagline"] = (st.get("tagline") or "").strip()
    cfg["update_link"] = (st.get("update_link") or "").strip()
    return cfg

def get_payment_config():
    """Live UPI ID, payee name and auto-verify switch. Read from the DB on every payment, so a
    change made in the admin panel applies to the very next QR code (no redeploy needed)."""
    try:
        row = db.get_payment_settings()
        return {
            "upi_id": (row.get("upi_id") or "").strip(),
            "payee_name": (row.get("payee_name") or "Elite Hackers Store").strip(),
            "auto_verify": bool(row.get("auto_verify")),
        }
    except Exception:
        logger.exception("Could not load payment settings")
        return {"upi_id": "", "payee_name": "Elite Hackers Store", "auto_verify": False}

def valid_url(url):
    return isinstance(url, str) and url.strip().lower().startswith(("http://", "https://", "tg://"))

def db_add_or_update_user(user_id, full_name, username, joined_date):
    db.add_or_update_user(user_id, full_name, username, joined_date)

def db_get_user(user_id):
    row = db.get_user(user_id)
    if not row:
        return None
    return (
        row["full_name"],
        row["username"],
        row["joined_date"],
        row["orders_count"],
        float(row["wallet_balance"] or 0),
        float(row["total_spent"] or 0),
        row["total_referrals"],
        float(row["referral_earnings"] or 0),
        row["account_type"],
    )

def db_get_user_history(user_id, limit=20):
    rows = db.get_user_history(user_id, limit)
    return [
        (
            row["prod_name"],
            row["plan"],
            row["key_delivered"],
            row["timestamp"],
            float(row["amount"] or 0),
        )
        for row in rows
    ]

def get_products_by_category(category):
    rows = db.get_products_by_category(category)
    result = {}
    for row in rows:
        if row.get("stock_out"):
            continue
        result[row["prod_key"]] = {
            "name": row["name"],
            "prices": row.get("prices") or [],
            "icon": row.get("icon") or "⚡",
            "download_link": row.get("download_link"),
            "category": row.get("category"),
            "maintenance": row.get("maintenance", 0),
            "stock_out": row.get("stock_out", 0),
        }
    return result

def get_product_by_key(prod_key):
    row = db.get_product_by_key(prod_key)
    if not row:
        return None
    return {
        "name": row["name"],
        "category": row["category"],
        "prices": row.get("prices") or [],
        "icon": row.get("icon") or "⚡",
        "download_link": row.get("download_link"),
        "maintenance": row.get("maintenance", 0),
        "stock_out": row.get("stock_out", 0),
    }

# ---------- pricing & purchase (shared with the web admin / auto payment verification) ----------
# ==========================================
# 💳 PAYMENT LOGIC: wallet purchases, bank alerts, automatic confirmation
# (everything the bot needs to confirm UPI payments by itself lives below)
# ==========================================
import hashlib
import threading
import imaplib
import email
import email.policy
import urllib.request
import html as htmllib
from datetime import timezone
from email.header import decode_header, make_header
from email.utils import parseaddr, parsedate_to_datetime

RESELLER_FACTOR = 0.6
# A bank credit may settle a payment in any of these states (never one that is already verified/rejected).
OPEN_STATUSES = ["pending", "awaiting_approval", "expired", "cancelled"]


def now_ist():
    return datetime.now(IST).strftime("%d %b %Y, %I:%M %p (IST)")


# ----------------------------------------------------------------------
# Pricing & wallet purchase
# ----------------------------------------------------------------------
# ======================================================================
# 🔌 WEB ADMIN / DATABASE HOOKS  (main.py is already ready for these)
# ----------------------------------------------------------------------
# HOOK 1 - Reseller price per plan, typed by the owner in the web admin:
#   database.py : add column  products.reseller_prices  (TEXT, SAME format as products.prices,
#                 e.g. "1 day:3,7 day:15") and save/return it with the product.
#   web_admin.py: in Add/Edit Product, give every plan a "Reseller price" box next to the
#                 regular price and save it into products.reseller_prices.
# As soon as a plan has a reseller price, this file uses it (read live from the DB - no restart).
# Plans without one still use the old "factor" price while RESELLER_AUTO_FALLBACK is True.
# When every plan has its own reseller price, set RESELLER_AUTO_FALLBACK = False to remove the
# automatic % price completely (then a plan without a reseller price sells at the regular price).
# ======================================================================
RESELLER_AUTO_FALLBACK = True

def _reseller_price_for(prod_key, plan_name, row=None):
    """Owner-typed reseller price for one plan, or None if the web admin has not set it."""
    try:
        row = row or db.get_product_by_key(prod_key)
        raw = (row or {}).get("reseller_prices")
        if not raw:
            return None
        pairs = parse_prices(raw) if isinstance(raw, str) else raw
        if isinstance(pairs, dict):
            pairs = list(pairs.items())
        for n, p in pairs:
            if n == plan_name and p not in (None, ""):
                return float(p)
    except Exception:
        logger.exception("Could not read reseller price for %s/%s", prod_key, plan_name)
    return None

def unit_price(reg_price, is_reseller, prod_key=None, plan_name=None):
    if not is_reseller:
        return float(reg_price)
    custom = _reseller_price_for(prod_key, plan_name) if prod_key and plan_name else None
    if custom is not None:
        return round(custom, 2)
    if RESELLER_AUTO_FALLBACK:
        return round(float(reg_price) * get_reseller_cfg()["factor"], 2)
    return float(reg_price)


def fmt_price(value):
    value = float(value)
    return f"₹{value:,.0f}" if value.is_integer() else f"₹{value:,.2f}"


def _product(prod_key):
    row = db.get_product_by_key(prod_key)
    if not row:
        return None
    return {
        "name": row["name"],
        "category": row["category"],
        "prices": row.get("prices") or [],
        "icon": row.get("icon") or "⚡",
        "download_link": row.get("download_link"),
        "maintenance": row.get("maintenance", 0),
        "stock_out": row.get("stock_out", 0),
    }


def purchase_with_wallet(user_id, prod_key, plan_name, allow_pending=False, utr="WALLET_PAY", discount=0.0):
    """Charge the wallet and deliver a key. Money is only taken when the balance is enough.

    Returns a dict with a "status": ok | pending | insufficient | out_of_stock |
    no_product | no_plan | unavailable.
    """
    prod = _product(prod_key)
    if not prod:
        return {"status": "no_product"}
    if prod.get("maintenance") or prod.get("stock_out"):
        return {"status": "unavailable", "prod": prod}

    user = db.get_user(user_id)
    is_reseller = bool(user and user["account_type"] == "Reseller")
    reg_price = next((p for n, p in prod["prices"] if n == plan_name), None)
    if reg_price is None:
        return {"status": "no_plan", "prod": prod}

    price = max(0.0, round(unit_price(reg_price, is_reseller, prod_key, plan_name) - float(discount or 0), 2))
    new_balance = db.try_deduct(user_id, price)
    if new_balance is None:
        balance = float(user["wallet_balance"] or 0) if user else 0.0
        return {"status": "insufficient", "prod": prod, "price": price, "balance": balance}

    key, supplier_order = None, None
    try:
        key = db.pop_auto_key(prod_key, plan_name)
        if not key:
            # No local stock -> buy it live from the supplier panel (only if this plan is mapped to a PID).
            key, supplier_order = _buy_from_supplier(user_id, prod_key, plan_name)
            if key:
                utr = f"API:{supplier_order}" if supplier_order else "API"
        status = "ok"
        if not key:
            if not allow_pending:
                db.update_balance(user_id, price)  # nothing delivered -> give the money back
                return {"status": "out_of_stock", "prod": prod, "price": price}
            # Customer already paid by UPI: queue the order, admin dispatches it from the web panel.
            key, status = "PENDING", "pending"
        db.add_order(user_id, prod["name"], plan_name, key, price, utr, now_ist())
        try:
            db.mark_spin_rewards_used(user_id)  # bought in time -> spin cash is kept
        except Exception:
            logger.exception("Could not mark spin rewards as used")
    except Exception:
        db.update_balance(user_id, price)
        if supplier_order is not None and key:
            # The key was bought from the supplier but saving the order failed: never lose that key.
            tg_call("sendMessage", {
                "chat_id": ADMIN_ID, "parse_mode": "HTML",
                "text": (f"🚨 <b>Supplier key bought but order could not be saved</b>\n"
                         f"👤 <code>{user_id}</code> · {_esc(prod_key)} {_esc(plan_name)}\n"
                         f"🧾 Supplier order: <code>{_esc(supplier_order)}</code>\n"
                         f"🗝️ <code>{_esc(key)}</code>\n\nThe customer's wallet was refunded."),
            })
        raise

    return {"status": status, "prod": prod, "price": price, "key": key, "balance": new_balance}


def _buy_from_supplier(user_id, prod_key, plan_name):
    """Buy one key from the supplier API for this product+plan.
    Returns (key, order_code) or (None, None) when it is not mapped / not possible (customer is then refunded)."""
    if not supplier_api.configured():
        return None, None
    try:
        pid = db.get_supplier_pid(prod_key, plan_name)
    except Exception:
        logger.exception("Could not read supplier PID mapping")
        return None, None
    if not pid:
        return None, None
    try:
        res = supplier_api.order(pid, 1, f"tg_{user_id}")
    except supplier_api.SupplierError as e:
        logger.error("Supplier order failed (pid %s): %s", pid, e)
        warn = ""
        if isinstance(e, supplier_api.SupplierUncertain):
            warn = "\n⚠️ <b>The order MAY have been charged by the supplier</b> — check their panel before retrying."
        _alert_once(
            f"supplier:{prod_key}:{plan_name}",
            f"⚠️ <b>Supplier purchase failed</b>\n📦 {_esc(prod_key)} · {_esc(plan_name)} (PID {pid})\n"
            f"Reason: {_esc(str(e)[:300])}{warn}\nThe customer's money stays safe in their bot wallet (no bank refund).",
            hours=0.1 if warn else 1,
        )
        return None, None
    rem = res.get("remaining_balance")
    if rem is not None and rem < 100:
        _alert_once("supplier-low-balance",
                    f"💰 <b>Supplier wallet is low:</b> ₹{rem:,.2f} left. Top it up so customers keep getting keys.")
    return res["license_key"], res["order_code"]


# ----------------------------------------------------------------------
# Message texts (HTML) shared by the bot and the webhook
# ----------------------------------------------------------------------
def _esc(value):
    return (str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def build_key_card(prod_name, plan_name, key, balance):
    return (
        f"✅ <b>Payment verified — here's your key!</b>\n"
        f"⏩ ~~~~~~~~~~~~~~~~~~~~~~~~~~\n\n"
        f"🛒 <b>{_esc(prod_name)} — {_esc(plan_name.replace('_', ' '))}</b>\n\n"
        f"🗝️ Your Key:\n"
        f"<code>{_esc(key)}</code>\n\n"
        f"💰 Remaining balance: <b>₹{balance:,.2f}</b>"
    )


def build_out_of_stock_notice(prod_name, plan_name, amount):
    return (
        "⚠️ <b>Out of Stock</b>\n\n"
        f"<b>{_esc(prod_name)}</b> ({_esc(plan_name.replace('_', ' '))}) has no stock right now.\n"
        f"🛡️ Your money is safe — <b>₹{amount:,.2f}</b> has been added to your wallet.\n"
        "You can buy it as soon as it is restocked."
    )


def build_pending_notice(prod_name, plan_name):
    return (
        "✅ <b>Payment Verified &amp; Received!</b>\n\n"
        f"⚠️ NOTICE: Auto-stock for <b>{_esc(prod_name)}</b> ({_esc(plan_name.replace('_', ' '))}) is currently restocking!\n\n"
        "🛡️ 100% SECURE: Admin is preparing your fresh key right now. It will be delivered directly to this chat shortly!"
    )


# ----------------------------------------------------------------------
# Telegram (plain HTTP so the web admin thread can use it too)
# ----------------------------------------------------------------------
def tg_call(method, payload):
    if not BOT_TOKEN:
        return None
    payload = _localize_payload(method, payload)
    for attempt in (1, 2):
        try:
            req = urllib.request.Request(
                f"https://api.telegram.org/bot{BOT_TOKEN}/{method}",
                data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=8) as resp:
                result = json.loads(resp.read().decode())
            if attempt == 2:
                pause_icons()
            return result
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8", "ignore")
            except Exception:
                pass
            if (attempt == 1 and e.code == 400 and _payload_has_icons(payload)
                    and any(w in body.lower() for w in _ICON_ERR_WORDS)):
                payload = _strip_payload_icons(payload)      # Telegram refused a premium icon: send it plain
                continue
            if e.code == 403:
                logger.warning("Telegram API %s refused (403): %s", method, body[:200])
            else:
                logger.error("Telegram API call failed: %s -> HTTP %s %s", method, e.code, body[:300])
            return None
        except Exception:
            logger.exception("Telegram API call failed: %s", method)
            return None
    return None


# ----------------------------------------------------------------------
# Reading bank alerts
# ----------------------------------------------------------------------
_CREDIT_RE = re.compile(r"\b(?:credited|received|deposited|paid you|sent you|credit alert)\b|\bcr\b", re.I)
_DEBIT_RE = re.compile(r"\b(?:debited|withdrawn|sent to|paid to)\b|\bdebit\b(?!\s*card)", re.I)
_BALANCE_RE = re.compile(
    r"\b(?:avl\.?\s*)?(?:clear\s*)?bal(?:ance)?\b[^0-9₹]{0,12}(?:rs\.?|inr|₹)?\s*[0-9][0-9,]*(?:\.[0-9]{1,2})?",
    re.I,
)
_AMOUNT_RE = re.compile(r"(?:rs\.?|inr|₹)\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)", re.I)
_UTR_RES = [
    re.compile(r"(?:utr|rrn|ref(?:erence)?(?:\s*(?:no|number|id))?\.?)[\s:#.\-]*([0-9]{9,18})", re.I),
    re.compile(r"upi/(?:cr|credit)/([0-9]{9,18})", re.I),
    re.compile(r"\b([0-9]{12})\b"),
]


def parse_sms(text):
    """Return {"is_credit": bool, "amount": float | None, "utr": str | None} for an alert text."""
    text = (text or "").strip()
    result = {"is_credit": False, "amount": None, "utr": None}
    if not text:
        return result

    result["is_credit"] = bool(_CREDIT_RE.search(text)) and not _DEBIT_RE.search(text)

    without_balance = _BALANCE_RE.sub(" ", text)
    m = _AMOUNT_RE.search(without_balance)
    if m:
        try:
            result["amount"] = round(float(m.group(1).replace(",", "")), 2)
        except ValueError:
            pass

    for rx in _UTR_RES:
        m = rx.search(text)
        if m:
            result["utr"] = m.group(1)
            break
    return result


_ORDER_ID_RE = re.compile(r"UPI\d{14}[0-9A-F]{8}", re.I)


def _find_payment(text, amount):
    """Which open payment does this bank alert belong to?

    1) If the alert shows one of our order ids (the UPI payment note) and the amount is exactly right, use it.
    2) Otherwise match by the exact amount (every open payment has its own paise, so only one can match).
    """
    for oid in _ORDER_ID_RE.findall(text or ""):
        cand = db.get_payment(oid.upper())
        if not cand or cand["status"] not in OPEN_STATUSES:
            continue
        created = cand.get("created_ts")
        if created is not None and getattr(created, "tzinfo", None) is not None:
            if datetime.now(created.tzinfo) - created > timedelta(hours=2):
                continue
        if abs(float(cand.get("pay_amount") or cand["amount"]) - float(amount)) < 0.005:
            return cand
    return db.find_open_payment_by_amount(amount)


def _fingerprint(text):
    bucket = int(datetime.now().timestamp() // 3600)
    return hashlib.sha256((text or "").strip().encode()).hexdigest() + f":{bucket}"


# ----------------------------------------------------------------------
# Settling a payment
# ----------------------------------------------------------------------
def settle_payment(order_id, from_statuses, utr, via="auto"):
    """Atomically mark a payment verified, credit the wallet and (for a product purchase) deliver the key.

    Returns None when the payment was not in an allowed state (already settled), so it can never be
    credited twice, otherwise a dict describing what happened.
    """
    row = db.claim_payment(order_id, from_statuses, "verified", utr, now_ist())
    if not row:
        return None

    user_id = row["user_id"]
    credit = float(row.get("pay_amount") or row["amount"])
    db.update_balance(user_id, credit)

    outcome = {"row": row, "amount": credit, "via": via, "purchase": None}
    if row.get("is_deficit") and row.get("prod_key") and row.get("plan"):
        try:
            outcome["purchase"] = purchase_with_wallet(
                user_id, row["prod_key"], row["plan"], allow_pending=False,
                utr=("BINANCE_USDT" if row.get("method") == "binance" else "UPI_DEFICIT"),
                discount=float(row.get("discount") or 0),
            )
            if outcome["purchase"] and outcome["purchase"]["status"] == "ok" and row.get("coupon_code"):
                db.use_coupon(row["coupon_code"])
        except Exception:
            logger.exception("Purchase after verified payment failed (wallet stays credited)")
    return outcome


def notify_settlement(outcome):
    """Tell the customer (and the admin) about an automatically settled payment."""
    row = outcome["row"]
    user_id = row["user_id"]
    amount = outcome["amount"]
    purchase = outcome.get("purchase")
    admin_note = ""

    if row.get("chat_id") and row.get("qr_msg_id"):
        tg_call("deleteMessage", {"chat_id": row["chat_id"], "message_id": row["qr_msg_id"]})

    buttons = [[tg_button("➡️ Back to Menu", "back_menu", callback_data="main_menu")]]
    if purchase and purchase["status"] == "ok":
        text = build_key_card(purchase["prod"]["name"], row["plan"], purchase["key"], purchase["balance"])
        if valid_url(purchase["prod"].get("download_link")):
            buttons.insert(0, [tg_button("📥 Update File ↗️", "update_file", url=purchase["prod"]["download_link"])])
    elif purchase and purchase["status"] in ("out_of_stock", "unavailable"):
        text = build_out_of_stock_notice(purchase["prod"]["name"], row["plan"], amount)
        admin_note = "\n⚠️ Out of stock: the amount was added to the customer's wallet."
    elif purchase and purchase["status"] == "pending":
        text = build_pending_notice(purchase["prod"]["name"], row["plan"])
        admin_note = "\n⚠️ Out of stock: dispatch the key from <b>Pending Keys Queue</b> in the admin panel."
    else:
        text = f"✅ <b>Payment Verified Successfully!</b>\n₹{amount:,.2f} has been added to your wallet."
        if row.get("is_deficit"):
            text += "\n\nYour balance is ready — open the shop to complete your purchase."

    tg_call("sendMessage", {
        "chat_id": user_id, "text": text, "parse_mode": "HTML",
        "reply_markup": {"inline_keyboard": buttons},
    })
    tg_call("sendMessage", {
        "chat_id": ADMIN_ID, "parse_mode": "HTML",
        "text": (
            f"✅ <b>Payment auto-verified</b>\n"
            f"👤 <code>{user_id}</code> · ₹{amount:,.2f}\n"
            f"🆔 <code>{row['order_id']}</code>{admin_note}"
        ),
    })


def process_bank_message(text, settle=True):
    """Handle one forwarded alert. Returns {"status", "order_id", "parsed"}.

    status: matched | unmatched | duplicate | ignored | logged
    """
    parsed = parse_sms(text)
    out = {"status": "ignored", "order_id": None, "parsed": parsed}

    if not parsed["is_credit"] or parsed["amount"] is None:
        db.record_bank_credit(_fingerprint(text), parsed["amount"], parsed["utr"], text, "ignored")
        return out

    fingerprint = ("utr:" + parsed["utr"]) if parsed["utr"] else _fingerprint(text)
    credit_id = db.record_bank_credit(
        fingerprint, parsed["amount"], parsed["utr"], text, "new" if settle else "logged"
    )
    if credit_id is None:
        out["status"] = "duplicate"
        return out
    if not settle:
        out["status"] = "logged"
        return out

    pay = _find_payment(text, parsed["amount"])
    outcome = None
    if pay:
        outcome = settle_payment(pay["order_id"], OPEN_STATUSES, parsed["utr"] or "AUTO", via="auto")

    if not outcome:
        db.set_bank_credit_status(credit_id, "unmatched")
        out["status"] = "unmatched"
        tg_call("sendMessage", {
            "chat_id": ADMIN_ID, "parse_mode": "HTML",
            "text": (
                f"⚠️ <b>Money received but no matching order</b>\n"
                f"💰 ₹{parsed['amount']:,.2f}" + (f" · UTR <code>{parsed['utr']}</code>" if parsed["utr"] else "") +
                "\nThe customer may have changed the amount or paid after 60 minutes. Check the admin panel → UPI Payment Setup."
            ),
        })
        return out

    db.set_bank_credit_status(credit_id, "matched", pay["order_id"])
    out["status"] = "matched"
    out["order_id"] = pay["order_id"]
    notify_settlement(outcome)
    return out


# ==========================================
# 📧 GMAIL PAYMENT READER
# Reads your bank's "money received" e-mails (Gmail + App Password) every 5 seconds and confirms the
# matching payment by itself: exact amount (with its own paise), order id if shown, UTR (never twice),
# and the e-mail time. Only e-mails really sent by your bank are trusted (sender + Gmail's DKIM check).
#   Render Environment: GMAIL_USER, GMAIL_APP_PASSWORD, BANK_EMAIL_FROM
# ==========================================
GMAIL_POLL_SECONDS = 5               # the bot looks at Gmail every 5 seconds
GMAIL_STATUS = {"state": "off", "detail": "Gmail is not set up yet.", "last_check": "", "last_mail": ""}
_GMAIL_WAKE = threading.Event()      # set when a customer taps "I have paid": check Gmail immediately
MAX_EMAIL_AGE = timedelta(minutes=90)   # never act on old e-mails
MAX_PER_POLL = 30                    # newest e-mails looked at per poll
BODY_CHARS = 800                     # the transaction is at the top; ignore long disclaimers


_SECOND_LEVEL = {"co", "com", "net", "org", "gov", "bank", "ac", "edu"}


def _suggest_domain(address):
    """alerts@hdfcbank.bank.in -> hdfcbank.bank.in, alerts.hdfcbank.net -> hdfcbank.net"""
    labels = address.split("@")[-1].lower().strip(".").split(".")
    if len(labels) <= 2:
        return ".".join(labels)
    if labels[-2] in _SECOND_LEVEL:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def _gmail_find_senders(imap):
    """Senders of the 'money received' e-mails of the last 3 days -> {address: count}."""
    imap.select("INBOX")
    since = (datetime.now(timezone.utc) - timedelta(days=3)).strftime("%d-%b-%Y")
    typ, data = imap.uid("search", None, "SINCE", since)
    uids = sorted(int(x) for x in (data[0].split() if typ == "OK" and data and data[0] else []))[-60:]
    found = {}
    for uid in uids:
        typ, parts = imap.uid("fetch", str(uid), "(BODY.PEEK[])")
        if typ != "OK" or not parts or not isinstance(parts[0], tuple):
            continue
        msg = email.message_from_bytes(parts[0][1])
        text = _header_text(msg.get("Subject")) + " " + _body_text(msg)[:BODY_CHARS]
        p = parse_sms(text)
        addr = parseaddr(msg.get("From", ""))[1].lower()
        if addr and p["is_credit"] and p["amount"] is not None:
            found[addr] = found.get(addr, 0) + 1

    return found


def _gmail_suggest_senders(imap):
    """Tell the admin on Telegram which sender domain to use."""
    found = _gmail_find_senders(imap)
    if found:
        top = sorted(found, key=found.get, reverse=True)[:3]
        word = _suggest_domain(top[0])
        text = (
            "📧 <b>Gmail is connected!</b>\nI found bank 'money received' e-mails from:\n"
            + "\n".join(f"• <code>{a}</code>" for a in top)
            + "\n\nTo switch on automatic payments set this:\n"
            + f"Admin panel → UPI Payment Setup → Sender domain = <code>{word}</code>"
        )
    else:
        text = (
            "📧 <b>Gmail is connected</b>, but I found no bank 'money received' e-mail from the last 3 days.\n"
            "Make sure your bank sends e-mail alerts for money received, receive a small UPI payment "
            "(₹1-₹2) and restart the bot. I will then tell you what to add."
        )
    tg_call("sendMessage", {"chat_id": ADMIN_ID, "text": text, "parse_mode": "HTML"})


def _header_text(value):
    try:
        return str(make_header(decode_header(value or "")))
    except Exception:
        return value or ""


def _body_text(msg):
    plain, html_parts = [], []
    for part in msg.walk():
        ctype = part.get_content_type()
        if ctype not in ("text/plain", "text/html") or part.get_filename():
            continue
        payload = part.get_payload(decode=True)
        if payload is None:
            continue
        text = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
        (plain if ctype == "text/plain" else html_parts).append(text)

    if plain:
        text = " ".join(plain)
    else:
        text = " ".join(html_parts)
        text = re.sub(r"(?is)<(script|style).*?</\1>", " ", text)
        text = re.sub(r"<[^>]+>", " ", text)
        text = htmllib.unescape(text)
    return " ".join(text.split())


def _domain_ok(domain, allowed):
    """True if `domain` is exactly an allowed bank domain or a sub-domain of it (never a look-alike)."""
    domain = (domain or "").lower().strip(".@ ")
    return any(domain == a or domain.endswith("." + a) for a in allowed)


_alert_times = {}


def _alert_once(key, text, hours=1):
    """Tell the admin about a problem, but not more than once per hour for the same problem."""
    now = time.time()
    if now - _alert_times.get(key, 0) > hours * 3600:
        _alert_times[key] = now
        tg_call("sendMessage", {"chat_id": ADMIN_ID, "text": text, "parse_mode": "HTML"})


def sender_is_trusted(msg, allowed):
    """True only if the sender is the bank's domain AND Gmail verified that it really came from there.

    `allowed` holds real domains such as famapp.in. Gmail's own check (the top Authentication-Results
    header) must show either a valid DKIM signature or a passed SPF check for that same domain.
    A look-alike such as famapp.in.evil.com or famapp-secure.com is NOT accepted.
    """
    from_addr = parseaddr(msg.get("From", ""))[1].lower()
    if not from_addr or not _domain_ok(from_addr.split("@")[-1], allowed):
        return False, f"sender {from_addr or '?'} is not from the bank domain in Gmail settings"

    # The top-most Authentication-Results header is the one Gmail added itself.
    results = msg.get_all("Authentication-Results") or []
    header = " ".join(str(results[0]).split()) if results else ""
    if not results or header.split(";")[0].strip().lower() != "mx.google.com":
        if os.getenv("BANK_EMAIL_ALLOW_UNVERIFIED", "").strip().lower() in ("1", "yes", "true", "on"):
            return True, "ok (unverified sender allowed by BANK_EMAIL_ALLOW_UNVERIFIED)"
        return False, "DKIM/SPF not verified for the bank domain (no Gmail authentication result)"
    for clause in re.findall(r"dkim=pass[^;]*", header, re.I):
        for d in re.findall(r"header\.(?:d|i)=@?([A-Za-z0-9.\-]+)", clause, re.I):
            if _domain_ok(d, allowed):
                return True, "ok"
    for clause in re.findall(r"spf=pass[^;]*", header, re.I):
        for d in re.findall(r"smtp\.mailfrom=(?:[^@\s;]*@)?([A-Za-z0-9.\-]+)", clause, re.I):
            if _domain_ok(d, allowed):
                return True, "ok"
    for clause in re.findall(r"dmarc=pass[^;]*", header, re.I):
        for d in re.findall(r"header\.from=@?([A-Za-z0-9.\-]+)", clause, re.I):
            if _domain_ok(d, allowed):
                return True, "ok"
    if os.getenv("BANK_EMAIL_ALLOW_UNVERIFIED", "").strip().lower() in ("1", "yes", "true", "on"):
        return True, "ok (unverified sender allowed by BANK_EMAIL_ALLOW_UNVERIFIED)"
    return False, "DKIM/SPF not verified for the bank domain"


def _mark_processed(key):
    """Remember an e-mail forever (survives restarts). False if it was already handled."""
    row = db.fetchone(
        "INSERT INTO processed_mails (message_key) VALUES (%s) ON CONFLICT DO NOTHING RETURNING message_key",
        (key,),
    )
    return row is not None


def _unmark(key):
    db.execute("DELETE FROM processed_mails WHERE message_key = %s", (key,))


def handle_email(raw, allowed):
    """Process one raw e-mail. Returns a short status string (useful for logs/tests)."""
    msg = email.message_from_bytes(raw)

    try:
        sent = parsedate_to_datetime(msg.get("Date", ""))
        if sent.tzinfo is None:
            sent = sent.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) - sent > MAX_EMAIL_AGE:
            return "too old"
    except Exception:
        return "bad date"

    ok, reason = sender_is_trusted(msg, allowed)
    if not ok:
        from_addr = parseaddr(msg.get("From", ""))[1].lower()
        if reason.startswith("DKIM/SPF") and from_addr:   # looks like the bank but could not be verified
            GMAIL_STATUS["last_mail"] = f"{datetime.now(IST).strftime('%d %b %I:%M:%S %p')} · ignored: sender not verified by Gmail"
            _alert_once("auth:" + from_addr,
                        f"⚠️ An e-mail from <code>{from_addr}</code> was ignored because Gmail could not verify "
                        "the sender (no valid DKIM/SPF/DMARC).\n\nIf this is really your FamPay alert, add the Render "
                        "variable <code>BANK_EMAIL_ALLOW_UNVERIFIED</code> = <code>yes</code> and redeploy. "
                        "(Slightly less secure: someone who knows your Gmail address could then fake an alert e-mail.)")
        return "rejected: " + reason

    message_id = (msg.get("Message-ID") or "").strip() or hashlib.sha1(raw).hexdigest()
    key = hashlib.sha256(message_id.encode()).hexdigest()
    if not _mark_processed(key):
        return "already processed"

    try:   # the modern policy decodes both RFC2047 and raw UTF-8 headers (e.g. a raw ₹ sign) correctly
        subject = str(email.message_from_bytes(raw, policy=email.policy.default).get("Subject") or "")
    except Exception:
        subject = _header_text(msg.get("Subject"))
    body = _body_text(msg)[:BODY_CHARS]
    head = parse_sms(subject)
    if head["is_credit"] and head["amount"] is not None:
        # The subject itself says money was received ("You received ₹10.01 in your FamX account"),
        # so stray words like "sent to" in the template body must not cancel it.
        body = _DEBIT_RE.sub(" ", body)
    text = subject + " " + body
    try:
        settle = bool(db.get_payment_settings().get("auto_verify"))
        result = process_bank_message(text, settle=settle)
    except Exception:
        _unmark(key)   # try again on the next poll
        raise
    GMAIL_STATUS["last_mail"] = f"{datetime.now(IST).strftime('%d %b %I:%M:%S %p')} · {result['status']}"
    return "processed: " + result["status"]


def _gmail_poll_once(imap, allowed, last_uid):
    imap.select("INBOX")
    since = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%d-%b-%Y")
    typ, data = imap.uid("search", None, "SINCE", since)
    if typ != "OK" or not data or not data[0]:
        return last_uid

    uids = sorted(int(x) for x in data[0].split())
    for uid in [u for u in uids if u > last_uid][-MAX_PER_POLL:]:
        typ, parts = imap.uid("fetch", str(uid), "(BODY.PEEK[])")
        if typ != "OK" or not parts or not isinstance(parts[0], tuple):
            continue
        status = handle_email(parts[0][1], allowed)
        logger.info("Gmail alert uid=%s -> %s", uid, status)
    return max(uids[-1], last_uid)


def _gmail_close(imap):
    try:
        if imap is not None:
            imap.logout()
    except Exception:
        pass


def _gmail_run(user, password, allowed, stop=None):
    stop = stop or threading.Event()
    if not allowed:
        # No sender domain yet: never accept any e-mail, just tell the admin what to set.
        GMAIL_STATUS.update(state="setup", detail="Connected. Press 'Connect & detect sender' in the admin panel.")
        try:
            imap = imaplib.IMAP4_SSL("imap.gmail.com", 993, timeout=30)
            imap.login(user, password)
            _gmail_suggest_senders(imap)
            _gmail_close(imap)
        except imaplib.IMAP4.error as e:
            logger.error("Gmail login/IMAP error: %s", e)
            GMAIL_STATUS.update(state="error", detail="Gmail login failed: wrong address or App Password.")
            tg_call("sendMessage", {
                "chat_id": ADMIN_ID,
                "text": "⚠️ Gmail payment reader could not log in. Check the Gmail address and App Password "
                        "(2-Step Verification must be on).",
            })
        except Exception:
            logger.exception("Gmail suggestion failed")
        return
    db.execute(
        "CREATE TABLE IF NOT EXISTS processed_mails ("
        "message_key TEXT PRIMARY KEY, created_ts TIMESTAMPTZ NOT NULL DEFAULT now())"
    )
    imap, last_uid, alerted = None, 0, False
    logger.info("Gmail payment reader started for %s (trusted senders: %s)", user, ", ".join(allowed))
    while not stop.is_set():
        try:
            if imap is None:
                imap = imaplib.IMAP4_SSL("imap.gmail.com", 993, timeout=30)
                imap.login(user, password)
                alerted = False
                logger.info("Gmail login OK")
            last_uid = _gmail_poll_once(imap, allowed, last_uid)
            GMAIL_STATUS.update(state="ok", detail="Connected. Checking every 5 seconds.",
                                last_check=datetime.now(IST).strftime("%d %b %I:%M:%S %p"))
            _GMAIL_WAKE.wait(GMAIL_POLL_SECONDS)
            _GMAIL_WAKE.clear()
        except imaplib.IMAP4.error as e:
            logger.error("Gmail login/IMAP error: %s", e)
            GMAIL_STATUS.update(state="error", detail="Gmail login failed: wrong address or App Password.")
            if not alerted:
                alerted = True
                tg_call("sendMessage", {
                    "chat_id": ADMIN_ID,
                    "text": "⚠️ Gmail payment reader could not log in. Check the Gmail address and App Password "
                            "(2-Step Verification must be on).",
                })
            _gmail_close(imap)
            imap = None
            stop.wait(120)
        except Exception:
            logger.exception("Gmail reader error")
            GMAIL_STATUS.update(state="error", detail="Temporary Gmail error, retrying.")
            _gmail_close(imap)
            imap = None
            stop.wait(30)
    _gmail_close(imap)


def _gmail_settings():
    """Gmail address, App Password and sender domain. The admin panel wins; Render variables are a fallback."""
    try:
        cfg = db.get_gmail_config()
    except Exception:
        cfg = {}
    user = (cfg.get("gmail_user") or os.getenv("GMAIL_USER", "")).strip()
    password = "".join((cfg.get("gmail_app_password") or os.getenv("GMAIL_APP_PASSWORD", "")).split())
    raw = cfg.get("bank_domain") or os.getenv("BANK_EMAIL_FROM", "")
    allowed = [a.strip().lower().lstrip("@*. ") for a in raw.split(",") if a.strip()]
    if any("." not in a for a in allowed):
        logger.error("The bank sender domain must be a full domain like famapp.in (not a single word): %s", allowed)
    allowed = [a for a in allowed if "." in a]   # a bare word could be matched by look-alike domains
    return user, password, allowed


def gmail_detect_sender():
    """Admin panel button: log in, find the bank's 'money received' e-mails and return their sender domain."""
    user, password, _ = _gmail_settings()
    if not user or not password:
        return {"ok": False, "message": "Save the Gmail address and App Password first."}
    try:
        imap = imaplib.IMAP4_SSL("imap.gmail.com", 993, timeout=30)
        imap.login(user, password)
        found = _gmail_find_senders(imap)
        _gmail_close(imap)
    except imaplib.IMAP4.error:
        return {"ok": False, "message": "Gmail login failed. Check the address and the App Password (2-Step Verification must be on)."}
    except Exception as e:
        return {"ok": False, "message": f"Could not reach Gmail: {e}"}
    if not found:
        return {"ok": False, "message": "Login OK, but no 'money received' e-mail from the last 3 days was found. "
                                        "Receive a small UPI payment (₹1-₹2) and press the button again."}
    top = sorted(found, key=found.get, reverse=True)
    return {"ok": True, "domain": _suggest_domain(top[0]), "senders": top[:3],
            "message": "Login OK. Found money-received e-mails from " + ", ".join(top[:3])}


def _gmail_supervisor():
    """Starts / restarts the Gmail reader whenever the settings in the admin panel change."""
    current, stop = None, None
    while True:
        try:
            cfg = _gmail_settings()
            if cfg != current:
                if stop is not None:
                    stop.set()
                    _GMAIL_WAKE.set()
                current, stop = cfg, threading.Event()
                user, password, allowed = cfg
                if user and password:
                    Thread(target=_gmail_run, args=(user, password, allowed, stop), daemon=True).start()
                else:
                    GMAIL_STATUS.update(state="off", detail="Gmail is not set up yet.")
        except Exception:
            logger.exception("Gmail supervisor error")
        time.sleep(10)


def start_gmail_reader():
    """Start watching Gmail in the background (settings come from the admin panel)."""
    t = Thread(target=_gmail_supervisor, daemon=True)
    t.start()
    return t


# The web admin (imported at the top) uses these two for its optional SMS webhook / "test parser" box.
web_admin.parse_sms = parse_sms
web_admin.gmail_status = lambda: dict(GMAIL_STATUS)
web_admin.gmail_detect = gmail_detect_sender

# ==========================================
# 🎨 BUTTON COLORS (chosen in the admin panel → "Button Colors")
# ==========================================
_style_cache = {"t": 0.0, "data": {}}
_icon_pause = {"until": 0.0}      # icons are switched off for a while after Telegram refused one

def get_button_looks():
    """{key: (color, premium_emoji_id)} saved in the admin panel, cached 5 s (a change shows up almost at once)."""
    if time.time() - _style_cache["t"] > 5:
        try:
            _style_cache["data"] = db.get_button_looks()
        except Exception:
            logger.exception("Could not load button colors")
        _style_cache["t"] = time.time()
    return _style_cache["data"]

def style_of(key):
    """The color saved for a button key (or its default). None = Telegram's normal grey."""
    look = get_button_looks().get(key)
    style = look[0] if look else MENU_BUTTONS.get(key, ("", ""))[1]
    return style if style in BUTTON_COLORS else None

def icon_of(key):
    """Premium (custom) emoji id saved for a button, '' if none or while icons are paused."""
    if time.time() < _icon_pause["until"]:
        return ""
    look = get_button_looks().get(key)
    emoji_id = (look[1] if look else "") or ""
    return emoji_id if emoji_id.isdigit() else ""

def pause_icons(minutes=10):
    """Telegram refused a custom emoji icon (e.g. the owner has no Premium): send plain buttons for a while."""
    _icon_pause["until"] = time.time() + minutes * 60
    logger.warning("Premium emoji icons paused for %d minutes", minutes)

def styled_button(text, key, **kwargs):
    """InlineKeyboardButton with the color and premium emoji saved for `key`. Both are sent as Bot API
    fields ('style', 'icon_custom_emoji_id') through api_kwargs, which works on any python-telegram-bot version."""
    extra = {}
    style, icon = style_of(key), icon_of(key)
    if style:
        extra["style"] = style
    if icon:
        extra["icon_custom_emoji_id"] = icon
    if extra:
        kwargs["api_kwargs"] = extra
    return InlineKeyboardButton(text, **kwargs)

def tg_button(text, key, **extra):
    """Same as styled_button, for the plain-HTTP messages (dict buttons)."""
    btn = {"text": text, **extra}
    style, icon = style_of(key), icon_of(key)
    if style:
        btn["style"] = style
    if icon:
        btn["icon_custom_emoji_id"] = icon
    return btn

def key_card_keyboard(prod):
    keyboard = []
    if prod and valid_url(prod.get("download_link")):
        keyboard.append([styled_button("📥 Update File ↗️", "update_file", url=prod["download_link"])])
    keyboard.append([styled_button("➡️ Back to Menu", "back_menu", callback_data="main_menu")])
    return InlineKeyboardMarkup(keyboard)

def back_to_menu_markup():
    return InlineKeyboardMarkup([[styled_button("➡️ Back to Menu", "back_menu", callback_data="main_menu")]])

# ==========================================
# ⚙️ LIVE SETTINGS FROM THE ADMIN PANEL (ui_settings table, cached 5 s)
# ==========================================
_ui_cache = {"t": 0.0, "v": {}}

def ui_get(key, default=""):
    """A value saved in the admin panel (welcome text, alerts, language switch ...). Never raises."""
    if time.time() - _ui_cache["t"] > 5:
        try:
            _ui_cache["v"] = db.get_ui_settings()
        except Exception:
            pass
        _ui_cache["t"] = time.time()
    value = _ui_cache["v"].get(key)
    return default if value is None else value


# ==========================================
# 🌐 LANGUAGES: English (default), हिन्दी, తెలుగు, தமிழ், മലയാളം
# Every text of the bot is written in English in this file. Just before a message / button / pop-up is sent,
# it is translated with the tables below into the language the customer chose with the Language button.
# A table row is (English line, Hindi, Telugu, Tamil, Malayalam). {name} marks a part that changes
# (amount, name ...) and is copied as it is. A line that is not in a table is sent in English, so nothing
# can break - a missing translation simply stays English. Fix or add translations in the tables only.
# ==========================================
import contextvars
import functools
import inspect
import telegram

I18N_LANGS = {"en": "English", "hi": "हिन्दी", "te": "తెలుగు", "ta": "தமிழ்", "ml": "മലയാളം"}
_I18N_ORDER = ("hi", "te", "ta", "ml")
_I18N_PH = re.compile(r"\{(\w+)\}")
_I18N_TAG = re.compile(r"<[^>]+>")
_CUR_USER = contextvars.ContextVar("cur_user", default=0)   # who pressed the button being handled

# ======================================================================
# TABLES - one row = (English, Hindi, Telugu, Tamil, Malayalam)
# `{name}` = a changing part (amount, name, date ...), copied into the translation as it is.
# ======================================================================
_LINES = [
    # ---- main menu ----
    ("👏 Welcome, <b>{n}!</b> 🥷",
     "👏 स्वागत है, <b>{n}!</b> 🥷", "👏 స్వాగతం, <b>{n}!</b> 🥷", "👏 வரவேற்கிறோம், <b>{n}!</b> 🥷", "👏 സ്വാഗതം, <b>{n}!</b> 🥷"),
    ("📈 ── <b>STORE HIGHLIGHTS</b> ── 📈",
     "📈 ── <b>स्टोर की खासियतें</b> ── 📈", "📈 ── <b>స్టోర్ ప్రత్యేకతలు</b> ── 📈", "📈 ── <b>ஸ்டோர் சிறப்பம்சங்கள்</b> ── 📈", "📈 ── <b>സ്റ്റോർ സവിശേഷതകൾ</b> ── 📈"),
    ("🗝️ Premium Game Keys", "🗝️ प्रीमियम गेम कीज़", "🗝️ ప్రీమియం గేమ్ కీలు", "🗝️ பிரீமியம் கேம் கீகள்", "🗝️ പ്രീമിയം ഗെയിം കീകൾ"),
    ("⚡ Instant Delivery 24/7", "⚡ 24/7 तुरंत डिलीवरी", "⚡ 24/7 తక్షణ డెలివరీ", "⚡ 24/7 உடனடி டெலிவரி", "⚡ 24/7 തൽക്ഷണ ഡെലിവറി"),
    ("🔓 100% Secure Payment", "🔓 100% सुरक्षित पेमेंट", "🔓 100% సురక్షిత చెల్లింపు", "🔓 100% பாதுகாப்பான கட்டணம்", "🔓 100% സുരക്ഷിത പേയ്‌മെന്റ്"),
    ("💸 Best Prices Guaranteed", "💸 सबसे अच्छे दाम की गारंटी", "💸 ఉత్తమ ధరల హామీ", "💸 சிறந்த விலை உத்தரவாதம்", "💸 മികച്ച വില ഉറപ്പ്"),
    ("🎁 Referral Rewards", "🎁 रेफरल रिवॉर्ड", "🎁 రెఫరల్ రివార్డులు", "🎁 பரிந்துரை பரிசுகள்", "🎁 റഫറൽ റിവാർഡുകൾ"),
    ("💬 Professional Support", "💬 प्रोफेशनल सपोर्ट", "💬 ప్రొఫెషనల్ సపోర్ట్", "💬 தொழில்முறை ஆதரவு", "💬 പ്രൊഫഷണൽ സപ്പോർട്ട്"),
    ("👤 User ID: <code>{i}</code>", "👤 यूज़र आईडी: <code>{i}</code>", "👤 యూజర్ ఐడి: <code>{i}</code>", "👤 பயனர் ஐடி: <code>{i}</code>", "👤 യൂസർ ഐഡി: <code>{i}</code>"),
    ("💰 Wallet Balance: {v}", "💰 वॉलेट बैलेंस: {v}", "💰 వాలెట్ బ్యాలెన్స్: {v}", "💰 வாலட் இருப்பு: {v}", "💰 വാലറ്റ് ബാലൻസ്: {v}"),
    ("🚀 Tap Shop Now to Start!", "🚀 शुरू करने के लिए Shop Now दबाएँ!", "🚀 ప్రారంభించడానికి Shop Now నొక్కండి!", "🚀 தொடங்க Shop Now அழுத்தவும்!", "🚀 തുടങ്ങാൻ Shop Now അമർത്തുക!"),
    ("🎉 <b>New Referral!</b> User <code>{u}</code> joined via your link.",
     "🎉 <b>नया रेफरल!</b> यूज़र <code>{u}</code> आपके लिंक से जुड़ा।",
     "🎉 <b>కొత్త రెఫరల్!</b> యూజర్ <code>{u}</code> మీ లింక్ ద్వారా చేరారు.",
     "🎉 <b>புதிய பரிந்துரை!</b> பயனர் <code>{u}</code> உங்கள் இணைப்பு மூலம் சேர்ந்தார்.",
     "🎉 <b>പുതിയ റഫറൽ!</b> യൂസർ <code>{u}</code> നിങ്ങളുടെ ലിങ്ക് വഴി ചേർന്നു."),
    ("💰 <b>{a}</b> bonus added to your wallet!",
     "💰 आपके वॉलेट में <b>{a}</b> बोनस जुड़ गया!", "💰 మీ వాలెట్‌లో <b>{a}</b> బోనస్ జమ అయింది!",
     "💰 உங்கள் வாலட்டில் <b>{a}</b> போனஸ் சேர்க்கப்பட்டது!", "💰 നിങ്ങളുടെ വാലറ്റിൽ <b>{a}</b> ബോണസ് ചേർത്തു!"),
    # ---- support ----
    ("💬 <b>SUPPORT</b>", "💬 <b>सपोर्ट</b>", "💬 <b>సపోర్ట్</b>", "💬 <b>ஆதரவு</b>", "💬 <b>സപ്പോർട്ട്</b>"),
    ("Need help with an order or payment? Contact our support team:",
     "ऑर्डर या पेमेंट में मदद चाहिए? हमारी सपोर्ट टीम से संपर्क करें:",
     "ఆర్డర్ లేదా చెల్లింపులో సహాయం కావాలా? మా సపోర్ట్ టీమ్‌ను సంప్రదించండి:",
     "ஆர்டர் அல்லது கட்டணத்தில் உதவி வேண்டுமா? எங்கள் ஆதரவு குழுவைத் தொடர்பு கொள்ளுங்கள்:",
     "ഓർഡറിലോ പേയ്‌മെന്റിലോ സഹായം വേണോ? ഞങ്ങളുടെ സപ്പോർട്ട് ടീമിനെ ബന്ധപ്പെടുക:"),
    ("👤 Support not configured yet", "👤 सपोर्ट अभी सेट नहीं है", "👤 సపోర్ట్ ఇంకా సెట్ చేయలేదు", "👤 ஆதரவு இன்னும் அமைக்கப்படவில்லை", "👤 സപ്പോർട്ട് ഇതുവരെ സജ്ജമാക്കിയിട്ടില്ല"),
    ("⏱️ We usually reply within a few hours.", "⏱️ हम आमतौर पर कुछ घंटों में जवाब देते हैं।",
     "⏱️ మేము సాధారణంగా కొన్ని గంటల్లో సమాధానం ఇస్తాము.", "⏱️ நாங்கள் பொதுவாக சில மணி நேரத்தில் பதில் அளிப்போம்.", "⏱️ ഞങ്ങൾ സാധാരണയായി ഏതാനും മണിക്കൂറിനുള്ളിൽ മറുപടി നൽകും."),
    # ---- profile ----
    ("👤 ── <b>YOUR PROFILE</b> ── 👤", "👤 ── <b>आपकी प्रोफ़ाइल</b> ── 👤", "👤 ── <b>మీ ప్రొఫైల్</b> ── 👤", "👤 ── <b>உங்கள் சுயவிவரம்</b> ── 👤", "👤 ── <b>നിങ്ങളുടെ പ്രൊഫൈൽ</b> ── 👤"),
    ("🚩 User ID: <code>{i}</code>", "🚩 यूज़र आईडी: <code>{i}</code>", "🚩 యూజర్ ఐడి: <code>{i}</code>", "🚩 பயனர் ஐடி: <code>{i}</code>", "🚩 യൂസർ ഐഡി: <code>{i}</code>"),
    ("🚩 Name: <b>{n}</b>", "🚩 नाम: <b>{n}</b>", "🚩 పేరు: <b>{n}</b>", "🚩 பெயர்: <b>{n}</b>", "🚩 പേര്: <b>{n}</b>"),
    ("🤖 Phone: N/A", "🤖 फ़ोन: उपलब्ध नहीं", "🤖 ఫోన్: లేదు", "🤖 தொலைபேசி: இல்லை", "🤖 ഫോൺ: ലഭ്യമല്ല"),
    ("👑 Account: {b} | 🟢 Active", "👑 अकाउंट: {b} | 🟢 सक्रिय", "👑 ఖాతా: {b} | 🟢 యాక్టివ్", "👑 கணக்கு: {b} | 🟢 செயலில்", "👑 അക്കൗണ്ട്: {b} | 🟢 സജീവം"),
    ("👑 Reseller User", "👑 रीसेलर यूज़र", "👑 రీసెల్లర్ యూజర్", "👑 மறுவிற்பனையாளர்", "👑 റീസെല്ലർ യൂസർ"),
    ("👤 Regular", "👤 सामान्य", "👤 సాధారణ", "👤 சாதாரண", "👤 സാധാരണ"),
    ("💰 ── <b>Balance</b> ── 💰", "💰 ── <b>बैलेंस</b> ── 💰", "💰 ── <b>బ్యాలెన్స్</b> ── 💰", "💰 ── <b>இருப்பு</b> ── 💰", "💰 ── <b>ബാലൻസ്</b> ── 💰"),
    ("🎟️ Current: {v}", "🎟️ वर्तमान: {v}", "🎟️ ప్రస్తుతం: {v}", "🎟️ தற்போது: {v}", "🎟️ നിലവിൽ: {v}"),
    ("📊 ── <b>Statistics</b> ── 📊", "📊 ── <b>आँकड़े</b> ── 📊", "📊 ── <b>గణాంకాలు</b> ── 📊", "📊 ── <b>புள்ளிவிவரம்</b> ── 📊", "📊 ── <b>കണക്കുകൾ</b> ── 📊"),
    ("🎯 Orders: {n}", "🎯 ऑर्डर: {n}", "🎯 ఆర్డర్లు: {n}", "🎯 ஆர்டர்கள்: {n}", "🎯 ഓർഡറുകൾ: {n}"),
    ("💸 Spent: {v}", "💸 खर्च: {v}", "💸 ఖర్చు: {v}", "💸 செலவு: {v}", "💸 ചെലവ്: {v}"),
    ("🤝 Referrals: {n}", "🤝 रेफरल: {n}", "🤝 రెఫరల్స్: {n}", "🤝 பரிந்துரைகள்: {n}", "🤝 റഫറലുകൾ: {n}"),
    ("🎁 Referral Earnings: {v}", "🎁 रेफरल कमाई: {v}", "🎁 రెఫరల్ సంపాదన: {v}", "🎁 பரிந்துரை வருமானம்: {v}", "🎁 റഫറൽ വരുമാനം: {v}"),
    ("🕒 Joined: {d}", "🕒 जुड़े: {d}", "🕒 చేరినది: {d}", "🕒 சேர்ந்த நாள்: {d}", "🕒 ചേർന്നത്: {d}"),
    # ---- reseller (customer messages; the plan text itself is written by the admin) ----
    ("👑 <b>Reseller Plan</b>", "👑 <b>रीसेलर प्लान</b>", "👑 <b>రీసెల్లర్ ప్లాన్</b>", "👑 <b>மறுவிற்பனையாளர் திட்டம்</b>", "👑 <b>റീസെല്ലർ പ്ലാൻ</b>"),
    ("🎉 <b>CONGRATULATIONS!</b>", "🎉 <b>बधाई हो!</b>", "🎉 <b>అభినందనలు!</b>", "🎉 <b>வாழ்த்துகள்!</b>", "🎉 <b>അഭിനന്ദനങ്ങൾ!</b>"),
    ("You are now officially a <b>👑 VIP Reseller!</b>", "अब आप आधिकारिक रूप से <b>👑 VIP रीसेलर</b> हैं!",
     "ఇప్పుడు మీరు అధికారికంగా <b>👑 VIP రీసెల్లర్</b>!", "இப்போது நீங்கள் அதிகாரப்பூர்வமாக <b>👑 VIP மறுவிற்பனையாளர்</b>!", "ഇപ്പോൾ നിങ്ങൾ ഔദ്യോഗികമായി <b>👑 VIP റീസെല്ലർ</b> ആണ്!"),
    ("💸 Activation fee deducted: {v}", "💸 एक्टिवेशन फ़ीस कटी: {v}", "💸 యాక్టివేషన్ ఫీజు తగ్గించబడింది: {v}", "💸 செயல்படுத்தும் கட்டணம் கழிக்கப்பட்டது: {v}", "💸 ആക്ടിവേഷൻ ഫീസ് കുറച്ചു: {v}"),
    ("💰 Wallet balance now: {v}", "💰 अब वॉलेट बैलेंस: {v}", "💰 ఇప్పుడు వాలెట్ బ్యాలెన్స్: {v}", "💰 இப்போதைய வாலட் இருப்பு: {v}", "💰 ഇപ്പോഴത്തെ വാലറ്റ് ബാലൻസ്: {v}"),
    ("👑 <b>You are already a VIP Reseller!</b>", "👑 <b>आप पहले से VIP रीसेलर हैं!</b>", "👑 <b>మీరు ఇప్పటికే VIP రీసెల్లర్!</b>",
     "👑 <b>நீங்கள் ஏற்கனவே VIP மறுவிற்பனையாளர்!</b>", "👑 <b>നിങ്ങൾ ഇതിനകം VIP റീസെല്ലർ ആണ്!</b>"),
    ("Your reseller plan is already active — nothing was charged.", "आपका रीसेलर प्लान पहले से चालू है — कुछ भी नहीं काटा गया।",
     "మీ రీసెల్లర్ ప్లాన్ ఇప్పటికే యాక్టివ్‌లో ఉంది — ఏమీ తగ్గించబడలేదు.", "உங்கள் மறுவிற்பனையாளர் திட்டம் ஏற்கனவே செயலில் உள்ளது — எதுவும் கழிக்கப்படவில்லை.",
     "നിങ്ങളുടെ റീസെല്ലർ പ്ലാൻ ഇതിനകം സജീവമാണ് — ഒന്നും കുറച്ചിട്ടില്ല."),
    ("⚠️ <b>Insufficient Balance!</b>", "⚠️ <b>बैलेंस कम है!</b>", "⚠️ <b>బ్యాలెన్స్ సరిపోదు!</b>", "⚠️ <b>இருப்பு போதவில்லை!</b>", "⚠️ <b>ബാലൻസ് മതിയാകില്ല!</b>"),
    ("💰 Required: {v}", "💰 ज़रूरी: {v}", "💰 అవసరం: {v}", "💰 தேவை: {v}", "💰 ആവശ്യം: {v}"),
    ("💸 Your Balance: {v}", "💸 आपका बैलेंस: {v}", "💸 మీ బ్యాలెన్స్: {v}", "💸 உங்கள் இருப்பு: {v}", "💸 നിങ്ങളുടെ ബാലൻസ്: {v}"),
    ("Please add balance to your wallet to activate the VIP Reseller Plan.", "VIP रीसेलर प्लान चालू करने के लिए अपने वॉलेट में बैलेंस जोड़ें।",
     "VIP రీసెల్లర్ ప్లాన్ యాక్టివేట్ చేయడానికి మీ వాలెట్‌లో బ్యాలెన్స్ జోడించండి.", "VIP மறுவிற்பனையாளர் திட்டத்தை செயல்படுத்த உங்கள் வாலட்டில் பணம் சேர்க்கவும்.",
     "VIP റീസെല്ലർ പ്ലാൻ സജീവമാക്കാൻ നിങ്ങളുടെ വാലറ്റിൽ ബാലൻസ് ചേർക്കുക."),
    ("⚠️ Please try again — could not activate right now.", "⚠️ कृपया फिर कोशिश करें — अभी चालू नहीं हो सका।",
     "⚠️ దయచేసి మళ్లీ ప్రయత్నించండి — ఇప్పుడు యాక్టివేట్ కాలేదు.", "⚠️ தயவுசெய்து மீண்டும் முயற்சிக்கவும் — இப்போது செயல்படுத்த முடியவில்லை.",
     "⚠️ ദയവായി വീണ്ടും ശ്രമിക്കുക — ഇപ്പോൾ സജീവമാക്കാനായില്ല."),
    # ---- language screen ----
    ("🌐 <b>Choose your language</b>", "🌐 <b>अपनी भाषा चुनें</b>", "🌐 <b>మీ భాషను ఎంచుకోండి</b>", "🌐 <b>உங்கள் மொழியைத் தேர்ந்தெடுக்கவும்</b>", "🌐 <b>നിങ്ങളുടെ ഭാഷ തിരഞ്ഞെടുക്കുക</b>"),
    ("Tap a language below. The whole bot will switch to it.", "नीचे से भाषा चुनें। पूरा बॉट उसी भाषा में बदल जाएगा।",
     "క్రింద భాషను నొక్కండి. బాట్ మొత్తం ఆ భాషలోకి మారుతుంది.", "கீழே ஒரு மொழியைத் தொடவும். முழு பாட்டும் அந்த மொழிக்கு மாறும்.", "താഴെ ഒരു ഭാഷ തൊടുക. ബോട്ട് മുഴുവൻ ആ ഭാഷയിലേക്ക് മാറും."),
    ("✅ Language changed to <b>{l}</b>.", "✅ भाषा बदलकर <b>{l}</b> हो गई।", "✅ భాష <b>{l}</b>కు మార్చబడింది.", "✅ மொழி <b>{l}</b> ஆக மாற்றப்பட்டது.", "✅ ഭാഷ <b>{l}</b> ആയി മാറ്റി."),
    ("✅ Language updated", "✅ भाषा बदल गई", "✅ భాష మార్చబడింది", "✅ மொழி மாற்றப்பட்டது", "✅ ഭാഷ മാറ്റി"),
    # ---- add balance ----
    ("💰 <b>ADD BALANCE</b>", "💰 <b>बैलेंस जोड़ें</b>", "💰 <b>బ్యాలెన్స్ జోడించండి</b>", "💰 <b>இருப்பு சேர்க்க</b>", "💰 <b>ബാലൻസ് ചേർക്കുക</b>"),
    ("💳 Pay with any UPI app (GPay, PhonePe, Paytm...)", "💳 किसी भी UPI ऐप (GPay, PhonePe, Paytm...) से भुगतान करें",
     "💳 ఏదైనా UPI యాప్ (GPay, PhonePe, Paytm...) తో చెల్లించండి", "💳 எந்த UPI ஆப் (GPay, PhonePe, Paytm...) மூலமும் செலுத்தலாம்", "💳 ഏതെങ്കിലും UPI ആപ്പ് (GPay, PhonePe, Paytm...) വഴി പേയ്‌മെന്റ് ചെയ്യുക"),
    ("⏱️ Balance is added as soon as your payment is confirmed.", "⏱️ पेमेंट की पुष्टि होते ही बैलेंस जुड़ जाता है।",
     "⏱️ చెల్లింపు నిర్ధారణ అయిన వెంటనే బ్యాలెన్స్ జమ అవుతుంది.", "⏱️ கட்டணம் உறுதியானவுடன் இருப்பு சேர்க்கப்படும்.", "⏱️ പേയ്‌മെന്റ് സ്ഥിരീകരിച്ചാലുടൻ ബാലൻസ് ചേർക്കും."),
    ("💸 <b>Add Balance</b>", "💸 <b>बैलेंस जोड़ें</b>", "💸 <b>బ్యాలెన్స్ జోడించండి</b>", "💸 <b>இருப்பு சேர்க்க</b>", "💸 <b>ബാലൻസ് ചേർക്കുക</b>"),
    ("Current balance: {v}", "वर्तमान बैलेंस: {v}", "ప్రస్తుత బ్యాలెన్స్: {v}", "தற்போதைய இருப்பு: {v}", "നിലവിലെ ബാലൻസ്: {v}"),
    ("Pick a quick amount below, or enter a custom amount.", "नीचे से कोई रकम चुनें, या अपनी रकम दर्ज करें।",
     "క్రింద ఒక మొత్తాన్ని ఎంచుకోండి, లేదా మీ మొత్తాన్ని నమోదు చేయండి.", "கீழே ஒரு தொகையைத் தேர்ந்தெடுக்கவும், அல்லது உங்கள் தொகையை உள்ளிடவும்.", "താഴെ ഒരു തുക തിരഞ്ഞെടുക്കുക, അല്ലെങ്കിൽ സ്വന്തം തുക നൽകുക."),
    ("Min: {a} · Max: {b}", "न्यूनतम: {a} · अधिकतम: {b}", "కనిష్టం: {a} · గరిష్టం: {b}", "குறைந்தபட்சம்: {a} · அதிகபட்சம்: {b}", "കുറഞ്ഞത്: {a} · കൂടിയത്: {b}"),
    ("💰 <b>Enter Amount</b>", "💰 <b>रकम दर्ज करें</b>", "💰 <b>మొత్తాన్ని నమోదు చేయండి</b>", "💰 <b>தொகையை உள்ளிடவும்</b>", "💰 <b>തുക നൽകുക</b>"),
    ("⚠️ Minimum deposit is {v}!", "⚠️ न्यूनतम जमा {v} है!", "⚠️ కనిష్ట డిపాజిట్ {v}!", "⚠️ குறைந்தபட்ச டெபாசிட் {v}!", "⚠️ കുറഞ്ഞ ഡെപ്പോസിറ്റ് {v} ആണ്!"),
    ("⚠️ Maximum deposit is {v}!", "⚠️ अधिकतम जमा {v} है!", "⚠️ గరిష్ట డిపాజిట్ {v}!", "⚠️ அதிகபட்ச டெபாசிட் {v}!", "⚠️ കൂടിയ ഡെപ്പോസിറ്റ് {v} ആണ്!"),
    ("✅ You are already a <b>👑 VIP Reseller</b>.", "✅ आप पहले से <b>👑 VIP रीसेलर</b> हैं।", "✅ మీరు ఇప్పటికే <b>👑 VIP రీసెల్లర్</b>.",
     "✅ நீங்கள் ஏற்கனவே <b>👑 VIP மறுவிற்பனையாளர்</b>.", "✅ നിങ്ങൾ ഇതിനകം <b>👑 VIP റീസെല്ലർ</b> ആണ്."),
    ("⚠️ The reseller plan is not available right now.", "⚠️ रीसेलर प्लान अभी उपलब्ध नहीं है।", "⚠️ రీసెల్లర్ ప్లాన్ ప్రస్తుతం అందుబాటులో లేదు.",
     "⚠️ மறுவிற்பனையாளர் திட்டம் இப்போது கிடைக்கவில்லை.", "⚠️ റീസെല്ലർ പ്ലാൻ ഇപ്പോൾ ലഭ്യമല്ല."),
    ("✅ Reseller plan is already active on your account.", "✅ आपके अकाउंट पर रीसेलर प्लान पहले से चालू है।", "✅ మీ ఖాతాలో రీసెల్లర్ ప్లాన్ ఇప్పటికే యాక్టివ్‌గా ఉంది.",
     "✅ உங்கள் கணக்கில் மறுவிற்பனையாளர் திட்டம் ஏற்கனவே செயலில் உள்ளது.", "✅ നിങ്ങളുടെ അക്കൗണ്ടിൽ റീസെല്ലർ പ്ലാൻ ഇതിനകം സജീവമാണ്."),
    # ---- order summary / deposit ----
    ("📋 <b>ORDER SUMMARY</b>", "📋 <b>ऑर्डर सारांश</b>", "📋 <b>ఆర్డర్ సారాంశం</b>", "📋 <b>ஆர்டர் சுருக்கம்</b>", "📋 <b>ഓർഡർ സംഗ്രഹം</b>"),
    ("💰 Add Balance to Wallet", "💰 वॉलेट में बैलेंस जोड़ें", "💰 వాలెట్‌కు బ్యాలెన్స్ జోడించండి", "💰 வாலட்டில் இருப்பு சேர்க்க", "💰 വാലറ്റിലേക്ക് ബാലൻസ് ചേർക്കുക"),
    ("💵 Amount: {v}", "💵 रकम: {v}", "💵 మొత్తం: {v}", "💵 தொகை: {v}", "💵 തുക: {v}"),
    ("💰 <b>Final Total: {v}</b>", "💰 <b>कुल देय: {v}</b>", "💰 <b>మొత్తం చెల్లించాల్సినది: {v}</b>", "💰 <b>மொத்தம் செலுத்த வேண்டியது: {v}</b>", "💰 <b>ആകെ അടയ്ക്കേണ്ടത്: {v}</b>"),
    ("Select payment method below:", "नीचे भुगतान का तरीका चुनें:", "క్రింద చెల్లింపు పద్ధతిని ఎంచుకోండి:", "கீழே கட்டண முறையைத் தேர்ந்தெடுக்கவும்:", "താഴെ പേയ്‌മെന്റ് രീതി തിരഞ്ഞെടുക്കുക:"),
    ("⚠️ Session expired. Choose the amount again.", "⚠️ सेशन समाप्त हो गया। रकम दोबारा चुनें।", "⚠️ సెషన్ ముగిసింది. మొత్తాన్ని మళ్లీ ఎంచుకోండి.",
     "⚠️ அமர்வு முடிந்தது. தொகையை மீண்டும் தேர்ந்தெடுக்கவும்.", "⚠️ സെഷൻ കഴിഞ്ഞു. തുക വീണ്ടും തിരഞ്ഞെടുക്കുക."),
    ("⚠️ Binance payment is not available right now.", "⚠️ Binance पेमेंट अभी उपलब्ध नहीं है।", "⚠️ Binance చెల్లింపు ప్రస్తుతం అందుబాటులో లేదు.",
     "⚠️ Binance கட்டணம் இப்போது கிடைக்கவில்லை.", "⚠️ Binance പേയ്‌മെന്റ് ഇപ്പോൾ ലഭ്യമല്ല."),
    ("🟡 <b>BINANCE USDT PAYMENT</b>", "🟡 <b>BINANCE USDT पेमेंट</b>", "🟡 <b>BINANCE USDT చెల్లింపు</b>", "🟡 <b>BINANCE USDT கட்டணம்</b>", "🟡 <b>BINANCE USDT പേയ്‌മെന്റ്</b>"),
    ("💰 Add Balance: {v}", "💰 बैलेंस जोड़ें: {v}", "💰 బ్యాలెన్స్ జోడింపు: {v}", "💰 இருப்பு சேர்ப்பு: {v}", "💰 ബാലൻസ് ചേർക്കൽ: {v}"),
    ("💵 Send exactly: <b>{u} USDT</b>", "💵 ठीक इतना भेजें: <b>{u} USDT</b>", "💵 సరిగ్గా ఇంత పంపండి: <b>{u} USDT</b>", "💵 சரியாக இவ்வளவு அனுப்பவும்: <b>{u} USDT</b>", "💵 കൃത്യമായി ഇത്ര അയയ്ക്കുക: <b>{u} USDT</b>"),
    ("📨 Binance Pay ID: <code>{i}</code>", "📨 Binance Pay आईडी: <code>{i}</code>", "📨 Binance Pay ఐడి: <code>{i}</code>", "📨 Binance Pay ஐடி: <code>{i}</code>", "📨 Binance Pay ഐഡി: <code>{i}</code>"),
    ("After paying, tap <b>✅ I have paid</b>. Balance is added as soon as the admin confirms.",
     "भुगतान के बाद <b>✅ I have paid</b> दबाएँ। एडमिन की पुष्टि होते ही बैलेंस जुड़ जाएगा।",
     "చెల్లించిన తర్వాత <b>✅ I have paid</b> నొక్కండి. అడ్మిన్ నిర్ధారించిన వెంటనే బ్యాలెన్స్ జమ అవుతుంది.",
     "பணம் செலுத்திய பின் <b>✅ I have paid</b> அழுத்தவும். அட்மின் உறுதிப்படுத்தியவுடன் இருப்பு சேர்க்கப்படும்.",
     "പണമടച്ച ശേഷം <b>✅ I have paid</b> അമർത്തുക. അഡ്മിൻ സ്ഥിരീകരിച്ചാലുടൻ ബാലൻസ് ചേർക്കും."),
    ("⚠️ Payments are temporarily unavailable. Please try again later or contact support.",
     "⚠️ पेमेंट अभी अस्थायी रूप से बंद है। कृपया बाद में कोशिश करें या सपोर्ट से संपर्क करें।",
     "⚠️ చెల్లింపులు తాత్కాలికంగా అందుబాటులో లేవు. దయచేసి తర్వాత ప్రయత్నించండి లేదా సపోర్ట్‌ను సంప్రదించండి.",
     "⚠️ கட்டணங்கள் தற்காலிகமாகக் கிடைக்கவில்லை. பிறகு முயற்சிக்கவும் அல்லது ஆதரவைத் தொடர்பு கொள்ளவும்.",
     "⚠️ പേയ്‌മെന്റുകൾ താൽക്കാലികമായി ലഭ്യമല്ല. പിന്നീട് ശ്രമിക്കുക അല്ലെങ്കിൽ സപ്പോർട്ടുമായി ബന്ധപ്പെടുക."),
    ("⚠️ Could not create the payment. Please try again in a moment.", "⚠️ पेमेंट नहीं बन सका। कृपया थोड़ी देर में फिर कोशिश करें।",
     "⚠️ చెల్లింపును సృష్టించలేకపోయాం. దయచేసి కొద్దిసేపటి తర్వాత మళ్లీ ప్రయత్నించండి.", "⚠️ கட்டணத்தை உருவாக்க முடியவில்லை. சிறிது நேரம் கழித்து மீண்டும் முயற்சிக்கவும்.",
     "⚠️ പേയ്‌മെന്റ് ഉണ്ടാക്കാനായില്ല. അൽപ്പം കഴിഞ്ഞ് വീണ്ടും ശ്രമിക്കുക."),
    ("Scan &amp; pay exactly <b>{v}</b> via any UPI app.", "किसी भी UPI ऐप से स्कैन करके ठीक <b>{v}</b> का भुगतान करें।",
     "ఏదైనా UPI యాప్‌తో స్కాన్ చేసి సరిగ్గా <b>{v}</b> చెల్లించండి.", "எந்த UPI ஆப் மூலமும் ஸ்கேன் செய்து சரியாக <b>{v}</b> செலுத்தவும்.", "ഏതെങ്കിലും UPI ആപ്പ് ഉപയോഗിച്ച് സ്കാൻ ചെയ്ത് കൃത്യമായി <b>{v}</b> അടയ്ക്കുക."),
    ("Your balance updates <b>automatically</b> the moment payment is confirmed — no button tap needed.",
     "भुगतान की पुष्टि होते ही आपका बैलेंस <b>अपने आप</b> अपडेट हो जाएगा — कोई बटन दबाने की ज़रूरत नहीं।",
     "చెల్లింపు నిర్ధారణ అయిన వెంటనే మీ బ్యాలెన్స్ <b>ఆటోమేటిక్‌గా</b> అప్‌డేట్ అవుతుంది — ఏ బటన్ నొక్కాల్సిన అవసరం లేదు.",
     "கட்டணம் உறுதியானவுடன் உங்கள் இருப்பு <b>தானாகவே</b> புதுப்பிக்கப்படும் — எந்த பட்டனையும் அழுத்த வேண்டியதில்லை.",
     "പേയ്‌മെന്റ് സ്ഥിരീകരിച്ചാലുടൻ നിങ്ങളുടെ ബാലൻസ് <b>സ്വയം</b> അപ്ഡേറ്റ് ആകും — ബട്ടൺ അമർത്തേണ്ടതില്ല."),
    ("After paying, tap <b>✅ I have paid</b>. It is confirmed as soon as the admin approves the payment.",
     "भुगतान के बाद <b>✅ I have paid</b> दबाएँ। एडमिन के मंज़ूर करते ही पुष्टि हो जाएगी।",
     "చెల్లించిన తర్వాత <b>✅ I have paid</b> నొక్కండి. అడ్మిన్ ఆమోదించిన వెంటనే నిర్ధారణ అవుతుంది.",
     "பணம் செலுத்திய பின் <b>✅ I have paid</b> அழுத்தவும். அட்மின் ஒப்புதல் அளித்தவுடன் உறுதியாகும்.",
     "പണമടച്ച ശേഷം <b>✅ I have paid</b> അമർത്തുക. അഡ്മിൻ അംഗീകരിച്ചാലുടൻ സ്ഥിരീകരിക്കും."),
    ("🛒 <b>{p} — {l}</b>", "🛒 <b>{p} — {l}</b>", "🛒 <b>{p} — {l}</b>", "🛒 <b>{p} — {l}</b>", "🛒 <b>{p} — {l}</b>"),
    ("💰 Balance: {a}  |  Price: {b}", "💰 बैलेंस: {a}  |  कीमत: {b}", "💰 బ్యాలెన్స్: {a}  |  ధర: {b}", "💰 இருப்பு: {a}  |  விலை: {b}", "💰 ബാലൻസ്: {a}  |  വില: {b}"),
    ("⏰ <b>This QR expires in {t} if payment isn't completed.</b>", "⏰ <b>भुगतान पूरा न होने पर यह QR {t} में समाप्त हो जाएगा।</b>",
     "⏰ <b>చెల్లింపు పూర్తికాకపోతే ఈ QR {t} లో గడువు ముగుస్తుంది.</b>", "⏰ <b>கட்டணம் முடிக்கப்படாவிட்டால் இந்த QR {t} இல் காலாவதியாகும்.</b>",
     "⏰ <b>പേയ്‌മെന്റ് പൂർത്തിയായില്ലെങ്കിൽ ഈ QR {t} കഴിഞ്ഞാൽ അവസാനിക്കും.</b>"),
    ("💵 <b>Add Balance — {v}</b>", "💵 <b>बैलेंस जोड़ें — {v}</b>", "💵 <b>బ్యాలెన్స్ జోడించండి — {v}</b>", "💵 <b>இருப்பு சேர்க்க — {v}</b>", "💵 <b>ബാലൻസ് ചേർക്കുക — {v}</b>"),
    ("💳 UPI ID: <code>{i}</code>", "💳 UPI आईडी: <code>{i}</code>", "💳 UPI ఐడి: <code>{i}</code>", "💳 UPI ஐடி: <code>{i}</code>", "💳 UPI ഐഡി: <code>{i}</code>"),
    ("🕒 <b>PAYMENT EXPIRED</b>", "🕒 <b>पेमेंट की समय सीमा समाप्त</b>", "🕒 <b>చెల్లింపు గడువు ముగిసింది</b>", "🕒 <b>கட்டணம் காலாவதியானது</b>", "🕒 <b>പേയ്‌മെന്റ് കാലഹരണപ്പെട്ടു</b>"),
    ("— 🔒 Order: <code>{o}</code>", "— 🔒 ऑर्डर: <code>{o}</code>", "— 🔒 ఆర్డర్: <code>{o}</code>", "— 🔒 ஆர்டர்: <code>{o}</code>", "— 🔒 ഓർഡർ: <code>{o}</code>"),
    ("— ❌ No payment detected within {t}", "— ❌ {t} के अंदर कोई भुगतान नहीं मिला", "— ❌ {t} లోపల ఎలాంటి చెల్లింపు కనుగొనబడలేదు", "— ❌ {t} க்குள் எந்த கட்டணமும் கண்டறியப்படவில்லை", "— ❌ {t} ഉള്ളിൽ പേയ്‌മെന്റ് ഒന്നും കണ്ടെത്തിയില്ല"),
    ("— ℹ️ If you already paid the exact amount, it is still credited automatically (up to 1 hour).",
     "— ℹ️ अगर आपने ठीक रकम पहले ही दी है, तो वह अब भी अपने आप जमा हो जाएगी (1 घंटे तक)।",
     "— ℹ️ మీరు ఇప్పటికే సరైన మొత్తం చెల్లించి ఉంటే, అది ఇంకా ఆటోమేటిక్‌గా జమ అవుతుంది (1 గంట వరకు).",
     "— ℹ️ நீங்கள் ஏற்கனவே சரியான தொகையை செலுத்தியிருந்தால், அது இன்னும் தானாகவே வரவு வைக்கப்படும் (1 மணி நேரம் வரை).",
     "— ℹ️ നിങ്ങൾ ഇതിനകം കൃത്യമായ തുക അടച്ചിട്ടുണ്ടെങ്കിൽ, അത് ഇപ്പോഴും സ്വയം വരവ് വയ്ക്കും (1 മണിക്കൂർ വരെ)."),
    ("<i>Paid but nothing arrived? Tap the button below. Or tap /start for a new order.</i>",
     "<i>पैसे दिए पर कुछ नहीं आया? नीचे का बटन दबाएँ। या नए ऑर्डर के लिए /start दबाएँ।</i>",
     "<i>చెల్లించినా ఏమీ రాలేదా? క్రింది బటన్ నొక్కండి. లేదా కొత్త ఆర్డర్ కోసం /start నొక్కండి.</i>",
     "<i>பணம் செலுத்தியும் எதுவும் வரவில்லையா? கீழுள்ள பட்டனை அழுத்தவும். அல்லது புதிய ஆர்டருக்கு /start அழுத்தவும்.</i>",
     "<i>പണമടച്ചിട്ടും ഒന്നും വന്നില്ലേ? താഴെയുള്ള ബട്ടൺ അമർത്തുക. അല്ലെങ്കിൽ പുതിയ ഓർഡറിന് /start അമർത്തുക.</i>"),
    ("— ❌ Not paid within {t}", "— ❌ {t} के अंदर भुगतान नहीं हुआ", "— ❌ {t} లోపల చెల్లించలేదు", "— ❌ {t} க்குள் செலுத்தப்படவில்லை", "— ❌ {t} ഉള്ളിൽ അടച്ചില്ല"),
    ("— 🔒 QR / payment details are no longer valid", "— 🔒 QR / पेमेंट विवरण अब मान्य नहीं हैं", "— 🔒 QR / చెల్లింపు వివరాలు ఇక చెల్లవు", "— 🔒 QR / கட்டண விவரங்கள் இனி செல்லாது", "— 🔒 QR / പേയ്‌മെന്റ് വിവരങ്ങൾ ഇനി സാധുവല്ല"),
    ("<i>Tap /start to begin a new order.</i>", "<i>नया ऑर्डर शुरू करने के लिए /start दबाएँ।</i>", "<i>కొత్త ఆర్డర్ ప్రారంభించడానికి /start నొక్కండి.</i>", "<i>புதிய ஆர்டரைத் தொடங்க /start அழுத்தவும்.</i>", "<i>പുതിയ ഓർഡർ തുടങ്ങാൻ /start അമർത്തുക.</i>"),
    ("⏳ <b>Payment not received yet</b>", "⏳ <b>पेमेंट अभी तक नहीं मिला</b>", "⏳ <b>చెల్లింపు ఇంకా అందలేదు</b>", "⏳ <b>கட்டணம் இன்னும் வரவில்லை</b>", "⏳ <b>പേയ്‌മെന്റ് ഇതുവരെ ലഭിച്ചില്ല</b>"),
    ("If you already paid, the admin has been told and will confirm it shortly. It is also still detected automatically.",
     "अगर आपने भुगतान कर दिया है, तो एडमिन को बता दिया गया है और वे जल्द पुष्टि करेंगे। यह अब भी अपने आप पहचाना जाएगा।",
     "మీరు చెల్లించి ఉంటే, అడ్మిన్‌కు తెలియజేయబడింది, త్వరలో నిర్ధారిస్తారు. ఇది ఇంకా ఆటోమేటిక్‌గా కూడా గుర్తించబడుతుంది.",
     "நீங்கள் செலுத்தியிருந்தால், அட்மினுக்குத் தெரிவிக்கப்பட்டுள்ளது, விரைவில் உறுதிப்படுத்துவார். இது இன்னும் தானாகவும் கண்டறியப்படும்.",
     "നിങ്ങൾ പണമടച്ചിട്ടുണ്ടെങ്കിൽ അഡ്മിനെ അറിയിച്ചിട്ടുണ്ട്, ഉടൻ സ്ഥിരീകരിക്കും. ഇത് ഇപ്പോഴും സ്വയം കണ്ടെത്തപ്പെടും."),
    ("⚠️ Payment not found.", "⚠️ पेमेंट नहीं मिला।", "⚠️ చెల్లింపు కనుగొనబడలేదు.", "⚠️ கட்டணம் கண்டறியப்படவில்லை.", "⚠️ പേയ്‌മെന്റ് കണ്ടെത്തിയില്ല."),
    ("⏳ Already checking your payment. You will get a message here the moment it arrives.",
     "⏳ आपका पेमेंट पहले से जाँचा जा रहा है। आते ही आपको यहाँ संदेश मिलेगा।", "⏳ మీ చెల్లింపును ఇప్పటికే తనిఖీ చేస్తున్నాం. అది రాగానే ఇక్కడ సందేశం వస్తుంది.",
     "⏳ உங்கள் கட்டணம் ஏற்கனவே சரிபார்க்கப்படுகிறது. வந்தவுடன் இங்கே செய்தி வரும்.", "⏳ നിങ്ങളുടെ പേയ്‌മെന്റ് ഇതിനകം പരിശോധിക്കുന്നുണ്ട്. വന്നാലുടൻ ഇവിടെ സന്ദേശം ലഭിക്കും."),
    ("✅ This payment was already confirmed.", "✅ यह पेमेंट पहले ही कन्फर्म हो चुका है।", "✅ ఈ చెల్లింపు ఇప్పటికే నిర్ధారించబడింది.", "✅ இந்தக் கட்டணம் ஏற்கனவே உறுதிப்படுத்தப்பட்டது.", "✅ ഈ പേയ്‌മെന്റ് ഇതിനകം സ്ഥിരീകരിച്ചു."),
    ("⚠️ This payment session has ended. Tap /start to begin again.", "⚠️ यह पेमेंट सेशन समाप्त हो गया है। दोबारा शुरू करने के लिए /start दबाएँ।",
     "⚠️ ఈ చెల్లింపు సెషన్ ముగిసింది. మళ్లీ ప్రారంభించడానికి /start నొక్కండి.", "⚠️ இந்தக் கட்டண அமர்வு முடிந்தது. மீண்டும் தொடங்க /start அழுத்தவும்.",
     "⚠️ ഈ പേയ്‌മെന്റ് സെഷൻ അവസാനിച്ചു. വീണ്ടും തുടങ്ങാൻ /start അമർത്തുക."),
    ("Checking your payment…", "आपका पेमेंट जाँचा जा रहा है…", "మీ చెల్లింపును తనిఖీ చేస్తున్నాం…", "உங்கள் கட்டணத்தைச் சரிபார்க்கிறோம்…", "നിങ്ങളുടെ പേയ്‌മെന്റ് പരിശോധിക്കുന്നു…"),
    ("⏳ <b>Checking your payment…</b>", "⏳ <b>आपका पेमेंट जाँचा जा रहा है…</b>", "⏳ <b>మీ చెల్లింపును తనిఖీ చేస్తున్నాం…</b>", "⏳ <b>உங்கள் கட்டணத்தைச் சரிபார்க்கிறோம்…</b>", "⏳ <b>നിങ്ങളുടെ പേയ്‌മെന്റ് പരിശോധിക്കുന്നു…</b>"),
    ("I am checking every 5 seconds. Your key / balance arrives here as soon as the payment is found. You do not need to press anything again.",
     "मैं हर 5 सेकंड में जाँच रहा हूँ। पेमेंट मिलते ही आपकी की / बैलेंस यहीं आ जाएगा। दोबारा कुछ दबाने की ज़रूरत नहीं।",
     "నేను ప్రతి 5 సెకన్లకు తనిఖీ చేస్తున్నాను. చెల్లింపు దొరికిన వెంటనే మీ కీ / బ్యాలెన్స్ ఇక్కడికే వస్తుంది. మళ్లీ ఏమీ నొక్కాల్సిన అవసరం లేదు.",
     "நான் ஒவ்வொரு 5 விநாடிக்கும் சரிபார்க்கிறேன். கட்டணம் கிடைத்தவுடன் உங்கள் கீ / இருப்பு இங்கே வரும். மீண்டும் எதுவும் அழுத்த வேண்டியதில்லை.",
     "ഞാൻ ഓരോ 5 സെക്കൻഡിലും പരിശോധിക്കുന്നു. പേയ്‌മെന്റ് കണ്ടെത്തിയാലുടൻ നിങ്ങളുടെ കീ / ബാലൻസ് ഇവിടെ എത്തും. വീണ്ടും ഒന്നും അമർത്തേണ്ടതില്ല."),
    ("Submitted for verification ✅", "सत्यापन के लिए भेजा गया ✅", "ధృవీకరణ కోసం సమర్పించబడింది ✅", "சரிபார்ப்புக்கு சமர்ப்பிக்கப்பட்டது ✅", "പരിശോധനയ്ക്കായി സമർപ്പിച്ചു ✅"),
    ("⏳ <b>Payment submitted!</b>", "⏳ <b>पेमेंट भेज दिया गया!</b>", "⏳ <b>చెల్లింపు సమర్పించబడింది!</b>", "⏳ <b>கட்டணம் சமர்ப்பிக்கப்பட்டது!</b>", "⏳ <b>പേയ്‌മെന്റ് സമർപ്പിച്ചു!</b>"),
    ("The admin is verifying your payment. You will get a message here as soon as it is confirmed.",
     "एडमिन आपका पेमेंट जाँच रहे हैं। पुष्टि होते ही आपको यहाँ संदेश मिलेगा।", "అడ్మిన్ మీ చెల్లింపును ధృవీకరిస్తున్నారు. నిర్ధారణ అయిన వెంటనే ఇక్కడ సందేశం వస్తుంది.",
     "அட்மின் உங்கள் கட்டணத்தைச் சரிபார்க்கிறார். உறுதியானவுடன் இங்கே செய்தி வரும்.", "അഡ്മിൻ നിങ്ങളുടെ പേയ്‌മെന്റ് പരിശോധിക്കുന്നു. സ്ഥിരീകരിച്ചാലുടൻ ഇവിടെ സന്ദേശം ലഭിക്കും."),
    ("Already processed.", "पहले ही प्रोसेस हो चुका है।", "ఇప్పటికే ప్రాసెస్ అయింది.", "ஏற்கனவே செயலாக்கப்பட்டது.", "ഇതിനകം പ്രോസസ് ചെയ്തു."),
    ("❌ <b>Payment not verified</b>", "❌ <b>पेमेंट सत्यापित नहीं हुआ</b>", "❌ <b>చెల్లింపు ధృవీకరించబడలేదు</b>", "❌ <b>கட்டணம் சரிபார்க்கப்படவில்லை</b>", "❌ <b>പേയ്‌മെന്റ് സ്ഥിരീകരിച്ചില്ല</b>"),
    ("We could not confirm this payment. If you already paid, please contact support with your order ID.",
     "हम इस पेमेंट की पुष्टि नहीं कर सके। अगर आपने भुगतान कर दिया है, तो कृपया अपनी ऑर्डर आईडी के साथ सपोर्ट से संपर्क करें।",
     "ఈ చెల్లింపును మేము నిర్ధారించలేకపోయాం. మీరు చెల్లించి ఉంటే, దయచేసి మీ ఆర్డర్ ఐడితో సపోర్ట్‌ను సంప్రదించండి.",
     "இந்தக் கட்டணத்தை எங்களால் உறுதிப்படுத்த முடியவில்லை. நீங்கள் செலுத்தியிருந்தால், உங்கள் ஆர்டர் ஐடியுடன் ஆதரவைத் தொடர்பு கொள்ளவும்.",
     "ഈ പേയ്‌മെന്റ് സ്ഥിരീകരിക്കാൻ ഞങ്ങൾക്കായില്ല. നിങ്ങൾ പണമടച്ചിട്ടുണ്ടെങ്കിൽ ദയവായി ഓർഡർ ഐഡി സഹിതം സപ്പോർട്ടിനെ ബന്ധപ്പെടുക."),
    ("Rejected", "अस्वीकृत", "తిరస్కరించబడింది", "நிராகரிக்கப்பட்டது", "നിരസിച്ചു"),
    ("Approved ✅", "स्वीकृत ✅", "ఆమోదించబడింది ✅", "ஒப்புதல் அளிக்கப்பட்டது ✅", "അംഗീകരിച്ചു ✅"),
    ("🔌 DB connection pool: <b>{v}</b>", "🔌 DB कनेक्शन पूल: <b>{v}</b>", "🔌 DB కనెక్షన్ పూల్: <b>{v}</b>", "🔌 DB இணைப்பு பூல்: <b>{v}</b>", "🔌 DB കണക്ഷൻ പൂൾ: <b>{v}</b>"),
    # ---- delivery / notices (messages sent by the payment code) ----
    ("✅ <b>Payment verified — here's your key!</b>", "✅ <b>पेमेंट सत्यापित — यह रही आपकी की!</b>", "✅ <b>చెల్లింపు ధృవీకరించబడింది — ఇదిగో మీ కీ!</b>", "✅ <b>கட்டணம் சரிபார்க்கப்பட்டது — இதோ உங்கள் கீ!</b>", "✅ <b>പേയ്‌മെന്റ് സ്ഥിരീകരിച്ചു — ഇതാ നിങ്ങളുടെ കീ!</b>"),
    ("🗝️ Your Key:", "🗝️ आपकी की:", "🗝️ మీ కీ:", "🗝️ உங்கள் கீ:", "🗝️ നിങ്ങളുടെ കീ:"),
    ("💰 Remaining balance: {v}", "💰 बचा हुआ बैलेंस: {v}", "💰 మిగిలిన బ్యాలెన్స్: {v}", "💰 மீதமுள்ள இருப்பு: {v}", "💰 மீதமுள்ள ബാലൻസ്: {v}"),
    ("⚠️ <b>Out of Stock</b>", "⚠️ <b>स्टॉक खत्म</b>", "⚠️ <b>స్టాక్ లేదు</b>", "⚠️ <b>கையிருப்பு இல்லை</b>", "⚠️ <b>സ്റ്റോക്ക് തീർന്നു</b>"),
    ("<b>{p}</b> ({l}) has no stock right now.", "<b>{p}</b> ({l}) का अभी स्टॉक नहीं है।", "<b>{p}</b> ({l}) కు ప్రస్తుతం స్టాక్ లేదు.", "<b>{p}</b> ({l}) க்கு தற்போது கையிருப்பு இல்லை.", "<b>{p}</b> ({l}) ഇപ്പോൾ സ്റ്റോക്കില്ല."),
    ("🛡️ Your money is safe — {a} has been added to your wallet.", "🛡️ आपका पैसा सुरक्षित है — {a} आपके वॉलेट में जोड़ दिए गए हैं।",
     "🛡️ మీ డబ్బు సురక్షితం — {a} మీ వాలెట్‌లో జమ చేయబడింది.", "🛡️ உங்கள் பணம் பாதுகாப்பாக உள்ளது — {a} உங்கள் வாலட்டில் சேர்க்கப்பட்டது.", "🛡️ നിങ്ങളുടെ പണം സുരക്ഷിതമാണ് — {a} നിങ്ങളുടെ വാലറ്റിൽ ചേർത്തു."),
    ("You can buy it as soon as it is restocked.", "रीस्टॉक होते ही आप इसे खरीद सकते हैं।", "రీస్టాక్ అయిన వెంటనే మీరు దీన్ని కొనవచ్చు.", "மீண்டும் கையிருப்பு வந்தவுடன் இதை வாங்கலாம்.", "വീണ്ടും സ്റ്റോക്ക് വന്നാലുടൻ നിങ്ങൾക്കിത് വാങ്ങാം."),
    ("✅ <b>Payment Verified &amp; Received!</b>", "✅ <b>पेमेंट सत्यापित और प्राप्त!</b>", "✅ <b>చెల్లింపు ధృవీకరించబడింది & అందింది!</b>", "✅ <b>கட்டணம் சரிபார்க்கப்பட்டு பெறப்பட்டது!</b>", "✅ <b>പേയ്‌മെന്റ് സ്ഥിരീകരിച്ച് ലഭിച്ചു!</b>"),
    ("⚠️ NOTICE: Auto-stock for <b>{p}</b> ({l}) is currently restocking!", "⚠️ सूचना: <b>{p}</b> ({l}) का ऑटो-स्टॉक अभी रीस्टॉक हो रहा है!",
     "⚠️ గమనిక: <b>{p}</b> ({l}) ఆటో-స్టాక్ ప్రస్తుతం రీస్టాక్ అవుతోంది!", "⚠️ அறிவிப்பு: <b>{p}</b> ({l}) தானியங்கி கையிருப்பு தற்போது நிரப்பப்படுகிறது!", "⚠️ അറിയിപ്പ്: <b>{p}</b> ({l}) ഓട്ടോ-സ്റ്റോക്ക് ഇപ്പോൾ റീസ്റ്റോക്ക് ചെയ്യുകയാണ്!"),
    ("🛡️ 100% SECURE: Admin is preparing your fresh key right now. It will be delivered directly to this chat shortly!",
     "🛡️ 100% सुरक्षित: एडमिन अभी आपकी नई की तैयार कर रहे हैं। यह जल्द ही सीधे इस चैट में भेज दी जाएगी!",
     "🛡️ 100% సురక్షితం: అడ్మిన్ ఇప్పుడు మీ కొత్త కీని సిద్ధం చేస్తున్నారు. ఇది త్వరలో నేరుగా ఈ చాట్‌కు పంపబడుతుంది!",
     "🛡️ 100% பாதுகாப்பு: அட்மின் இப்போது உங்கள் புதிய கீயைத் தயாரிக்கிறார். இது விரைவில் நேரடியாக இந்த சாட்டுக்கு அனுப்பப்படும்!",
     "🛡️ 100% സുരക്ഷിതം: അഡ്മിൻ ഇപ്പോൾ നിങ്ങളുടെ പുതിയ കീ തയ്യാറാക്കുകയാണ്. ഇത് ഉടൻ ഈ ചാറ്റിലേക്ക് നേരിട്ട് എത്തും!"),
    ("✅ <b>Payment Verified Successfully!</b>", "✅ <b>पेमेंट सफलतापूर्वक सत्यापित!</b>", "✅ <b>చెల్లింపు విజయవంతంగా ధృవీకరించబడింది!</b>", "✅ <b>கட்டணம் வெற்றிகரமாக சரிபார்க்கப்பட்டது!</b>", "✅ <b>പേയ്‌മെന്റ് വിജയകരമായി സ്ഥിരീകരിച്ചു!</b>"),
    ("{a} has been added to your wallet.", "{a} आपके वॉलेट में जोड़ दिए गए हैं।", "{a} మీ వాలెట్‌లో జమ చేయబడింది.", "{a} உங்கள் வாலட்டில் சேர்க்கப்பட்டது.", "{a} നിങ്ങളുടെ വാലറ്റിൽ ചേർത്തു."),
    ("Your balance is ready — open the shop to complete your purchase.", "आपका बैलेंस तैयार है — खरीदारी पूरी करने के लिए शॉप खोलें।",
     "మీ బ్యాలెన్స్ సిద్ధంగా ఉంది — కొనుగోలు పూర్తి చేయడానికి షాప్ తెరవండి.", "உங்கள் இருப்பு தயாராக உள்ளது — வாங்குதலை முடிக்க ஷாப்பைத் திறக்கவும்.", "നിങ്ങളുടെ ബാലൻസ് തയ്യാറാണ് — വാങ്ങൽ പൂർത്തിയാക്കാൻ ഷോപ്പ് തുറക്കുക."),
    # ---- lucky spin ----
    ("── <b>CASH SPIN</b> ──", "── <b>कैश स्पिन</b> ──", "── <b>క్యాష్ స్పిన్</b> ──", "── <b>கேஷ் ஸ்பின்</b> ──", "── <b>ക്യാഷ് സ്പിൻ</b> ──"),
    ("🎁 <b>Daily Free Spin!</b>", "🎁 <b>रोज़ाना फ्री स्पिन!</b>", "🎁 <b>రోజువారీ ఉచిత స్పిన్!</b>", "🎁 <b>தினசரி இலவச ஸ்பின்!</b>", "🎁 <b>ദിവസേനയുള്ള സൗജന്യ സ്പിൻ!</b>"),
    ("💰 Win real cash directly into your wallet!", "💰 असली कैश सीधे अपने वॉलेट में जीतें!", "💰 నిజమైన నగదును నేరుగా మీ వాలెట్‌లో గెలవండి!", "💰 உண்மையான பணத்தை நேரடியாக உங்கள் வாலட்டில் வெல்லுங்கள்!", "💰 യഥാർത്ഥ പണം നേരിട്ട് നിങ്ങളുടെ വാലറ്റിൽ നേടൂ!"),
    ("Get 1 Free Spin every {t}!", "हर {t} में 1 फ्री स्पिन पाएँ!", "ప్రతి {t} కు 1 ఉచిత స్పిన్ పొందండి!", "ஒவ்வொரு {t} க்கும் 1 இலவச ஸ்பின் பெறுங்கள்!", "ഓരോ {t} കൂടുമ്പോഴും 1 സൗജന്യ സ്പിൻ നേടൂ!"),
    ("⏳ Use the cash for a purchase within <b>{t}</b> or it expires!", "⏳ कैश को <b>{t}</b> के अंदर खरीदारी में इस्तेमाल करें, वरना यह खत्म हो जाएगा!",
     "⏳ నగదును <b>{t}</b> లోపల కొనుగోలుకు వాడండి, లేకపోతే అది గడువు ముగుస్తుంది!", "⏳ பணத்தை <b>{t}</b> க்குள் வாங்குதலுக்குப் பயன்படுத்தவும், இல்லையெனில் காலாவதியாகும்!", "⏳ പണം <b>{t}</b> ഉള്ളിൽ വാങ്ങലിന് ഉപയോഗിക്കുക, അല്ലെങ്കിൽ അത് കാലഹരണപ്പെടും!"),
    ("🕒 Spin Status: ✅ <b>Available Now!</b>", "🕒 स्पिन स्थिति: ✅ <b>अभी उपलब्ध!</b>", "🕒 స్పిన్ స్థితి: ✅ <b>ఇప్పుడు అందుబాటులో!</b>", "🕒 ஸ்பின் நிலை: ✅ <b>இப்போது கிடைக்கும்!</b>", "🕒 സ്പിൻ നില: ✅ <b>ഇപ്പോൾ ലഭ്യം!</b>"),
    ("👇 Tap below to spin and try your luck!", "👇 स्पिन करने और किस्मत आज़माने के लिए नीचे दबाएँ!", "👇 స్పిన్ చేసి అదృష్టం పరీక్షించుకోవడానికి క్రింద నొక్కండి!", "👇 ஸ்பின் செய்து அதிர்ஷ்டத்தை சோதிக்க கீழே அழுத்தவும்!", "👇 സ്പിൻ ചെയ്ത് ഭാഗ്യം പരീക്ഷിക്കാൻ താഴെ അമർത്തുക!"),
    ("🕒 Spin Status: ⏳ Next free spin in <b>{t}</b>", "🕒 स्पिन स्थिति: ⏳ अगला फ्री स्पिन <b>{t}</b> में", "🕒 స్పిన్ స్థితి: ⏳ తదుపరి ఉచిత స్పిన్ <b>{t}</b> లో",
     "🕒 ஸ்பின் நிலை: ⏳ அடுத்த இலவச ஸ்பின் <b>{t}</b> இல்", "🕒 സ്പിൻ നില: ⏳ അടുത്ത സൗജന്യ സ്പിൻ <b>{t}</b> കഴിഞ്ഞ്"),
    ("🎁 Active spin cash: <b>{a}</b> — expires in <b>{t}</b>. Buy a key now to keep it!", "🎁 सक्रिय स्पिन कैश: <b>{a}</b> — <b>{t}</b> में समाप्त। इसे रखने के लिए अभी की खरीदें!",
     "🎁 యాక్టివ్ స్పిన్ నగదు: <b>{a}</b> — <b>{t}</b> లో ముగుస్తుంది. దీన్ని ఉంచుకోవడానికి ఇప్పుడే కీ కొనండి!", "🎁 செயலில் உள்ள ஸ்பின் பணம்: <b>{a}</b> — <b>{t}</b> இல் காலாவதியாகும். வைத்துக்கொள்ள இப்போதே கீ வாங்குங்கள்!",
     "🎁 സജീവ സ്പിൻ പണം: <b>{a}</b> — <b>{t}</b> കഴിഞ്ഞാൽ കാലഹരണപ്പെടും. സൂക്ഷിക്കാൻ ഇപ്പോൾ തന്നെ കീ വാങ്ങൂ!"),
    ("⏳ Your next free spin is not available yet. Try again later!", "⏳ आपका अगला फ्री स्पिन अभी उपलब्ध नहीं है। बाद में कोशिश करें!",
     "⏳ మీ తదుపరి ఉచిత స్పిన్ ఇంకా అందుబాటులో లేదు. తర్వాత మళ్లీ ప్రయత్నించండి!", "⏳ உங்கள் அடுத்த இலவச ஸ்பின் இன்னும் கிடைக்கவில்லை. பிறகு முயற்சிக்கவும்!", "⏳ നിങ്ങളുടെ അടുത്ത സൗജന്യ സ്പിൻ ഇതുവരെ ലഭ്യമല്ല. പിന്നീട് ശ്രമിക്കുക!"),
    ("🎰 Spinning... {r}", "🎰 स्पिन हो रहा है... {r}", "🎰 స్పిన్ అవుతోంది... {r}", "🎰 ஸ்பின் ஆகிறது... {r}", "🎰 സ്പിൻ ചെയ്യുന്നു... {r}"),
    ("🎰 Stopping... {r}", "🎰 रुक रहा है... {r}", "🎰 ఆగుతోంది... {r}", "🎰 நிற்கிறது... {r}", "🎰 നിൽക്കുന്നു... {r}"),
    ("🎁 ── <b>CASH SPIN RESULT!</b> ── 🎁", "🎁 ── <b>कैश स्पिन परिणाम!</b> ── 🎁", "🎁 ── <b>క్యాష్ స్పిన్ ఫలితం!</b> ── 🎁", "🎁 ── <b>கேஷ் ஸ்பின் முடிவு!</b> ── 🎁", "🎁 ── <b>ക്യാഷ് സ്പിൻ ഫലം!</b> ── 🎁"),
    ("🎉 <b>Congratulations!</b>", "🎉 <b>बधाई हो!</b>", "🎉 <b>అభినందనలు!</b>", "🎉 <b>வாழ்த்துகள்!</b>", "🎉 <b>അഭിനന്ദനങ്ങൾ!</b>"),
    ("💰 You won <b>{a} Cash Reward!</b>", "💰 आपने <b>{a} कैश इनाम</b> जीता!", "💰 మీరు <b>{a} నగదు బహుమతి</b> గెలిచారు!", "💰 நீங்கள் <b>{a} பண பரிசு</b> வென்றீர்கள்!", "💰 നിങ്ങൾ <b>{a} ക്യാഷ് റിവാർഡ്</b> നേടി!"),
    ("It has been added to your wallet.", "इसे आपके वॉलेट में जोड़ दिया गया है।", "ఇది మీ వాలెట్‌లో జమ చేయబడింది.", "இது உங்கள் வாலட்டில் சேர்க்கப்பட்டது.", "ഇത് നിങ്ങളുടെ വാലറ്റിൽ ചേർത്തു."),
    ("⏳ <b>Use it for a purchase within {t}</b> — otherwise this cash expires!", "⏳ <b>इसे {t} के अंदर खरीदारी में इस्तेमाल करें</b> — वरना यह कैश खत्म हो जाएगा!",
     "⏳ <b>దీన్ని {t} లోపల కొనుగోలుకు వాడండి</b> — లేకపోతే ఈ నగదు గడువు ముగుస్తుంది!", "⏳ <b>இதை {t} க்குள் வாங்குதலுக்குப் பயன்படுத்தவும்</b> — இல்லையெனில் இந்தப் பணம் காலாவதியாகும்!", "⏳ <b>ഇത് {t} ഉള്ളിൽ വാങ്ങലിന് ഉപയോഗിക്കുക</b> — അല്ലെങ്കിൽ ഈ പണം കാലഹരണപ്പെടും!"),
    ("💳 New balance: {v}", "💳 नया बैलेंस: {v}", "💳 కొత్త బ్యాలెన్స్: {v}", "💳 புதிய இருப்பு: {v}", "💳 പുതിയ ബാലൻസ്: {v}"),
    ("🕒 Next free spin available in {t}!", "🕒 अगला फ्री स्पिन {t} में उपलब्ध होगा!", "🕒 తదుపరి ఉచిత స్పిన్ {t} లో అందుబాటులో ఉంటుంది!", "🕒 அடுத்த இலவச ஸ்பின் {t} இல் கிடைக்கும்!", "🕒 അടുത്ത സൗജന്യ സ്പിൻ {t} കഴിഞ്ഞ് ലഭ്യമാകും!"),
    ("⏰ <b>SPIN CASH EXPIRED</b>", "⏰ <b>स्पिन कैश की समय सीमा समाप्त</b>", "⏰ <b>స్పిన్ నగదు గడువు ముగిసింది</b>", "⏰ <b>ஸ்பின் பணம் காலாவதியானது</b>", "⏰ <b>സ്പിൻ പണം കാലഹരണപ്പെട്ടു</b>"),
    ("Your free spin reward of <b>{a}</b> was not used for a purchase in time, so it has been removed from your wallet.",
     "आपका <b>{a}</b> का फ्री स्पिन इनाम समय पर खरीदारी में इस्तेमाल नहीं हुआ, इसलिए इसे आपके वॉलेट से हटा दिया गया है।",
     "మీ <b>{a}</b> ఉచిత స్పిన్ బహుమతి సమయానికి కొనుగోలుకు వాడలేదు, కాబట్టి దాన్ని మీ వాలెట్ నుండి తొలగించాం.",
     "உங்கள் <b>{a}</b> இலவச ஸ்பின் பரிசு உரிய நேரத்தில் வாங்குதலுக்குப் பயன்படுத்தப்படவில்லை, எனவே அது உங்கள் வாலட்டிலிருந்து நீக்கப்பட்டது.",
     "നിങ്ങളുടെ <b>{a}</b> സൗജന്യ സ്പിൻ റിവാർഡ് സമയത്തിനുള്ളിൽ വാങ്ങലിന് ഉപയോഗിച്ചില്ല, അതിനാൽ അത് നിങ്ങളുടെ വാലറ്റിൽ നിന്ന് നീക്കി."),
    ("💳 Wallet balance: {v}", "💳 वॉलेट बैलेंस: {v}", "💳 వాలెట్ బ్యాలెన్స్: {v}", "💳 வாலட் இருப்பு: {v}", "💳 വാലറ്റ് ബാലൻസ്: {v}"),
    ("🎰 Spin again when your next free spin is ready — and buy a key before the timer ends!", "🎰 अगला फ्री स्पिन तैयार होने पर फिर स्पिन करें — और टाइमर खत्म होने से पहले की खरीदें!",
     "🎰 మీ తదుపరి ఉచిత స్పిన్ సిద్ధమైనప్పుడు మళ్లీ స్పిన్ చేయండి — టైమర్ ముగిసేలోపు కీ కొనండి!", "🎰 அடுத்த இலவச ஸ்பின் தயாரானதும் மீண்டும் ஸ்பின் செய்யுங்கள் — டைமர் முடிவதற்குள் கீ வாங்குங்கள்!", "🎰 അടുത്ത സൗജന്യ സ്പിൻ തയ്യാറാകുമ്പോൾ വീണ്ടും സ്പിൻ ചെയ്യൂ — ടൈമർ തീരും മുൻപ് കീ വാങ്ങൂ!"),
    # ---- referral / history / top sellers ----
    ("🤝 <b>REFERRAL SYSTEM</b>", "🤝 <b>रेफरल सिस्टम</b>", "🤝 <b>రెఫరల్ సిస్టమ్</b>", "🤝 <b>பரிந்துரை அமைப்பு</b>", "🤝 <b>റഫറൽ സിസ്റ്റം</b>"),
    ("🔗 Your Referral Link:", "🔗 आपका रेफरल लिंक:", "🔗 మీ రెఫరల్ లింక్:", "🔗 உங்கள் பரிந்துரை இணைப்பு:", "🔗 നിങ്ങളുടെ റഫറൽ ലിങ്ക്:"),
    ("🤝 Total Referrals: <b>{n}</b>", "🤝 कुल रेफरल: <b>{n}</b>", "🤝 మొత్తం రెఫరల్స్: <b>{n}</b>", "🤝 மொத்த பரிந்துரைகள்: <b>{n}</b>", "🤝 ആകെ റഫറലുകൾ: <b>{n}</b>"),
    ("💰 Total Earnings: <b>{v}</b>", "💰 कुल कमाई: <b>{v}</b>", "💰 మొత్తం సంపాదన: <b>{v}</b>", "💰 மொத்த வருமானம்: <b>{v}</b>", "💰 ആകെ വരുമാനം: <b>{v}</b>"),
    ("💰 Invite your friends → Get a guaranteed bonus on their first purchase!", "💰 अपने दोस्तों को बुलाएँ → उनकी पहली खरीदारी पर पक्का बोनस पाएँ!",
     "💰 మీ స్నేహితులను ఆహ్వానించండి → వారి మొదటి కొనుగోలుపై హామీగా బోనస్ పొందండి!", "💰 உங்கள் நண்பர்களை அழைக்கவும் → அவர்களின் முதல் வாங்குதலில் உறுதியான போனஸ் பெறுங்கள்!", "💰 നിങ്ങളുടെ സുഹൃത്തുക്കളെ ക്ഷണിക്കൂ → അവരുടെ ആദ്യ വാങ്ങലിൽ ഉറപ്പായ ബോണസ് നേടൂ!"),
    ("🛒 ── <b>MY ORDERS</b> ── 🛒", "🛒 ── <b>मेरे ऑर्डर</b> ── 🛒", "🛒 ── <b>నా ఆర్డర్లు</b> ── 🛒", "🛒 ── <b>என் ஆர்டர்கள்</b> ── 🛒", "🛒 ── <b>എന്റെ ഓർഡറുകൾ</b> ── 🛒"),
    ("🚫 <b>No orders yet!</b>", "🚫 <b>अभी तक कोई ऑर्डर नहीं!</b>", "🚫 <b>ఇంకా ఆర్డర్లు లేవు!</b>", "🚫 <b>இன்னும் ஆர்டர்கள் இல்லை!</b>", "🚫 <b>ഇതുവരെ ഓർഡറുകൾ ഇല്ല!</b>"),
    ("🛒 Start shopping to see your orders here!", "🛒 अपने ऑर्डर यहाँ देखने के लिए खरीदारी शुरू करें!", "🛒 మీ ఆర్డర్లు ఇక్కడ చూడటానికి షాపింగ్ ప్రారంభించండి!", "🛒 உங்கள் ஆர்டர்களை இங்கே காண ஷாப்பிங்கைத் தொடங்குங்கள்!", "🛒 നിങ്ങളുടെ ഓർഡറുകൾ ഇവിടെ കാണാൻ ഷോപ്പിംഗ് തുടങ്ങൂ!"),
    ("✈️ All your purchased keys will be saved here safely!", "✈️ आपकी खरीदी हुई सभी कीज़ यहाँ सुरक्षित रहेंगी!", "✈️ మీరు కొన్న అన్ని కీలు ఇక్కడ సురక్షితంగా ఉంటాయి!", "✈️ நீங்கள் வாங்கிய அனைத்து கீகளும் இங்கே பாதுகாப்பாக இருக்கும்!", "✈️ നിങ്ങൾ വാങ്ങിയ എല്ലാ കീകളും ഇവിടെ സുരക്ഷിതമായി സൂക്ഷിക്കും!"),
    ("🔑 <b>MY ORDERS (last 20)</b>", "🔑 <b>मेरे ऑर्डर (पिछले 20)</b>", "🔑 <b>నా ఆర్డర్లు (చివరి 20)</b>", "🔑 <b>என் ஆர்டர்கள் (கடைசி 20)</b>", "🔑 <b>എന്റെ ഓർഡറുകൾ (അവസാന 20)</b>"),
    ("💰 {a} • {s}", "💰 {a} • {s}", "💰 {a} • {s}", "💰 {a} • {s}", "💰 {a} • {s}"),
    ("⏳ Pending delivery", "⏳ डिलीवरी लंबित", "⏳ డెలివరీ పెండింగ్", "⏳ டெலிவரி நிலுவையில்", "⏳ ഡെലിവറി തീർപ്പാക്കാനുണ്ട്"),
    ("Completed", "पूर्ण", "పూర్తయింది", "முடிந்தது", "പൂർത്തിയായി"),
    ("📅 {d} (IST)", "📅 {d} (IST)", "📅 {d} (IST)", "📅 {d} (IST)", "📅 {d} (IST)"),
    ("👑 <b>TOP 5 SELLERS MONTHLY</b>", "👑 <b>मासिक टॉप 5 सेलर्स</b>", "👑 <b>నెలవారీ టాప్ 5 సెల్లర్లు</b>", "👑 <b>மாதாந்திர முதல் 5 விற்பனையாளர்கள்</b>", "👑 <b>പ്രതിമാസ ടോപ്പ് 5 സെല്ലർമാർ</b>"),
    ("📅 <i>{m} — list resets automatically on the 1st of every month</i>", "📅 <i>{m} — सूची हर महीने की 1 तारीख को अपने आप रीसेट होती है</i>",
     "📅 <i>{m} — జాబితా ప్రతి నెల 1వ తేదీన ఆటోమేటిక్‌గా రీసెట్ అవుతుంది</i>", "📅 <i>{m} — பட்டியல் ஒவ்வொரு மாதமும் 1ஆம் தேதி தானாக மீட்டமைக்கப்படும்</i>", "📅 <i>{m} — പട്ടിക എല്ലാ മാസവും 1-ാം തീയതി സ്വയം റീസെറ്റ് ആകും</i>"),
    ("🚫 <b>No purchases yet this month!</b>", "🚫 <b>इस महीने अभी तक कोई खरीदारी नहीं!</b>", "🚫 <b>ఈ నెల ఇంకా కొనుగోళ్లు లేవు!</b>", "🚫 <b>இந்த மாதம் இன்னும் வாங்குதல்கள் இல்லை!</b>", "🚫 <b>ഈ മാസം ഇതുവരെ വാങ്ങലുകൾ ഇല്ല!</b>"),
    ("🛒 Buy a key now and be the first on the leaderboard! 🔥", "🛒 अभी की खरीदें और लीडरबोर्ड पर सबसे पहले आएँ! 🔥", "🛒 ఇప్పుడే కీ కొని లీడర్‌బోర్డ్‌లో మొదటి వారు అవ్వండి! 🔥", "🛒 இப்போதே கீ வாங்கி லீடர்போர்டில் முதலாவதாக இருங்கள்! 🔥", "🛒 ഇപ്പോൾ കീ വാങ്ങി ലീഡർബോർഡിൽ ഒന്നാമനാകൂ! 🔥"),
    ("👑 Role: {r}", "👑 भूमिका: {r}", "👑 పాత్ర: {r}", "👑 பங்கு: {r}", "👑 റോൾ: {r}"),
    ("RESELLER", "रीसेलर", "రీసెల్లర్", "மறுவிற்பனையாளர்", "റീസെല്ലർ"),
    ("USER", "यूज़र", "యూజర్", "பயனர்", "യൂസർ"),
    ("🔑 Keys: {n}", "🔑 कीज़: {n}", "🔑 కీలు: {n}", "🔑 கீகள்: {n}", "🔑 കീകൾ: {n}"),
    ("💰 Total Buy: {v}", "💰 कुल खरीद: {v}", "💰 మొత్తం కొనుగోలు: {v}", "💰 மொத்த வாங்குதல்: {v}", "💰 ആകെ വാങ്ങൽ: {v}"),
    ("📍 <b>Your rank:</b> #{r} • 🔑 {k} keys • {v}", "📍 <b>आपकी रैंक:</b> #{r} • 🔑 {k} कीज़ • {v}", "📍 <b>మీ ర్యాంక్:</b> #{r} • 🔑 {k} కీలు • {v}", "📍 <b>உங்கள் தரவரிசை:</b> #{r} • 🔑 {k} கீகள் • {v}", "📍 <b>നിങ്ങളുടെ റാങ്ക്:</b> #{r} • 🔑 {k} കീകൾ • {v}"),
    ("📍 <b>Your rank:</b> not ranked yet — buy a key to join the list!", "📍 <b>आपकी रैंक:</b> अभी रैंक नहीं — सूची में आने के लिए की खरीदें!", "📍 <b>మీ ర్యాంక్:</b> ఇంకా ర్యాంక్ లేదు — జాబితాలో చేరడానికి కీ కొనండి!",
     "📍 <b>உங்கள் தரவரிசை:</b> இன்னும் தரவரிசை இல்லை — பட்டியலில் சேர கீ வாங்குங்கள்!", "📍 <b>നിങ്ങളുടെ റാങ്ക്:</b> ഇതുവരെ റാങ്ക് ഇല്ല — പട്ടികയിൽ ചേരാൻ കീ വാങ്ങൂ!"),
    ("⏳ Resets in <b>{d}</b> day", "⏳ <b>{d}</b> दिन में रीसेट", "⏳ <b>{d}</b> రోజులో రీసెట్", "⏳ <b>{d}</b> நாளில் மீட்டமைப்பு", "⏳ <b>{d}</b> ദിവസത്തിൽ റീസെറ്റ്"),
    ("⏳ Resets in <b>{d}</b> days", "⏳ <b>{d}</b> दिनों में रीसेट", "⏳ <b>{d}</b> రోజుల్లో రీసెట్", "⏳ <b>{d}</b> நாட்களில் மீட்டமைப்பு", "⏳ <b>{d}</b> ദിവസത്തിനുള്ളിൽ റീസെറ്റ്"),
    # ---- ping ----
    ("✅ Everything is running fast.", "✅ सब कुछ तेज़ चल रहा है।", "✅ అంతా వేగంగా నడుస్తోంది.", "✅ எல்லாம் வேகமாக இயங்குகிறது.", "✅ എല്ലാം വേഗത്തിൽ പ്രവർത്തിക്കുന്നു."),
    ("⚠️ A little slow right now. Tap <b>🔄 Restart</b> to refresh your connection.", "⚠️ अभी थोड़ा धीमा है। कनेक्शन रीफ्रेश करने के लिए <b>🔄 Restart</b> दबाएँ।",
     "⚠️ ప్రస్తుతం కొంచెం నెమ్మదిగా ఉంది. మీ కనెక్షన్‌ను రిఫ్రెష్ చేయడానికి <b>🔄 Restart</b> నొక్కండి.", "⚠️ இப்போது சற்று மெதுவாக உள்ளது. உங்கள் இணைப்பைப் புதுப்பிக்க <b>🔄 Restart</b> அழுத்தவும்.", "⚠️ ഇപ്പോൾ അൽപ്പം വേഗത കുറവാണ്. കണക്ഷൻ റിഫ്രഷ് ചെയ്യാൻ <b>🔄 Restart</b> അമർത്തുക."),
    ("📡 <b>PING RESULT</b>", "📡 <b>पिंग परिणाम</b>", "📡 <b>పింగ్ ఫలితం</b>", "📡 <b>பிங் முடிவு</b>", "📡 <b>പിംഗ് ഫലം</b>"),
    ("⚡ Total: <b>{t} ms</b>", "⚡ कुल: <b>{t} ms</b>", "⚡ మొత్తం: <b>{t} ms</b>", "⚡ மொத்தம்: <b>{t} ms</b>", "⚡ ആകെ: <b>{t} ms</b>"),
    ("<i>Your own phone network adds a little on top of this.</i>", "<i>आपके फ़ोन का नेटवर्क इसमें थोड़ा और जोड़ता है।</i>", "<i>మీ ఫోన్ నెట్‌వర్క్ దీనికి కొంచెం అదనంగా కలుపుతుంది.</i>", "<i>உங்கள் தொலைபேசி நெட்வொர்க் இதற்குச் சற்று கூடுதலாகச் சேர்க்கும்.</i>", "<i>നിങ്ങളുടെ ഫോൺ നെറ്റ്‌വർക്ക് ഇതിൽ അൽപ്പം കൂടി ചേർക്കും.</i>"),
    ("📡 <b>Ping testing...</b>", "📡 <b>पिंग टेस्ट हो रहा है...</b>", "📡 <b>పింగ్ పరీక్ష జరుగుతోంది...</b>", "📡 <b>பிங் சோதனை நடக்கிறது...</b>", "📡 <b>പിംഗ് ടെസ്റ്റ് നടക്കുന്നു...</b>"),
    ("⏳ Measuring your speed live, please wait.", "⏳ आपकी स्पीड लाइव मापी जा रही है, कृपया प्रतीक्षा करें।", "⏳ మీ వేగాన్ని ప్రత్యక్షంగా కొలుస్తున్నాం, దయచేసి వేచి ఉండండి.", "⏳ உங்கள் வேகத்தை நேரடியாக அளவிடுகிறோம், காத்திருக்கவும்.", "⏳ നിങ്ങളുടെ വേഗത തത്സമയം അളക്കുന്നു, ദയവായി കാത്തിരിക്കുക."),
    ("🟢 Fast", "🟢 तेज़", "🟢 వేగం", "🟢 வேகம்", "🟢 വേഗം"),
    ("🔴 Slow", "🔴 धीमा", "🔴 నెమ్మది", "🔴 மெதுவு", "🔴 വേഗത കുറവ്"),
    # ---- shop ----
    ("🛒 <b>PRODUCT STORE — SHOP</b> 🛒", "🛒 <b>प्रोडक्ट स्टोर — शॉप</b> 🛒", "🛒 <b>ప్రొడక్ట్ స్టోర్ — షాప్</b> 🛒", "🛒 <b>தயாரிப்பு ஸ்டோர் — ஷாப்</b> 🛒", "🛒 <b>പ്രൊഡക്റ്റ് സ്റ്റോർ — ഷോപ്പ്</b> 🛒"),
    ("🎮 Select your game:", "🎮 अपना गेम चुनें:", "🎮 మీ గేమ్‌ను ఎంచుకోండి:", "🎮 உங்கள் கேமைத் தேர்ந்தெடுக்கவும்:", "🎮 നിങ്ങളുടെ ഗെയിം തിരഞ്ഞെടുക്കുക:"),
    ("🔥 <b>FREE FIRE — SHOP</b> 🔥", "🔥 <b>FREE FIRE — शॉप</b> 🔥", "🔥 <b>FREE FIRE — షాప్</b> 🔥", "🔥 <b>FREE FIRE — ஷாப்</b> 🔥", "🔥 <b>FREE FIRE — ഷോപ്പ്</b> 🔥"),
    ("📱 Select your device type:", "📱 अपना डिवाइस प्रकार चुनें:", "📱 మీ డివైస్ రకాన్ని ఎంచుకోండి:", "📱 உங்கள் சாதன வகையைத் தேர்ந்தெடுக்கவும்:", "📱 നിങ്ങളുടെ ഡിവൈസ് തരം തിരഞ്ഞെടുക്കുക:"),
    ("⚠️ No products available in this category yet. Admin will restock soon!", "⚠️ इस श्रेणी में अभी कोई प्रोडक्ट उपलब्ध नहीं है। एडमिन जल्द रीस्टॉक करेंगे!",
     "⚠️ ఈ వర్గంలో ఇంకా ఉత్పత్తులు లేవు. అడ్మిన్ త్వరలో రీస్టాక్ చేస్తారు!", "⚠️ இந்த வகையில் இன்னும் தயாரிப்புகள் இல்லை. அட்மின் விரைவில் நிரப்புவார்!", "⚠️ ഈ വിഭാഗത്തിൽ ഇതുവരെ ഉൽപ്പന്നങ്ങൾ ഇല്ല. അഡ്മിൻ ഉടൻ റീസ്റ്റോക്ക് ചെയ്യും!"),
    ("🔥 Choose a product:", "🔥 कोई प्रोडक्ट चुनें:", "🔥 ఒక ఉత్పత్తిని ఎంచుకోండి:", "🔥 ஒரு தயாரிப்பைத் தேர்ந்தெடுக்கவும்:", "🔥 ഒരു ഉൽപ്പന്നം തിരഞ്ഞെടുക്കുക:"),
    ("Available | 🎱 {g}", "उपलब्ध | 🎱 {g}", "అందుబాటులో | 🎱 {g}", "கிடைக்கும் | 🎱 {g}", "ലഭ്യം | 🎱 {g}"),
    ("Available | 🔴 {c} Only", "उपलब्ध | 🔴 सिर्फ़ {c}", "అందుబాటులో | 🔴 {c} మాత్రమే", "கிடைக்கும் | 🔴 {c} மட்டும்", "ലഭ്യം | 🔴 {c} മാത്രം"),
    ("🔘 Silent Aim &amp; Headshot", "🔘 साइलेंट एम और हेडशॉट", "🔘 సైలెంట్ ఎయిమ్ & హెడ్‌షాట్", "🔘 சைலன்ட் எய்ம் & ஹெட்ஷாட்", "🔘 സൈലന്റ് എയിം & ഹെഡ്ഷോട്ട്"),
    ("🔘 AimFov 360° Location", "🔘 AimFov 360° लोकेशन", "🔘 AimFov 360° లొకేషన్", "🔘 AimFov 360° இடம்", "🔘 AimFov 360° ലൊക്കേഷൻ"),
    ("🔘 CS / BR Rank Working Safe", "🔘 CS / BR रैंक सुरक्षित रूप से काम करता है", "🔘 CS / BR ర్యాంక్ సురక్షితంగా పనిచేస్తుంది", "🔘 CS / BR ரேங்க் பாதுகாப்பாக வேலை செய்யும்", "🔘 CS / BR റാങ്ക് സുരക്ഷിതമായി പ്രവർത്തിക്കുന്നു"),
    ("🔘 100% Anti-Ban Protection", "🔘 100% एंटी-बैन सुरक्षा", "🔘 100% యాంటీ-బ్యాన్ రక్షణ", "🔘 100% ஆன்டி-பேன் பாதுகாப்பு", "🔘 100% ആന്റി-ബാൻ സംരക്ഷണം"),
    ("Current Tier: <b>{b}</b>", "वर्तमान टियर: <b>{b}</b>", "ప్రస్తుత టియర్: <b>{b}</b>", "தற்போதைய நிலை: <b>{b}</b>", "നിലവിലെ ടിയർ: <b>{b}</b>"),
    ("Select Duration:", "अवधि चुनें:", "వ్యవధిని ఎంచుకోండి:", "காலத்தைத் தேர்ந்தெடுக்கவும்:", "കാലയളവ് തിരഞ്ഞെടുക്കുക:"),
    ("👑 Reseller Price", "👑 रीसेलर कीमत", "👑 రీసెల్లర్ ధర", "👑 மறுவிற்பனையாளர் விலை", "👑 റീസെല്ലർ വില"),
    ("👤 Regular Price", "👤 सामान्य कीमत", "👤 సాధారణ ధర", "👤 சாதாரண விலை", "👤 സാധാരണ വില"),
    ("This product is currently under maintenance. Please check back later — sorry for the inconvenience!</blockquote>",
     "यह प्रोडक्ट अभी मेंटेनेंस में है। कृपया बाद में देखें — असुविधा के लिए क्षमा करें!</blockquote>",
     "ఈ ఉత్పత్తి ప్రస్తుతం మెయింటెనెన్స్‌లో ఉంది. దయచేసి తర్వాత చూడండి — అసౌకర్యానికి క్షమించండి!</blockquote>",
     "இந்தத் தயாரிப்பு தற்போது பராமரிப்பில் உள்ளது. பிறகு பார்க்கவும் — சிரமத்திற்கு வருந்துகிறோம்!</blockquote>",
     "ഈ ഉൽപ്പന്നം ഇപ്പോൾ മെയിന്റനൻസിലാണ്. പിന്നീട് നോക്കുക — അസൗകര്യത്തിൽ ക്ഷമിക്കുക!</blockquote>"),
    ("⚠️ Product not available.", "⚠️ प्रोडक्ट उपलब्ध नहीं है।", "⚠️ ఉత్పత్తి అందుబాటులో లేదు.", "⚠️ தயாரிப்பு கிடைக்கவில்லை.", "⚠️ ഉൽപ്പന്നം ലഭ്യമല്ല."),
    ("⏱️ Plan: {p}", "⏱️ प्लान: {p}", "⏱️ ప్లాన్: {p}", "⏱️ திட்டம்: {p}", "⏱️ പ്ലാൻ: {p}"),
    ("💵 Price: {v}", "💵 कीमत: {v}", "💵 ధర: {v}", "💵 விலை: {v}", "💵 വില: {v}"),
    ("🎟️ Coupon <code>{c}</code>: -{v}", "🎟️ कूपन <code>{c}</code>: -{v}", "🎟️ కూపన్ <code>{c}</code>: -{v}", "🎟️ கூப்பன் <code>{c}</code>: -{v}", "🎟️ കൂപ്പൺ <code>{c}</code>: -{v}"),
    ("💳 Wallet Balance: {v}", "💳 वॉलेट बैलेंस: {v}", "💳 వాలెట్ బ్యాలెన్స్: {v}", "💳 வாலட் இருப்பு: {v}", "💳 വാലറ്റ് ബാലൻസ്: {v}"),
    ("⚠️ <b>To pay now: {v}</b>", "⚠️ <b>अभी देना है: {v}</b>", "⚠️ <b>ఇప్పుడు చెల్లించాల్సింది: {v}</b>", "⚠️ <b>இப்போது செலுத்த வேண்டியது: {v}</b>", "⚠️ <b>ഇപ്പോൾ അടയ്ക്കേണ്ടത്: {v}</b>"),
    ("⚠️ Session expired. Please select the product again.", "⚠️ सेशन समाप्त हो गया। कृपया प्रोडक्ट दोबारा चुनें।", "⚠️ సెషన్ ముగిసింది. దయచేసి ఉత్పత్తిని మళ్లీ ఎంచుకోండి.", "⚠️ அமர்வு முடிந்தது. தயாரிப்பை மீண்டும் தேர்ந்தெடுக்கவும்.", "⚠️ സെഷൻ കഴിഞ്ഞു. ഉൽപ്പന്നം വീണ്ടും തിരഞ്ഞെടുക്കുക."),
    ("⚠️ This product is currently unavailable.", "⚠️ यह प्रोडक्ट अभी उपलब्ध नहीं है।", "⚠️ ఈ ఉత్పత్తి ప్రస్తుతం అందుబాటులో లేదు.", "⚠️ இந்தத் தயாரிப்பு தற்போது கிடைக்கவில்லை.", "⚠️ ഈ ഉൽപ്പന്നം ഇപ്പോൾ ലഭ്യമല്ല."),
    ("⚠️ This product or plan is no longer available.", "⚠️ यह प्रोडक्ट या प्लान अब उपलब्ध नहीं है।", "⚠️ ఈ ఉత్పత్తి లేదా ప్లాన్ ఇకపై అందుబాటులో లేదు.", "⚠️ இந்தத் தயாரிப்பு அல்லது திட்டம் இனி கிடைக்காது.", "⚠️ ഈ ഉൽപ്പന്നമോ പ്ലാനോ ഇനി ലഭ്യമല്ല."),
    ("⚠️ <b>Out of Stock!</b> Keys will be restocked shortly. You were not charged.", "⚠️ <b>स्टॉक खत्म!</b> कीज़ जल्द रीस्टॉक होंगी। आपसे कुछ नहीं काटा गया।",
     "⚠️ <b>స్టాక్ లేదు!</b> కీలు త్వరలో రీస్టాక్ అవుతాయి. మీ నుండి ఏమీ తగ్గించబడలేదు.", "⚠️ <b>கையிருப்பு இல்லை!</b> கீகள் விரைவில் நிரப்பப்படும். உங்களிடமிருந்து எதுவும் கழிக்கப்படவில்லை.", "⚠️ <b>സ്റ്റോക്ക് തീർന്നു!</b> കീകൾ ഉടൻ റീസ്റ്റോക്ക് ചെയ്യും. നിങ്ങളിൽ നിന്ന് ഒന്നും കുറച്ചിട്ടില്ല."),
    ("🎟️ <b>Send your coupon code</b> as a message now.", "🎟️ अब अपना <b>कूपन कोड</b> संदेश के रूप में भेजें।", "🎟️ ఇప్పుడు మీ <b>కూపన్ కోడ్</b>ను సందేశంగా పంపండి.", "🎟️ இப்போது உங்கள் <b>கூப்பன் குறியீட்டை</b> செய்தியாக அனுப்பவும்.", "🎟️ ഇപ്പോൾ നിങ്ങളുടെ <b>കൂപ്പൺ കോഡ്</b> സന്ദേശമായി അയയ്ക്കുക."),
    ("❌ Invalid or expired coupon code.", "❌ कूपन कोड अमान्य या समाप्त है।", "❌ కూపన్ కోడ్ చెల్లదు లేదా గడువు ముగిసింది.", "❌ கூப்பன் குறியீடு தவறானது அல்லது காலாவதியானது.", "❌ കൂപ്പൺ കോഡ് അസാധുവാണ് അല്ലെങ്കിൽ കാലഹരണപ്പെട്ടു."),
    ("✅ Coupon applied!", "✅ कूपन लागू हो गया!", "✅ కూపన్ వర్తించబడింది!", "✅ கூப்பன் பயன்படுத்தப்பட்டது!", "✅ കൂപ്പൺ പ്രയോഗിച്ചു!"),
    ("💰 To pay: {v}", "💰 देना है: {v}", "💰 చెల్లించాల్సింది: {v}", "💰 செலுத்த வேண்டியது: {v}", "💰 അടയ്ക്കേണ്ടത്: {v}"),
    ("After paying, tap <b>✅ I have paid</b>. Your key arrives as soon as the admin confirms.", "भुगतान के बाद <b>✅ I have paid</b> दबाएँ। एडमिन की पुष्टि होते ही आपकी की आ जाएगी।",
     "చెల్లించిన తర్వాత <b>✅ I have paid</b> నొక్కండి. అడ్మిన్ నిర్ధారించిన వెంటనే మీ కీ వస్తుంది.", "பணம் செலுத்திய பின் <b>✅ I have paid</b> அழுத்தவும். அட்மின் உறுதிப்படுத்தியவுடன் உங்கள் கீ வரும்.", "പണമടച്ച ശേഷം <b>✅ I have paid</b> അമർത്തുക. അഡ്മിൻ സ്ഥിരീകരിച്ചാലുടൻ നിങ്ങളുടെ കീ എത്തും."),
    ("🚀 Start the bot", "🚀 बॉट शुरू करें", "🚀 బాట్ ప్రారంభించండి", "🚀 பாட்டைத் தொடங்கவும்", "🚀 ബോട്ട് തുടങ്ങുക"),
    ("📡 Check speed / fix slowness", "📡 स्पीड जाँचें / धीमापन ठीक करें", "📡 వేగం తనిఖీ / నెమ్మదితనం సరిచేయండి", "📡 வேகத்தைச் சரிபார்க்க / மெதுவை சரிசெய்ய", "📡 വേഗത പരിശോധിക്കുക / വേഗതക്കുറവ് പരിഹരിക്കുക"),
    ("🌐 Language", "🌐 भाषा", "🌐 భాష", "🌐 மொழி", "🌐 ഭാഷ"),
    # ---- durations (digits only, used inside other lines) ----
    ("{num} minutes", "{num} मिनट", "{num} నిమిషాలు", "{num} நிமிடங்கள்", "{num} മിനിറ്റ്"),
    ("{num} minute", "{num} मिनट", "{num} నిమిషం", "{num} நிமிடம்", "{num} മിനിറ്റ്"),
    ("{num} hours", "{num} घंटे", "{num} గంటలు", "{num} மணி நேரம்", "{num} മണിക്കൂർ"),
    ("{num} hour", "{num} घंटा", "{num} గంట", "{num} மணி நேரம்", "{num} മണിക്കൂർ"),
]

# ---- button labels (without the leading emoji; emoji stay as they are) ----
_BUTTONS = [
    ("Shop Key", "की शॉप", "కీ షాప్", "கீ ஷாப்", "കീ ഷോപ്പ്"),
    ("My Profile", "मेरी प्रोफ़ाइल", "నా ప్రొఫైల్", "என் சுயவிவரம்", "എന്റെ പ്രൊഫൈൽ"),
    ("Add Balance", "बैलेंस जोड़ें", "బ్యాలెన్స్ జోడించండి", "இருப்பு சேர்க்க", "ബാലൻസ് ചേർക്കുക"),
    ("All History", "सारा इतिहास", "అన్ని చరిత్ర", "அனைத்து வரலாறு", "എല്ലാ ഹിസ്റ്ററി"),
    ("Tutorial watch", "ट्यूटोरियल देखें", "ట్యుటోరియల్ చూడండి", "பயிற்சி பார்க்க", "ട്യൂട്ടോറിയൽ കാണുക"),
    ("Reseller", "रीसेलर", "రీసెల్లర్", "மறுவிற்பனையாளர்", "റീസെല്ലർ"),
    ("Selling Proof ↗️", "बिक्री का सबूत ↗️", "అమ్మకాల రుజువు ↗️", "விற்பனை ஆதாரம் ↗️", "വിൽപ്പന തെളിവ് ↗️"),
    ("Support", "सपोर्ट", "సపోర్ట్", "ஆதரவு", "സപ്പോർട്ട്"),
    ("Lucky Spin", "लकी स्पिन", "లక్కీ స్పిన్", "லக்கி ஸ்பின்", "ലക്കി സ്പിൻ"),
    ("Referral", "रेफरल", "రెఫరల్", "பரிந்துரை", "റഫറൽ"),
    ("Top 5 Sellers Monthly", "मासिक टॉप 5 सेलर", "నెలవారీ టాప్ 5 సెల్లర్లు", "மாதாந்திர சிறந்த 5 விற்பனையாளர்கள்", "പ്രതിമാസ ടോപ്പ് 5 സെല്ലർമാർ"),
    ("Check All Updates", "सभी अपडेट देखें", "అన్ని అప్‌డేట్‌లు చూడండి", "அனைத்து புதுப்பிப்புகளையும் பார்க்க", "എല്ലാ അപ്‌ഡേറ്റുകളും കാണുക"),
    ("Language", "भाषा", "భాష", "மொழி", "ഭാഷ"),
    ("Contact Support", "सपोर्ट से संपर्क करें", "సపోర్ట్‌ను సంప్రదించండి", "ஆதரவைத் தொடர்பு கொள்ள", "സപ്പോർട്ടുമായി ബന്ധപ്പെടുക"),
    ("Back", "वापस", "వెనుకకు", "பின்செல்", "തിരികെ"),
    ("Back to Menu", "मेनू पर वापस", "మెనూకు తిరిగి", "மெனுவுக்குத் திரும்ப", "മെനുവിലേക്ക് മടങ്ങുക"),
    ("Back to Plans", "प्लान पर वापस", "ప్లాన్‌లకు తిరిగి", "திட்டங்களுக்குத் திரும்ப", "പ്ലാനുകളിലേക്ക് മടങ്ങുക"),
    ("Upgrade To Reseller", "रीसेलर बनें", "రీసెల్లర్‌గా అప్‌గ్రేడ్ చేయండి", "மறுவிற்பனையாளராக மேம்படுத்த", "റീസെല്ലർ ആയി അപ്‌ഗ്രേഡ് ചെയ്യുക"),
    ("Buy Reseller Plan", "रीसेलर प्लान खरीदें", "రీసెల్లర్ ప్లాన్ కొనండి", "மறுவிற்பனையாளர் திட்டம் வாங்க", "റീസെല്ലർ പ്ലാൻ വാങ്ങുക"),
    ("View Profile", "प्रोफ़ाइल देखें", "ప్రొఫైల్ చూడండి", "சுயவிவரம் பார்க்க", "പ്രൊഫൈൽ കാണുക"),
    ("Custom Amount", "अपनी रकम", "మీ మొత్తం", "விருப்பத் தொகை", "സ്വന്തം തുക"),
    ("Confirm", "पक्का करें", "నిర్ధారించండి", "உறுதிப்படுத்து", "സ്ഥിരീകരിക്കുക"),
    ("Confirm {a}", "{a} पक्का करें", "{a} నిర్ధారించండి", "{a} உறுதிப்படுத்து", "{a} സ്ഥിരീകരിക്കുക"),
    ("UPI Payment (QR Code)", "UPI पेमेंट (QR कोड)", "UPI చెల్లింపు (QR కోడ్)", "UPI கட்டணம் (QR குறியீடு)", "UPI പേയ്‌മെന്റ് (QR കോഡ്)"),
    ("Binance USDT Payment", "Binance USDT पेमेंट", "Binance USDT చెల్లింపు", "Binance USDT கட்டணம்", "Binance USDT പേയ്‌മെന്റ്"),
    ("I have paid", "मैंने भुगतान कर दिया", "నేను చెల్లించాను", "நான் செலுத்திவிட்டேன்", "ഞാൻ പണം അടച്ചു"),
    ("Cancel Payment", "पेमेंट रद्द करें", "చెల్లింపు రద్దు చేయండి", "கட்டணத்தை ரத்து செய்", "പേയ്‌മെന്റ് റദ്ദാക്കുക"),
    ("I paid but wasn't credited", "मैंने भुगतान किया पर क्रेडिट नहीं हुआ", "నేను చెల్లించాను కానీ జమ కాలేదు", "நான் செலுத்தினேன் ஆனால் வரவு வைக்கப்படவில்லை", "ഞാൻ പണം അടച്ചു പക്ഷേ ക്രെഡിറ്റ് ആയില്ല"),
    ("Spin Now! (FREE)", "अभी स्पिन करें! (फ्री)", "ఇప్పుడే స్పిన్ చేయండి! (ఉచితం)", "இப்போது ஸ்பின் செய்! (இலவசம்)", "ഇപ്പോൾ സ്പിൻ ചെയ്യൂ! (ഫ്രീ)"),
    ("Shop Now — Use {v}", "अभी खरीदें — {v} इस्तेमाल करें", "ఇప్పుడే కొనండి — {v} ఉపయోగించండి", "இப்போது வாங்கு — {v} பயன்படுத்து", "ഇപ്പോൾ വാങ്ങൂ — {v} ഉപയോഗിക്കൂ"),
    ("Test Again", "फिर से जाँचें", "మళ్లీ పరీక్షించండి", "மீண்டும் சோதிக்க", "വീണ്ടും പരിശോധിക്കുക"),
    ("Restart", "रीस्टार्ट", "రీస్టార్ట్", "மறுதொடக்கம்", "റീസ്റ്റാർട്ട്"),
    ("Non-Root Mobile", "नॉन-रूट मोबाइल", "నాన్-రూట్ మొబైల్", "நான்-ரூட் மொபைல்", "നോൺ-റൂട്ട് മൊബൈൽ"),
    ("Root Mobile", "रूट मोबाइल", "రూట్ మొబైల్", "ரூட் மொபைல்", "റൂട്ട് മൊബൈൽ"),
    ("iOS Panels", "iOS पैनल", "iOS ప్యానెల్స్", "iOS பேனல்கள்", "iOS പാനലുകൾ"),
    ("PC Panels", "PC पैनल", "PC ప్యానెల్స్", "PC பேனல்கள்", "PC പാനലുകൾ"),
    ("8 Level ID", "8 लेवल आईडी", "8 లెవెల్ ఐడి", "8 லெவல் ஐடி", "8 ലെവൽ ഐഡി"),
    ("Preview Video", "प्रीव्यू वीडियो", "ప్రివ్యూ వీడియో", "முன்னோட்ட வீடியோ", "പ്രിവ്യൂ വീഡിയോ"),
    ("Check Update", "अपडेट देखें", "అప్‌డేట్ చూడండి", "புதுப்பிப்பைப் பார்க்க", "അപ്‌ഡേറ്റ് കാണുക"),
    ("Apply Coupon Code", "कूपन कोड लगाएँ", "కూపన్ కోడ్ వర్తింపజేయండి", "கூப்பன் குறியீட்டைப் பயன்படுத்து", "കൂപ്പൺ കോഡ് പ്രയോഗിക്കുക"),
    ("Open Shop", "शॉप खोलें", "షాప్ తెరవండి", "ஷாப்பைத் திற", "ഷോപ്പ് തുറക്കുക"),
    ("Update File ↗️", "अपडेट फ़ाइल ↗️", "అప్‌డేట్ ఫైల్ ↗️", "புதுப்பிப்பு கோப்பு ↗️", "അപ്‌ഡേറ്റ് ഫയൽ ↗️"),
]


def _i18n_compile(rows):
    """rows -> ({english: {lang: text}}, [(prefix, regex, {lang: text})])"""
    exact, templates = {}, []
    for row in rows:
        en, translated = row[0], dict(zip(_I18N_ORDER, row[1:]))
        if _I18N_PH.search(en):
            parts = _I18N_PH.split(en)
            pattern = "".join(re.escape(p) if i % 2 == 0 else "(?P<%s>.*?)" % p for i, p in enumerate(parts))
            templates.append((parts[0], re.compile("^" + pattern + "$", re.S), translated))
        else:
            exact[en] = translated
    return exact, templates

_LINE_EXACT, _LINE_TEMPL = _i18n_compile(_LINES)
_BTN_EXACT, _BTN_TEMPL = _i18n_compile(_BUTTONS)

def _i18n_fill(template, values):
    return _I18N_PH.sub(lambda m: values.get(m.group(1), m.group(0)), template)

def _i18n_lookup(core, lang, exact, templates, depth=0):
    """Translate one stripped line. None = not in the table."""
    hit = exact.get(core)
    if hit is not None:
        return hit.get(lang)
    for prefix, rx, translated in templates:
        if prefix and not core.startswith(prefix):
            continue
        m = rx.match(core)
        if not m:
            continue
        text = translated.get(lang)
        if text is None:
            return None
        values = m.groupdict()
        if depth < 2:                       # the changing parts too ("5 minutes", "RESELLER" ...)
            for key, val in values.items():
                inner = _i18n_lookup(val.strip(), lang, exact, templates, depth + 1) if val.strip() else None
                if inner is not None:
                    values[key] = inner
        return _i18n_fill(text, values)
    return None

def translate_text(text, lang):
    """Translate a whole message (HTML) line by line. Lines that are not in the table stay as they are."""
    if not text or lang == "en" or lang not in I18N_LANGS or not isinstance(text, str):
        return text
    out = []
    for line in text.split("\n"):
        core = line.strip()
        done = _i18n_lookup(core, lang, _LINE_EXACT, _LINE_TEMPL) if core else None
        if done is None:
            out.append(line)
        else:
            out.append(line[: len(line) - len(line.lstrip())] + done + line[len(line.rstrip()):])
    return "\n".join(out)

_BTN_SPLIT = re.compile(r"^([^\w]*)(.*)$", re.S)

def translate_button(text, lang):
    """Translate a button label; leading emoji / symbols stay exactly as they are."""
    if not text or lang == "en" or lang not in I18N_LANGS or not isinstance(text, str):
        return text
    m = _BTN_SPLIT.match(text)
    pre, body = (m.group(1), m.group(2).strip()) if m else ("", text.strip())
    done = _i18n_lookup(body, lang, _BTN_EXACT, _BTN_TEMPL) if body else None
    return text if done is None else pre + done

def i18n_selftest():
    """Returns a list of problems (empty = all tables are consistent). Not run at start-up."""
    problems = []
    for name, rows in (("lines", _LINES), ("buttons", _BUTTONS)):
        seen = set()
        for row in rows:
            if len(row) != 5:
                problems.append(f"{name}: row needs 5 columns: {row[0][:40]!r}")
                continue
            if row[0] in seen:
                problems.append(f"{name}: duplicate English row {row[0][:40]!r}")
            seen.add(row[0])
            ph, tags = sorted(_I18N_PH.findall(row[0])), sorted(_I18N_TAG.findall(row[0]))
            for lang, tr in zip(_I18N_ORDER, row[1:]):
                if not tr.strip():
                    problems.append(f"{name}/{lang}: empty translation of {row[0][:40]!r}")
                if sorted(_I18N_PH.findall(tr)) != ph:
                    problems.append(f"{name}/{lang}: placeholders differ in {row[0][:40]!r}")
                if sorted(_I18N_TAG.findall(tr)) != tags:
                    problems.append(f"{name}/{lang}: HTML tags differ in {row[0][:40]!r}")
    return problems

def lang_of(chat_id):
    """Language of the customer in a chat ('en' when unknown, or when the Language switch is off)."""
    if ui_get("lang_on", "1") == "0":
        return "en"
    try:
        return db.get_user_lang(chat_id)
    except Exception:
        return "en"

_ICON_KEY = "icon_custom_emoji_id"
_OTHER_BUTTON_FIELDS = ("web_app", "login_url", "switch_inline_query", "switch_inline_query_current_chat",
                        "switch_inline_query_chosen_chat", "callback_game", "pay", "copy_text")

def _clone_button(b, text=None, drop_icon=False):
    """Copy of an inline button with other text and/or without its premium emoji icon."""
    if any(getattr(b, f, None) for f in _OTHER_BUTTON_FIELDS):
        return b
    extra = dict(getattr(b, "api_kwargs", None) or {})
    if drop_icon:
        extra.pop(_ICON_KEY, None)
    kwargs = {f: getattr(b, f) for f in ("url", "callback_data") if getattr(b, f, None) is not None}
    if extra:
        kwargs["api_kwargs"] = extra
    return InlineKeyboardButton(b.text if text is None else text, **kwargs)

def _map_markup(markup, fn):
    rows, changed = [], False
    for row in markup.inline_keyboard:
        new_row = []
        for b in row:
            nb = fn(b)
            changed = changed or nb is not b
            new_row.append(nb)
        rows.append(new_row)
    return InlineKeyboardMarkup(rows) if changed else markup

def _markup_has_icons(markup):
    try:
        return any(_ICON_KEY in (getattr(b, "api_kwargs", None) or {}) for row in markup.inline_keyboard for b in row)
    except Exception:
        return False

def _strip_markup_icons(markup):
    if not isinstance(markup, InlineKeyboardMarkup) or not _markup_has_icons(markup):
        return markup
    return _map_markup(markup, lambda b: _clone_button(b, drop_icon=True) if _ICON_KEY in (getattr(b, "api_kwargs", None) or {}) else b)

def _localize_markup(markup, lang):
    if lang == "en" or not isinstance(markup, InlineKeyboardMarkup):
        return markup
    def one(b):
        new = translate_button(b.text, lang)
        return _clone_button(b, new) if new != b.text else b
    return _map_markup(markup, one)

_ICON_ERR_WORDS = ("emoji", "icon", "premium", "button")

def install_i18n():
    """Wrap the send / edit methods of telegram.Bot: every text and button is translated for its reader,
    and a premium-emoji icon that Telegram refuses is dropped (the message is re-sent without it)."""
    Bot = telegram.Bot
    if getattr(Bot, "_i18n_installed", False):
        return

    def wrap(name, text_fields):
        orig = getattr(Bot, name, None)
        if orig is None:
            return
        sig = inspect.signature(orig)

        @functools.wraps(orig)
        async def wrapper(self, *args, **kwargs):
            try:
                bound = sig.bind(self, *args, **kwargs)
                ar = bound.arguments
                if name == "answer_callback_query" or ar.get("chat_id") is None:
                    lang = lang_of(_CUR_USER.get())
                else:
                    lang = lang_of(ar.get("chat_id"))
                if lang != "en":
                    for field in text_fields:
                        if isinstance(ar.get(field), str):
                            ar[field] = translate_text(ar[field], lang)
                    if ar.get("reply_markup") is not None:
                        ar["reply_markup"] = _localize_markup(ar["reply_markup"], lang)
            except Exception:
                logger.exception("Translation step failed - sending the original text")
                return await orig(self, *args, **kwargs)
            try:
                return await orig(*bound.args, **bound.kwargs)
            except Exception as exc:
                markup = bound.arguments.get("reply_markup")
                if (type(exc).__name__ == "BadRequest" and _markup_has_icons(markup)
                        and any(w in str(exc).lower() for w in _ICON_ERR_WORDS)):
                    bound.arguments["reply_markup"] = _strip_markup_icons(markup)
                    result = await orig(*bound.args, **bound.kwargs)     # still failing -> the real error is raised
                    pause_icons()
                    return result
                raise

        setattr(Bot, name, wrapper)

    wrap("send_message", ("text",))
    wrap("send_photo", ("caption",))
    wrap("edit_message_text", ("text",))
    wrap("edit_message_caption", ("caption",))
    wrap("edit_message_reply_markup", ())
    wrap("answer_callback_query", ("text",))
    Bot._i18n_installed = True

try:
    install_i18n()
except Exception:
    logger.exception("Language support could not start - the bot continues in English")

def _localize_payload(method, payload):
    """Same translation for the plain-HTTP sends (tg_call), by the customer's chat id."""
    try:
        if method not in ("sendMessage", "sendPhoto", "editMessageText", "editMessageCaption", "editMessageReplyMarkup"):
            return payload
        lang = lang_of(payload.get("chat_id"))
        if lang == "en":
            return payload
        out = dict(payload)
        for field in ("text", "caption"):
            if isinstance(out.get(field), str):
                out[field] = translate_text(out[field], lang)
        markup = out.get("reply_markup")
        if isinstance(markup, dict) and markup.get("inline_keyboard"):
            out["reply_markup"] = {"inline_keyboard": [
                [dict(b, text=translate_button(b.get("text", ""), lang)) for b in row] for row in markup["inline_keyboard"]]}
        return out
    except Exception:
        logger.exception("Translation of a plain message failed - sending the original text")
        return payload

def _payload_has_icons(payload):
    markup = payload.get("reply_markup") if isinstance(payload, dict) else None
    return isinstance(markup, dict) and any(_ICON_KEY in b for row in markup.get("inline_keyboard", []) for b in row)

def _strip_payload_icons(payload):
    out = dict(payload)
    out["reply_markup"] = {"inline_keyboard": [[{k: v for k, v in b.items() if k != _ICON_KEY} for b in row]
                                               for row in payload["reply_markup"]["inline_keyboard"]]}
    return out


# ==========================================
# 🚨 ERROR ALERTS: every error of the bot / payments / web admin reaches the owner on Telegram
# Switch and target chat are set in the admin panel (alert_on, alert_chat). The same error is sent at most
# once per 10 minutes (with a count of repeats), at most 8 alerts per 10 minutes, and normal events such as
# "user blocked the bot" or short network hiccups are not reported.
# ==========================================
import threading
import traceback
import urllib.error
from collections import deque

_alert_state = {"sent": {}, "times": deque(), "lock": threading.Lock(), "transient": {}}
_alert_local = threading.local()
_ALERT_IGNORE_LOGGERS = ("httpx", "httpcore", "urllib3", "hpack")
_ALERT_IGNORE_TEXT = ("blocked by the user", "chat not found", "user is deactivated", "message is not modified",
                      "message to delete not found", "bot was kicked", "query is too old", "http error 403",
                      "message can't be deleted", "message to edit not found")
_ALERT_TRANSIENT = ("NetworkError", "TimedOut", "RetryAfter", "ConnectError", "ReadError", "ReadTimeout",
                    "ConnectTimeout", "RemoteProtocolError", "ConnectionResetError", "ProtocolError", "TimeoutError", "ConnectionError")

def _alert_chat():
    chat = str(ui_get("alert_chat", "")).strip()
    return int(chat) if chat.isdigit() else ADMIN_ID

def _alert_post(text):
    """Send one alert (plain HTTP, no translation, no icons). True if Telegram accepted it."""
    _alert_local.busy = True
    try:
        data = json.dumps({"chat_id": _alert_chat(), "text": text[:4000], "parse_mode": "HTML",
                           "disable_web_page_preview": True}).encode()
        req = urllib.request.Request(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", data=data,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            return bool(json.loads(resp.read().decode()).get("ok"))
    except Exception:
        return False
    finally:
        _alert_local.busy = False

def send_test_alert():
    """Used by the 'Send test alert' button of the admin panel."""
    return _alert_post("🧪 <b>Test alert</b>\nIf you can read this, error alerts reach you correctly.\n"
                       f"🕒 {get_ist_time()}")

class _AlertHandler(logging.Handler):
    def emit(self, record):
        try:
            if record.levelno < logging.ERROR or getattr(_alert_local, "busy", False):
                return
            if record.name.startswith(_ALERT_IGNORE_LOGGERS):
                return
            _alert_local.busy = True
            try:
                if str(ui_get("alert_on", "1")) == "0":
                    return
                message = record.getMessage()
                exc_name = record.exc_info[0].__name__ if record.exc_info and record.exc_info[0] else ""
                blob = (message + " " + (str(record.exc_info[1]) if record.exc_info else "")).lower()
                if any(t in blob for t in _ALERT_IGNORE_TEXT):
                    return
                sig = (record.name, message.split("\n")[0][:120], exc_name)
                now = time.time()
                with _alert_state["lock"]:
                    if exc_name in _ALERT_TRANSIENT:             # short network problems: only if they keep coming
                        hits = [t for t in _alert_state["transient"].get(sig, []) if now - t < 600] + [now]
                        _alert_state["transient"][sig] = hits
                        if len(hits) < 5:
                            return
                    last, skipped = _alert_state["sent"].get(sig, (0.0, 0))
                    if now - last < 600:
                        _alert_state["sent"][sig] = (last, skipped + 1)
                        return
                    times = _alert_state["times"]
                    while times and now - times[0] > 600:
                        times.popleft()
                    if len(times) >= 8:
                        return
                    times.append(now)
                    _alert_state["sent"][sig] = (now, 0)
                    if len(_alert_state["sent"]) > 500:
                        _alert_state["sent"].clear()
                tb = ""
                if record.exc_info and record.exc_info[0]:
                    lines = "".join(traceback.format_exception(*record.exc_info)).strip().splitlines()
                    tb = "\n".join(lines[-7:])
                text = ("🚨 <b>BOT ERROR</b>\n"
                        f"🕒 {get_ist_time()}\n"
                        f"📍 <code>{html.escape(record.name)}</code>\n"
                        f"💬 {html.escape(message[:600])}"
                        + (f"\n<pre>{html.escape(tb[-1800:])}</pre>" if tb else "")
                        + (f"\n🔁 Same error was also seen {skipped} more time(s) since the last alert." if skipped else ""))
                threading.Thread(target=_alert_post, args=(text,), daemon=True).start()
            finally:
                _alert_local.busy = False
        except Exception:
            pass

def _hook_uncaught():
    """Errors that would otherwise only appear in the Render log are sent to the same alert channel."""
    old_hook = sys.excepthook
    def _sys_hook(exc_type, exc, tb):
        logger.error("Uncaught exception", exc_info=(exc_type, exc, tb))
        old_hook(exc_type, exc, tb)
    sys.excepthook = _sys_hook
    def _thread_hook(args):
        logger.error("Uncaught exception in thread %s", getattr(args.thread, "name", "?"),
                     exc_info=(args.exc_type, args.exc_value, args.exc_traceback))
    threading.excepthook = _thread_hook

def _loop_exception_handler(loop, context):
    logger.error("Asyncio error: %s", context.get("message", ""), exc_info=context.get("exception"))

try:
    import sys
    logging.getLogger().addHandler(_AlertHandler())
    _hook_uncaught()
except Exception:
    logger.exception("Error alerts could not start")

# the web admin uses these two (test-alert button, translation of messages it sends to customers)
web_admin.send_test_alert = send_test_alert
web_admin.translate_hook = translate_text

# Initialize the persistent PostgreSQL schema before the bot starts.
init_db()
publish_reseller_schema()

# ==========================================
# ✨ MESSAGE-REPLACEMENT NAVIGATION
# ==========================================
FAST_EDIT = os.environ.get("FAST_EDIT", "0") == "1"   # OFF = original "delete + send a fresh message" behaviour

async def replace_callback_message(query, context, text, reply_markup=None, parse_mode="HTML"):
    """Delete the current callback message, then open the next screen as a fresh message.
    Used only for callback-menu navigation so every screen transition uses the same behavior.
    """
    msg = query.message
    # Fast path: ONE Telegram call edits the screen in place (works for text messages).
    # Only used when FAST_EDIT=1 is set in Render (off by default).
    if FAST_EDIT and msg is not None and getattr(msg, "text", None) is not None:
        try:
            return await query.edit_message_text(text=text, parse_mode=parse_mode, reply_markup=reply_markup)
        except BadRequest as e:
            if "not modified" in str(e).lower():
                return msg
        except Exception:
            pass
    chat_id = query.message.chat_id
    try:
        await query.message.delete()
    except Exception:
        pass
    return await context.bot.send_message(
        chat_id=chat_id,
        text=text,
        parse_mode=parse_mode,
        reply_markup=reply_markup
    )

# ==========================================
# 🏠 1. MAIN MENU (10 BUTTONS & HIGHLIGHTS)
# ==========================================
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    args = context.args
    cfg = get_store_config()
    existing = db_get_user(user.id)   # one read, reused below

    # Referral Tracking Logic
    if args and args[0].startswith("ref_"):
        try:
            ref_by = int(args[0].replace("ref_", ""))
            if ref_by != user.id and not existing:
                ref_bonus = _num(get_settings().get("referral_bonus"), 0)
                if ref_bonus > 0:
                    db.execute(
                        'UPDATE users SET total_referrals = total_referrals + 1, '
                        'wallet_balance = wallet_balance + %s, referral_earnings = referral_earnings + %s '
                        'WHERE user_id = %s',
                        (ref_bonus, ref_bonus, ref_by)
                    )
                else:
                    db.execute(
                        'UPDATE users SET total_referrals = total_referrals + 1 WHERE user_id = %s',
                        (ref_by,)
                    )
                try:
                    bonus_line = f"\n💰 <b>₹{fmt_amount(ref_bonus)}</b> bonus added to your wallet!" if ref_bonus > 0 else ""
                    await context.bot.send_message(chat_id=ref_by, text=f"🎉 <b>New Referral!</b> User <code>{user.id}</code> joined via your link.{bonus_line}", parse_mode="HTML")
                except Exception:
                    pass
        except Exception:
            pass

    uname = f"@{user.username}" if user.username else "N/A"
    if (not existing) or existing[0] != user.full_name or existing[1] != uname:
        db_add_or_update_user(user.id, user.full_name, uname, get_ist_time())
        existing = db_get_user(user.id)

    # Direct buy link from the admin panel: /start buy_<product_key>
    if args and args[0].startswith("buy_") and update.message:
        view = build_plans_view(user.id, args[0][len("buy_"):])
        if view:
            await update.message.reply_text(view[0], parse_mode="HTML", reply_markup=view[1])
            return

    u_data = existing
    balance = u_data[4] if u_data else 0.0

    text = (
        f"🛒 ── <b>{html.escape(cfg['shop_name'].upper())}</b> ── 🛒\n"
        f"{('<i>' + html.escape(cfg['tagline']) + '</i>' + chr(10)) if cfg['tagline'] else ''}\n"
        f"👏 Welcome, <b>{html.escape(user.first_name or 'there')}!</b> 🥷\n\n"
        f"📈 ── <b>STORE HIGHLIGHTS</b> ── 📈\n\n"
        f"🗝️ Premium Game Keys\n"
        f"⚡ Instant Delivery 24/7\n"
        f"🔓 100% Secure Payment\n"
        f"💸 Best Prices Guaranteed\n"
        f"🎁 Referral Rewards\n"
        f"💬 Professional Support\n"
        f"_________________________\n\n"
        f"👤 User ID: <code>{user.id}</code>\n"
        f"💰 Wallet Balance: <b>₹{balance:,.2f}</b>\n"
        f"_________________________\n\n"
        f"🚀 Tap Shop Now to Start!"
    )

    custom = ui_get("welcome_text", "").strip()
    if custom:      # welcome text written in the admin panel ({name} {user_id} {balance} {shop_name})
        text = _safe_html(_fill_text(custom, name=html.escape(user.first_name or "there"), user_id=user.id,
                                     balance=f"{balance:,.2f}", shop_name=html.escape(cfg["shop_name"])))

    tutorial_link = cfg.get("how_to_use_link")
    if not valid_url(tutorial_link):
        tutorial_link = "https://t.me/chatelitehackers"

    keyboard = [
        [styled_button("🛒 Shop Key", "shop_key", callback_data="shop_key"), styled_button("👤 My Profile", "my_profile", callback_data="my_profile")],
        [styled_button("💰 Add Balance", "add_balance", callback_data="add_balance"), styled_button("🧾 All History", "all_history", callback_data="all_history")],
        [styled_button("▶️ Tutorial watch", "tutorial", url=tutorial_link)]
        + ([] if (_is_reseller_row(u_data) or not get_reseller_cfg()["enabled"])
           else [styled_button("👑 Reseller", "reseller", callback_data="reseller_plan")]),
        [styled_button("💼 Selling Proof ↗️", "selling_proof", url="https://t.me/+fJrFACSrntgwNjll"), styled_button("💬 Support", "support", callback_data="support_contact")],
        [styled_button("🎰 Lucky Spin", "lucky_spin", callback_data="lucky_spin"), styled_button("👥 Referral", "referral", callback_data="referral_menu")],
        [styled_button("👑 Top 5 Sellers Monthly", "top_sellers", callback_data="top_sellers")]
    ]

    if valid_url(cfg.get("update_link")):
        keyboard.append([styled_button("📢 Check All Updates", "check_updates", url=cfg["update_link"])])

    if ui_get("lang_on", "1") != "0":
        keyboard.append([styled_button("🌐 Language", "language", callback_data="language_menu")])

    if user.id == ADMIN_ID:
        r_url = os.environ.get("RENDER_EXTERNAL_URL", "http://localhost:8080")
        keyboard.append([styled_button("👑 Web Admin Panel", "web_admin", url=r_url)])

    reply_markup = InlineKeyboardMarkup(keyboard)
    await _send_menu(update, context, text, reply_markup)

async def _send_menu(update, context, text, markup):
    """The main menu. With the welcome picture (set in the admin panel) when it is switched on."""
    image = ui_get("welcome_image", "").strip()
    if image and ui_get("welcome_image_on", "0") == "1" and update.effective_chat:
        chat_id = update.effective_chat.id
        query = update.callback_query
        if query is not None and query.message is not None:
            try:
                await query.message.delete()
            except Exception:
                pass
        try:
            if len(text) <= 1024:
                await context.bot.send_photo(chat_id=chat_id, photo=image, caption=text, parse_mode="HTML", reply_markup=markup)
            else:
                await context.bot.send_photo(chat_id=chat_id, photo=image)
                await context.bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML", reply_markup=markup)
        except Exception:
            logger.warning("Welcome picture could not be sent - sending the text only", exc_info=True)
            await context.bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML", reply_markup=markup)
        return
    if update.message:
        await update.message.reply_text(text, parse_mode="HTML", reply_markup=markup)
    elif update.callback_query:
        await replace_callback_message(update.callback_query, context, text, markup)

# ==========================================
# 🌐 LANGUAGE MENU
# ==========================================
async def track_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Remember who is being served, so pop-up alerts are translated for the right person."""
    user = update.effective_user
    if user:
        _CUR_USER.set(user.id)

async def language_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    current = db.get_user_lang(user.id) if user else "en"
    text = "🌐 <b>Choose your language</b>\n\nTap a language below. The whole bot will switch to it."
    rows = [[styled_button(("✅ " if code == current else "") + name, "lang_option", callback_data=f"setlang_{code}")]
            for code, name in I18N_LANGS.items()]
    rows.append([styled_button("🔜 Back", "back", callback_data="main_menu")])
    markup = InlineKeyboardMarkup(rows)
    if update.callback_query:
        ack(update.callback_query)
        await replace_callback_message(update.callback_query, context, text, markup)
    elif update.message:
        await update.message.reply_text(text, parse_mode="HTML", reply_markup=markup)

async def set_language(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    code = (query.data or "").replace("setlang_", "", 1)
    if code not in I18N_LANGS:
        ack(query)
        return
    try:
        db.set_user_lang(update.effective_user.id, code)
    except Exception:
        logger.exception("Could not save the language")
        ack(query)
        return
    try:
        await query.answer("✅ Language updated")
    except Exception:
        pass
    await start_command(update, context)

# ==========================================
# 💬 SUPPORT
# ==========================================
async def support_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    ack(query)
    username = (get_store_config().get("support_username") or "").strip()
    handle = username.lstrip("@")

    text = (
        "💬 <b>SUPPORT</b>\n"
        "_________________________\n\n"
        "Need help with an order or payment? Contact our support team:\n\n"
        f"👤 {html.escape(username) if username else 'Support not configured yet'}\n\n"
        "⏱️ We usually reply within a few hours."
    )
    keyboard = []
    if handle:
        keyboard.append([styled_button("💬 Contact Support", "support_contact", url=f"https://t.me/{handle}")])
    keyboard.append([styled_button("🔜 Back", "back", callback_data="main_menu")])
    await replace_callback_message(query, context, text, InlineKeyboardMarkup(keyboard))

# ==========================================
# 👤 2. MY PROFILE
# ==========================================
async def profile_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    ack(query)
    user = update.effective_user
    u_data = db_get_user(user.id)

    name = html.escape(u_data[0] or "") if u_data else html.escape(user.full_name or "")
    joined = u_data[2] if u_data else get_ist_time()
    orders_cnt = u_data[3] if u_data else 0
    balance = u_data[4] if u_data else 0.0
    spent = u_data[5] if u_data else 0.0
    ref_cnt = u_data[6] if u_data else 0
    ref_earn = u_data[7] if u_data else 0.0
    account_type = u_data[8] if u_data else "Regular"

    account_badge = "👑 Reseller User" if account_type == "Reseller" else "👤 Regular"

    text = (
        f"👤 ── <b>YOUR PROFILE</b> ── 👤\n\n"
        f"🚩 User ID: <code>{user.id}</code>\n"
        f"🚩 Name: <b>{name}</b>\n"
        f"🤖 Phone: N/A\n"
        f"👑 Account: {account_badge} | 🟢 Active\n\n"
        f"💰 ── <b>Balance</b> ── 💰\n"
        f"🎟️ Current: <b>₹{balance:,.2f}</b>\n\n"
        f"📊 ── <b>Statistics</b> ── 📊\n"
        f"🎯 Orders: {orders_cnt}\n"
        f"💸 Spent: ₹{spent:,.2f}\n"
        f"🤝 Referrals: {ref_cnt}\n"
        f"🎁 Referral Earnings: ₹{ref_earn:,.2f}\n\n"
        f"🕒 Joined: {joined}"
    )

    keyboard = []
    if account_type != "Reseller" and get_reseller_cfg()["enabled"]:
        keyboard.append([styled_button("👑 Upgrade To Reseller", "profile_upgrade", callback_data="reseller_plan")])
    keyboard.append([styled_button("🔜 Back", "back", callback_data="main_menu")])
    await replace_callback_message(query, context, text, InlineKeyboardMarkup(keyboard))

# ==========================================
# 👑 3. RESELLER PLAN
# ==========================================
async def reseller_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    ack(query)
    user = update.effective_user
    cfg = get_reseller_cfg()

    # One-time purchase: an existing reseller (or a switched-off plan) never sees this screen again.
    if _is_reseller_row(db_get_user(user.id)) or not cfg["enabled"]:
        msg = ("✅ You are already a <b>👑 VIP Reseller</b>." if cfg["enabled"]
               else "⚠️ The reseller plan is not available right now.")
        await replace_callback_message(query, context, msg, back_to_menu_markup())
        return

    text = (_safe_html(_fill(cfg["reseller_text"], min_balance=cfg["min_balance"], fee=cfg["fee"],
                             rest=max(0.0, cfg["min_balance"] - cfg["fee"])))
            if cfg["reseller_text"].strip() else "👑 <b>Reseller Plan</b>")
    keyboard = [
        [styled_button(cfg["reseller_buy_label"] or "Buy Reseller Plan", "reseller_buy", callback_data="buy_reseller_action")],
        [styled_button("🔜 Back", "back", callback_data="main_menu")]
    ]
    await replace_callback_message(query, context, text, InlineKeyboardMarkup(keyboard))

async def buy_reseller_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    ack(query)
    user = update.effective_user
    cfg = get_reseller_cfg()
    u_data = db_get_user(user.id)
    balance = u_data[4] if u_data else 0.0

    if _is_reseller_row(u_data) or not cfg["enabled"]:
        await replace_callback_message(query, context, "✅ Reseller plan is already active on your account." if cfg["enabled"]
                                       else "⚠️ The reseller plan is not available right now.", back_to_menu_markup())
        return

    try:
        status, balance = db.activate_reseller(user.id, cfg["min_balance"], cfg["fee"])
    except Exception:
        logger.exception("Reseller activation failed")
        await replace_callback_message(query, context, "⚠️ Please try again — could not activate right now.", back_to_menu_markup())
        return
    rest = max(0.0, cfg["min_balance"] - cfg["fee"])
    if status == "already":
        await replace_callback_message(query, context, "👑 <b>You are already a VIP Reseller!</b>\n\nYour reseller plan is already active — nothing was charged.", back_to_menu_markup())
    elif status == "ok":
        _settings_cache["v"] = None
        if cfg["reseller_success_text"].strip():
            text = _safe_html(_fill(cfg["reseller_success_text"], fee=cfg["fee"], rest=rest, min_balance=cfg["min_balance"]))
        else:
            text = ("🎉 <b>CONGRATULATIONS!</b>\n\nYou are now officially a <b>👑 VIP Reseller!</b>\n\n"
                    f"💸 Activation fee deducted: <b>₹{fmt_amount(cfg['fee'])}</b>\n"
                    f"💰 Wallet balance now: <b>₹{fmt_amount(balance)}</b>")
        keyboard = [[styled_button("👤 View Profile", "view_profile", callback_data="my_profile")]]
        await replace_callback_message(query, context, text, InlineKeyboardMarkup(keyboard))
    else:
        if cfg["reseller_lowbal_text"].strip():
            text = _safe_html(_fill(cfg["reseller_lowbal_text"], min_balance=cfg["min_balance"], balance=balance, fee=cfg["fee"]))
        else:
            text = ("⚠️ <b>Insufficient Balance!</b>\n\n"
                    f"💰 Required: <b>₹{fmt_amount(max(cfg['min_balance'], cfg['fee']))}</b>\n"
                    f"💸 Your Balance: <b>₹{fmt_amount(balance)}</b>\n\n"
                    "Please add balance to your wallet to activate the VIP Reseller Plan.")
        keyboard = [
            [styled_button("💰 Add Balance", "reseller_add_balance", callback_data="add_balance")],
            [styled_button("🔜 Back", "back", callback_data="reseller_plan")]
        ]
        await replace_callback_message(query, context, text, InlineKeyboardMarkup(keyboard))

# ==========================================
# 💰 4. ADD BALANCE & NUMPAD CALCULATOR
# ==========================================
async def add_balance_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Add Balance goes straight to the quick-amount / keypad screen (no UPI-method step)."""
    await select_deposit_amount(update, context)

async def select_deposit_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    ack(query)
    context.user_data['deposit_method'] = "upi"

    user = update.effective_user
    u_data = db_get_user(user.id)
    balance = u_data[4] if u_data else 0.0

    text = (
        f"💸 <b>Add Balance</b>\n\n"
        f"Current balance: <b>₹{balance:,.2f}</b>\n\n"
        f"Pick a quick amount below, or enter a custom amount.\n"
        f"Min: ₹10.00 · Max: ₹50,000.00"
    )
    keyboard = [
        [styled_button("₹100", "preset_amount", callback_data="preset_dep_100"), styled_button("₹500", "preset_amount", callback_data="preset_dep_500")],
        [styled_button("₹1000", "preset_amount", callback_data="preset_dep_1000"), styled_button("₹2000", "preset_amount", callback_data="preset_dep_2000")],
        [styled_button("✏️ Custom Amount", "custom_amount", callback_data="numpad_open")],
        [styled_button("👆 Back to Menu", "back", callback_data="main_menu")]
    ]
    await replace_callback_message(query, context, text, InlineKeyboardMarkup(keyboard))

async def numpad_open(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ack(update.callback_query)
    await render_numpad(update, context)

async def render_numpad(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    current_digits = context.user_data.get('numpad_val', '')
    amt_display = f"₹{int(current_digits):,}" if current_digits and int(current_digits) > 0 else "₹0"
    st = get_settings()
    min_dep, max_dep = _num(st.get("min_deposit"), 10), _num(st.get("max_deposit"), 50000)

    text = (
        "💰 <b>Enter Amount</b>\n\n"
        f"<b>{amt_display}</b>\n\n"
        f"Min: ₹{min_dep:,.2f} · Max: ₹{max_dep:,.2f}"
    )

    confirm_text = f"✅ Confirm {amt_display}" if current_digits and int(current_digits) >= min_dep else "✅ Confirm"

    keyboard = [
        [styled_button("1", "np_digit", callback_data="np_1"), styled_button("2", "np_digit", callback_data="np_2"), styled_button("3", "np_digit", callback_data="np_3")],
        [styled_button("4", "np_digit", callback_data="np_4"), styled_button("5", "np_digit", callback_data="np_5"), styled_button("6", "np_digit", callback_data="np_6")],
        [styled_button("7", "np_digit", callback_data="np_7"), styled_button("8", "np_digit", callback_data="np_8"), styled_button("9", "np_digit", callback_data="np_9")],
        [styled_button("C", "np_clear", callback_data="np_clear"), styled_button("0", "np_digit", callback_data="np_0"), styled_button("⌫", "np_backspace", callback_data="np_backspace")],
        [styled_button(confirm_text, "np_confirm", callback_data="np_confirm")],
        [styled_button("✋ Back", "back", callback_data="dep_method_upi")]
    ]
    markup = InlineKeyboardMarkup(keyboard)

    try:
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
    except BadRequest as e:
        if "not modified" in str(e).lower():
            return
        await replace_callback_message(query, context, text, markup)

async def handle_numpad_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    action = query.data.replace("np_", "")
    val = context.user_data.get('numpad_val', '')

    if action in ["0", "1", "2", "3", "4", "5", "6", "7", "8", "9"]:
        if len(val) < 6:
            val += action
            context.user_data['numpad_val'] = val
        ack(query)
        await render_numpad(update, context)

    elif action == "clear":
        context.user_data['numpad_val'] = ''
        ack(query)
        await render_numpad(update, context)

    elif action == "backspace":
        context.user_data['numpad_val'] = val[:-1]
        ack(query)
        await render_numpad(update, context)

    elif action == "confirm":
        st = get_settings()
        min_dep, max_dep = _num(st.get("min_deposit"), 10), _num(st.get("max_deposit"), 50000)
        if not val or int(val) < min_dep:
            await query.answer(f"⚠️ Minimum deposit is ₹{min_dep:,.2f}!", show_alert=True)
            return
        if int(val) > max_dep:
            await query.answer(f"⚠️ Maximum deposit is ₹{max_dep:,.2f}!", show_alert=True)
            return

        ack(query)
        deposit_amt = float(val)
        context.user_data['numpad_val'] = ''
        await show_deposit_summary(query, context, deposit_amt)

def _deposit_markup():
    kb = [[styled_button("💳 UPI Payment (QR Code)", "pay_upi", callback_data="depay_upi")]]
    try:
        if db.get_binance_settings()["enabled"]:
            kb.append([styled_button("🟡 Binance USDT Payment", "pay_binance", callback_data="depay_bin")])
    except Exception:
        pass
    kb.append([styled_button("❌ Back", "back", callback_data="dep_method_upi")])
    return InlineKeyboardMarkup(kb)

async def show_deposit_summary(query, context, amount):
    """ORDER SUMMARY for an Add Balance top-up: amount + UPI / Binance choice."""
    context.user_data["deposit_amount"] = float(amount)
    text = (
        "📋 <b>ORDER SUMMARY</b>\n__________________________________\n\n"
        "💰 Add Balance to Wallet\n"
        f"💵 Amount: ₹{float(amount):,.2f}\n"
        "__________________________________\n"
        f"💰 <b>Final Total: ₹{float(amount):,.2f}</b>\n\n"
        "Select payment method below:"
    )
    await replace_callback_message(query, context, text, _deposit_markup())

async def deposit_pay_upi_click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    amt = context.user_data.get("deposit_amount")
    if not amt:
        await query.answer("⚠️ Session expired. Choose the amount again.", show_alert=True)
        return
    ack(query)
    context.user_data["deposit_method"] = "upi"
    await generate_deposit_qr(update, context, float(amt))

async def deposit_pay_binance_click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    amt = context.user_data.get("deposit_amount")
    bn = db.get_binance_settings()
    if not amt or not bn["enabled"] or not bn["pay_id"]:
        await query.answer("⚠️ Binance payment is not available right now.", show_alert=True)
        return
    ack(query)
    user = update.effective_user
    usdt = round(float(amt) / max(bn["rate"], 0.01), 2)
    order_id = f"BIN{datetime.now(IST).strftime('%Y%m%d%H%M%S')}{os.urandom(4).hex().upper()}"
    await asyncio.to_thread(
        db.create_payment, user.id, order_id, float(amt), "binance", bn["pay_id"], get_ist_time(),
        None, None, False, False)
    text = (
        "🟡 <b>BINANCE USDT PAYMENT</b>\n__________________________________\n\n"
        f"💰 Add Balance: ₹{float(amt):,.2f}\n"
        f"💵 Send exactly: <b>{usdt:,.2f} USDT</b>\n\n"
        f"📨 Binance Pay ID: <code>{html.escape(bn['pay_id'])}</code>\n"
        + (f"\n{html.escape(bn['note'])}\n" if bn["note"] else "") +
        "\nAfter paying, tap <b>✅ I have paid</b>. Balance is added as soon as the admin confirms.\n"
        f"🆔 <code>{order_id}</code>"
    )
    kb = [[styled_button("✅ I have paid", "paid", callback_data=f"paid_{order_id}")],
          [styled_button("❌ Cancel Payment", "cancel_payment", callback_data=f"canceldep_{order_id}")]]
    await replace_callback_message(query, context, text, InlineKeyboardMarkup(kb))

async def preset_dep_click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ack(update.callback_query)
    amt = float(update.callback_query.data.replace("preset_dep_", ""))
    await show_deposit_summary(update.callback_query, context, amt)

# ----------------------------------------------------------------------
# 🖼️ Clean UPI QR image (white background, big, UPI badge in the middle)
# Needs the optional package  qrcode[pil]  (add it to requirements.txt).
# Without it the bot automatically falls back to the online QR service.
# ----------------------------------------------------------------------
try:
    import qrcode
    from qrcode.constants import ERROR_CORRECT_H
    from PIL import Image, ImageDraw, ImageFont
    _QR_LOCAL = True
except Exception:  # package not installed -> use the fallback URL
    _QR_LOCAL = False


def _qr_font(size):
    for path in (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
        "DejaVuSans-Bold.ttf",
    ):
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            continue
    try:
        return ImageFont.load_default(size=size)
    except Exception:
        return ImageFont.load_default()


def build_upi_qr_png(upi_uri):
    """Return PNG bytes of a large black-on-white QR with a small UPI badge in the centre."""
    qr = qrcode.QRCode(error_correction=ERROR_CORRECT_H, box_size=10, border=4)
    qr.add_data(upi_uri)
    qr.make(fit=True)
    modules = qr.modules_count + 8
    qr.box_size = max(10, 960 // modules)
    img = qr.make_image(fill_color="black", back_color="white").convert("RGB")

    w, h = img.size
    d = int(w * 0.20)                      # badge covers ~4% of the code (safe with level H)
    badge = Image.new("RGB", (d, d), "white")
    draw = ImageDraw.Draw(badge)
    draw.rounded_rectangle((2, 2, d - 3, d - 3), radius=d // 6, fill="white", outline="#1f2937", width=max(2, d // 28))
    # two small arrows (orange + green) like the UPI mark, with the text below
    a = d // 6
    top = d // 5
    draw.polygon([(d // 2 - a * 2, top), (d // 2 - a // 2, top), (d // 2 + a // 2, top + a * 2), (d // 2 - a, top + a * 2)], fill="#f97316")
    draw.polygon([(d // 2 - a // 2, top), (d // 2 + a * 2, top), (d // 2 + a, top + a * 2), (d // 2 + a // 2, top + a * 2)], fill="#16a34a")
    font = _qr_font(max(10, d // 4))
    text = "UPI"
    try:
        tw = draw.textlength(text, font=font)
    except Exception:
        tw = d // 2
    draw.text(((d - tw) / 2, d - d // 3 - d // 12), text, fill="#1f2937", font=font)
    img.paste(badge, ((w - d) // 2, (h - d) // 2))

    out = io.BytesIO()
    img.save(out, format="PNG", optimize=True)
    return out.getvalue()


# 📷 Dynamic QR Generator with auto expiry (minutes set in the admin panel)
async def generate_deposit_qr(update: Update, context: ContextTypes.DEFAULT_TYPE, amount: float,
                              is_deficit=False, prod_name=None, plan_name=None, prod_key=None,
                              full_price=False, coupon_code=None, discount=0.0):
    query = update.callback_query
    user_id = query.from_user.id
    chat_id = query.message.chat_id
    method = "UPI"
    qr_minutes = max(1, int(_num(get_settings().get("qr_expiry_minutes"), 5)))
    qr_minutes_txt = f"{qr_minutes} minute" + ("" if qr_minutes == 1 else "s")

    cfg = get_payment_config()
    upi_id, payee_name, auto_on = cfg["upi_id"], cfg["payee_name"], cfg["auto_verify"]
    if not upi_id:
        await context.bot.send_message(
            chat_id=chat_id,
            text="⚠️ Payments are temporarily unavailable. Please try again later or contact support.",
            reply_markup=back_to_menu_markup()
        )
        return

    order_id = f"{method}{datetime.now(IST).strftime('%Y%m%d%H%M%S')}{os.urandom(4).hex().upper()}"

    # Persist the pending payment in PostgreSQL (the payment record is the source of truth).
    # In automatic mode a few paise are added so this payment has its own exact amount.
    try:
        pay_amount = db.create_payment(
            user_id=user_id,
            order_id=order_id,
            amount=amount,
            method=method.lower(),
            upi_id=upi_id,
            created_at=get_ist_time(),
            prod_key=prod_key,
            plan=plan_name,
            is_deficit=is_deficit,
            unique_amount=auto_on,
        )
    except Exception as e:
        logger.exception("Failed to save payment record: %s", e)
        await context.bot.send_message(chat_id=chat_id, text="⚠️ Could not create the payment. Please try again in a moment.")
        return

    if coupon_code:
        try:
            db.set_payment_coupon(order_id, coupon_code, discount)
        except Exception:
            logger.exception("Could not save coupon on payment")

    upi_uri = f"upi://pay?pa={upi_id}&pn={urllib.parse.quote(payee_name, safe='')}&am={pay_amount:.2f}&cu=INR&tn={order_id}"
    # Fallback image (used only if the local generator is not available): big, white background.
    qr_url = (
        "https://api.qrserver.com/v1/create-qr-code/?size=800x800&margin=30&ecc=H&bgcolor=ffffff&color=000000"
        f"&data={urllib.parse.quote(upi_uri)}"
    )
    qr_photo = qr_url
    if _QR_LOCAL:
        try:
            qr_photo = io.BytesIO(await asyncio.to_thread(build_upi_qr_png, upi_uri))
            qr_photo.name = "payment_qr.png"
        except Exception:
            logger.exception("Local QR generation failed - using the online QR service")
            qr_photo = qr_url

    if auto_on:
        how_to = (
            f"Scan &amp; pay exactly <b>₹{pay_amount:,.2f}</b> via any UPI app.\n\n"
            "Your balance updates <b>automatically</b> the moment payment is confirmed — "
            "no button tap needed."
        )
    else:
        how_to = (
            f"Scan &amp; pay exactly <b>₹{pay_amount:,.2f}</b> via any UPI app.\n\n"
            "After paying, tap <b>✅ I have paid</b>. "
            "It is confirmed as soon as the admin approves the payment."
        )

    if is_deficit:
        u_data = db_get_user(user_id)
        bal = u_data[4] if u_data else 0.0
        caption = (
            f"🛒 <b>{html.escape(prod_name or '')} — {html.escape((plan_name or '').replace('_', ' '))}</b>\n"
            f"💰 Balance: ₹{bal:,.2f}  |  Price: ₹{(amount if full_price else bal + amount):,.2f}\n\n"
            f"{how_to}\n\n"
            f"⏰ <b>This QR expires in {qr_minutes_txt} if payment isn't completed.</b>\n"
            f"🆔 <code>{order_id}</code>"
        )
    else:
        caption = (
            f"💵 <b>Add Balance — ₹{amount:,.2f}</b>\n\n"
            f"{how_to}\n\n"
            f"⏰ <b>This QR expires in {qr_minutes_txt} if payment isn't completed.</b>\n"
            f"🆔 <code>{order_id}</code>"
        )

    keyboard = [
        [styled_button("✅ I have paid", "paid", callback_data=f"paid_{order_id}")],
        [styled_button("❌ Cancel Payment", "cancel_payment", callback_data=f"canceldep_{order_id}")],
    ]

    try:
        await query.message.delete()
    except Exception:
        pass

    try:
        qr_msg = await context.bot.send_photo(
            chat_id=chat_id,
            photo=qr_photo,
            caption=caption,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    except Exception:
        logger.exception("Sending the QR photo failed for %s - retrying with the online QR", order_id)
        try:
            qr_msg = await context.bot.send_photo(
                chat_id=chat_id, photo=qr_url, caption=caption,
                parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard)
            )
        except Exception:
            logger.exception("QR fallback failed for %s - sending UPI details as text", order_id)
            qr_msg = await context.bot.send_message(
                chat_id=chat_id,
                text=caption + f"\n\n💳 UPI ID: <code>{html.escape(upi_id)}</code>\n💰 Amount: <b>₹{pay_amount:,.2f}</b>",
                parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard)
            )
    try:
        db.set_payment_message(order_id, qr_msg.chat_id, qr_msg.message_id)
    except Exception:
        logger.exception("Could not remember the QR message for %s", order_id)

    # ⏳ 5-Minute Expiry Watchdog (stops as soon as the payment leaves "pending", e.g. auto-confirmed)
    async def expiry_watchdog():
        try:
            start_t = time.time()
            while time.time() - start_t < qr_minutes * 60:
                await asyncio.sleep(5)
                row = await asyncio.to_thread(db.get_payment, order_id)
                if not row or row["status"] != "pending":
                    return

            expired = await asyncio.to_thread(
                db.claim_payment, order_id, "pending", "expired", None, get_ist_time()
            )
            if not expired:
                return

            try:
                await context.bot.delete_message(chat_id=qr_msg.chat_id, message_id=qr_msg.message_id)
            except Exception:
                pass

            if auto_on:
                exp_text = (
                    f"🕒 <b>PAYMENT EXPIRED</b>\n"
                    f"_________________________\n\n"
                    f"— 🔒 Order: <code>{order_id}</code>\n"
                    f"— ❌ No payment detected within {qr_minutes_txt}\n"
                    f"— ℹ️ If you already paid the exact amount, it is still credited automatically (up to 1 hour).\n\n"
                    f"<i>Paid but nothing arrived? Tap the button below. Or tap /start for a new order.</i>"
                )
                exp_markup = InlineKeyboardMarkup(
                    [[styled_button("💬 I paid but wasn't credited", "paid_not_credited", callback_data=f"paid_{order_id}")]]
                )
            else:
                exp_text = (
                    f"🕒 <b>PAYMENT EXPIRED</b>\n"
                    f"_________________________\n\n"
                    f"— 🔒 Order: <code>{order_id}</code>\n"
                    f"— ❌ Not paid within {qr_minutes_txt}\n"
                    f"— 🔒 QR / payment details are no longer valid\n\n"
                    f"<i>Tap /start to begin a new order.</i>"
                )
                exp_markup = None
            await context.bot.send_message(
                chat_id=qr_msg.chat_id, text=exp_text, parse_mode="HTML", reply_markup=exp_markup
            )
        except Exception:
            logger.exception("Expiry watchdog failed for %s", order_id)

    spawn(expiry_watchdog())

async def notify_admin_to_verify(bot, pay, user_id, full_name, note=""):
    """Ask the admin to approve / reject a payment (manual mode, or when automatic checking found nothing)."""
    order_id = pay["order_id"]
    amount = float(pay.get("pay_amount") or pay["amount"])
    lines = [
        "💳 <b>PAYMENT VERIFICATION NEEDED</b>",
        "_________________________",
        "",
        f"👤 User: <a href=\"tg://user?id={user_id}\">{html.escape(full_name or 'User')}</a> (<code>{user_id}</code>)",
        f"💰 Amount: <b>₹{amount:,.2f}</b>",
        f"💳 Method: {html.escape(str(pay['method'] or '').upper())} → <code>{html.escape(str(pay['upi_id'] or ''))}</code>",
        f"🆔 Order: <code>{order_id}</code>",
    ]
    if pay.get("is_deficit") and pay.get("prod_key"):
        prod = get_product_by_key(pay["prod_key"])
        lines.append(f"🛒 For: <b>{html.escape((prod or {}).get('name', pay['prod_key']))} — {html.escape((pay.get('plan') or '').replace('_', ' '))}</b>")
    if note:
        lines += ["", note]
    if pay.get("method") == "binance":
        lines += ["", "Check your <b>Binance</b> app for this USDT payment, then choose:"]
    else:
        lines += ["", "Check your FamPay / bank app for this amount, then choose:"]
    markup = InlineKeyboardMarkup([[
        styled_button("✅ Approve", "admin_approve", callback_data=f"adm_ok_{order_id}"),
        styled_button("❌ Reject", "admin_reject", callback_data=f"adm_no_{order_id}"),
    ]])
    try:
        await bot.send_message(chat_id=ADMIN_ID, text="\n".join(lines), parse_mode="HTML", reply_markup=markup)
    except Exception:
        logger.exception("Could not notify the admin about payment %s", order_id)


async def watch_unconfirmed(bot, order_id, user_id, full_name):
    """After the customer pressed 'I have paid': look every 5 seconds. If the payment is still not
    confirmed after 10 minutes, hand it to the admin so no customer is left waiting."""
    try:
        for _ in range(120):                      # 120 x 5 s = 10 minutes
            await asyncio.sleep(5)
            row = await asyncio.to_thread(db.get_payment, order_id)
            if not row or row["status"] != "awaiting_approval":
                return                            # confirmed (or rejected / cancelled) in the meantime
        row = await asyncio.to_thread(db.get_payment, order_id)
        if row and row["status"] == "awaiting_approval":
            await bot.send_message(
                chat_id=user_id,
                text=("⏳ <b>Payment not received yet</b>\n\n"
                      f"🆔 <code>{order_id}</code>\n"
                      "If you already paid, the admin has been told and will confirm it shortly. "
                      "It is also still detected automatically."),
                parse_mode="HTML", reply_markup=back_to_menu_markup()
            )
            await notify_admin_to_verify(bot, row, user_id, full_name,
                                         "⚠️ The bot could not find this payment in the e-mails for 10 minutes.")
    except Exception:
        logger.exception("Payment watcher failed for %s", order_id)


async def payment_paid_click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """'I have paid' (press once).
    Automatic mode: the bot checks every 5 seconds and confirms by itself, then delivers the key.
    Manual mode: the admin approves in Telegram."""
    query = update.callback_query
    user = update.effective_user
    order_id = query.data[len("paid_"):]

    pay = db.get_payment(order_id)
    if not pay or pay["user_id"] != user.id:
        await query.answer("⚠️ Payment not found.", show_alert=True)
        return

    auto_on = get_payment_config()["auto_verify"] and pay.get("method") != "binance"
    claimed = db.claim_payment(order_id, ("pending", "expired"), "awaiting_approval")
    if not claimed:
        status = pay["status"]
        if status == "awaiting_approval":
            msg = "⏳ Already checking your payment. You will get a message here the moment it arrives."
        elif status == "verified":
            msg = "✅ This payment was already confirmed."
        else:
            msg = "⚠️ This payment session has ended. Tap /start to begin again."
        await query.answer(msg, show_alert=True)
        return

    amount = float(pay.get("pay_amount") or pay["amount"])

    if auto_on:
        await query.answer("Checking your payment…")
        text = (
            "⏳ <b>Checking your payment…</b>\n"
            "_________________________\n\n"
            f"🆔 Order: <code>{order_id}</code>\n"
            f"💰 Amount: <b>₹{amount:,.2f}</b>\n\n"
            "I am checking every 5 seconds. Your key / balance arrives here as soon as the payment is found. "
            "You do not need to press anything again."
        )
        try:
            await query.edit_message_caption(caption=text, parse_mode="HTML", reply_markup=None)
        except Exception:
            try:
                await query.message.delete()
            except Exception:
                pass
            sent = await context.bot.send_message(chat_id=user.id, text=text, parse_mode="HTML")
            try:
                db.set_payment_message(order_id, sent.chat_id, sent.message_id)
            except Exception:
                pass
        _GMAIL_WAKE.set()                          # look at Gmail right now, not in 5 seconds
        spawn(watch_unconfirmed(context.bot, order_id, user.id, user.full_name))
        return

    await query.answer("Submitted for verification ✅")
    try:
        await query.message.delete()
    except Exception:
        pass
    await context.bot.send_message(
        chat_id=user.id,
        text=(
            "⏳ <b>Payment submitted!</b>\n"
            "_________________________\n\n"
            f"🆔 Order: <code>{order_id}</code>\n"
            f"💰 Amount: <b>₹{amount:,.2f}</b>\n\n"
            "The admin is verifying your payment. You will get a message here as soon as it is confirmed."
        ),
        parse_mode="HTML",
        reply_markup=back_to_menu_markup()
    )
    await notify_admin_to_verify(context.bot, pay, user.id, user.full_name)

async def admin_payment_decision(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin taps Approve / Reject on a payment notification."""
    query = update.callback_query
    if update.effective_user.id != ADMIN_ID:
        await query.answer("⛔ Not allowed.", show_alert=True)
        return

    approve = query.data.startswith("adm_ok_")
    order_id = query.data.split("_", 2)[2]
    now_txt = get_ist_time()

    async def finish_admin_message(note):
        try:
            await query.edit_message_text(f"{query.message.text_html}\n\n{note}", parse_mode="HTML")
        except Exception:
            pass

    if not approve:
        row = db.claim_payment(order_id, "awaiting_approval", "rejected", None, now_txt)
        if not row:
            await query.answer("Already processed.", show_alert=True)
            return
        await query.answer("Rejected")
        await finish_admin_message(f"❌ <b>Rejected</b> at {now_txt}")
        try:
            await context.bot.send_message(
                chat_id=row["user_id"],
                text=(
                    "❌ <b>Payment not verified</b>\n\n"
                    f"🆔 Order: <code>{order_id}</code>\n"
                    "We could not confirm this payment. If you already paid, please contact support with your order ID."
                ),
                parse_mode="HTML",
                reply_markup=back_to_menu_markup()
            )
        except Exception:
            logger.exception("Could not notify user about rejected payment")
        return

    row = db.claim_payment(order_id, "awaiting_approval", "verified", "ADMIN_APPROVED", now_txt)
    if not row:
        await query.answer("Already processed.", show_alert=True)
        return

    user_id = row["user_id"]
    amount = float(row.get("pay_amount") or row["amount"])
    db.update_balance(user_id, amount)
    await query.answer("Approved ✅")

    note = f"✅ <b>Approved</b> at {now_txt}"
    notice = f"✅ <b>Payment Verified Successfully!</b>\n₹{amount:,.2f} has been added to your wallet."
    try:
        if row.get("is_deficit") and row.get("prod_key") and row.get("plan"):
            res = await asyncio.to_thread(
                purchase_with_wallet, user_id, row["prod_key"], row["plan"], allow_pending=False, utr="UPI_DEFICIT"
            )
            status = res["status"]
            if status == "ok":
                await context.bot.send_message(
                    chat_id=user_id,
                    text=build_key_card(res["prod"]["name"], row["plan"], res["key"], res["balance"]),
                    parse_mode="HTML",
                    reply_markup=key_card_keyboard(res["prod"])
                )
            elif status in ("out_of_stock", "unavailable"):
                note += "\n⚠️ Out of stock: the amount was added to the customer's wallet."
                await context.bot.send_message(
                    chat_id=user_id,
                    text=build_out_of_stock_notice(res["prod"]["name"], row["plan"], amount),
                    parse_mode="HTML",
                    reply_markup=back_to_menu_markup()
                )
            elif status == "pending":
                note += "\n⚠️ Out of stock: key is in the <b>Pending Keys Queue</b>. Dispatch it from the admin panel."
                await context.bot.send_message(
                    chat_id=user_id,
                    text=build_pending_notice(res["prod"]["name"], row["plan"]),
                    parse_mode="HTML",
                    reply_markup=back_to_menu_markup()
                )
            else:
                await context.bot.send_message(
                    chat_id=user_id,
                    text=notice + "\n\nYour balance is ready — open the shop to complete your purchase.",
                    parse_mode="HTML",
                    reply_markup=back_to_menu_markup()
                )
        else:
            await context.bot.send_message(chat_id=user_id, text=notice, parse_mode="HTML", reply_markup=back_to_menu_markup())
    except Exception:
        logger.exception("Failed to notify user after approval")
        note += "\n⚠️ Could not message the user."
    await finish_admin_message(note)

async def cancel_deposit_click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    ack(query)
    order_id = query.data[len("canceldep_"):]

    pay = db.get_payment(order_id)
    if pay and pay["user_id"] == update.effective_user.id:
        try:
            db.claim_payment(order_id, "pending", "cancelled", None, get_ist_time())
        except Exception as e:
            logger.exception("Failed to update cancelled payment record: %s", e)

    try:
        await query.message.delete()
    except Exception:
        pass
    await start_command(update, context)

# ==========================================
# 🎰 5. LUCKY CASH SPIN (2-SEC ANIMATION + 1-HR EXPIRY)
# ==========================================
def spin_settings():
    """(cooldown_hours, min_prize, max_prize, expiry_hours) from the admin panel."""
    st = get_settings()
    cooldown = max(0.0, _num(st.get("spin_cooldown_hours"), 24))
    lo = max(0.0, _num(st.get("spin_min"), 2))
    hi = max(lo, _num(st.get("spin_max"), 10))
    expiry = max(0.0, _num(st.get("spin_expiry_hours"), 1))
    return cooldown, lo, hi, expiry

def pick_spin_prize(lo, hi):
    if float(lo).is_integer() and float(hi).is_integer():
        return int(random.randint(int(lo), int(hi)))
    return round(random.uniform(lo, hi), 2)

def fmt_wait(seconds):
    hours, rem = divmod(int(seconds), 3600)
    return f"{hours}h {rem // 60}m"

async def lucky_spin_home(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    ack(query)
    cooldown_h, _lo, _hi, expiry_h = spin_settings()
    remaining = db.spin_remaining_seconds(update.effective_user.id, cooldown_h)

    if remaining <= 0:
        status_line = "🕒 Spin Status: ✅ <b>Available Now!</b>\n\n👇 Tap below to spin and try your luck!"
    else:
        status_line = f"🕒 Spin Status: ⏳ Next free spin in <b>{fmt_wait(remaining)}</b>"

    text = (
        "── <b>CASH SPIN</b> ──\n\n"
        "🎁 <b>Daily Free Spin!</b>\n"
        "💰 Win real cash directly into your wallet!\n"
        f"Get 1 Free Spin every {fmt_hours(cooldown_h)}!\n"
        + (f"⏳ Use the cash for a purchase within <b>{fmt_hours(expiry_h)}</b> or it expires!\n" if expiry_h > 0 else "")
        + "_________________________\n\n"
        f"{status_line}"
    )
    active = db.active_spin_reward(update.effective_user.id) if expiry_h > 0 else None
    if active:
        text += (
            f"\n\n🎁 Active spin cash: <b>₹{fmt_amount(active['amount'])}</b> — "
            f"expires in <b>{fmt_wait(float(active['secs']))}</b>. Buy a key now to keep it!"
        )
    keyboard = []
    if remaining <= 0:
        keyboard.append([styled_button("🍯 Spin Now! (FREE)", "spin_now", callback_data="spin_action_play")])
    keyboard.append([styled_button("🔜 Back", "back", callback_data="main_menu")])
    await replace_callback_message(query, context, text, InlineKeyboardMarkup(keyboard))

async def perform_lucky_spin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = update.effective_user

    # The cooldown is enforced in the database, so a spin can never be repeated early.
    cooldown_h, spin_lo, spin_hi, expiry_h = spin_settings()
    if not db.claim_spin(user.id, cooldown_h):
        await query.answer("⏳ Your next free spin is not available yet. Try again later!", show_alert=True)
        return
    ack(query)

    frames = [
        "🎰 Spinning... [ 🍒 | 🍋 | 💎 ]",
        "🎰 Spinning... [ 🔔 | 7️⃣ | 💰 ]",
        "🎰 Stopping... [ 💎 | 💎 | 💎 ]"
    ]
    for f in frames:
        try:
            await query.message.edit_text(f"── <b>CASH SPIN</b> ──\n\n{f}", parse_mode="HTML")
        except BadRequest:
            pass
        await asyncio.sleep(0.6)

    won_amount = pick_spin_prize(spin_lo, spin_hi)
    db.update_balance(user.id, won_amount)
    if expiry_h > 0 and won_amount > 0:
        db.add_spin_reward(user.id, won_amount, expiry_h)
    u_data = db_get_user(user.id)
    new_bal = u_data[4] if u_data else won_amount

    result_text = (
        "🎁 ── <b>CASH SPIN RESULT!</b> ── 🎁\n\n"
        "👑 [ 💎 | 💎 | 💎 ]\n"
        "_________________________\n\n"
        "🎉 <b>Congratulations!</b>\n\n"
        f"💰 You won <b>₹{fmt_amount(won_amount)} Cash Reward!</b>\n"
        "It has been added to your wallet.\n"
        + (f"⏳ <b>Use it for a purchase within {fmt_hours(expiry_h)}</b> — otherwise this cash expires!\n" if expiry_h > 0 else "")
        + f"\n💳 New balance: <b>₹{new_bal:,.2f}</b>\n"
        "_________________________\n\n"
        f"🕒 Next free spin available in {fmt_hours(cooldown_h)}!"
    )
    keyboard = [
        [styled_button(f"🛒 Shop Now — Use ₹{fmt_amount(won_amount)}", "spin_shop", callback_data="shop_key")],
        [styled_button("🔜 Back", "back", callback_data="main_menu")]
    ]
    await query.message.edit_text(result_text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))

# ==========================================
# 👥 6. REFERRAL SYSTEM
# ==========================================
async def referral_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    ack(query)
    user = update.effective_user
    bot_me = await context.bot.get_me()

    u_data = db_get_user(user.id)
    ref_cnt = u_data[6] if u_data else 0
    ref_earn = u_data[7] if u_data else 0.0

    ref_link = f"https://t.me/{bot_me.username}?start=ref_{user.id}"

    text = (
        "🤝 <b>REFERRAL SYSTEM</b>\n"
        "_________________________\n\n"
        f"🔗 Your Referral Link:\n"
        f"<code>{ref_link}</code>\n\n"
        f"🤝 Total Referrals: <b>{ref_cnt}</b>\n"
        f"💰 Total Earnings: <b>₹{ref_earn:,.2f}</b>\n"
        "_________________________\n\n"
        "💰 Invite your friends → Get a guaranteed bonus on their first purchase!"
    )
    keyboard = [[styled_button("🔜 Back", "back", callback_data="main_menu")]]
    await replace_callback_message(query, context, text, InlineKeyboardMarkup(keyboard))

# ==========================================
# 🧾 7. ALL HISTORY (LAST 20 ORDERS)
# ==========================================
async def history_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    ack(query)
    user = update.effective_user
    orders = db_get_user_history(user.id, limit=20)

    if not orders:
        text = (
            "🛒 ── <b>MY ORDERS</b> ── 🛒\n"
            "_________________________\n\n"
            "🚫 <b>No orders yet!</b>\n\n"
            "🛒 Start shopping to see your orders here!\n"
            "✈️ All your purchased keys will be saved here safely!\n"
            "_________________________"
        )
    else:
        text = "🔑 <b>MY ORDERS (last 20)</b>\n__________________________________\n\n"
        for idx, o in enumerate(orders, 1):
            plan_txt = html.escape(str(o[1] or "").replace("_", " "))
            status_txt = "⏳ Pending delivery" if str(o[2] or "").startswith("PENDING") else "Completed"
            text += (
                f"{idx}. 🛒 <b>{html.escape(str(o[0]))} - {plan_txt}</b>\n"
                f"   💰 ₹{o[4]:,.2f} • {status_txt}\n"
                f"   🔐 <code>{html.escape(str(o[2]))}</code>\n"
                f"   📅 {o[3]} (IST)\n\n"
            )
        text += "__________________________________"

    keyboard = [[styled_button("🔜 Back", "back", callback_data="main_menu")]]
    await replace_callback_message(query, context, text, InlineKeyboardMarkup(keyboard))

# ==========================================
# 👑 TOP 5 SELLERS MONTHLY (auto-calculated from real purchases, resets every month)
# ==========================================
async def top_sellers_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    ack(query)
    user = update.effective_user
    now = datetime.now(IST)
    month_label = now.strftime("%B %Y")
    # days left until the list resets on the 1st
    next_month = (now.replace(day=1) + timedelta(days=32)).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    days_left = max(1, (next_month - now).days)

    try:
        rows = db.get_top_sellers_monthly(5)
        mine = db.get_user_monthly_rank(user.id)
    except Exception:
        logger.exception("Top sellers query failed")
        rows, mine = [], None

    medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣"]
    text = (
        "👑 <b>TOP 5 SELLERS MONTHLY</b>\n"
        f"📅 <i>{month_label} — list resets automatically on the 1st of every month</i>\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
    )
    if not rows:
        text += (
            "🚫 <b>No purchases yet this month!</b>\n\n"
            "🛒 Buy a key now and be the first on the leaderboard! 🔥\n"
        )
    else:
        for i, r in enumerate(rows):
            role = "RESELLER" if str(r["account_type"]).lower() == "reseller" else "USER"
            name = html.escape((r["full_name"] or "Unknown").strip() or "Unknown")
            text += (
                f"{medals[i]} <b>{name}</b>\n"
                f"👑 Role: {role}\n"
                f"🔑 Keys: {int(r['keys_count'])}\n"
                f"💰 Total Buy: ₹{float(r['total']):,.2f}\n\n"
            )
    text += "━━━━━━━━━━━━━━━━━━━━\n"
    if mine:
        text += f"📍 <b>Your rank:</b> #{int(mine['rnk'])} • 🔑 {int(mine['keys_count'])} keys • ₹{float(mine['total']):,.2f}\n"
    else:
        text += "📍 <b>Your rank:</b> not ranked yet — buy a key to join the list!\n"
    text += f"⏳ Resets in <b>{days_left}</b> day{'s' if days_left != 1 else ''}"

    keyboard = [[styled_button("➡️ Back to Menu", "back_menu", callback_data="main_menu")]]
    await replace_callback_message(query, context, text, InlineKeyboardMarkup(keyboard))

# ==========================================
# 📡 /ping — live speed test + "Restart" (refreshes the customer's session)
# ==========================================
def _ms(t0):
    return int((time.perf_counter() - t0) * 1000)

def _grade(ms, good, ok):
    return "🟢 Fast" if ms < good else ("🟡 OK" if ms < ok else "🔴 Slow")

def ping_keyboard():
    return InlineKeyboardMarkup([
        [styled_button("🔁 Test Again", "ping_again", callback_data="ping_again")],
        [styled_button("🔄 Restart", "ping_restart", callback_data="ping_restart")],
        [styled_button("➡️ Back to Menu", "back_menu", callback_data="main_menu")],
    ])

async def _ping_finish(context, chat_id, message_id, first_ms):
    """Measure the bot -> Telegram and bot -> database speed, then show the result in the same message."""
    samples = [first_ms]
    try:
        for _ in range(2):
            t = time.perf_counter()
            await context.bot.get_me()
            samples.append(_ms(t))
    except Exception:
        pass
    tg_ms = int(sum(samples) / len(samples))

    db_times = []
    for _ in range(2):
        t = time.perf_counter()
        try:
            await asyncio.to_thread(db.fetchone, "SELECT 1")
            db_times.append(_ms(t))
        except Exception:
            db_times.append(9999)
    db_ms = min(db_times)

    total = tg_ms + db_ms
    verdict = (
        "✅ Everything is running fast." if total < 900
        else "⚠️ A little slow right now. Tap <b>🔄 Restart</b> to refresh your connection."
    )
    text = (
        "📡 <b>PING RESULT</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"🤖 Bot ↔ Telegram: <b>{tg_ms} ms</b>  {_grade(tg_ms, 500, 1000)}\n"
        f"🗄️ Database: <b>{'n/a' if db_ms >= 9999 else str(db_ms) + ' ms'}</b>  {_grade(db_ms, 150, 500)}\n"
        f"⚡ Total: <b>{total} ms</b>\n"
        f"🔌 DB connection pool: <b>{'ON' if getattr(db, '_pool', None) is not None else 'OFF'}</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"{verdict}\n"
        "<i>Your own phone network adds a little on top of this.</i>"
    )
    try:
        await context.bot.edit_message_text(
            chat_id=chat_id, message_id=message_id, text=text,
            parse_mode="HTML", reply_markup=ping_keyboard())
    except Exception:
        logger.exception("Could not show the ping result")

async def ping_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    t0 = time.perf_counter()
    msg = await context.bot.send_message(
        chat_id=chat_id, parse_mode="HTML",
        text="📡 <b>Ping testing...</b>\n⏳ Measuring your speed live, please wait.")
    await _ping_finish(context, chat_id, msg.message_id, _ms(t0))

async def ping_again(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    ack(query)
    t0 = time.perf_counter()
    try:
        await query.edit_message_text(
            "📡 <b>Ping testing...</b>\n⏳ Measuring your speed live, please wait.", parse_mode="HTML")
    except Exception:
        pass
    await _ping_finish(context, query.message.chat_id, query.message.message_id, _ms(t0))

async def ping_restart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Refreshes THIS customer's session (forgets half-finished steps, clears caches, wakes the database
    connection) and opens a fresh main menu. It does not restart the server for everyone."""
    query = update.callback_query
    ack(query)
    context.user_data.clear()
    _settings_cache["v"] = None
    _style_cache["t"] = 0.0
    try:
        await asyncio.to_thread(db.fetchone, "SELECT 1")
    except Exception:
        pass
    await start_command(update, context)

# ==========================================
# 🛒 8. SHOP KEY & DEVICE CATALOG
# ==========================================
async def shop_categories(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    ack(query)

    text = "🛒 <b>PRODUCT STORE — SHOP</b> 🛒\n\n🎮 Select your game:"
    keyboard = [
        [styled_button("🔥 FREE FIRE", "game_ff", callback_data="shop_ff")],
        [styled_button("🎱 CARROM POOL", "game_carrom", callback_data="pcat_carrom")],
        [styled_button("🎱 8BALL POOL", "game_pool8", callback_data="pcat_pool8")],
        [styled_button("👆 Back", "back", callback_data="main_menu")]
    ]
    await replace_callback_message(query, context, text, InlineKeyboardMarkup(keyboard))

async def shop_ff_categories(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """FREE FIRE -> the device-type list (Non-Root, Root, iOS, PC, 8 Level ID)."""
    query = update.callback_query
    ack(query)

    text = "🔥 <b>FREE FIRE — SHOP</b> 🔥\n\n📱 Select your device type:"
    keyboard = [
        [styled_button("☎️ Non-Root Mobile", "cat_non_root", callback_data="pcat_non_root")],
        [styled_button("🔓 Root Mobile", "cat_root", callback_data="pcat_root")],
        [styled_button("🍏 iOS Panels", "cat_ios", callback_data="pcat_ios")],
        [styled_button("💻 PC Panels", "cat_pc", callback_data="pcat_pc")],
        [styled_button("🆔 8 Level ID", "cat_likes", callback_data="pcat_likes")],
        [styled_button("👆 Back", "back", callback_data="shop_key")]
    ]
    await replace_callback_message(query, context, text, InlineKeyboardMarkup(keyboard))

async def list_category_products(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    ack(query)
    cat_key = query.data.replace("pcat_", "")
    context.user_data['selected_cat'] = cat_key

    prods = get_products_by_category(cat_key)
    back_cb = "shop_key" if cat_key in ("carrom", "pool8") else "shop_ff"

    if not prods:
        text = "🛒 <b>PRODUCT STORE — SHOP</b> 🛒\n\n⚠️ No products available in this category yet. Admin will restock soon!"
        keyboard = [[styled_button("👆 Back", "back", callback_data=back_cb)]]
        await replace_callback_message(query, context, text, InlineKeyboardMarkup(keyboard))
        return

    text = "🛒 <b>PRODUCT STORE — SHOP</b> 🛒\n\n🔥 Choose a product:"
    keyboard = []
    for k, p in prods.items():
        if p.get("maintenance"):
            # Under maintenance: always a RED button; tapping it shows the maintenance notice.
            keyboard.append([InlineKeyboardButton(f"{p['icon']} {p['name']}", callback_data=f"selprod_{k}",
                                                  api_kwargs={"style": "danger"})])
        else:
            keyboard.append([styled_button(f"{p['icon']} {p['name']}", "product_item", callback_data=f"selprod_{k}")])
    keyboard.append([styled_button("👆 Back", "back", callback_data=back_cb)])
    await replace_callback_message(query, context, text, InlineKeyboardMarkup(keyboard))

def build_plans_view(user_id, prod_key):
    """Return (text, keyboard) for a product's plan list, or None if the product is missing."""
    prod = get_product_by_key(prod_key)
    if not prod:
        return None

    u_data = db_get_user(user_id)
    is_reseller = bool(u_data and u_data[8] == "Reseller")
    role_badge = "👑 Reseller Price" if is_reseller else "👤 Regular Price"

    cat = prod.get("category") or ""
    if cat in ("carrom", "pool8"):
        game = "CARROM POOL" if cat == "carrom" else "8BALL POOL"
        text = (
            f"🖼️ <b>{html.escape(prod['name'] or '')}</b>\n"
            f"Available | 🎱 {game}\n"
            f"_________________________\n\n"
            f"Current Tier: <b>{role_badge}</b>\n\n"
            f"Select Duration:"
        )
    else:
        text = (
            f"🖼️ <b>{html.escape(prod['name'] or '')}</b>\n"
            f"Available | 🔴 {html.escape(cat.upper())} Only\n"
            f"_________________________\n\n"
            f"🔘 Silent Aim &amp; Headshot\n"
            f"🔘 AimFov 360° Location\n"
            f"🔘 CS / BR Rank Working Safe\n"
            f"🔘 100% Anti-Ban Protection\n\n"
            f"Current Tier: <b>{role_badge}</b>\n\n"
            f"Select Duration:"
        )

    keyboard = []
    for pl_name, reg_price in prod.get("prices", []):
        final_price = unit_price(reg_price, is_reseller, prod_key, pl_name)
        keyboard.append([styled_button(
            f"🕒 {pl_name.replace('_', ' ')} | {fmt_price(final_price)}", "plan_item",
            callback_data=f"buyplan_{prod_key}_{pl_name}"
        )])

    if valid_url(prod.get("download_link")):
        keyboard.append([styled_button("🎥 Preview Video", "preview_video", url=prod["download_link"])])

    keyboard.append([styled_button("🔜 Back", "back", callback_data="pcat_" + (prod.get("category") or "non_root"))])
    return text, InlineKeyboardMarkup(keyboard)

async def show_product_plans(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    prod_key = query.data.replace("selprod_", "", 1)

    _mp = get_product_by_key(prod_key)
    if _mp and _mp.get("maintenance"):
        # Maintenance: send the notice as a NEW message (product name + Check Update + Back).
        ack(query)
        kb = []
        if valid_url(_mp.get("download_link")):
            kb.append([styled_button("Check Update", "check_update", url=_mp["download_link"])])
        kb.append([styled_button("Back", "back", callback_data="pcat_" + (_mp.get("category") or "non_root"))])
        try:
            await context.bot.send_message(
                chat_id=query.message.chat_id, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(kb),
                text=(f"<blockquote><b>{html.escape(_mp['icon'] or '')} {html.escape(_mp['name'] or '')}</b>\n\n"
                      "This product is currently under maintenance. Please check back later "
                      "— sorry for the inconvenience!</blockquote>"))
        except Exception:
            logger.exception("Could not send maintenance notice")
        return

    view = build_plans_view(update.effective_user.id, prod_key)
    if not view:
        await query.answer("⚠️ Product not available.", show_alert=True)
        return
    ack(query)
    await replace_callback_message(query, context, view[0], view[1])

def parse_buyplan(data):
    """Split 'buyplan_<product_key>_<plan>' where both parts may contain underscores."""
    parts = data[len("buyplan_"):].split("_")
    for i in range(1, len(parts)):
        cand_key = "_".join(parts[:i])
        cand_plan = "_".join(parts[i:])
        prod = get_product_by_key(cand_key)
        if prod and any(name == cand_plan for name, _ in prod["prices"]):
            return cand_key, cand_plan
    return None, None

def _order_price(user_id, prod_key, plan_name):
    prod = _product(prod_key)
    if not prod or prod.get("maintenance") or prod.get("stock_out"):
        return None, prod
    user = db.get_user(user_id)
    is_reseller = bool(user and user["account_type"] == "Reseller")
    reg = next((p for n, p in prod["prices"] if n == plan_name), None)
    if reg is None:
        return None, prod
    return unit_price(reg, is_reseller, prod_key, plan_name), prod

def _order_markup(prod_key):
    kb = [[styled_button("💳 UPI Payment (QR Code)", "pay_upi", callback_data="ordpay_upi")]]
    if db.get_binance_settings()["enabled"]:
        kb.append([styled_button("🟡 Binance USDT Payment", "pay_binance", callback_data="ordpay_bin")])
    kb.append([styled_button("🎟️ Apply Coupon Code", "apply_coupon", callback_data="ordcoupon")])
    kb.append([styled_button("❌ Back", "back", callback_data=f"selprod_{prod_key}")])
    return InlineKeyboardMarkup(kb)

def _order_text(prod, od, price, note="", balance=None):
    discount = float(od.get("discount") or 0)
    final = round(price - discount, 2)
    lines = [
        "📋 <b>ORDER SUMMARY</b>", "__________________________________", "",
        f"{html.escape(prod.get('icon') or '⚡')} Product: <b>{html.escape(prod['name'])}</b>",
        f"⏱️ Plan: {html.escape(od['plan'].replace('_', ' '))}",
        f"💵 Price: ₹{price:,.2f}",
    ]
    if od.get("coupon"):
        lines.append(f"🎟️ Coupon <code>{html.escape(od['coupon'])}</code>: -₹{discount:,.2f}")
    lines += ["__________________________________", f"💰 <b>Final Total: ₹{final:,.2f}</b>"]
    if balance is not None and balance < final:
        lines += [f"💳 Wallet Balance: ₹{balance:,.2f}", f"⚠️ <b>To pay now: ₹{final - balance:,.2f}</b>"]
    if note:
        lines += ["", note]
    return "\n".join(lines)

async def show_order_summary(query, context, user_id, note=""):
    """ORDER SUMMARY + payment options. Nothing is charged or delivered here."""
    od = context.user_data.get("order")
    if not od:
        await replace_callback_message(query, context, "⚠️ Session expired. Please select the product again.", back_to_menu_markup())
        return
    price, prod = _order_price(user_id, od["prod_key"], od["plan"])
    if price is None:
        await replace_callback_message(query, context, "⚠️ This product is currently unavailable.", back_to_menu_markup())
        return
    od["discount"] = min(float(od.get("discount") or 0), price)
    u_data = db_get_user(user_id)
    balance = float(u_data[4]) if u_data else 0.0
    await replace_callback_message(query, context, _order_text(prod, od, price, note, balance), _order_markup(od["prod_key"]))

async def process_plan_purchase(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Plan tapped -> order summary only. The key is delivered after a payment is confirmed."""
    query = update.callback_query
    ack(query)
    prod_key, plan_name = parse_buyplan(query.data)
    if not prod_key:
        await replace_callback_message(query, context, "⚠️ This product or plan is no longer available.", back_to_menu_markup())
        return
    context.user_data.pop("awaiting_coupon", None)
    context.user_data["order"] = {"prod_key": prod_key, "plan": plan_name, "coupon": None, "discount": 0.0}
    user_id = update.effective_user.id
    price, _prod = _order_price(user_id, prod_key, plan_name)
    u_data = db_get_user(user_id)
    balance = float(u_data[4]) if u_data else 0.0
    if price is not None and balance >= price:
        # Enough money in the wallet -> deliver the key directly, no summary / QR.
        if await _deliver_from_wallet(query, context, user_id, prod_key, plan_name):
            return
    await show_order_summary(query, context, user_id)

async def _deliver_from_wallet(query, context, user_id, prod_key, plan_name, discount=0.0, coupon=None):
    """Charge the wallet and show the key card. Returns True if the customer was served (or told why not)."""
    result = await asyncio.to_thread(purchase_with_wallet, user_id, prod_key, plan_name, False, "WALLET_PAY", discount)
    status = result["status"]
    if status == "ok":
        if coupon:
            try:
                db.use_coupon(coupon)
            except Exception:
                logger.exception("Could not count coupon use")
        context.user_data.pop("order", None)
        await replace_callback_message(
            query, context,
            build_key_card(result["prod"]["name"], plan_name, result["key"], result["balance"]),
            key_card_keyboard(result["prod"]))
        return True
    if status == "out_of_stock":
        kb = [[styled_button("🔜 Back to Plans", "back", callback_data=f"selprod_{prod_key}")]]
        await replace_callback_message(
            query, context, "⚠️ <b>Out of Stock!</b> Keys will be restocked shortly. You were not charged.",
            InlineKeyboardMarkup(kb))
        return True
    return False  # insufficient / unavailable -> caller shows the summary or an error

async def order_coupon_click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    ack(query)
    if not context.user_data.get("order"):
        await replace_callback_message(query, context, "⚠️ Session expired. Please select the product again.", back_to_menu_markup())
        return
    context.user_data["awaiting_coupon"] = True
    await replace_callback_message(
        query, context, "🎟️ <b>Send your coupon code</b> as a message now.",
        InlineKeyboardMarkup([[styled_button("❌ Back", "back", callback_data="ordback")]]))

async def order_back_click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    ack(query)
    context.user_data.pop("awaiting_coupon", None)
    await show_order_summary(query, context, update.effective_user.id)

async def coupon_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("awaiting_coupon") or not update.message or not update.message.text:
        return
    od = context.user_data.get("order")
    context.user_data.pop("awaiting_coupon", None)
    if not od:
        return
    price, prod = _order_price(update.effective_user.id, od["prod_key"], od["plan"])
    if price is None:
        await update.message.reply_text("⚠️ This product is currently unavailable.", reply_markup=back_to_menu_markup())
        return
    cp = await asyncio.to_thread(db.get_coupon, update.message.text.strip())
    if not cp:
        od["coupon"], od["discount"] = None, 0.0
        note = "❌ Invalid or expired coupon code."
    else:
        val = float(cp["discount_val"] or 0)
        disc = price * val / 100.0 if cp.get("discount_type") == "percent" else val
        od["coupon"], od["discount"] = cp["code"], round(min(disc, price), 2)
        note = "✅ Coupon applied!"
    _u = db_get_user(update.effective_user.id)
    await update.message.reply_text(_order_text(prod, od, price, note, float(_u[4]) if _u else 0.0), parse_mode="HTML",
                                    reply_markup=_order_markup(od["prod_key"]))

async def order_pay_upi_click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    ack(query)
    od = context.user_data.get("order")
    if not od:
        await replace_callback_message(query, context, "⚠️ Session expired. Please select the product again.", back_to_menu_markup())
        return
    price, prod = _order_price(update.effective_user.id, od["prod_key"], od["plan"])
    if price is None:
        await replace_callback_message(query, context, "⚠️ This product is currently unavailable.", back_to_menu_markup())
        return
    discount = min(float(od.get("discount") or 0), price)
    final = round(price - discount, 2)
    u_data = db_get_user(update.effective_user.id)
    balance = float(u_data[4]) if u_data else 0.0
    if balance >= final:
        # A coupon made the wallet enough -> no QR needed.
        if await _deliver_from_wallet(query, context, update.effective_user.id, od["prod_key"], od["plan"],
                                      discount, od.get("coupon")):
            return
    deficit = round(final - balance, 2) if balance < final else final
    context.user_data["deposit_method"] = "upi"
    await generate_deposit_qr(
        update, context, deficit, is_deficit=True, prod_name=prod["name"],
        plan_name=od["plan"], prod_key=od["prod_key"], full_price=False,
        coupon_code=od.get("coupon"), discount=discount
    )

async def order_pay_binance_click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    od = context.user_data.get("order")
    bn = db.get_binance_settings()
    if not od or not bn["enabled"] or not bn["pay_id"]:
        await query.answer("⚠️ Binance payment is not available right now.", show_alert=True)
        return
    ack(query)
    user = update.effective_user
    price, prod = _order_price(user.id, od["prod_key"], od["plan"])
    if price is None:
        await replace_callback_message(query, context, "⚠️ This product is currently unavailable.", back_to_menu_markup())
        return
    discount = min(float(od.get("discount") or 0), price)
    final = round(price - discount, 2)
    _bu = db_get_user(user.id)
    _bal = float(_bu[4]) if _bu else 0.0
    if _bal >= final:
        if await _deliver_from_wallet(query, context, user.id, od["prod_key"], od["plan"], discount, od.get("coupon")):
            return
    final = round(final - _bal, 2) if _bal < final else final   # amount still to pay
    usdt = round(final / max(bn["rate"], 0.01), 2)
    order_id = f"BIN{datetime.now(IST).strftime('%Y%m%d%H%M%S')}{os.urandom(4).hex().upper()}"
    await asyncio.to_thread(
        db.create_payment, user.id, order_id, final, "binance", bn["pay_id"], get_ist_time(),
        od["prod_key"], od["plan"], True, False)
    if od.get("coupon"):
        await asyncio.to_thread(db.set_payment_coupon, order_id, od["coupon"], discount)
    text = (
        "🟡 <b>BINANCE USDT PAYMENT</b>\n__________________________________\n\n"
        f"🛒 {html.escape(prod['name'])} — {html.escape(od['plan'].replace('_', ' '))}\n"
        f"💰 To pay: ₹{final:,.2f}\n"
        f"💵 Send exactly: <b>{usdt:,.2f} USDT</b>\n\n"
        f"📨 Binance Pay ID: <code>{html.escape(bn['pay_id'])}</code>\n"
        + (f"\n{html.escape(bn['note'])}\n" if bn["note"] else "") +
        "\nAfter paying, tap <b>✅ I have paid</b>. Your key arrives as soon as the admin confirms.\n"
        f"🆔 <code>{order_id}</code>"
    )
    kb = [[styled_button("✅ I have paid", "paid", callback_data=f"paid_{order_id}")],
          [styled_button("❌ Cancel Payment", "cancel_payment", callback_data=f"canceldep_{order_id}")]]
    await replace_callback_message(query, context, text, InlineKeyboardMarkup(kb))

async def def_pay_click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    ack(query)
    context.user_data['deposit_method'] = "upi"

    def_data = context.user_data.get('pending_deficit')
    if not def_data:
        keyboard = [[styled_button("🛒 Open Shop", "open_shop", callback_data="shop_key")]]
        await replace_callback_message(
            query, context, "⚠️ Session expired. Please select the product again.", InlineKeyboardMarkup(keyboard)
        )
        return

    await generate_deposit_qr(
        update, context, def_data['deficit'], is_deficit=True,
        prod_name=def_data['prod_name'], plan_name=def_data['plan'], prod_key=def_data['prod_key']
    )

# ==========================================
# 🤖 BOT SETUP & CRASH-PROOF RUNNER
# ==========================================
async def spin_expiry_loop(app):
    """Every 30 s: close spin rewards whose time ran out, take the unused cash back and tell the user."""
    while True:
        try:
            expired = await asyncio.to_thread(db.claim_expired_spin_rewards)
            for r in expired:
                taken, new_bal = await asyncio.to_thread(db.take_back_spin_cash, r["user_id"], float(r["amount"]))
                if taken <= 0:
                    continue
                text = (
                    "⏰ <b>SPIN CASH EXPIRED</b>\n"
                    "_________________________\n\n"
                    f"Your free spin reward of <b>₹{fmt_amount(taken)}</b> was not used for a purchase in time, "
                    "so it has been removed from your wallet.\n\n"
                    f"💳 Wallet balance: <b>₹{new_bal:,.2f}</b>\n\n"
                    "🎰 Spin again when your next free spin is ready — and buy a key before the timer ends!"
                )
                try:
                    await app.bot.send_message(chat_id=r["user_id"], text=text, parse_mode="HTML",
                                               reply_markup=back_to_menu_markup())
                except Exception:
                    logger.warning("Could not notify user %s about expired spin cash", r["user_id"])
        except Exception:
            logger.exception("Spin expiry loop error")
        await asyncio.sleep(30)

async def post_init(app):
    # The "Menu" list customers see when they type "/" in the chat
    try:
        await app.bot.set_my_commands([
            BotCommand("start", "🚀 Start the bot"),
            BotCommand("language", "🌐 Change language"),
            BotCommand("ping", "📡 Check speed / fix slowness"),
        ])
        await app.bot.set_chat_menu_button(menu_button=MenuButtonCommands())
    except Exception:
        logger.exception("Could not set the bot command menu")
    try:        # text shown before /start ("What can this bot do?"): set in the admin panel
        desc, short = ui_get("bot_description", "").strip(), ui_get("bot_short_description", "").strip()
        if desc:
            await app.bot.set_my_description(description=desc[:512])
        if short:
            await app.bot.set_my_short_description(short_description=short[:120])
    except Exception:
        logger.warning("Could not sync the bot description", exc_info=True)
    spawn(spin_expiry_loop(app))

# ==========================================
# 🔌 SUPPLIER API: admin commands (only ADMIN_ID can use them)
#   /apibalance                      -> supplier wallet balance
#   /apiservices                     -> supplier products with their PIDs
#   /apimaps                         -> our products/plans and which PID each is linked to
#   /apimap <prod_key> <plan> <pid>  -> link one of our plans to a supplier PID
#   /apiunmap <prod_key> <plan>      -> remove the link
# ==========================================
def _is_admin(update):
    return bool(update.effective_user and update.effective_user.id == ADMIN_ID)

async def _reply_chunks(update, lines, header=""):
    chunk = header
    for line in lines:
        if len(chunk) + len(line) + 1 > 3500:
            await update.message.reply_text(chunk, parse_mode="HTML")
            chunk = ""
        chunk += line + "\n"
    if chunk.strip():
        await update.message.reply_text(chunk, parse_mode="HTML")

async def api_balance_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        return
    if not supplier_api.configured():
        await update.message.reply_text("⚠️ SUPPLIER_API_KEY is not set in Render → Environment.")
        return
    try:
        bal, cur = await asyncio.to_thread(supplier_api.get_balance)
        await update.message.reply_text(f"💰 Supplier wallet: ₹{bal:,.2f} {cur}")
    except supplier_api.SupplierError as e:
        await update.message.reply_text(f"❌ {html.escape(str(e))}")

async def api_services_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        return
    try:
        items = await asyncio.to_thread(supplier_api.get_services)
    except supplier_api.SupplierError as e:
        await update.message.reply_text(f"❌ {html.escape(str(e))}")
        return
    if not items:
        await update.message.reply_text("No products returned by the supplier.")
        return
    lines = []
    for it in items:
        extra = " · ".join(x for x in (
            f"₹{it['price']}" if it["price"] != "" else "",
            f"stock {it['stock']}" if it["stock"] != "" else "",
            str(it["device"]) if it.get("device") else "",
        ) if x)
        lines.append(f"PID <code>{html.escape(str(it['pid']))}</code> — {html.escape(str(it['name']))}" + (f" ({html.escape(extra)})" if extra else ""))
    await _reply_chunks(update, lines, f"🔌 <b>Supplier products ({len(items)})</b>\n\n")

async def api_maps_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        return
    mapped = {(m["prod_key"], m["plan"]): m["pid"] for m in db.list_supplier_map()}
    lines = []
    for row in db.fetchall("SELECT prod_key, name, prices FROM products ORDER BY category, prod_key"):
        lines.append(f"\n<b>{html.escape(row['name'] or row['prod_key'])}</b>")
        for plan, _price in parse_prices(row["prices"]):
            pid = mapped.get((row["prod_key"], plan))
            lines.append(f"• <code>{html.escape(row['prod_key'])} {html.escape(plan)}</code> → " + (f"PID {pid} ✅" if pid else "—"))
    if not lines:
        await update.message.reply_text("No products yet.")
        return
    lines.append("\nLink: <code>/apimap prod_key plan pid</code>")
    await _reply_chunks(update, lines, "🔗 <b>Supplier links</b>\n")

async def api_map_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        return
    args = context.args
    if len(args) != 3 or not args[2].isdigit():
        await update.message.reply_text("Use: /apimap <prod_key> <plan> <pid>\nExample: /apimap drip_apk 1_Day 109\nSee /apimaps for the exact names.")
        return
    prod_key, plan, pid = args[0], args[1], int(args[2])
    prod = db.get_product_by_key(prod_key)
    if not prod or not any(n == plan for n, _ in prod["prices"]):
        await update.message.reply_text("⚠️ Product or plan name not found. Copy it exactly from /apimaps.")
        return
    db.set_supplier_pid(prod_key, plan, pid)
    await update.message.reply_text(f"✅ {prod_key} / {plan} → supplier PID {pid}\nWhen local stock is empty, keys are now bought live from the supplier.")

async def api_unmap_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        return
    if len(context.args) != 2:
        await update.message.reply_text("Use: /apiunmap <prod_key> <plan>")
        return
    db.delete_supplier_pid(context.args[0], context.args[1])
    await update.message.reply_text("✅ Link removed.")

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Unhandled error while processing an update", exc_info=context.error)

_HOOK_STATE = {"ok": None, "last_warn": 0}

def _hooks_missing():
    missing = []
    try:
        rows = db.fetchall("SELECT * FROM products LIMIT 1") if hasattr(db, "fetchall") else []
        if rows and "reseller_prices" not in rows[0]:
            missing.append("database.py: products.reseller_prices column (reseller price per plan)")
    except Exception:
        pass
    return missing

def _hook_watcher():
    """Re-checks every 30 s. While a hook is missing it keeps warning in the logs (every 10 min);
    when database.py / web_admin.py are updated it switches on by itself - no change needed here."""
    while True:
        try:
            missing = _hooks_missing()
            ok = not missing
            if ok and _HOOK_STATE["ok"] is False:
                logger.info("✅ Web admin hooks are now ACTIVE - reseller prices come from the admin panel.")
            if not ok and time.time() - _HOOK_STATE["last_warn"] > 600:
                logger.warning("⚠️ main.py is waiting for: %s. Add it to database.py / web_admin.py "
                               "(see the 'WEB ADMIN / DATABASE HOOKS' block in main.py).", "; ".join(missing))
                _HOOK_STATE["last_warn"] = time.time()
            _HOOK_STATE["ok"] = ok
        except Exception:
            logger.exception("hook watcher error")
        time.sleep(30)

def start_bot():
    Thread(target=_hook_watcher, daemon=True).start()
    app = Application.builder().token(BOT_TOKEN).concurrent_updates(False).post_init(post_init).build()

    app.add_handler(TypeHandler(Update, track_user), group=-1)
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("language", language_menu))
    app.add_handler(CallbackQueryHandler(language_menu, pattern="^language_menu$"))
    app.add_handler(CallbackQueryHandler(set_language, pattern="^setlang_"))
    app.add_handler(CallbackQueryHandler(start_command, pattern="^main_menu$"))
    app.add_handler(CallbackQueryHandler(profile_handler, pattern="^my_profile$"))
    app.add_handler(CallbackQueryHandler(reseller_handler, pattern="^reseller_plan$"))
    app.add_handler(CallbackQueryHandler(buy_reseller_action, pattern="^buy_reseller_action$"))
    app.add_handler(CallbackQueryHandler(support_handler, pattern="^support_contact$"))
    app.add_handler(CallbackQueryHandler(add_balance_menu, pattern="^add_balance$"))
    app.add_handler(CallbackQueryHandler(select_deposit_amount, pattern="^dep_method_"))
    app.add_handler(CallbackQueryHandler(numpad_open, pattern="^numpad_open$"))
    app.add_handler(CallbackQueryHandler(deposit_pay_upi_click, pattern="^depay_upi$"))
    app.add_handler(CallbackQueryHandler(deposit_pay_binance_click, pattern="^depay_bin$"))
    app.add_handler(CallbackQueryHandler(handle_numpad_input, pattern="^np_"))
    app.add_handler(CallbackQueryHandler(preset_dep_click, pattern="^preset_dep_"))
    app.add_handler(CallbackQueryHandler(payment_paid_click, pattern="^paid_"))
    app.add_handler(CallbackQueryHandler(cancel_deposit_click, pattern="^canceldep_"))
    app.add_handler(CallbackQueryHandler(admin_payment_decision, pattern="^adm_(ok|no)_"))
    app.add_handler(CallbackQueryHandler(lucky_spin_home, pattern="^lucky_spin$"))
    app.add_handler(CallbackQueryHandler(perform_lucky_spin, pattern="^spin_action_play$"))
    app.add_handler(CallbackQueryHandler(referral_handler, pattern="^referral_menu$"))
    app.add_handler(CallbackQueryHandler(history_handler, pattern="^all_history$"))
    app.add_handler(CallbackQueryHandler(top_sellers_handler, pattern="^top_sellers$"))
    app.add_handler(CommandHandler("ping", ping_command))
    app.add_handler(CallbackQueryHandler(ping_again, pattern="^ping_again$"))
    app.add_handler(CallbackQueryHandler(ping_restart, pattern="^ping_restart$"))
    app.add_handler(CallbackQueryHandler(shop_categories, pattern="^shop_key$"))
    app.add_handler(CallbackQueryHandler(shop_ff_categories, pattern="^shop_ff$"))
    app.add_handler(CallbackQueryHandler(list_category_products, pattern="^pcat_"))
    app.add_handler(CallbackQueryHandler(show_product_plans, pattern="^selprod_"))
    app.add_handler(CallbackQueryHandler(process_plan_purchase, pattern="^buyplan_"))
    app.add_handler(CallbackQueryHandler(def_pay_click, pattern="^def_pay_"))
    app.add_handler(CallbackQueryHandler(order_pay_upi_click, pattern="^ordpay_upi$"))
    app.add_handler(CallbackQueryHandler(order_pay_binance_click, pattern="^ordpay_bin$"))
    app.add_handler(CallbackQueryHandler(order_coupon_click, pattern="^ordcoupon$"))
    app.add_handler(CallbackQueryHandler(order_back_click, pattern="^ordback$"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, coupon_text_handler))
    app.add_handler(CommandHandler("apibalance", api_balance_cmd))
    app.add_handler(CommandHandler("apiservices", api_services_cmd))
    app.add_handler(CommandHandler("apimaps", api_maps_cmd))
    app.add_handler(CommandHandler("apimap", api_map_cmd))
    app.add_handler(CommandHandler("apiunmap", api_unmap_cmd))
    app.add_error_handler(error_handler)

    try:
        asyncio.get_event_loop().set_exception_handler(_loop_exception_handler)
    except Exception:
        pass
    print("🤖 Telegram Bot Engine Running...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    print("🌐 Launching Web Admin Panel in Background Thread...")
    t = Thread(target=web_admin.run_web)
    t.daemon = True
    t.start()

    # 📧 Optional: read bank credit e-mails from Gmail (needs GMAIL_USER, GMAIL_APP_PASSWORD, BANK_EMAIL_FROM)
    start_gmail_reader()

    # 🛡️ Crash Prevention Loop (Never Dies)
    while True:
        try:
            # run_polling closes its event loop when it exits, so every restart needs a fresh one.
            asyncio.set_event_loop(asyncio.new_event_loop())
            start_bot()
        except Exception as e:
            print(f"Bot crash prevented: {e}. Auto-restarting in 1s...")
            logger.error("Bot crashed and is restarting: %s", e, exc_info=True)
        time.sleep(1)
