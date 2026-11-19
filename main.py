import logging
import asyncio
import os
import time
import json
import re
import random
import urllib.parse
import html
from threading import Thread
from datetime import datetime, timedelta
import pytz
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
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
from database import db, init_db as init_persistent_db

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

def init_db():
    return init_persistent_db()

def get_store_config():
    """Support username / tutorial link: values saved in the web admin, with fallback to defaults."""
    cfg = dict(STORE_CONFIG)
    try:
        row = db.fetchone("SELECT support_username, how_to_use_link FROM store_settings WHERE id = 1")
        if row:
            if row.get("support_username"):
                cfg["support_username"] = row["support_username"]
            if row.get("how_to_use_link"):
                cfg["how_to_use_link"] = row["how_to_use_link"]
    except Exception:
        logger.exception("Could not load store settings")
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
from fulfillment import unit_price, fmt_price, purchase_with_wallet, build_key_card, build_pending_notice

def key_card_keyboard(prod):
    keyboard = []
    if prod and valid_url(prod.get("download_link")):
        keyboard.append([InlineKeyboardButton("📥 Update File ↗️", url=prod["download_link"])])
    keyboard.append([InlineKeyboardButton("➡️ Back to Menu", callback_data="main_menu")])
    return InlineKeyboardMarkup(keyboard)

def back_to_menu_markup():
    return InlineKeyboardMarkup([[InlineKeyboardButton("➡️ Back to Menu", callback_data="main_menu")]])

# Initialize the persistent PostgreSQL schema before the bot starts.
init_db()

# ==========================================
# ✨ MESSAGE-REPLACEMENT NAVIGATION
# ==========================================
async def replace_callback_message(query, context, text, reply_markup=None, parse_mode="HTML"):
    """Delete the current callback message, then open the next screen as a fresh message.
    Used only for callback-menu navigation so every screen transition uses the same behavior.
    """
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

    # Referral Tracking Logic
    if args and args[0].startswith("ref_"):
        try:
            ref_by = int(args[0].replace("ref_", ""))
            if ref_by != user.id and not db_get_user(user.id):
                db.execute(
                    'UPDATE users SET total_referrals = total_referrals + 1 WHERE user_id = %s',
                    (ref_by,)
                )
                try:
                    await context.bot.send_message(chat_id=ref_by, text=f"🎉 <b>New Referral!</b> User <code>{user.id}</code> joined via your link.", parse_mode="HTML")
                except Exception:
                    pass
        except Exception:
            pass

    db_add_or_update_user(user.id, user.full_name, f"@{user.username}" if user.username else "N/A", get_ist_time())

    # Direct buy link from the admin panel: /start buy_<product_key>
    if args and args[0].startswith("buy_") and update.message:
        view = build_plans_view(user.id, args[0][len("buy_"):])
        if view:
            await update.message.reply_text(view[0], parse_mode="HTML", reply_markup=view[1])
            return

    u_data = db_get_user(user.id)
    balance = u_data[4] if u_data else 0.0

    text = (
        f"🛒 ── <b>ELITE HACKERS STORE</b> ── 🛒\n\n"
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
        [InlineKeyboardButton("🛒 Shop Key", callback_data="shop_key"), InlineKeyboardButton("👤 My Profile", callback_data="my_profile")],
        [InlineKeyboardButton("💰 Add Balance", callback_data="add_balance"), InlineKeyboardButton("🧾 All History", callback_data="all_history")],
        [InlineKeyboardButton("▶️ Tutorial watch", url=tutorial_link), InlineKeyboardButton("👑 Reseller", callback_data="reseller_plan")],
        [InlineKeyboardButton("💼 Selling Proof ↗️", url="https://t.me/+fJrFACSrntgwNjll"), InlineKeyboardButton("💬 Support", callback_data="support_contact")],
        [InlineKeyboardButton("🎰 Lucky Spin", callback_data="lucky_spin"), InlineKeyboardButton("👥 Referral", callback_data="referral_menu")]
    ]

    if user.id == ADMIN_ID:
        r_url = os.environ.get("RENDER_EXTERNAL_URL", "http://localhost:8080")
        keyboard.append([InlineKeyboardButton("👑 Web Admin Panel", url=r_url)])

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
    await query.answer()
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
        keyboard.append([InlineKeyboardButton("💬 Contact Support", url=f"https://t.me/{handle}")])
    keyboard.append([InlineKeyboardButton("🔜 Back", callback_data="main_menu")])
    await replace_callback_message(query, context, text, InlineKeyboardMarkup(keyboard))

# ==========================================
# 👤 2. MY PROFILE
# ==========================================
async def profile_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
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

    keyboard = [
        [InlineKeyboardButton("👑 Upgrade To Reseller", callback_data="reseller_plan")],
        [InlineKeyboardButton("🔜 Back", callback_data="main_menu")]
    ]
    await replace_callback_message(query, context, text, InlineKeyboardMarkup(keyboard))

# ==========================================
# 👑 3. RESELLER PLAN
# ==========================================
async def reseller_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    text = (
        "🤝 <b>RESELLER PLAN</b>\n"
        "_________________________\n\n"
        "💰 Minimum Balance: <b>₹2500</b>\n"
        "💸 Activation Fee: <b>Only ₹700</b>\n"
        "_________________________\n\n"
        "⚡ <b>How does it work?</b>\n"
        "Deposit ₹2500 in your wallet — only ₹700 will be charged as activation fee!\n"
        "The remaining ₹1800 stays safe in your wallet to purchase any products at wholesale rates!\n\n"
        "👑 <b>Reseller Benefits:</b>\n"
        "• Get all products at exclusive discounted prices!\n"
        "• Set your own margin & earn maximum profit\n"
        "• Unlimited earning opportunity\n"
        "• Exclusive VIP Reseller badge on your profile\n\n"
        "💸 <b>Example:</b>\n"
        "🔷 Regular ₹90 Product → Reseller Price: <b>₹40</b>\n"
        "🔷 Regular ₹300 Product → Reseller Price: <b>₹150</b>\n\n"
        "🔥 Start reselling now and earn huge profits!"
    )

    keyboard = [
        [InlineKeyboardButton("Buy Reseller Plan", callback_data="buy_reseller_action")],
        [InlineKeyboardButton("🔜 Back", callback_data="main_menu")]
    ]
    await replace_callback_message(query, context, text, InlineKeyboardMarkup(keyboard))

async def buy_reseller_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = update.effective_user
    u_data = db_get_user(user.id)
    balance = u_data[4] if u_data else 0.0

    if balance >= 2500 and db.try_deduct(user.id, 700) is not None:
        db_set_reseller(user.id)
        text = (
            "🎉 <b>CONGRATULATIONS!</b>\n\n"
            "You are now officially a <b>👑 VIP Reseller!</b>\n"
            "₹700 activation fee deducted. Your remaining ₹1800+ is safe in your wallet for purchases."
        )
        keyboard = [[InlineKeyboardButton("👤 View Profile", callback_data="my_profile")]]
        await replace_callback_message(query, context, text, InlineKeyboardMarkup(keyboard))
    else:
        text = (
            "⚠️ <b>Insufficient Balance!</b>\n\n"
            "💰 Required: <b>₹2500</b>\n"
            f"💸 Your Balance: <b>₹{balance:,.2f}</b>\n\n"
            "Please add balance to your wallet to activate the VIP Reseller Plan."
        )
        keyboard = [
            [InlineKeyboardButton("💰 Add Balance", callback_data="add_balance")],
            [InlineKeyboardButton("🔜 Back", callback_data="reseller_plan")]
        ]
        await replace_callback_message(query, context, text, InlineKeyboardMarkup(keyboard))

# ==========================================
# 💰 4. ADD BALANCE & NUMPAD CALCULATOR
# ==========================================
async def add_balance_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    text = (
        "💰 <b>ADD BALANCE</b>\n"
        "_________________________\n\n"
        "💳 Pay with any UPI app (GPay, PhonePe, Paytm...)\n"
        "⏱️ Balance is added as soon as your payment is confirmed.\n"
        "_________________________"
    )
    keyboard = [
        [InlineKeyboardButton("💳 Pay via UPI", callback_data="dep_method_upi")],
        [InlineKeyboardButton("🔜 Back", callback_data="main_menu")]
    ]
    await replace_callback_message(query, context, text, InlineKeyboardMarkup(keyboard))

async def select_deposit_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
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
        [InlineKeyboardButton("₹100", callback_data="preset_dep_100"), InlineKeyboardButton("₹500", callback_data="preset_dep_500")],
        [InlineKeyboardButton("₹1000", callback_data="preset_dep_1000"), InlineKeyboardButton("₹2000", callback_data="preset_dep_2000")],
        [InlineKeyboardButton("✏️ Custom Amount", callback_data="numpad_open")],
        [InlineKeyboardButton("👆 Back to Menu", callback_data="add_balance")]
    ]
    await replace_callback_message(query, context, text, InlineKeyboardMarkup(keyboard))

async def numpad_open(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    await render_numpad(update, context)

async def render_numpad(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    current_digits = context.user_data.get('numpad_val', '')
    amt_display = f"₹{int(current_digits):,}" if current_digits and int(current_digits) > 0 else "₹0"

    text = (
        "💰 <b>Enter Amount</b>\n\n"
        f"<b>{amt_display}</b>\n\n"
        "Min: ₹10.00 · Max: ₹50,000.00"
    )

    confirm_text = f"✅ Confirm {amt_display}" if current_digits and int(current_digits) >= 10 else "✅ Confirm"

    keyboard = [
        [InlineKeyboardButton("1", callback_data="np_1"), InlineKeyboardButton("2", callback_data="np_2"), InlineKeyboardButton("3", callback_data="np_3")],
        [InlineKeyboardButton("4", callback_data="np_4"), InlineKeyboardButton("5", callback_data="np_5"), InlineKeyboardButton("6", callback_data="np_6")],
        [InlineKeyboardButton("7", callback_data="np_7"), InlineKeyboardButton("8", callback_data="np_8"), InlineKeyboardButton("9", callback_data="np_9")],
        [InlineKeyboardButton("C", callback_data="np_clear"), InlineKeyboardButton("0", callback_data="np_0"), InlineKeyboardButton("⌫", callback_data="np_backspace")],
        [InlineKeyboardButton(confirm_text, callback_data="np_confirm")],
        [InlineKeyboardButton("✋ Back", callback_data="dep_method_upi")]
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
        await query.answer()
        await render_numpad(update, context)

    elif action == "clear":
        context.user_data['numpad_val'] = ''
        await query.answer()
        await render_numpad(update, context)

    elif action == "backspace":
        context.user_data['numpad_val'] = val[:-1]
        await query.answer()
        await render_numpad(update, context)

    elif action == "confirm":
        if not val or int(val) < 10:
            await query.answer("⚠️ Minimum deposit is ₹10.00!", show_alert=True)
            return
        if int(val) > 50000:
            await query.answer("⚠️ Maximum deposit is ₹50,000.00!", show_alert=True)
            return

        await query.answer()
        deposit_amt = float(val)
        context.user_data['numpad_val'] = ''
        await generate_deposit_qr(update, context, deposit_amt)

async def preset_dep_click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    amt = float(update.callback_query.data.replace("preset_dep_", ""))
    await generate_deposit_qr(update, context, amt)

# 📷 Dynamic QR Generator with 5-Min Auto Expiry
async def generate_deposit_qr(update: Update, context: ContextTypes.DEFAULT_TYPE, amount: float,
                              is_deficit=False, prod_name=None, plan_name=None, prod_key=None):
    query = update.callback_query
    user_id = query.from_user.id
    chat_id = query.message.chat_id
    method = "UPI"

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

    upi_uri = f"upi://pay?pa={upi_id}&pn={urllib.parse.quote(payee_name, safe='')}&am={pay_amount:.2f}&cu=INR&tn={order_id}"
    qr_url = f"https://api.qrserver.com/v1/create-qr-code/?size=500x500&data={urllib.parse.quote(upi_uri)}"

    if auto_on:
        how_to = (
            "✅ <b>Detected automatically</b> — it is confirmed within a minute of paying. "
            "No need to press anything.\n"
            f"⚠️ Pay the <b>exact amount ₹{pay_amount:,.2f}</b> (with the paise) so it can be matched."
        )
    else:
        how_to = (
            "After paying, tap <b>✅ I have paid</b>. "
            "It is confirmed as soon as the admin approves the payment."
        )

    if is_deficit:
        u_data = db_get_user(user_id)
        bal = u_data[4] if u_data else 0.0
        caption = (
            f"🛒 <b>{html.escape(prod_name or '')} — {html.escape((plan_name or '').replace('_', ' '))}</b>\n\n"
            f"💰 Balance: ₹{bal:,.2f} | Price: ₹{(bal + amount):,.2f}\n"
            f"💸 Scan & pay <b>₹{pay_amount:,.2f}</b> to complete this purchase.\n\n"
            f"{how_to}\n\n"
            f"🆔 Order:\n<code>{order_id}</code>\n\n"
            f"⏰ <b>This QR expires in 5 minutes if payment isn't completed.</b>"
        )
    else:
        caption = (
            f"🎟️ <b>{method} — ₹{pay_amount:,.2f}</b>\n\n"
            f"💰 Amount: <b>₹{pay_amount:,.2f}</b>\n"
            f"🆔 Order:\n<code>{order_id}</code>\n\n"
            f"{how_to}\n\n"
            f"⏰ <b>This QR expires in 5 minutes if payment isn't completed.</b>"
        )

    keyboard = []
    if not auto_on:
        keyboard.append([InlineKeyboardButton("✅ I have paid", callback_data=f"paid_{order_id}")])
    keyboard.append([InlineKeyboardButton("❌ Cancel Payment", callback_data=f"canceldep_{order_id}")])

    try:
        await query.message.delete()
    except Exception:
        pass

    qr_msg = await context.bot.send_photo(
        chat_id=chat_id,
        photo=qr_url,
        caption=caption,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    try:
        db.set_payment_message(order_id, qr_msg.chat_id, qr_msg.message_id)
    except Exception:
        logger.exception("Could not remember the QR message for %s", order_id)

    # ⏳ 5-Minute Expiry Watchdog (stops as soon as the payment leaves "pending", e.g. auto-confirmed)
    async def expiry_watchdog():
        try:
            start_t = time.time()
            while time.time() - start_t < 300:
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
                    f"— ❌ No payment detected within 5 minutes\n"
                    f"— ℹ️ If you already paid the exact amount, it is still credited automatically (up to 1 hour).\n\n"
                    f"<i>Paid but nothing arrived? Tap the button below. Or tap /start for a new order.</i>"
                )
                exp_markup = InlineKeyboardMarkup(
                    [[InlineKeyboardButton("💬 I paid but wasn't credited", callback_data=f"paid_{order_id}")]]
                )
            else:
                exp_text = (
                    f"🕒 <b>PAYMENT EXPIRED</b>\n"
                    f"_________________________\n\n"
                    f"— 🔒 Order: <code>{order_id}</code>\n"
                    f"— ❌ Not paid within 5 minutes\n"
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

async def payment_paid_click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """User says 'I have paid'. Nothing is credited here: the admin must approve first."""
    query = update.callback_query
    user = update.effective_user
    order_id = query.data[len("paid_"):]

    pay = db.get_payment(order_id)
    if not pay or pay["user_id"] != user.id:
        await query.answer("⚠️ Payment not found.", show_alert=True)
        return

    claimed = db.claim_payment(order_id, ("pending", "expired"), "awaiting_approval")
    if not claimed:
        status = pay["status"]
        if status == "awaiting_approval":
            msg = "⏳ Already submitted. Waiting for admin approval."
        elif status == "verified":
            msg = "✅ This payment was already confirmed."
        else:
            msg = "⚠️ This payment session has ended. Tap /start to begin again."
        await query.answer(msg, show_alert=True)
        return

    await query.answer("Submitted for verification ✅")
    try:
        await query.message.delete()
    except Exception:
        pass

    amount = float(pay.get("pay_amount") or pay["amount"])
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

    # Ask the admin to verify and approve / reject.
    lines = [
        "💳 <b>PAYMENT VERIFICATION NEEDED</b>",
        "_________________________",
        "",
        f"👤 User: <a href=\"tg://user?id={user.id}\">{html.escape(user.full_name or 'User')}</a> (<code>{user.id}</code>)",
        f"💰 Amount: <b>₹{amount:,.2f}</b>",
        f"💳 Method: {html.escape(str(pay['method'] or '').upper())} → <code>{html.escape(str(pay['upi_id'] or ''))}</code>",
        f"🆔 Order: <code>{order_id}</code>",
    ]
    if pay.get("is_deficit") and pay.get("prod_key"):
        prod = get_product_by_key(pay["prod_key"])
        lines.append(f"🛒 For: <b>{html.escape((prod or {}).get('name', pay['prod_key']))} — {html.escape((pay.get('plan') or '').replace('_', ' '))}</b>")
    lines += ["", "Check your GPay / bank app for this amount and order ID, then choose:"]

    admin_markup = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Approve", callback_data=f"adm_ok_{order_id}"),
        InlineKeyboardButton("❌ Reject", callback_data=f"adm_no_{order_id}"),
    ]])
    try:
        await context.bot.send_message(chat_id=ADMIN_ID, text="\n".join(lines), parse_mode="HTML", reply_markup=admin_markup)
    except Exception:
        logger.exception("Could not notify the admin about payment %s", order_id)

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
            res = purchase_with_wallet(user_id, row["prod_key"], row["plan"], allow_pending=True, utr="UPI_DEFICIT")
            status = res["status"]
            if status == "ok":
                await context.bot.send_message(
                    chat_id=user_id,
                    text=build_key_card(res["prod"]["name"], row["plan"], res["key"], res["balance"]),
                    parse_mode="HTML",
                    reply_markup=key_card_keyboard(res["prod"])
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
    await query.answer()
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
SPIN_COOLDOWN_HOURS = 24

def fmt_wait(seconds):
    hours, rem = divmod(int(seconds), 3600)
    return f"{hours}h {rem // 60}m"

async def lucky_spin_home(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    remaining = db.spin_remaining_seconds(update.effective_user.id, SPIN_COOLDOWN_HOURS)

    if remaining <= 0:
        status_line = "🕒 Spin Status: ✅ <b>Available Now!</b>\n\n👇 Tap below to spin and try your luck!"
    else:
        status_line = f"🕒 Spin Status: ⏳ Next free spin in <b>{fmt_wait(remaining)}</b>"

    text = (
        "── <b>CASH SPIN</b> ──\n\n"
        "🎁 <b>Daily Free Spin!</b>\n"
        "💰 Win real cash directly into your wallet!\n"
        "Get 1 Free Spin every single day!\n"
        "_________________________\n\n"
        f"{status_line}"
    )
    keyboard = []
    if remaining <= 0:
        keyboard.append([InlineKeyboardButton("🍯 Spin Now! (FREE)", callback_data="spin_action_play")])
    keyboard.append([InlineKeyboardButton("🔜 Back", callback_data="main_menu")])
    await replace_callback_message(query, context, text, InlineKeyboardMarkup(keyboard))

async def perform_lucky_spin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = update.effective_user

    # The cooldown is enforced in the database, so a spin can never be repeated early.
    if not db.claim_spin(user.id, SPIN_COOLDOWN_HOURS):
        await query.answer("⏳ Your next free spin is not available yet. Try again later!", show_alert=True)
        return
    await query.answer()

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

    won_amount = random.choice([2, 4, 5, 10])
    db.update_balance(user.id, won_amount)
    u_data = db_get_user(user.id)
    new_bal = u_data[4] if u_data else won_amount

    result_text = (
        "🎁 ── <b>CASH SPIN RESULT!</b> ── 🎁\n\n"
        "👑 [ 💎 | 💎 | 💎 ]\n"
        "_________________________\n\n"
        "🎉 <b>Congratulations!</b>\n\n"
        f"💰 You won <b>₹{won_amount} Cash Reward!</b>\n"
        "It has been added to your wallet.\n\n"
        f"💳 New balance: <b>₹{new_bal:,.2f}</b>\n"
        "_________________________\n\n"
        f"🕒 Next free spin available in {SPIN_COOLDOWN_HOURS} Hours!"
    )
    keyboard = [
        [InlineKeyboardButton(f"🛒 Shop Now — Use ₹{won_amount}", callback_data="shop_key")],
        [InlineKeyboardButton("🔜 Back", callback_data="main_menu")]
    ]
    await query.message.edit_text(result_text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))

# ==========================================
# 👥 6. REFERRAL SYSTEM
# ==========================================
async def referral_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
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
    keyboard = [[InlineKeyboardButton("🔜 Back", callback_data="main_menu")]]
    await replace_callback_message(query, context, text, InlineKeyboardMarkup(keyboard))

# ==========================================
# 🧾 7. ALL HISTORY (LAST 20 ORDERS)
# ==========================================
async def history_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
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

    keyboard = [[InlineKeyboardButton("🔜 Back", callback_data="main_menu")]]
    await replace_callback_message(query, context, text, InlineKeyboardMarkup(keyboard))

# ==========================================
# 🛒 8. SHOP KEY & DEVICE CATALOG
# ==========================================
async def shop_categories(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    text = "🛒 <b>PRODUCT STORE — SHOP</b> 🛒\n\n📱 Select your device type:"
    keyboard = [
        [InlineKeyboardButton("☎️ Non-Root Mobile", callback_data="pcat_non_root")],
        [InlineKeyboardButton("🔓 Root Mobile", callback_data="pcat_root")],
        [InlineKeyboardButton("🍏 iOS Panels", callback_data="pcat_ios")],
        [InlineKeyboardButton("💻 PC Panels", callback_data="pcat_pc")],
        [InlineKeyboardButton("🆔 8 Level ID", callback_data="pcat_likes")],
        [InlineKeyboardButton("👆 Back", callback_data="main_menu")]
    ]
    await replace_callback_message(query, context, text, InlineKeyboardMarkup(keyboard))

async def list_category_products(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    cat_key = query.data.replace("pcat_", "")
    context.user_data['selected_cat'] = cat_key

    prods = get_products_by_category(cat_key)

    if not prods:
        text = "🛒 <b>PRODUCT STORE — SHOP</b> 🛒\n\n⚠️ No products available in this category yet. Admin will restock soon!"
        keyboard = [[InlineKeyboardButton("👆 Back", callback_data="shop_key")]]
        await replace_callback_message(query, context, text, InlineKeyboardMarkup(keyboard))
        return

    text = "🛒 <b>PRODUCT STORE — SHOP</b> 🛒\n\n🔥 Choose a product:"
    keyboard = [[InlineKeyboardButton(f"{p['icon']} {p['name']}", callback_data=f"selprod_{k}")] for k, p in prods.items()]
    keyboard.append([InlineKeyboardButton("👆 Back", callback_data="shop_key")])
    await replace_callback_message(query, context, text, InlineKeyboardMarkup(keyboard))

def build_plans_view(user_id, prod_key):
    """Return (text, keyboard) for a product's plan list, or None if the product is missing."""
    prod = get_product_by_key(prod_key)
    if not prod:
        return None

    u_data = db_get_user(user_id)
    is_reseller = bool(u_data and u_data[8] == "Reseller")
    role_badge = "👑 Reseller Price" if is_reseller else "👤 Regular Price"

    text = (
        f"🖼️ <b>{html.escape(prod['name'] or '')}</b>\n"
        f"Available | 🔴 {html.escape((prod.get('category') or '').upper())} Only\n"
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
        keyboard.append([InlineKeyboardButton(
            f"🕒 {pl_name.replace('_', ' ')} | {fmt_price(final_price)}",
            callback_data=f"buyplan_{prod_key}_{pl_name}"
        )])

    if valid_url(prod.get("download_link")):
        keyboard.append([InlineKeyboardButton("🎥 Preview Video", url=prod["download_link"])])

    keyboard.append([InlineKeyboardButton("🔜 Back", callback_data="pcat_" + (prod.get("category") or "non_root"))])
    return text, InlineKeyboardMarkup(keyboard)

async def show_product_plans(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    prod_key = query.data.replace("selprod_", "", 1)

    view = build_plans_view(update.effective_user.id, prod_key)
    if not view:
        await query.answer("⚠️ Product not available.", show_alert=True)
        return
    await query.answer()
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

async def process_plan_purchase(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = update.effective_user

    prod_key, plan_name = parse_buyplan(query.data)
    if not prod_key:
        await replace_callback_message(
            query, context, "⚠️ This product or plan is no longer available.", back_to_menu_markup()
        )
        return

    result = purchase_with_wallet(user.id, prod_key, plan_name)
    status = result["status"]

    # MODE A: Sufficient wallet balance ➔ instant key delivery
    if status == "ok":
        await replace_callback_message(
            query, context,
            build_key_card(result["prod"]["name"], plan_name, result["key"], result["balance"]),
            key_card_keyboard(result["prod"])
        )

    # MODE B: Insufficient balance ➔ deficit card
    elif status == "insufficient":
        prod_name = result["prod"]["name"]
        final_price = result["price"]
        balance = result["balance"]
        deficit = round(final_price - balance, 2)
        context.user_data['pending_deficit'] = {
            'prod_key': prod_key, 'prod_name': prod_name, 'plan': plan_name,
            'price': final_price, 'deficit': deficit
        }

        text = (
            f"💰 <b>INSUFFICIENT BALANCE</b>\n"
            f"__________________________________\n\n"
            f"┣ 📦 Product: <b>{html.escape(prod_name)}</b>\n"
            f"┣ ⏱️ Plan: {html.escape(plan_name.replace('_', ' '))}\n"
            f"┣ 💵 Price: 💰 ₹{final_price:,.2f}\n"
            f"┣ 💳 Your Balance: 💰 ₹{balance:,.2f}\n"
            f"┗ ⚠️ Deficit Need: 💰 <b>₹{deficit:,.2f}</b>\n\n"
            f"Select payment method below:"
        )
        keyboard = [
            [InlineKeyboardButton("💳 Pay via UPI", callback_data="def_pay_upi")],
            [InlineKeyboardButton("🔜 Back to Plans", callback_data=f"selprod_{prod_key}")]
        ]
        await replace_callback_message(query, context, text, InlineKeyboardMarkup(keyboard))

    elif status == "out_of_stock":
        keyboard = [[InlineKeyboardButton("🔜 Back to Plans", callback_data=f"selprod_{prod_key}")]]
        await replace_callback_message(
            query, context,
            "⚠️ <b>Out of Stock!</b> Keys will be restocked shortly. You were not charged.",
            InlineKeyboardMarkup(keyboard)
        )

    else:  # unavailable / no_plan / no_product
        await replace_callback_message(
            query, context, "⚠️ This product is currently unavailable.", back_to_menu_markup()
        )

async def def_pay_click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data['deposit_method'] = "upi"

    def_data = context.user_data.get('pending_deficit')
    if not def_data:
        keyboard = [[InlineKeyboardButton("🛒 Open Shop", callback_data="shop_key")]]
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
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Unhandled error while processing an update", exc_info=context.error)

def start_bot():
    app = Application.builder().token(BOT_TOKEN).concurrent_updates(False).build()

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
    app.add_handler(CallbackQueryHandler(shop_categories, pattern="^shop_key$"))
    app.add_handler(CallbackQueryHandler(list_category_products, pattern="^pcat_"))
    app.add_handler(CallbackQueryHandler(show_product_plans, pattern="^selprod_"))
    app.add_handler(CallbackQueryHandler(process_plan_purchase, pattern="^buyplan_"))
    app.add_handler(CallbackQueryHandler(def_pay_click, pattern="^def_pay_"))
    app.add_error_handler(error_handler)

    print("🤖 Telegram Bot Engine Running...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    print("🌐 Launching Web Admin Panel in Background Thread...")
    t = Thread(target=web_admin.run_web)
    t.daemon = True
    t.start()

    # 📧 Optional: read bank credit e-mails from Gmail (needs GMAIL_USER, GMAIL_APP_PASSWORD, BANK_EMAIL_FROM)
    import gmail_watcher
    gmail_watcher.start()

    # 🛡️ Crash Prevention Loop (Never Dies)
    while True:
        try:
            # run_polling closes its event loop when it exits, so every restart needs a fresh one.
            asyncio.set_event_loop(asyncio.new_event_loop())
            start_bot()
        except Exception as e:
            print(f"Bot crash prevented: {e}. Auto-restarting in 1s...")
        time.sleep(1)
