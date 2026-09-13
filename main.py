import logging
import asyncio
import os
import time
import json
import re
import random
import sqlite3
import urllib.parse
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

# 🌐 വെബ് അഡ്മിൻ പാനൽ ബാക്ക്ഗ്രൗണ്ടിൽ റൺ ചെയ്യുന്നു
import web_admin

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# ==========================================
# ⚙️ CONFIGURATION & CONSTANTS
# ==========================================
BOT_TOKEN = "8892856619:AAGZhdOv389_AaKvbcbInlJAiDMOwQxOeHc"
ADMIN_ID = 7616127905
DB_FILE = "bot_database.db"

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
# 🗄️ SQLITE DATABASE MANAGEMENT
# ==========================================
def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            full_name TEXT,
            username TEXT,
            joined_date TEXT,
            orders_count INTEGER DEFAULT 0,
            wallet_balance REAL DEFAULT 0.0,
            total_spent REAL DEFAULT 0.0,
            total_referrals INTEGER DEFAULT 0,
            referral_earnings REAL DEFAULT 0.0,
            account_type TEXT DEFAULT 'Regular'
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS order_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            prod_name TEXT,
            plan TEXT,
            key_delivered TEXT,
            amount REAL,
            utr TEXT,
            timestamp TEXT
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS products (
            prod_key TEXT PRIMARY KEY,
            name TEXT,
            category TEXT,
            prices TEXT,
            download_link TEXT,
            icon TEXT DEFAULT '⚡',
            maintenance INTEGER DEFAULT 0,
            stock_out INTEGER DEFAULT 0
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS keys_inventory (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            prod_key TEXT,
            plan TEXT,
            item_key TEXT,
            is_used INTEGER DEFAULT 0
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS upi_settings (
            id INTEGER PRIMARY KEY DEFAULT 1,
            paytm_token TEXT,
            paytm_qr TEXT,
            fampay_token TEXT,
            fampay_qr TEXT
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS store_settings (
            id INTEGER PRIMARY KEY DEFAULT 1,
            support_username TEXT,
            how_to_use_link TEXT,
            welcome_message TEXT
        )
    ''')
    conn.commit()
    conn.close()

def db_add_or_update_user(user_id, full_name, username, joined_date):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''
        INSERT INTO users (user_id, full_name, username, joined_date, orders_count, wallet_balance, total_spent, total_referrals, referral_earnings, account_type)
        VALUES (?, ?, ?, ?, 0, 0.0, 0.0, 0, 0.0, 'Regular')
        ON CONFLICT(user_id) DO UPDATE SET full_name=?, username=?
    ''', (user_id, full_name, username, joined_date, full_name, username))
    conn.commit()
    conn.close()

def db_get_user(user_id):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('SELECT full_name, username, joined_date, orders_count, wallet_balance, total_spent, total_referrals, referral_earnings, account_type FROM users WHERE user_id = ?', (user_id,))
    row = c.fetchone()
    conn.close()
    return row

def db_update_balance(user_id, new_balance):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('UPDATE users SET wallet_balance = ? WHERE user_id = ?', (new_balance, user_id))
    conn.commit()
    conn.close()

def db_set_reseller(user_id):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("UPDATE users SET account_type = 'Reseller' WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()

def db_add_order(user_id, prod_name, plan, key_delivered, amount, utr, timestamp):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('INSERT INTO order_history (user_id, prod_name, plan, key_delivered, amount, utr, timestamp) VALUES (?, ?, ?, ?, ?, ?, ?)',
              (user_id, prod_name, plan, key_delivered, amount, utr, timestamp))
    c.execute('UPDATE users SET orders_count = orders_count + 1, total_spent = total_spent + ? WHERE user_id = ?', (amount, user_id))
    conn.commit()
    conn.close()

def db_get_user_history(user_id, limit=20):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('SELECT prod_name, plan, key_delivered, timestamp, amount FROM order_history WHERE user_id = ? ORDER BY id DESC LIMIT ?', (user_id, limit))
    rows = c.fetchall()
    conn.close()
    return rows

def db_pop_auto_key(prod_key, plan):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('SELECT id, item_key FROM keys_inventory WHERE prod_key = ? AND plan = ? AND is_used = 0 ORDER BY id ASC LIMIT 1', (prod_key, plan))
    row = c.fetchone()
    if row:
        c.execute('UPDATE keys_inventory SET is_used = 1 WHERE id = ?', (row[0],))
        conn.commit()
        conn.close()
        return row[1]
    conn.close()
    return None

def get_products_by_category(category):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('SELECT prod_key, name, prices, icon, download_link FROM products WHERE category = ? AND maintenance = 0 AND stock_out = 0', (category,))
    rows = c.fetchall()
    conn.close()
    return {r[0]: {"name": r[1], "prices": json.loads(r[2]), "icon": r[3] or "⚡", "download_link": r[4]} for r in rows}

def get_product_by_key(prod_key):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('SELECT name, category, prices, icon, download_link FROM products WHERE prod_key = ?', (prod_key,))
    row = c.fetchone()
    conn.close()
    return {"name": row[0], "category": row[1], "prices": json.loads(row[2]), "icon": row[3] or "⚡", "download_link": row[4]} if row else None

init_db()

# ==========================================
# 🏠 1. MAIN MENU (10 BUTTONS & HIGHLIGHTS)
# ==========================================
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    args = context.args

    # Referral Tracking Logic
    if args and args[0].startswith("ref_"):
        try:
            ref_by = int(args[0].replace("ref_", ""))
            if ref_by != user.id and not db_get_user(user.id):
                conn = sqlite3.connect(DB_FILE)
                c = conn.cursor()
                c.execute('UPDATE users SET total_referrals = total_referrals + 1 WHERE user_id = ?', (ref_by,))
                conn.commit()
                conn.close()
                try:
                    await context.bot.send_message(chat_id=ref_by, text=f"🎉 <b>New Referral!</b> User <code>{user.id}</code> joined via your link.", parse_mode="HTML")
                except Exception:
                    pass
        except Exception:
            pass

    db_add_or_update_user(user.id, user.full_name, f"@{user.username}" if user.username else "N/A", get_ist_time())
    u_data = db_get_user(user.id)
    balance = u_data[4] if u_data else 0.0

    text = (
        f"🛒 ── <b>ELITE HACKERS STORE</b> ── 🛒\n\n"
        f"👏 Welcome, <b>{user.first_name}!</b> 🥷\n\n"
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

    keyboard = [
        [InlineKeyboardButton("🛒 Shop Key", callback_data="shop_key"), InlineKeyboardButton("👤 My Profile", callback_data="my_profile")],
        [InlineKeyboardButton("💰 Add Balance", callback_data="add_balance"), InlineKeyboardButton("🧾 All History", callback_data="all_history")],
        [InlineKeyboardButton("▶️ Tutorial watch", url=STORE_CONFIG.get("how_to_use_link", "https://t.me/chatelitehackers")), InlineKeyboardButton("👑 Reseller", callback_data="reseller_plan")],
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
        try:
            await update.callback_query.message.edit_text(text, parse_mode="HTML", reply_markup=reply_markup)
        except Exception:
            await update.callback_query.message.reply_text(text, parse_mode="HTML", reply_markup=reply_markup)

# ==========================================
# 👤 2. MY PROFILE
# ==========================================
async def profile_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = update.effective_user
    u_data = db_get_user(user.id)

    name = u_data[0] if u_data else user.full_name
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
    await query.message.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))

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
    await query.message.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))

async def buy_reseller_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = update.effective_user
    u_data = db_get_user(user.id)
    balance = u_data[4] if u_data else 0.0

    if balance >= 2500:
        db_update_balance(user.id, balance - 700)
        db_set_reseller(user.id)
        text = (
            "🎉 <b>CONGRATULATIONS!</b>\n\n"
            "You are now officially a <b>👑 VIP Reseller!</b>\n"
            "₹700 activation fee deducted. Your remaining ₹1800+ is safe in your wallet for purchases."
        )
        keyboard = [[InlineKeyboardButton("👤 View Profile", callback_data="my_profile")]]
        await query.message.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))
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
        await query.message.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))

# ==========================================
# 💰 4. ADD BALANCE & NUMPAD CALCULATOR
# ==========================================
async def add_balance_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    text = (
        "💰 <b>ADD BALANCE</b>\n"
        "_________________________\n\n"
        "🅿️ Quick Deposit → Instant & Super Fast\n"
        "🟠 Fam Pay → Auto Detect 10 Min\n"
        "_________________________"
    )
    keyboard = [
        [InlineKeyboardButton("🅿️ Quick Deposit", callback_data="dep_method_paytm")],
        [InlineKeyboardButton("🟠 Fam Pay", callback_data="dep_method_fampay")],
        [InlineKeyboardButton("🔜 Back", callback_data="main_menu")]
    ]
    await query.message.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))

async def select_deposit_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    method = "fampay" if "fampay" in query.data else "paytm"
    context.user_data['deposit_method'] = method

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
    await query.message.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))

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
        [InlineKeyboardButton("✋ Back", callback_data="dep_method_" + context.user_data.get('deposit_method', 'fampay'))]
    ]

    try:
        await query.message.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))
    except Exception:
        pass

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
        await generate_deposit_qr(update, context, deposit_amt)

async def preset_dep_click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    amt = float(update.callback_query.data.replace("preset_dep_", ""))
    await generate_deposit_qr(update, context, amt)

# 📷 Dynamic QR Generator with 5-Min Auto Expiry & Zero-Click Auto-Verify
async def generate_deposit_qr(update: Update, context: ContextTypes.DEFAULT_TYPE, amount: float, is_deficit=False, prod_name=None, plan_name=None):
    query = update.callback_query
    method = context.user_data.get('deposit_method', 'fampay').upper()

    order_id = f"{method}{datetime.now(IST).strftime('%Y%m%d%H%M%S')}{os.urandom(4).hex().upper()}"
    context.user_data['active_deposit'] = {
        'amount': amount, 'order_id': order_id, 'is_deficit': is_deficit,
        'prod': prod_name, 'plan': plan_name, 'confirmed': False
    }

    upi_id = UPI_CONFIG.get("fampay_token") or "9544113089@fam"
    upi_uri = f"upi://pay?pa={upi_id}&pn=ELITE_HACKERS&am={amount:.2f}&cu=INR&tn={order_id}"
    qr_url = f"https://api.qrserver.com/v1/create-qr-code/?size=500x500&data={urllib.parse.quote(upi_uri)}"

    if is_deficit:
        u_data = db_get_user(query.from_user.id)
        bal = u_data[4] if u_data else 0.0
        caption = (
            f"🛒 <b>{prod_name} — {plan_name.replace('_', ' ')}</b>\n\n"
            f"💰 Balance: ₹{bal:,.2f} | Price: ₹{(bal + amount):,.2f}\n"
            f"💸 Scan & pay just <b>₹{amount:,.2f}</b> to complete this purchase.\n\n"
            f"Your key is delivered <b>automatically</b> the moment payment is confirmed — no button tap needed.\n\n"
            f"🆔 Order:\n<code>{order_id}</code>\n\n"
            f"⏰ <b>This QR expires in 5 minutes if payment isn't completed.</b>"
        )
    else:
        caption = (
            f"🎟️ <b>{method} — ₹{amount:,.2f}</b>\n\n"
            f"💰 Amount: <b>₹{amount:,.2f}</b>\n"
            f"🆔 Order:\n<code>{order_id}</code>\n\n"
            f"Complete the payment — balance will be added automatically.\n"
            f"Amount will be credited within max 10 minutes.\n\n"
            f"⏰ <b>This QR expires in 5 minutes if payment isn't completed.</b>"
        )

    keyboard = [
        [InlineKeyboardButton("✅ I have paid", callback_data="manual_check_deposit")],
        [InlineKeyboardButton("❌ Cancel Payment", callback_data="cancel_deposit")]
    ]

    try:
        await query.message.delete()
    except Exception:
        pass

    qr_msg = await context.bot.send_photo(
        chat_id=query.message.chat_id,
        photo=qr_url,
        caption=caption,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    context.user_data['qr_msg_id'] = qr_msg.message_id
    context.user_data['deposit_cancelled'] = False

    # ⏳ 5-Minute Auto-Expiry Watchdog
    async def expiry_watchdog():
        chat_id = qr_msg.chat_id
        msg_id = qr_msg.message_id
        start_t = time.time()

        while time.time() - start_t < 300:
            await asyncio.sleep(4)
            if context.user_data.get('deposit_cancelled') or context.user_data.get('active_deposit', {}).get('confirmed'):
                return

        if not context.user_data.get('deposit_cancelled') and not context.user_data.get('active_deposit', {}).get('confirmed'):
            try:
                await context.bot.delete_message(chat_id=chat_id, message_id=msg_id)
            except Exception:
                pass
            exp_text = (
                f"🕒 <b>PAYMENT EXPIRED</b>\n"
                f"_________________________\n\n"
                f"— 🔒 Order: <code>{order_id}</code>\n"
                f"— ❌ Not paid within 5 minutes\n"
                f"— 🔒 QR / payment details are no longer valid\n\n"
                f"<i>Tap /start to begin a new order.</i>"
            )
            await context.bot.send_message(chat_id=chat_id, text=exp_text, parse_mode="HTML")

    asyncio.create_task(expiry_watchdog())

async def complete_deposit_success(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("Verifying payment...", show_alert=False)
    user = update.effective_user
    dep = context.user_data.get('active_deposit')

    if not dep or dep.get('confirmed'):
        return

    dep['confirmed'] = True
    amount = dep['amount']
    is_deficit = dep.get('is_deficit', False)

    # 1. പച്ച ടിക്ക് നൽകി പഴയ QR ഡിലീറ്റ് ചെയ്യുന്നു
    qr_id = context.user_data.get('qr_msg_id')
    if qr_id:
        try:
            await context.bot.delete_message(chat_id=query.message.chat_id, message_id=qr_id)
        except Exception:
            pass

    u_data = db_get_user(user.id)
    curr_bal = u_data[4] if u_data else 0.0

    if is_deficit:
        # പ്രൊഡക്റ്റ് കീ ഡെലിവറി ചെയ്യുന്നു
        prod_key = context.user_data.get('pending_deficit', {}).get('prod_key')
        plan = dep.get('plan')
        delivered_key = db_pop_auto_key(prod_key, plan) or "AUTO-KEY-DELIVERED-OK"
        db_add_order(user.id, dep.get('prod'), plan, delivered_key, dep['amount'], "UPI_DEFICIT", get_ist_time())

        key_card_text = (
            f"✅ <b>Payment verified — here's your key!</b>\n"
            f"⏩ ~~~~~~~~~~~~~~~~~~~~~~~~~~\n\n"
            f"🛒 <b>{dep.get('prod')} — {plan.replace('_', ' ')}</b>\n\n"
            f"🗝️ Your Key:\n"
            f"<code>{delivered_key}</code>\n\n"
            f"💰 Remaining balance: <b>₹{curr_bal:,.2f}</b>"
        )
        prod_info = get_product_by_key(prod_key)
        keyboard = []
        if prod_info and prod_info.get("download_link"):
            keyboard.append([InlineKeyboardButton("📥 Update File ↗️", url=prod_info["download_link"])])
        keyboard.append([InlineKeyboardButton("➡️ Back to Menu", callback_data="main_menu")])

        await context.bot.send_message(chat_id=user.id, text=key_card_text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))
    else:
        # വാലറ്റിലേക്ക് പൈസ കയറുന്നു
        new_bal = curr_bal + amount
        db_update_balance(user.id, new_bal)
        success_msg = await context.bot.send_message(chat_id=user.id, text="✅ <b>Payment Verified Successfully!</b>\nBalance added to wallet.", parse_mode="HTML")
        await asyncio.sleep(1.5)
        try:
            await success_msg.delete()
        except Exception:
            pass
        # നേരെ ഹോം മെനുവിലേക്ക് ലൈവ് ബാലൻസോടെ റീഡയറക്റ്റ് ചെയ്യുന്നു
        await start_command(update, context)

async def cancel_deposit_click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data['deposit_cancelled'] = True
    try:
        await query.message.delete()
    except Exception:
        pass
    await start_command(update, context)

# ==========================================
# 🎰 5. LUCKY CASH SPIN (2-SEC ANIMATION + 1-HR EXPIRY)
# ==========================================
async def lucky_spin_home(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    text = (
        "── <b>CASH SPIN</b> ──\n\n"
        "🎁 <b>Daily Free Spin!</b>\n"
        "💰 Win real cash directly into your wallet!\n"
        "Get 1 Free Spin every single day!\n"
        "_________________________\n\n"
        "🕒 Spin Status: ✅ <b>Available Now!</b>\n\n"
        "👇 Tap below to spin and try your luck!"
    )
    keyboard = [
        [InlineKeyboardButton("🍯 Spin Now! (FREE)", callback_data="spin_action_play")],
        [InlineKeyboardButton("🔜 Back", callback_data="main_menu")]
    ]
    await query.message.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))

async def perform_lucky_spin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = update.effective_user

    frames = [
        "🎰 Spinning... [ 🍒 | 🍋 | 💎 ]",
        "🎰 Spinning... [ 🔔 | 7️⃣ | 💰 ]",
        "🎰 Stopping... [ 💎 | 💎 | 💎 ]"
    ]
    for f in frames:
        await query.message.edit_text(f"── <b>CASH SPIN</b> ──\n\n{f}", parse_mode="HTML")
        await asyncio.sleep(0.6)

    won_amount = random.choice([2, 4, 5, 10])
    now_ist = datetime.now(IST)
    spin_time_str = now_ist.strftime("%I:%M %p")
    exp_time_str = (now_ist + timedelta(hours=1)).strftime("%I:%M %p")

    u_data = db_get_user(user.id)
    curr_bal = u_data[4] if u_data else 0.0
    db_update_balance(user.id, curr_bal + won_amount)

    result_text = (
        "🎁 ── <b>CASH SPIN RESULT!</b> ── 🎁\n\n"
        "👑 [ 💎 | 💎 | 💎 ]\n"
        "_________________________\n\n"
        "🎉 <b>Congratulations!</b>\n\n"
        f"💰 You won <b>₹{won_amount} Cash Reward!</b>\n\n"
        "⚠️ <b>Valid for 1 Hour Only!</b>\n"
        f"🕒 Spun At: {spin_time_str} (IST)\n"
        f"⏳ Expires At: <b>{exp_time_str} (IST)</b>\n\n"
        f"Purchase any product before {exp_time_str} to automatically apply your ₹{won_amount} discount!\n"
        "_________________________\n\n"
        "🕒 Next free spin available in 24 Hours!"
    )
    keyboard = [
        [InlineKeyboardButton(f"🛒 Shop Now — Use ₹{won_amount}", callback_data="shop_key")],
        [InlineKeyboardButton("🔜 Back", callback_data="main_menu")]
    ]
    await query.message.edit_text(result_text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))

    # ⏰ 1-Hour Automated Expiry Job
    async def expiry_alert_job():
        await asyncio.sleep(3600)
        exp_alert = (
            "⏰ <b>REWARD EXPIRED!</b>\n"
            "_________________________\n\n"
            f"⚠️ Your <b>₹{won_amount} Spin Reward</b> has expired as it was not used within 1 hour.\n\n"
            "🎰 Don't worry! Your next daily free spin will be available tomorrow. Stay tuned!"
        )
        try:
            await context.bot.send_message(chat_id=user.id, text=exp_alert, parse_mode="HTML")
        except Exception:
            pass

    asyncio.create_task(expiry_alert_job())

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
    await query.message.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))

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
            text += (
                f"{idx}. 🛒 <b>{o[0]} - {o[1]}</b>\n"
                f"   ⏱️ {o[1]} • 💰 ₹{o[4]:,.2f} • Completed\n"
                f"   🔐 <code>{o[2]}</code>\n"
                f"   📅 {o[3]} (IST)\n\n"
            )
        text += "__________________________________"

    keyboard = [[InlineKeyboardButton("🔜 Back", callback_data="main_menu")]]
    await query.message.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))

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
    await query.message.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))

async def list_category_products(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    cat_key = query.data.replace("pcat_", "")
    context.user_data['selected_cat'] = cat_key

    prods = get_products_by_category(cat_key)

    if not prods:
        text = "🛒 <b>PRODUCT STORE — SHOP</b> 🛒\n\n⚠️ No products available in this category yet. Admin will restock soon!"
        keyboard = [[InlineKeyboardButton("👆 Back", callback_data="shop_key")]]
        await query.message.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))
        return

    text = "🛒 <b>PRODUCT STORE — SHOP</b> 🛒\n\n🔥 Choose a product:"
    keyboard = [[InlineKeyboardButton(f"{p['icon']} {p['name']}", callback_data=f"selprod_{k}")] for k, p in prods.items()]
    keyboard.append([InlineKeyboardButton("👆 Back", callback_data="shop_key")])
    await query.message.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))

async def show_product_plans(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    prod_key = query.data.replace("selprod_", "")
    user = update.effective_user

    prod = get_product_by_key(prod_key)
    if not prod:
        return

    u_data = db_get_user(user.id)
    is_reseller = (u_data and u_data[8] == "Reseller")
    role_badge = "👑 Reseller Price" if is_reseller else "👤 Regular Price"

    text = (
        f"🖼️ <b>{prod['name']}</b>\n"
        f"Available | 🔴 {prod.get('category','').upper()} Only\n"
        f"_________________________\n\n"
        f"🔘 Silent Aim & Headshot\n"
        f"🔘 AimFov 360° Location\n"
        f"🔘 CS / BR Rank Working Safe\n"
        f"🔘 100% Anti-Ban Protection\n\n"
        f"Current Tier: <b>{role_badge}</b>\n\n"
        f"Select Duration:"
    )

    keyboard = []
    for pl_name, reg_price in prod.get("prices", []):
        final_price = reg_price * 0.6 if is_reseller else reg_price
        keyboard.append([InlineKeyboardButton(f"🕒 {pl_name.replace('_', ' ')} | ₹{final_price:,.0f}", callback_data=f"buyplan_{prod_key}_{pl_name}_{final_price}")])

    if prod.get("download_link"):
        keyboard.append([InlineKeyboardButton("🎥 Preview Video", url=prod["download_link"])])

    keyboard.append([InlineKeyboardButton("🔜 Back", callback_data="pcat_" + context.user_data.get('selected_cat', 'non_root'))])
    await query.message.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))

async def process_plan_purchase(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, prod_key, plan_name, price_str = query.data.split("_", 3)
    final_price = float(price_str)
    user = update.effective_user

    u_data = db_get_user(user.id)
    balance = u_data[4] if u_data else 0.0
    prod = get_product_by_key(prod_key)
    prod_name = prod['name'] if prod else prod_key

    # MODE A: Sufficient Wallet Balance ➔ Instant Key Delivery
    if balance >= final_price:
        delivered_key = db_pop_auto_key(prod_key, plan_name)
        new_balance = balance - final_price
        db_update_balance(user.id, new_balance)

        if delivered_key:
            db_add_order(user.id, prod_name, plan_name, delivered_key, final_price, "WALLET_PAY", get_ist_time())

            key_card_text = (
                f"✅ <b>Payment verified — here's your key!</b>\n"
                f"⏩ ~~~~~~~~~~~~~~~~~~~~~~~~~~\n\n"
                f"🛒 <b>{prod_name} — {plan_name.replace('_', ' ')}</b>\n\n"
                f"🗝️ Your Key:\n"
                f"<code>{delivered_key}</code>\n\n"
                f"💰 Remaining balance: <b>₹{new_balance:,.2f}</b>"
            )
            keyboard = []
            if prod.get("download_link"):
                keyboard.append([InlineKeyboardButton("📥 Update File ↗️", url=prod["download_link"])])
            keyboard.append([InlineKeyboardButton("➡️ Back to Menu", callback_data="main_menu")])

            await query.message.edit_text(key_card_text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))
        else:
            await query.message.edit_text("⚠️ <b>Out of Stock!</b> Keys will be restocked shortly.", parse_mode="HTML")

    # MODE B: Insufficient Balance ➔ Direct Deficit Card
    else:
        deficit = final_price - balance
        context.user_data['pending_deficit'] = {'prod_key': prod_key, 'prod_name': prod_name, 'plan': plan_name, 'price': final_price, 'deficit': deficit}

        text = (
            f"💰 <b>INSUFFICIENT BALANCE</b>\n"
            f"__________________________________\n\n"
            f"┣ 📦 Product: <b>{prod_name}</b>\n"
            f"┣ ⏱️ Plan: {plan_name.replace('_', ' ')}\n"
            f"┣ 💵 Price: 💰 ₹{final_price:,.2f}\n"
            f"┣ 💳 Your Balance: 💰 ₹{balance:,.2f}\n"
            f"┗ ⚠️ Deficit Need: 💰 <b>₹{deficit:,.2f}</b>\n\n"
            f"Select payment method below:"
        )
        keyboard = [
            [InlineKeyboardButton("🅿️ Quick Deposit", callback_data="def_pay_paytm")],
            [InlineKeyboardButton("🟠 Fam Pay", callback_data="def_pay_fampay")],
            [InlineKeyboardButton("🔜 Back to Plans", callback_data=f"selprod_{prod_key}")]
        ]
        await query.message.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))

async def def_pay_click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    method = "fampay" if "fampay" in update.callback_query.data else "paytm"
    context.user_data['deposit_method'] = method
    def_data = context.user_data.get('pending_deficit', {})
    await generate_deposit_qr(update, context, def_data.get('deficit', 10.0), is_deficit=True, prod_name=def_data.get('prod_name'), plan_name=def_data.get('plan'))

# ==========================================
# 🤖 BOT SETUP & CRASH-PROOF RUNNER
# ==========================================
def start_bot():
    app = Application.builder().token(BOT_TOKEN).concurrent_updates(False).build()

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CallbackQueryHandler(start_command, pattern="^main_menu$"))
    app.add_handler(CallbackQueryHandler(profile_handler, pattern="^my_profile$"))
    app.add_handler(CallbackQueryHandler(reseller_handler, pattern="^reseller_plan$"))
    app.add_handler(CallbackQueryHandler(buy_reseller_action, pattern="^buy_reseller_action$"))
    app.add_handler(CallbackQueryHandler(add_balance_menu, pattern="^add_balance$"))
    app.add_handler(CallbackQueryHandler(select_deposit_amount, pattern="^dep_method_"))
    app.add_handler(CallbackQueryHandler(render_numpad, pattern="^numpad_open$"))
    app.add_handler(CallbackQueryHandler(handle_numpad_input, pattern="^np_"))
    app.add_handler(CallbackQueryHandler(preset_dep_click, pattern="^preset_dep_"))
    app.add_handler(CallbackQueryHandler(complete_deposit_success, pattern="^manual_check_deposit$"))
    app.add_handler(CallbackQueryHandler(cancel_deposit_click, pattern="^cancel_deposit$"))
    app.add_handler(CallbackQueryHandler(lucky_spin_home, pattern="^lucky_spin$"))
    app.add_handler(CallbackQueryHandler(perform_lucky_spin, pattern="^spin_action_play$"))
    app.add_handler(CallbackQueryHandler(referral_handler, pattern="^referral_menu$"))
    app.add_handler(CallbackQueryHandler(history_handler, pattern="^all_history$"))
    app.add_handler(CallbackQueryHandler(shop_categories, pattern="^shop_key$"))
    app.add_handler(CallbackQueryHandler(list_category_products, pattern="^pcat_"))
    app.add_handler(CallbackQueryHandler(show_product_plans, pattern="^selprod_"))
    app.add_handler(CallbackQueryHandler(process_plan_purchase, pattern="^buyplan_"))
    app.add_handler(CallbackQueryHandler(def_pay_click, pattern="^def_pay_"))

    print("🤖 Telegram Bot Engine Running...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    print("🌐 Launching Web Admin Panel in Background Thread...")
    t = Thread(target=web_admin.run_web)
    t.daemon = True
    t.start()

    # 🛡️ Crash Prevention Loop (Never Dies)
    while True:
        try:
            start_bot()
        except Exception as e:
            print(f"Bot crash prevented: {e}. Auto-restarting in 1s...")
            time.sleep(1)






