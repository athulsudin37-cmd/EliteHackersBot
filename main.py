import logging
import asyncio
import os
import time
import json
import imaplib
import email
import re
import random
import sqlite3
import urllib.parse
from threading import Thread
from datetime import datetime
import pytz
from flask import Flask
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters
)

# Enable logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# ==========================================
# 🌐 FLASK KEEP-ALIVE SERVER
# ==========================================
flask_app = Flask('')

@flask_app.route('/')
def home():
    return "Bot is alive and running 24/7!"

def run_flask():
    port = int(os.environ.get("PORT", 8080))
    flask_app.run(host='0.0.0.0', port=port)

def keep_alive():
    t = Thread(target=run_flask)
    t.daemon = True
    t.start()

# ==========================================
# ⚙️ CONFIGURATION & CONFIG CONSTANTS
# ==========================================
BOT_TOKEN = "8892856619:AAGZhdOv389_AaKvbcbInlJAiDMOwQxOeHc"
ADMIN_ID = 7616127905
RECEIVER_UPI_ID = "9544113089@fam"
GMAIL_USER = os.environ.get("GMAIL_USER", "athulsudin37@gmail.com")
GMAIL_APP_PASS = os.environ.get("GMAIL_APP_PASS", "")

ACTIVE_ORDERS = {}       # admin_msg_id -> order_data
MAINTENANCE_MODE = {}    # prod_key -> True/False
PRODUCT_LINKS = {}       # prod_key -> download_url_string
KEYS_STOCK = {}          # (prod_key, plan_name) -> list of key strings
USED_UTRS = set()        # Used UTR list for double-spending protection

# ==========================================
# 🗄️ SQLITE DATABASE MANAGEMENT
# ==========================================
DB_FILE = "bot_database.db"

def init_db():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    
    # Users table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            full_name TEXT,
            username TEXT,
            joined_date TEXT,
            orders_count INTEGER DEFAULT 0
        )
    ''')
    
    # Orders history table
    cursor.execute('''
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
    
    # Stock Keys table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS key_stock (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            prod_key TEXT,
            plan_name TEXT,
            key_value TEXT
        )
    ''')

    # Used UTRs table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS used_utrs (
            utr TEXT PRIMARY KEY
        )
    ''')

    conn.commit()
    conn.close()

def db_add_or_update_user(user_id, full_name, username, joined_date):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO users (user_id, full_name, username, joined_date, orders_count)
        VALUES (?, ?, ?, ?, 0)
        ON CONFLICT(user_id) DO UPDATE SET full_name=?, username=?
    ''', (user_id, full_name, username, joined_date, full_name, username))
    conn.commit()
    conn.close()

def db_get_user(user_id):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('SELECT full_name, username, joined_date, orders_count FROM users WHERE user_id = ?', (user_id,))
    row = cursor.fetchone()
    conn.close()
    return row

def db_add_order(user_id, prod_name, plan, key_delivered, amount, utr, timestamp):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO order_history (user_id, prod_name, plan, key_delivered, amount, utr, timestamp)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    ''', (user_id, prod_name, plan, key_delivered, amount, utr, timestamp))
    
    cursor.execute('UPDATE users SET orders_count = orders_count + 1 WHERE user_id = ?', (user_id,))
    conn.commit()
    conn.close()

def db_get_user_history(user_id, limit=5):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('''
        SELECT prod_name, plan, key_delivered, timestamp 
        FROM order_history WHERE user_id = ? ORDER BY id DESC LIMIT ?
    ''', (user_id, limit))
    rows = cursor.fetchall()
    conn.close()
    return rows

init_db()

# Products Data
NON_ROOT_PRODUCTS = {
    "bala_mod": {
        "name": "BALA MOD NON ROOT",
        "prices": [("1_Hour", 45), ("2_Hour", 85), ("4_Hour", 150), ("6_Hour", 220), ("12_Hour", 300), ("1_Day", 420), ("3_Day", 1050)]
    },
    "tm_pannel": {
        "name": "TM PANNEL NON ROOT",
        "prices": [("1_Day", 70), ("7_Day", 210), ("15_Day", 310), ("31_Day", 450), ("Lifetime_Permanent", 1100)]
    },
    "drip_client": {
        "name": "DRIP CLIENT APK MOD",
        "prices": [("1_Day", 80), ("3_Day", 140), ("7_Day", 250), ("15_Day", 360), ("31_Day", 500)]
    },
    "prime_hook": {
        "name": "PRIME HOOK APK MOD",
        "prices": [("1_Day", 80), ("3_Day", 170), ("7_Day", 320), ("10_Day", 420)]
    },
    "hg_cheat": {
        "name": "HG CHEAT APK MOD",
        "prices": [("1_Day", 100), ("7_Day", 230), ("10_Day", 330), ("30_Day", 690)]
    },
    "silent_cheat": {
        "name": "SILENT CHEAT SAFE",
        "prices": [("1_Day", 90), ("3_Day", 190), ("7_Day", 320), ("15_Day", 550), ("30_Day", 830)]
    },
    "drip_proxy": {
        "name": "DRIP CLIENT PROXY",
        "prices": [("1_Day", 65), ("3_Day", 140), ("7_Day", 260), ("31_Day", 650)]
    }
}

ROOT_PRODUCTS = {
    "rapid_core": {
        "name": "RAPID CORE INJECTOR",
        "prices": [("1_Day", 90), ("7_Day", 310), ("15_Day", 470), ("30_Day", 690)]
    },
    "neo_strike": {
        "name": "NEO STRIKE BRUTAL",
        "prices": [("1_Day", 90), ("3_Day", 180), ("7_Day", 310), ("14_Day", 590), ("28_Day", 899)]
    },
    "haxx_cker": {
        "name": "HAXX-CKER PRO",
        "prices": [("10_Day", 550)]
    },
    "xytron_pro": {
        "name": "XYTRON PRO",
        "prices": [("1_Day", 100), ("7_Day", 310), ("15_Day", 550), ("31_Day", 830)]
    },
    "br_mod": {
        "name": "BR MOD INJECTOR",
        "prices": [("1_Day", 90), ("7_Day", 250), ("15_Day", 420), ("31_Day", 570)]
    },
    "angry_mod": {
        "name": "ANGRY MOD",
        "prices": [("1_Day", 70), ("7_Day", 130), ("15_Day", 170), ("31_Day", 290)]
    },
    "xyz_cheats": {
        "name": "XYZ CHEATS",
        "prices": [("1_Day", 80), ("3_Day", 160), ("7_Day", 310), ("15_Day", 520), ("30_Day", 880)]
    }
}

IOS_PRODUCTS = {
    "migul_pro": {
        "name": "MIGUL PRO IOS",
        "prices": [("1_Day", 200), ("7_Day", 480), ("31_Day", 900)]
    },
    "flourite_ios": {
        "name": "FLOURITE IOS",
        "prices": [("1_Day", 270), ("7_Day", 780), ("31_Day", 1600)]
    }
}

PC_PRODUCTS = {
    "br_mod_pc": {
        "name": "BR MOD PC",
        "prices": [("1_Day", 150), ("10_Day", 550), ("31_Day", 900)]
    },
    "internal_pc": {
        "name": "INTERNAL PC",
        "prices": [("1_Day", 99), ("3_Day", 199), ("7_Day", 370), ("15_Day", 650), ("30_Day", 900), ("Lifetime_Permanent", 2100)]
    }
}

LIKE_PRODUCTS = {
    "auto_like_everyday": {
        "name": "AUTO LIKE EVERY DAY",
        "prices": [("7_DAYS", 90), ("15_DAYS", 160), ("30_DAYS", 275), ("90_DAYS", 730)]
    }
}

ALL_CATEGORIES = [NON_ROOT_PRODUCTS, ROOT_PRODUCTS, IOS_PRODUCTS, PC_PRODUCTS, LIKE_PRODUCTS]

def get_product_by_key(prod_key):
    for cat in ALL_CATEGORIES:
        if prod_key in cat:
            return cat[prod_key]
    return None

def format_amt_simple(amount):
    return f"{amount:,.2f}"

def get_ist_time():
    ist = pytz.timezone('Asia/Kolkata')
    return datetime.now(ist).strftime("%d %b %Y, %I:%M %p (IST)")

def generate_dynamic_qr_url(upi_id, amount, note="FF Service"):
    formatted_amt = f"{amount:.2f}"
    upi_uri = f"upi://pay?pa={upi_id}&pn=ELITE_HACKERS&am={formatted_amt}&cu=INR&tn={urllib.parse.quote(note)}"
    return f"https://api.qrserver.com/v1/create-qr-code/?size=500x500&data={urllib.parse.quote(upi_uri)}"

def clean_html_text(text):
    clean = re.sub(r'<[^>]+>', ' ', text)
    return ' '.join(clean.split())

async def check_email_once(expected_amount, utr=None):
    def _imap_check():
        try:
            mail = imaplib.IMAP4_SSL("imap.gmail.com")
            mail.login(GMAIL_USER, GMAIL_APP_PASS)
            mail.select("inbox")

            status, messages = mail.search(None, "ALL")
            if status != "OK" or not messages[0]:
                mail.logout()
                return False, "Unable to access inbox or no emails found."

            msg_ids = messages[0].split()[-40:]
            expected_str = f"{float(expected_amount):.2f}"
            
            for msg_id in reversed(msg_ids):
                res, msg_data = mail.fetch(msg_id, "(RFC822)")
                for response_part in msg_data:
                    if isinstance(response_part, tuple):
                        msg = email.message_from_bytes(response_part[1])
                        body = ""
                        if msg.is_multipart():
                            for part in msg.walk():
                                c_type = part.get_content_type()
                                if c_type in ["text/plain", "text/html"]:
                                    part_str = part.get_payload(decode=True).decode('utf-8', errors='ignore')
                                    body += " " + clean_html_text(part_str)
                        else:
                            part_str = msg.get_payload(decode=True).decode('utf-8', errors='ignore')
                            body = clean_html_text(part_str)

                        if expected_str in body:
                            mail.logout()
                            return True, "SUCCESS"
                        if utr and str(utr).strip() in body:
                            mail.logout()
                            return True, "SUCCESS"
            mail.logout()
            return False, "Payment statement not found in bank email notifications yet."
        except Exception as e:
            logger.error(f"Gmail Verification Error: {e}")
            return False, f"Server error checking bank statement: {str(e)}"

    return await asyncio.to_thread(_imap_check)

async def verify_fampay_gmail_payment(expected_amount, utr=None, retries=2, delay=2):
    last_reason = "Payment notification not received."
    for attempt in range(retries):
        status, reason = await check_email_once(expected_amount, utr)
        if status:
            return True, "SUCCESS"
        last_reason = reason
        if attempt < retries - 1:
            await asyncio.sleep(delay)
    return False, last_reason

# ==========================================
# 🚀 START & WELCOME MESSAGE
# ==========================================
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user = update.effective_user
        if not user:
            return

        db_user = db_get_user(user.id)
        if not db_user:
            db_add_or_update_user(user.id, user.full_name, f"@{user.username}" if user.username else "N/A", get_ist_time())
            if user.id != ADMIN_ID:
                alert_text = (
                    "🔔 <b>NEW USER STARTED THE BOT!</b>\n\n"
                    f"👤 <b>Name:</b> {user.full_name}\n"
                    f"🔗 <b>Username:</b> @{user.username if user.username else 'N/A'}\n"
                    f"🆔 <b>User ID:</b> <code>{user.id}</code>\n"
                    f"📅 <b>Date:</b> {get_ist_time()}"
                )
                try:
                    await context.bot.send_message(chat_id=ADMIN_ID, text=alert_text, parse_mode="HTML")
                except Exception as e:
                    logger.error(f"Failed to alert admin: {e}")

        welcome_text = (
            "🚀 <b>Welcome to ELITE HACKERS</b> 🌟\n\n"
            "🥃 Hey! Thanks for reaching out.\n"
            "✉️ Please leave your message, and I'll respond as soon as I'm available.\n\n"
            "⌛ Your patience is greatly appreciated.\n"
            "____________________________________\n\n"
            "🏦 — FREE FIRE PANEL SERVICES — 🏦\n\n"
            f"🎉 <b>Hello, {user.first_name}!</b>\n"
            "🔑 <b>Power By ELITE HACKERS</b>\n\n"
            "— 🏦 Direct deals with every supplier\n"
            "— 💧 Instant delivery after payment\n"
            "— 🪙 Guaranteed discounted prices\n"
            "— 📞 24/7 admin support\n\n"
            "<b>Tap any button below to begin.</b>"
        )

        keyboard = [
            [InlineKeyboardButton("🛒 Shop Now", callback_data="shop_now")],
            [InlineKeyboardButton("📦 My Orders", callback_data="my_orders"), InlineKeyboardButton("👤 Profile", callback_data="profile")],
            [
                InlineKeyboardButton("💳 Pay Proof", url="https://t.me/+fJrFACSrntgwNjll"),
                InlineKeyboardButton("💬 Support", callback_data="support")
            ],
            [InlineKeyboardButton("ℹ️ How to Use", callback_data="how_to_use")]
        ]

        if user.id == ADMIN_ID:
            keyboard.append([InlineKeyboardButton("👑 Admin Panel", callback_data="admin_panel_home")])

        reply_markup = InlineKeyboardMarkup(keyboard)
        
        if update.message:
            await update.message.reply_text(welcome_text, parse_mode="HTML", reply_markup=reply_markup)
        elif update.callback_query:
            await update.callback_query.answer()
            try:
                await update.callback_query.message.edit_text(welcome_text, parse_mode="HTML", reply_markup=reply_markup)
            except Exception:
                await update.callback_query.message.reply_text(welcome_text, parse_mode="HTML", reply_markup=reply_markup)
    except Exception as e:
        logger.error(f"Error in start_command: {e}")

# ==========================================
# 👑 NO-SLASH BUTTON BASED ADMIN PANEL
# ==========================================
async def admin_panel_home(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if query.from_user.id != ADMIN_ID:
        return

    text = (
        "👑 <b>WELCOME TO ADMIN CONTROL PANEL</b>\n\n"
        " Select an option below to control your shop without typing commands:"
    )
    keyboard = [
        [InlineKeyboardButton("➕ Add Key to Stock", callback_data="admin_opt_addkey"), InlineKeyboardButton("🔑 View Stock", callback_data="admin_opt_viewkeys")],
        [InlineKeyboardButton("🗑️ Clear Stock", callback_data="admin_opt_clearstock"), InlineKeyboardButton("💵 Set Product Price", callback_data="admin_opt_setprice")],
        [InlineKeyboardButton("🛠️ Maintenance Mode", callback_data="admin_opt_maintain"), InlineKeyboardButton("🔗 Add Download Link", callback_data="admin_opt_addlink")],
        [InlineKeyboardButton("📢 Broadcast Message", callback_data="admin_opt_broadcast")],
        [InlineKeyboardButton("🔙 Back to Main Menu", callback_data="main_menu")]
    ]
    await query.message.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))

async def admin_option_click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if query.from_user.id != ADMIN_ID:
        return

    cb = query.data

    if cb == "admin_opt_addkey":
        context.user_data['admin_flow'] = 'WAITING_ADDKEY'
        msg = (
            "➕ <b>ADD KEYS TO STOCK</b>\n\n"
            "Please send the details in this exact format:\n"
            "<code>prod_key plan_name key1, key2, key3</code>\n\n"
            "<b>Example:</b>\n"
            "<code>bala_mod 1_Day ABC123KEY, XYZ456KEY</code>"
        )
    elif cb == "admin_opt_viewkeys":
        context.user_data['admin_flow'] = 'WAITING_VIEWKEYS'
        msg = (
            "🔑 <b>VIEW STOCK KEYS</b>\n\n"
            "Please send product key and plan:\n"
            "<code>prod_key plan_name</code>\n\n"
            "<b>Example:</b>\n"
            "<code>bala_mod 1_Day</code>"
        )
    elif cb == "admin_opt_clearstock":
        context.user_data['admin_flow'] = 'WAITING_CLEARSTOCK'
        msg = (
            "🗑️ <b>CLEAR STOCK KEYS</b>\n\n"
            "Please send product key and plan to clear:\n"
            "<code>prod_key plan_name</code>\n\n"
            "<b>Example:</b>\n"
            "<code>bala_mod 1_Day</code>"
        )
    elif cb == "admin_opt_setprice":
        context.user_data['admin_flow'] = 'WAITING_SETPRICE'
        msg = (
            "💵 <b>SET PRODUCT PRICE</b>\n\n"
            "Please send details:\n"
            "<code>prod_key plan_name new_price</code>\n\n"
            "<b>Example:</b>\n"
            "<code>bala_mod 1_Day 400</code>"
        )
    elif cb == "admin_opt_maintain":
        context.user_data['admin_flow'] = 'WAITING_MAINTAIN'
        msg = (
            "🛠️ <b>TOGGLE MAINTENANCE MODE</b>\n\n"
            "Please send details:\n"
            "<code>prod_key on/off</code>\n\n"
            "<b>Example:</b>\n"
            "<code>bala_mod on</code>"
        )
    elif cb == "admin_opt_addlink":
        context.user_data['admin_flow'] = 'WAITING_ADDLINK'
        msg = (
            "🔗 <b>ADD DOWNLOAD LINK</b>\n\n"
            "Please send details:\n"
            "<code>prod_key https://download-link.com</code>"
        )
    elif cb == "admin_opt_broadcast":
        context.user_data['admin_flow'] = 'WAITING_BROADCAST'
        msg = "📢 <b>BROADCAST MESSAGE</b>\n\nSend the text or photo message you want to broadcast to all users."

    keyboard = [[InlineKeyboardButton("❌ Cancel & Return", callback_data="admin_panel_home")]]
    await query.message.edit_text(msg, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))

# ==========================================
# 👤 PROFILE & ORDERS HANDLERS
# ==========================================
async def profile_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = update.effective_user
    db_data = db_get_user(user.id)
    
    name = db_data[0] if db_data else user.full_name
    uname = db_data[1] if db_data else (f"@{user.username}" if user.username else "N/A")
    joined = db_data[2] if db_data else get_ist_time()
    orders_cnt = db_data[3] if db_data else 0

    text = (
        "___________________________\n\n"
        "<b>👤 YOUR PROFILE</b>\n"
        "___________________________\n\n"
        f"🛡️ <b>Name:</b> {name}\n"
        f"🔗 <b>Username:</b> {uname}\n"
        f"🆔 <b>User ID:</b> <code>{user.id}</code>\n"
        f"📅 <b>Member Since:</b> {joined}\n"
        f"🪪 <b>Account Type:</b> 🟢 Regular\n"
        f"🛒 <b>Total Orders:</b> {orders_cnt}\n"
        "___________________________"
    )
    keyboard = [
        [InlineKeyboardButton("🛒 Shop Now", callback_data="shop_now"), InlineKeyboardButton("📦 My Orders", callback_data="my_orders")],
        [InlineKeyboardButton("↩️ Back to Menu", callback_data="main_menu")]
    ]
    await query.message.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))

async def my_orders_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = update.effective_user
    history = db_get_user_history(user.id)

    text = "___________________________\n\n<b>🔑 MY ORDERS (Last 5)</b>\n___________________________\n\n"
    if not history:
        text += "No purchase history found yet!\n"
    else:
        for idx, item in enumerate(history, 1):
            text += (
                f"<b>{idx}️⃣ Product:</b> {item[0]}\n"
                f"⏱️ <b>Plan:</b> {item[1]}\n"
                f"🔑 <b>Key:</b> <code>{item[2]}</code>\n"
                f"📅 <b>Date & Time:</b> {item[3]}\n\n"
            )
    text += "___________________________"

    keyboard = [
        [InlineKeyboardButton("🛒 Shop Now", callback_data="shop_now")],
        [InlineKeyboardButton("↩️ Back to Menu", callback_data="main_menu")]
    ]
    await query.message.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))

async def support_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    text = "📩 <b>Contact support:</b> @Athulsudin"
    keyboard = [[InlineKeyboardButton("🔙 Back", callback_data="main_menu")]]
    await query.message.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))

async def how_to_use_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    text = (
        "═══════════════════════\n"
        "📖 <b>HOW TO USE — FF SERVICES SHOP</b>\n"
        "═══════════════════════\n\n"
        "1️⃣ Tap <b>🛒 Shop Now</b> to view the store.\n"
        "2️⃣ Choose your product category.\n"
        "3️⃣ Pick your desired product and duration.\n"
        "4️⃣ Scan the UPI QR Code provided.\n"
        "5️⃣ Pay the exact dynamic total amount shown.\n"
        "6️⃣ Tap <b>[ VERIFY PAYMENT ]</b> button after paying.\n"
        "7️⃣ System auto-verifies payment & key is delivered instantly! 🚀"
    )
    keyboard = [
        [InlineKeyboardButton("🎬 Watch Tutorial Video", url="https://t.me/chatelitehackers")],
        [InlineKeyboardButton("🔙 Back", callback_data="main_menu")]
    ]
    await query.message.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))

# ==========================================
# 🛒 STORE NAVIGATION & CATEGORIES
# ==========================================
async def store_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    text = "<b>🛒 SELECT STORE CATEGORY:</b>"
    keyboard = [
        [InlineKeyboardButton("🔥 Free Fire Panel Services", callback_data="cat_panels")],
        [InlineKeyboardButton("💎 Free Fire Like Services", callback_data="cat_likes")],
        [InlineKeyboardButton("🏠 Main Menu", callback_data="main_menu")]
    ]
    await query.message.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))

async def category_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    text = "<b>📱 SELECT PANEL CATEGORY:</b>"
    keyboard = [
        [InlineKeyboardButton("📱 Non-Root Panels", callback_data="non_root_list")],
        [InlineKeyboardButton("⚡ Root Panels", callback_data="root_list")],
        [InlineKeyboardButton("🍏 iOS Panels", callback_data="ios_list")],
        [InlineKeyboardButton("💻 PC Panels", callback_data="pc_list")],
        [InlineKeyboardButton("🔙 Back to Shop", callback_data="shop_now")]
    ]
    await query.message.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))

async def likes_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    text = "<b>💎 FREE FIRE LIKE SERVICES:</b>"
    keyboard = [[InlineKeyboardButton(f"👍 {data['name']}", callback_data=f"prod_likes_{key}")] for key, data in LIKE_PRODUCTS.items()]
    keyboard.append([InlineKeyboardButton("🔙 Back to Shop", callback_data="shop_now")])
    await query.message.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))

async def non_root_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    text = "<b>📱 NON-ROOT PANELS:</b>"
    keyboard = [[InlineKeyboardButton(f"⚙️ {data['name']}", callback_data=f"prod_nonroot_{key}")] for key, data in NON_ROOT_PRODUCTS.items()]
    keyboard.append([InlineKeyboardButton("🔙 Back to Categories", callback_data="cat_panels")])
    await query.message.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))

async def root_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    text = "<b>⚡ ROOT PANELS:</b>"
    keyboard = [[InlineKeyboardButton(f"⚡ {data['name']}", callback_data=f"prod_root_{key}")] for key, data in ROOT_PRODUCTS.items()]
    keyboard.append([InlineKeyboardButton("🔙 Back to Categories", callback_data="cat_panels")])
    await query.message.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))

async def ios_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    text = "<b>🍏 IOS PANELS:</b>"
    keyboard = [[InlineKeyboardButton(f"🍏 {data['name']}", callback_data=f"prod_ios_{key}")] for key, data in IOS_PRODUCTS.items()]
    keyboard.append([InlineKeyboardButton("🔙 Back to Categories", callback_data="cat_panels")])
    await query.message.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))

async def pc_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    text = "<b>💻 PC PANELS:</b>"
    keyboard = [[InlineKeyboardButton(f"💻 {data['name']}", callback_data=f"prod_pc_{key}")] for key, data in PC_PRODUCTS.items()]
    keyboard.append([InlineKeyboardButton("🔙 Back to Categories", callback_data="cat_panels")])
    await query.message.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))

# ==========================================
# 🏷️ PRODUCTS & PRICES
# ==========================================
async def show_product_prices(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    cb_data = query.data

    if cb_data.startswith("prod_nonroot_"):
        prod_type, prod_key, back_target = "nonroot", cb_data.replace("prod_nonroot_", ""), "non_root_list"
    elif cb_data.startswith("prod_root_"):
        prod_type, prod_key, back_target = "root", cb_data.replace("prod_root_", ""), "root_list"
    elif cb_data.startswith("prod_ios_"):
        prod_type, prod_key, back_target = "ios", cb_data.replace("prod_ios_", ""), "ios_list"
    elif cb_data.startswith("prod_pc_"):
        prod_type, prod_key, back_target = "pc", cb_data.replace("prod_pc_", ""), "pc_list"
    else:
        prod_type, prod_key, back_target = "likes", cb_data.replace("prod_likes_", ""), "cat_likes"

    prod = get_product_by_key(prod_key)
    if not prod:
        return

    if MAINTENANCE_MODE.get(prod_key, False):
        m_text = (
            "<b>═══════════════════════</b>\n"
            "<b>⚠️ UNDER MAINTENANCE</b>\n"
            "<b>═══════════════════════</b>\n\n"
            f"🛠️ <b>{prod['name']}</b> is currently under maintenance!\n"
            "⏳ Please check back after some time."
        )
        keyboard = [[InlineKeyboardButton("🔙 Back", callback_data=back_target)]]
        await query.message.edit_text(m_text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))
        return

    lines = ["<b>═══════════════════════</b>", f"<b>🛒 {prod['name']}</b>", "<b>═══════════════════════</b>\n", "🔥 <b>Choose a plan:</b>\n"]
    keyboard = []
    
    for plan, price in prod["prices"]:
        btn_text = f"{plan.replace('_', ' ')} — ₹{price}"
        cb = f"plan_{prod_type}_{prod_key}_{plan}_{price}"
        
        lines.append(f"• {btn_text}")
        keyboard.append([InlineKeyboardButton(btn_text, callback_data=cb)])

    if prod_key in PRODUCT_LINKS:
        keyboard.append([InlineKeyboardButton("📥 Download File/Apk", url=PRODUCT_LINKS[prod_key])])

    keyboard.append([InlineKeyboardButton("🔙 Back", callback_data=back_target)])
    await query.message.edit_text("\n".join(lines), parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))

# ==========================================
# 📋 ORDER SUMMARY & DYNAMIC QR PAYMENT
# ==========================================
async def order_summary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    data_str = query.data.replace("plan_", "")
    parts = data_str.split("_")
    prod_type = parts[0]
    prod_key = parts[1]
    base_price = float(parts[-1])
    plan = "_".join(parts[2:-1])

    # Dynamic decimal amount addition
    random_paisa = round(random.randint(1, 99) / 100.0, 2)
    final_price = round(base_price + random_paisa, 2)

    prod = get_product_by_key(prod_key)
    prod_name = prod['name'] if prod else prod_key

    text = (
        "<b>═══════════════════════</b>\n"
        "<b>📋 ORDER SUMMARY</b>\n"
        "<b>═══════════════════════</b>\n\n"
        f"🔑 <b>Product:</b> {prod_name}\n"
        f"📄 <b>Plan:</b> {plan.replace('_', ' ')}\n"
        f"💵 <b>Base Price:</b> ₹{base_price:.2f}\n"
        "_______________________\n\n"
        f"💰 <b>Final Dynamic Total:</b> ₹{final_price:.2f}\n"
        "<i>(Unique paisa added for instant automatic verification)</i>"
    )

    context.user_data['pending_order'] = {
        'prod_type': prod_type, 
        'prod_key': prod_key, 
        'prod_name': prod_name, 
        'plan': plan, 
        'price': final_price
    }
    keyboard = [
        [InlineKeyboardButton("✅ Confirm & Pay", callback_data="confirm_pay")],
        [InlineKeyboardButton("🔙 Back to Plans", callback_data=f"prod_{prod_type}_{prod_key}")]
    ]
    await query.message.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))

# ==========================================
# ⏳ DYNAMIC LIVE COUNTDOWN PAYMENT SYSTEM
# ==========================================
async def confirm_pay(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    order = context.user_data.get('pending_order')
    if not order:
        return

    formatted_price = format_amt_simple(order['price'])
    qr_image_url = generate_dynamic_qr_url(RECEIVER_UPI_ID, order['price'], f"Order_{order['prod_key']}")

    context.user_data['order_cancelled'] = False
    context.user_data['payment_complete'] = False

    def get_caption(seconds):
        m, s = divmod(max(0, seconds), 60)
        return (
            "👇 <b>Merchant Name: ELITE HACKERS</b>\n\n"
            f"💰 <b>Scan & pay exactly 🤑 ₹{formatted_price}</b>\n\n"
            "<b>Tap verify below after completing payment.</b>\n\n"
            f"<b>BUY | Session expires in {m:02d}:{s:02d} minutes.</b>"
        )

    keyboard = [
        [InlineKeyboardButton("VERIFY PAYMENT", callback_data="verify_payment_btn")],
        [InlineKeyboardButton("➡️ Cancel Order", callback_data="cancel_order")]
    ]

    try:
        await query.message.delete()
    except Exception:
        pass

    sent_msg = await context.bot.send_photo(
        chat_id=query.message.chat_id,
        photo=qr_image_url,
        caption=get_caption(300),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    context.user_data['qr_msg_id'] = sent_msg.message_id

    # 5-Second Live Timer Loop
    async def timer_loop():
        msg_id = sent_msg.message_id
        chat_id = sent_msg.chat_id
        
        for remaining in range(300, 0, -5):
            await asyncio.sleep(5)
            if context.user_data.get('order_cancelled') or context.user_data.get('payment_complete'):
                break
            try:
                await context.bot.edit_message_caption(
                    chat_id=chat_id,
                    message_id=msg_id,
                    caption=get_caption(remaining - 5),
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )
            except Exception:
                break
        
        if not context.user_data.get('order_cancelled') and not context.user_data.get('payment_complete'):
            try:
                await context.bot.delete_message(chat_id=chat_id, message_id=msg_id)
                start_btn = [[InlineKeyboardButton("🔄 Click /start to Restart", callback_data="main_menu")]]
                await context.bot.send_message(
                    chat_id=chat_id,
                    text="❌ <b>Order Session Expired!</b>\n\nPlease click the button below to restart the bot.",
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup(start_btn)
                )
            except Exception:
                pass

    asyncio.create_task(timer_loop())

async def verify_payment_btn_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("Checking payment status...", show_alert=False)
    user = update.effective_user
    order = context.user_data.get('pending_order')

    if not order:
        await query.message.reply_text("⚠️ No active order found. Please start over.")
        return

    verifying_msg = await query.message.reply_text("🔄 <b>Verifying your payment automatically...</b>", parse_mode="HTML")

    is_verified, failure_reason = await verify_fampay_gmail_payment(order['price'])

    if is_verified:
        context.user_data['payment_complete'] = True
        
        try:
            await verifying_msg.edit_text("✅ <b>Payment Received & Verified Successfully!</b>", parse_mode="HTML")
            await asyncio.sleep(1)
            await verifying_msg.delete()
            qr_msg_id = context.user_data.get('qr_msg_id')
            if qr_msg_id:
                await context.bot.delete_message(chat_id=query.message.chat_id, message_id=qr_msg_id)
        except Exception:
            pass

        prod_key = order['prod_key']
        plan = order['plan']
        keys_list = KEYS_STOCK.get((prod_key, plan), [])

        if keys_list:
            delivered_key = keys_list.pop(0)
            KEYS_STOCK[(prod_key, plan)] = keys_list
            time_now = get_ist_time()

            db_add_order(user.id, order['prod_name'], plan, delivered_key, order['price'], "AUTO_VERIFIED", time_now)

            cust_text = (
                "<b>═══════════════════════</b>\n"
                "<b>🎉 YOUR ORDER IS READY!</b>\n"
                "<b>═══════════════════════</b>\n\n"
                f"🔮 <b>Product:</b> {order['prod_name']}\n"
                f"⏱️ <b>Duration:</b> {plan.replace('_', ' ')}\n\n"
                "🔑 <b>Key (Tap on Key to Copy):</b>\n"
                f"<code>{delivered_key}</code>\n"
                "<b>═══════════════════════</b>\n"
                "Thank you for shopping with us! 🛍️"
            )
            await context.bot.send_message(chat_id=user.id, text=cust_text, parse_mode="HTML")
            await start_command_for_user(context.bot, user.id)

            admin_text = (
                "🟢 <b>PAYMENT AUTO-VERIFIED & DELIVERED</b>\n\n"
                f"👤 <b>Customer:</b> {user.first_name} (@{user.username if user.username else 'N/A'})\n"
                f"🆔 <b>User ID:</b> <code>{user.id}</code>\n"
                f"🔮 <b>Product:</b> {order['prod_name']}\n"
                f"⏱️ <b>Plan:</b> {plan.replace('_', ' ')}\n"
                f"💰 <b>Amount:</b> ₹{order['price']:.2f}\n"
                f"🔑 <b>Delivered Key:</b> <code>{delivered_key}</code>"
            )
            await context.bot.send_message(chat_id=ADMIN_ID, text=admin_text, parse_mode="HTML")
        else:
            admin_text = (
                "🚨 <b>PAYMENT AUTO-VERIFIED (MANUAL APPROVAL REQUIRED)</b> 🚨\n\n"
                f"👤 <b>Customer:</b> {user.first_name} (@{user.username if user.username else 'N/A'})\n"
                f"🆔 <b>User ID:</b> <code>{user.id}</code>\n"
                f"🔮 <b>Product:</b> {order['prod_name']}\n"
                f"⏱️ <b>Plan:</b> {plan.replace('_', ' ')}\n"
                f"💰 <b>Amount:</b> ₹{order['price']:.2f}\n\n"
                "⚠️ Tap Approve below to type and send key to customer."
            )
            admin_keyboard = [[InlineKeyboardButton("✅ Approve & Send Key", callback_data="admin_approve")]]
            admin_msg = await context.bot.send_message(
                chat_id=ADMIN_ID,
                text=admin_text,
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(admin_keyboard)
            )
            ACTIVE_ORDERS[admin_msg.message_id] = {
                'user_id': user.id,
                'prod_name': order['prod_name'],
                'plan': plan,
                'price': order['price']
            }
            await query.message.reply_text("✅ <b>Payment verified!</b> Your order is being processed by admin, please wait...", parse_mode="HTML")
    else:
        try:
            await verifying_msg.delete()
        except Exception:
            pass
        fail_text = (
            "❌ <b>Payment Not Received Yet!</b>\n\n"
            "Please complete the payment on your UPI App and try tapping <b>VERIFY PAYMENT</b> again."
        )
        await query.message.reply_text(fail_text, parse_mode="HTML")

async def cancel_order(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data['order_cancelled'] = True
    try:
        await query.message.delete()
    except Exception:
        pass
    await start_command(update, context)

# ==========================================
# 📩 MESSAGES & ADMIN FLOW HANDLERS
# ==========================================
async def handle_user_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    if user.id == ADMIN_ID:
        flow = context.user_data.get('admin_flow')
        text = update.message.text.strip() if update.message.text else ""

        if flow == 'WAITING_ADDKEY':
            try:
                parts = text.split(" ")
                prod_key = parts[0]
                plan = parts[1]
                keys = [k.strip() for k in " ".join(parts[2:]).split(",")]
                
                target_tuple = (prod_key, plan)
                if target_tuple not in KEYS_STOCK:
                    KEYS_STOCK[target_tuple] = []
                KEYS_STOCK[target_tuple].extend(keys)
                
                await update.message.reply_text(f"✅ Added <b>{len(keys)} keys</b> to <b>{prod_key}</b> ({plan})!", parse_mode="HTML")
            except Exception as e:
                await update.message.reply_text(f"❌ Invalid format! Error: {e}")
            context.user_data['admin_flow'] = None
            return

        elif flow == 'WAITING_VIEWKEYS':
            try:
                parts = text.split(" ")
                prod_key = parts[0]
                plan = parts[1]
                keys = KEYS_STOCK.get((prod_key, plan), [])
                if not keys:
                    await update.message.reply_text(f"⚠️ No keys in stock for <b>{prod_key}</b> ({plan})!", parse_mode="HTML")
                else:
                    keys_text = "\n".join([f"• <code>{k}</code>" for k in keys])
                    await update.message.reply_text(f"🔑 <b>Available Keys ({prod_key} - {plan}):</b>\n\n{keys_text}", parse_mode="HTML")
            except Exception as e:
                await update.message.reply_text(f"❌ Invalid format! Error: {e}")
            context.user_data['admin_flow'] = None
            return

        elif flow == 'WAITING_CLEARSTOCK':
            try:
                parts = text.split(" ")
                prod_key = parts[0]
                plan = parts[1]
                KEYS_STOCK[(prod_key, plan)] = []
                await update.message.reply_text(f"✅ Cleared stock for <b>{prod_key}</b> ({plan})!", parse_mode="HTML")
            except Exception as e:
                await update.message.reply_text(f"❌ Invalid format! Error: {e}")
            context.user_data['admin_flow'] = None
            return

        elif flow == 'WAITING_SETPRICE':
            try:
                parts = text.split(" ")
                prod_key = parts[0]
                plan = parts[1]
                new_price = float(parts[2])
                prod = get_product_by_key(prod_key)
                if prod:
                    for idx, (p_name, p_price) in enumerate(prod["prices"]):
                        if p_name.lower() == plan.lower():
                            prod["prices"][idx] = (p_name, new_price)
                            await update.message.reply_text(f"✅ Price updated for <b>{prod_key}</b> ({p_name}) to <b>₹{new_price}</b>!", parse_mode="HTML")
                            break
            except Exception as e:
                await update.message.reply_text(f"❌ Invalid format! Error: {e}")
            context.user_data['admin_flow'] = None
            return

        elif flow == 'WAITING_MAINTAIN':
            try:
                parts = text.split(" ")
                prod_key = parts[0]
                status = parts[1].lower()
                MAINTENANCE_MODE[prod_key] = (status == "on")
                st_str = "ENABLED 🛠️" if MAINTENANCE_MODE[prod_key] else "DISABLED ✅"
                await update.message.reply_text(f"Maintenance mode for <b>{prod_key}</b> is now <b>{st_str}</b>!", parse_mode="HTML")
            except Exception as e:
                await update.message.reply_text(f"❌ Invalid format! Error: {e}")
            context.user_data['admin_flow'] = None
            return

        elif flow == 'WAITING_ADDLINK':
            try:
                parts = text.split(" ")
                prod_key = parts[0]
                link_url = parts[1]
                PRODUCT_LINKS[prod_key] = link_url
                await update.message.reply_text(f"✅ Download Link added for <b>{prod_key}</b>:\n{link_url}", parse_mode="HTML")
            except Exception as e:
                await update.message.reply_text(f"❌ Invalid format! Error: {e}")
            context.user_data['admin_flow'] = None
            return

        elif flow == 'WAITING_BROADCAST':
            conn = sqlite3.connect(DB_FILE)
            cursor = conn.cursor()
            cursor.execute('SELECT user_id FROM users')
            users = [row[0] for row in cursor.fetchall()]
            conn.close()

            success = 0
            await update.message.reply_text(f"📢 <b>Broadcast started for {len(users)} users...</b>", parse_mode="HTML")

            if update.message.photo:
                photo_id = update.message.photo[-1].file_id
                caption = update.message.caption or ""
                for u_id in users:
                    try:
                        await context.bot.send_photo(chat_id=u_id, photo=photo_id, caption=caption, parse_mode="HTML")
                        success += 1
                    except Exception:
                        pass
            else:
                for u_id in users:
                    try:
                        await context.bot.send_message(chat_id=u_id, text=text, parse_mode="HTML")
                        success += 1
                    except Exception:
                        pass

            await update.message.reply_text(f"✅ Broadcast completed! Sent to <b>{success}/{len(users)}</b> users.", parse_mode="HTML")
            context.user_data['admin_flow'] = None
            return

        if context.user_data.get('admin_state') == 'AWAITING_KEY':
            target_msg_id = context.user_data.get('active_admin_msg_id')
            order_info = ACTIVE_ORDERS.get(target_msg_id)

            if order_info and text:
                cust_id = order_info['user_id']
                prod_name = order_info['prod_name']
                plan = order_info['plan']
                time_now = get_ist_time()

                db_add_order(cust_id, prod_name, plan, text, order_info['price'], "MANUAL", time_now)

                cust_text = (
                    "<b>═══════════════════════</b>\n"
                    "<b>🎉 YOUR ORDER IS READY!</b>\n"
                    "<b>═══════════════════════</b>\n\n"
                    f"🔮 <b>Product:</b> {prod_name}\n"
                    f"⏱️ <b>Duration:</b> {plan.replace('_', ' ')}\n\n"
                    "🔑 <b>Key (Tap on Key to Copy):</b>\n"
                    f"<code>{text}</code>\n"
                    "<b>═══════════════════════</b>\n"
                    "Thank you for shopping with us! 🛍️"
                )
                await context.bot.send_message(chat_id=cust_id, text=cust_text, parse_mode="HTML")
                await start_command_for_user(context.bot, cust_id)
                await update.message.reply_text("✅ Key sent to customer successfully!")
                context.user_data['admin_state'] = None
                context.user_data['active_admin_msg_id'] = None
            return

    if user.id != ADMIN_ID:
        restart_btn = [[InlineKeyboardButton("🔄 Click /start to Restart", callback_data="main_menu")]]
        await update.message.reply_text(
            "❌ <b>Unknown Command or Message!</b>\n\nPlease restart the bot by clicking 👉 /start",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(restart_btn)
        )

async def start_command_for_user(bot, user_id):
    welcome_text = "<b>Tap any button below to continue shopping:</b>"
    keyboard = [
        [InlineKeyboardButton("🛒 Shop Now", callback_data="shop_now")],
        [InlineKeyboardButton("📦 My Orders", callback_data="my_orders"), InlineKeyboardButton("👤 Profile", callback_data="profile")]
    ]
    try:
        await bot.send_message(chat_id=user_id, text=welcome_text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))
    except Exception:
        pass

async def handle_admin_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.from_user.id != ADMIN_ID:
        return

    admin_msg_id = query.message.message_id
    order_info = ACTIVE_ORDERS.get(admin_msg_id)
    if not order_info:
        return

    cust_id = order_info['user_id']

    if query.data == "admin_approve":
        await context.bot.send_message(chat_id=cust_id, text="⚙️ <b>Order Approved!</b> Generating key...", parse_mode="HTML")
        context.user_data['admin_state'] = 'AWAITING_KEY'
        context.user_data['active_admin_msg_id'] = admin_msg_id
        await query.message.reply_text(f"🔑 <b>Order Approved!</b> Send the <b>KEY</b> for {order_info['prod_name']} ({order_info['plan']}):", parse_mode="HTML")

# ==========================================
# 🤖 BOT SETUP & RUNNER
# ==========================================
def start_bot():
    app = Application.builder().token(BOT_TOKEN).concurrent_updates(False).build()

    app.add_handler(CommandHandler("start", start_command))
    
    app.add_handler(CallbackQueryHandler(start_command, pattern="^main_menu$"))
    app.add_handler(CallbackQueryHandler(admin_panel_home, pattern="^admin_panel_home$"))
    app.add_handler(CallbackQueryHandler(admin_option_click, pattern="^admin_opt_"))
    
    app.add_handler(CallbackQueryHandler(profile_handler, pattern="^profile$"))
    app.add_handler(CallbackQueryHandler(my_orders_handler, pattern="^my_orders$"))
    app.add_handler(CallbackQueryHandler(support_handler, pattern="^support$"))
    app.add_handler(CallbackQueryHandler(how_to_use_handler, pattern="^how_to_use$"))
    app.add_handler(CallbackQueryHandler(store_menu, pattern="^shop_now$"))
    app.add_handler(CallbackQueryHandler(category_selection, pattern="^cat_panels$"))
    app.add_handler(CallbackQueryHandler(likes_list, pattern="^cat_likes$"))
    app.add_handler(CallbackQueryHandler(non_root_list, pattern="^non_root_list$"))
    app.add_handler(CallbackQueryHandler(root_list, pattern="^root_list$"))
    app.add_handler(CallbackQueryHandler(ios_list, pattern="^ios_list$"))
    app.add_handler(CallbackQueryHandler(pc_list, pattern="^pc_list$"))
    app.add_handler(CallbackQueryHandler(show_product_prices, pattern="^prod_"))
    app.add_handler(CallbackQueryHandler(order_summary, pattern="^plan_"))
    app.add_handler(CallbackQueryHandler(confirm_pay, pattern="^confirm_pay$"))
    app.add_handler(CallbackQueryHandler(verify_payment_btn_handler, pattern="^verify_payment_btn$"))
    app.add_handler(CallbackQueryHandler(handle_admin_action, pattern="^admin_approve$"))
    app.add_handler(CallbackQueryHandler(cancel_order, pattern="^cancel_order$"))

    app.add_handler(MessageHandler(filters.TEXT | filters.PHOTO, handle_user_message))

    print("Bot is running...")
    app.run_polling(drop_pending_updates=True)

def main():
    keep_alive()

    while True:
        try:
            start_bot()
        except Exception as e:
            print(f"Crash prevented: {e}. Auto-restarting in 1 second...")
            time.sleep(1)

if __name__ == "__main__":
    main()
