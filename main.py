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
from threading import Thread
from datetime import datetime, timedelta
import pytz
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand, MenuButtonCommands
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
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
UPI_CONFIG = {
    "fampay_token": "9544113089@fam",
    "paytm_token": ""
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
    # key, label, type, default
    ("reseller_enabled", "Reseller plan enabled (1 = on, 0 = off)", "number", "1"),
    ("reseller_min_balance", "Minimum wallet balance (₹)", "number", "2500"),
    ("reseller_fee", "Activation fee (₹)", "number", "700"),
    ("reseller_factor", "Reseller price factor (0.6 = pay 60% of regular price)", "number", "0.6"),
    ("reseller_text", "Reseller plan screen text (HTML). Use {min_balance} {fee} {rest}", "textarea",
     "🤝 <b>RESELLER PLAN</b>\n_________________________\n\n"
     "💰 Minimum Balance: <b>₹{min_balance}</b>\n"
     "💸 Activation Fee: <b>Only ₹{fee}</b>\n"
     "_________________________\n\n"
     "⚡ <b>How does it work?</b>\n"
     "Deposit ₹{min_balance} in your wallet — only ₹{fee} will be charged as activation fee!\n"
     "The remaining ₹{rest} stays safe in your wallet to purchase any products at wholesale rates!\n\n"
     "👑 <b>Reseller Benefits:</b>\n"
     "• Get all products at exclusive discounted prices!\n"
     "• Set your own margin & earn maximum profit\n"
     "• Unlimited earning opportunity\n"
     "• Exclusive VIP Reseller badge on your profile\n\n"
     "💸 <b>Example:</b>\n"
     "🔷 Regular ₹90 Product → Reseller Price: <b>₹40</b>\n"
     "🔷 Regular ₹300 Product → Reseller Price: <b>₹150</b>\n\n"
     "🔥 Start reselling now and earn huge profits!"),
    ("reseller_buy_label", "Buy button text", "text", "Buy Reseller Plan"),
    ("reseller_success_text", "Success message (HTML). Use {fee} {rest}", "textarea",
     "🎉 <b>CONGRATULATIONS!</b>\n\nYou are now officially a <b>👑 VIP Reseller!</b>\n"
     "₹{fee} activation fee deducted. Your remaining ₹{rest}+ is safe in your wallet for purchases."),
    ("reseller_lowbal_text", "Not-enough-balance message (HTML). Use {min_balance} {balance}", "textarea",
     "⚠️ <b>Insufficient Balance!</b>\n\n💰 Required: <b>₹{min_balance}</b>\n"
     "💸 Your Balance: <b>₹{balance}</b>\n\nPlease add balance to your wallet to activate the VIP Reseller Plan."),
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

def db_set_reseller(user_id):
    db.set_reseller(user_id)

def db_add_order(user_id, prod_name, plan, key_delivered, amount, utr, timestamp):
    db.add_order(user_id, prod_name, plan, key_delivered, amount, utr, timestamp)

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

def db_pop_auto_key(prod_key, plan):
    return db.pop_auto_key(prod_key, plan)

def get_products_by_category(category):
    rows = db.get_products_by_category(category)
    result = {}
    for row in rows:
        if row.get("maintenance") or row.get("stock_out"):
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
def unit_price(reg_price, is_reseller):
    return round(float(reg_price) * get_reseller_cfg()["factor"], 2) if is_reseller else float(reg_price)


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

    price = max(0.0, round(unit_price(reg_price, is_reseller) - float(discount or 0), 2))
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
    try:
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{BOT_TOKEN}/{method}",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=8) as resp:
            return json.loads(resp.read().decode())
    except Exception:
        logger.exception("Telegram API call failed: %s", method)
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

def get_button_styles():
    """Saved colors, cached for 5 seconds so a change in the admin panel shows up almost at once."""
    if time.time() - _style_cache["t"] > 5:
        try:
            _style_cache["data"] = db.get_button_styles()
        except Exception:
            logger.exception("Could not load button colors")
        _style_cache["t"] = time.time()
    return _style_cache["data"]

def style_of(key):
    """The color saved for a button key (or its default). None = Telegram's normal grey."""
    style = get_button_styles().get(key)
    if style is None:
        style = MENU_BUTTONS.get(key, ("", ""))[1]
    return style if style in BUTTON_COLORS else None

def styled_button(text, key, **kwargs):
    """InlineKeyboardButton with the color saved for `key`. The color is sent as the Bot API
    'style' field through api_kwargs, which works on any python-telegram-bot version."""
    style = style_of(key)
    if style:
        kwargs["api_kwargs"] = {"style": style}
    return InlineKeyboardButton(text, **kwargs)

def tg_button(text, key, **extra):
    """Same as styled_button, for the plain-HTTP messages (dict buttons)."""
    btn = {"text": text, **extra}
    style = style_of(key)
    if style:
        btn["style"] = style
    return btn

def key_card_keyboard(prod):
    keyboard = []
    if prod and valid_url(prod.get("download_link")):
        keyboard.append([styled_button("📥 Update File ↗️", "update_file", url=prod["download_link"])])
    keyboard.append([styled_button("➡️ Back to Menu", "back_menu", callback_data="main_menu")])
    return InlineKeyboardMarkup(keyboard)

def back_to_menu_markup():
    return InlineKeyboardMarkup([[styled_button("➡️ Back to Menu", "back_menu", callback_data="main_menu")]])

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

    if user.id == ADMIN_ID:
        r_url = os.environ.get("RENDER_EXTERNAL_URL", "http://localhost:8080")
        keyboard.append([styled_button("👑 Web Admin Panel", "web_admin", url=r_url)])

    reply_markup = InlineKeyboardMarkup(keyboard)

    if update.message:
        await update.message.reply_text(text, parse_mode="HTML", reply_markup=reply_markup)
    elif update.callback_query:
        await replace_callback_message(update.callback_query, context, text, reply_markup)

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

    text = _fill(cfg["reseller_text"], min_balance=cfg["min_balance"], fee=cfg["fee"],
                 rest=max(0.0, cfg["min_balance"] - cfg["fee"]))
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

    if balance >= cfg["min_balance"] and db.try_deduct(user.id, cfg["fee"]) is not None:
        db_set_reseller(user.id)
        text = _fill(cfg["reseller_success_text"], fee=cfg["fee"], rest=max(0.0, cfg["min_balance"] - cfg["fee"]),
                     min_balance=cfg["min_balance"])
        keyboard = [[styled_button("👤 View Profile", "view_profile", callback_data="my_profile")]]
        await replace_callback_message(query, context, text, InlineKeyboardMarkup(keyboard))
    else:
        text = _fill(cfg["reseller_lowbal_text"], min_balance=cfg["min_balance"], balance=balance, fee=cfg["fee"])
        keyboard = [
            [styled_button("💰 Add Balance", "reseller_add_balance", callback_data="add_balance")],
            [styled_button("🔜 Back", "back", callback_data="reseller_plan")]
        ]
        await replace_callback_message(query, context, text, InlineKeyboardMarkup(keyboard))

# ==========================================
# 💰 4. ADD BALANCE & NUMPAD CALCULATOR
# ==========================================
async def add_balance_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    ack(query)

    text = (
        "💰 <b>ADD BALANCE</b>\n"
        "_________________________\n\n"
        "💳 Pay with any UPI app (GPay, PhonePe, Paytm...)\n"
        "⏱️ Balance is added as soon as your payment is confirmed.\n"
        "_________________________"
    )
    keyboard = [
        [styled_button("💳 Pay via UPI", "pay_upi", callback_data="dep_method_upi")],
        [styled_button("🔜 Back", "back", callback_data="main_menu")]
    ]
    await replace_callback_message(query, context, text, InlineKeyboardMarkup(keyboard))

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
        [styled_button("👆 Back to Menu", "back", callback_data="add_balance")]
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
        await generate_deposit_qr(update, context, deposit_amt)

async def preset_dep_click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ack(update.callback_query)
    amt = float(update.callback_query.data.replace("preset_dep_", ""))
    await generate_deposit_qr(update, context, amt)

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
    keyboard = [[styled_button(f"{p['icon']} {p['name']}", "product_item", callback_data=f"selprod_{k}")] for k, p in prods.items()]
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
        final_price = unit_price(reg_price, is_reseller)
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
    return unit_price(reg, is_reseller), prod

def _order_markup(prod_key):
    kb = [[styled_button("💳 UPI Payment (QR Code)", "pay_upi", callback_data="ordpay_upi")]]
    if db.get_binance_settings()["enabled"]:
        kb.append([styled_button("🟡 Binance USDT Payment", "pay_binance", callback_data="ordpay_bin")])
    kb.append([styled_button("🎟️ Apply Coupon Code", "apply_coupon", callback_data="ordcoupon")])
    kb.append([styled_button("❌ Back", "back", callback_data=f"selprod_{prod_key}")])
    return InlineKeyboardMarkup(kb)

def _order_text(prod, od, price, note=""):
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
    await replace_callback_message(query, context, _order_text(prod, od, price, note), _order_markup(od["prod_key"]))

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
    await show_order_summary(query, context, update.effective_user.id)

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
    await update.message.reply_text(_order_text(prod, od, price, note), parse_mode="HTML",
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
    context.user_data["deposit_method"] = "upi"
    await generate_deposit_qr(
        update, context, round(price - discount, 2), is_deficit=True, prod_name=prod["name"],
        plan_name=od["plan"], prod_key=od["prod_key"], full_price=True,
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
        f"💰 Total: ₹{final:,.2f}\n"
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
            BotCommand("ping", "📡 Check speed / fix slowness"),
        ])
        await app.bot.set_chat_menu_button(menu_button=MenuButtonCommands())
    except Exception:
        logger.exception("Could not set the bot command menu")
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

def start_bot():
    app = Application.builder().token(BOT_TOKEN).concurrent_updates(False).post_init(post_init).build()

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CallbackQueryHandler(start_command, pattern="^main_menu$"))
    app.add_handler(CallbackQueryHandler(profile_handler, pattern="^my_profile$"))
    app.add_handler(CallbackQueryHandler(reseller_handler, pattern="^reseller_plan$"))
    app.add_handler(CallbackQueryHandler(buy_reseller_action, pattern="^buy_reseller_action$"))
    app.add_handler(CallbackQueryHandler(support_handler, pattern="^support_contact$"))
    app.add_handler(CallbackQueryHandler(add_balance_menu, pattern="^add_balance$"))
    app.add_handler(CallbackQueryHandler(select_deposit_amount, pattern="^dep_method_"))
    app.add_handler(CallbackQueryHandler(numpad_open, pattern="^numpad_open$"))
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
        time.sleep(1)

