import logging
import asyncio
import os
import time
import json
import re
import sqlite3
from threading import Thread
from datetime import datetime
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

# 🔗 വെബ് അഡ്മിൻ പാനൽ ഫയൽ ഇംപോർട്ട് ചെയ്യുന്നു
import web_admin

# Enable logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# ==========================================
# ⚙️ CONFIGURATION & CONSTANTS
# ==========================================
BOT_TOKEN = "8892856619:AAGZhdOv389_AaKvbcbInlJAiDMOwQxOeHc"
ADMIN_ID = 7616127905

ACTIVE_ORDERS = {}       
MAINTENANCE_MODE = {}    
STOCK_OUT_MODE = {}      
PRODUCT_LINKS = {}       
PRODUCT_ICONS = {}       
KEYS_STOCK = {}          

# Dynamic Store Config Defaults
STORE_CONFIG = {
    "support_username": "@Athulsudin",
    "how_to_use_link": "https://t.me/chatelitehackers",
    "welcome_message": (
        "🚀 <b>Welcome to ELITE HACKERS</b> 🌟\n\n"
        "🥃 Hey! Thanks for reaching out.\n"
        "✉️ Please leave your message, and I'll respond as soon as I'm available.\n\n"
        "⌛ Your patience is greatly appreciated.\n"
        "____________________________________\n\n"
        "🏦 — FREE FIRE PANEL SERVICES — 🏦\n\n"
        "— 🏦 Direct deals with every supplier\n"
        "— 💧 Instant delivery\n"
        "— 🪙 Guaranteed discounted prices\n"
        "— 📞 24/7 admin support\n\n"
        "<b>Tap any button below to begin.</b>"
    )
}

# ==========================================
# 🗄️ SQLITE DATABASE MANAGEMENT
# ==========================================
DB_FILE = "bot_database.db"

def init_db():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            full_name TEXT,
            username TEXT,
            joined_date TEXT,
            orders_count INTEGER DEFAULT 0
        )
    ''')
    
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
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS products (
            prod_key TEXT PRIMARY KEY,
            name TEXT,
            category TEXT,
            prices TEXT,
            download_link TEXT,
            icon TEXT DEFAULT '⚡',
            maintenance INTEGER DEFAULT 0,
            stock_out INTEGER DEFAULT 0,
            channel_link TEXT,
            reseller_price REAL DEFAULT 0,
            remap_id TEXT
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS keys_inventory (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            prod_key TEXT,
            plan TEXT,
            item_key TEXT,
            is_used INTEGER DEFAULT 0
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS store_settings (
            id INTEGER PRIMARY KEY DEFAULT 1,
            support_username TEXT,
            how_to_use_link TEXT,
            welcome_message TEXT
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS upi_settings (
            id INTEGER PRIMARY KEY DEFAULT 1,
            paytm_token TEXT,
            paytm_qr TEXT,
            fampay_token TEXT,
            fampay_qr TEXT
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS api_config (
            id INTEGER PRIMARY KEY DEFAULT 1,
            api_url TEXT,
            api_key TEXT,
            master_key TEXT,
            active_payment_gateway TEXT DEFAULT 'paytm',
            fampay_token TEXT,
            paytm_merchant_id TEXT,
            paytm_upi_id TEXT,
            bot_name TEXT,
            support_username TEXT
        )
    ''')

    conn.commit()
    conn.close()

def load_store_and_upi_settings():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    
    cursor.execute('SELECT support_username, how_to_use_link, welcome_message FROM store_settings WHERE id = 1')
    row = cursor.fetchone()
    if row:
        if row[0]: STORE_CONFIG["support_username"] = row[0]
        if row[1]: STORE_CONFIG["how_to_use_link"] = row[1]
        if row[2]: STORE_CONFIG["welcome_message"] = row[2]
    else:
        cursor.execute('''
            INSERT INTO store_settings (id, support_username, how_to_use_link, welcome_message)
            VALUES (1, ?, ?, ?)
        ''', (STORE_CONFIG["support_username"], STORE_CONFIG["how_to_use_link"], STORE_CONFIG["welcome_message"]))

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

def db_add_keys_to_inventory(prod_key, plan, keys_list):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    for k in keys_list:
        if k.strip():
            cursor.execute('''
                INSERT INTO keys_inventory (prod_key, plan, item_key, is_used)
                VALUES (?, ?, ?, 0)
            ''', (prod_key, plan, k.strip()))
    conn.commit()
    conn.close()

def db_pop_auto_key(prod_key, plan):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('''
        SELECT id, item_key FROM keys_inventory 
        WHERE prod_key = ? AND plan = ? AND is_used = 0 
        ORDER BY id ASC LIMIT 1
    ''', (prod_key, plan))
    row = cursor.fetchone()
    if row:
        key_id, item_key = row
        cursor.execute('UPDATE keys_inventory SET is_used = 1 WHERE id = ?', (key_id,))
        conn.commit()
        conn.close()
        return item_key
    
    conn.close()
    target_tuple = (prod_key, plan)
    if target_tuple in KEYS_STOCK and KEYS_STOCK[target_tuple]:
        return KEYS_STOCK[target_tuple].pop(0)
    return None

def db_get_key_count(prod_key, plan):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('''
        SELECT COUNT(*) FROM keys_inventory 
        WHERE prod_key = ? AND plan = ? AND is_used = 0
    ''', (prod_key, plan))
    count = cursor.fetchone()[0]
    conn.close()
    return count

NON_ROOT_PRODUCTS = {}
ROOT_PRODUCTS = {}
IOS_PRODUCTS = {}
PC_PRODUCTS = {}
LIKE_PRODUCTS = {}
ALL_CATEGORIES = [NON_ROOT_PRODUCTS, ROOT_PRODUCTS, IOS_PRODUCTS, PC_PRODUCTS, LIKE_PRODUCTS]

def load_products_from_db():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('SELECT prod_key, name, category, prices, download_link, icon, maintenance, stock_out FROM products')
    rows = cursor.fetchall()
    conn.close()

    cat_map = {
        "non_root": NON_ROOT_PRODUCTS,
        "root": ROOT_PRODUCTS,
        "ios": IOS_PRODUCTS,
        "pc": PC_PRODUCTS,
        "likes": LIKE_PRODUCTS
    }

    for cat in cat_map.values():
        cat.clear()

    for row in rows:
        p_key, name, category, prices_json, d_link, icon, maint, stockout = row
        prices = json.loads(prices_json)
        p_icon = icon if icon else "⚡"
        if category in cat_map:
            cat_map[category][p_key] = {"name": name, "prices": [tuple(p) for p in prices], "icon": p_icon}
        if d_link:
            PRODUCT_LINKS[p_key] = d_link
        PRODUCT_ICONS[p_key] = p_icon
        MAINTENANCE_MODE[p_key] = bool(maint)
        STOCK_OUT_MODE[p_key] = bool(stockout)

def db_seed_initial_products():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('SELECT count(*) FROM products')
    if cursor.fetchone()[0] == 0:
        initial_data = [
            ("bala_mod", "BALA MOD NON ROOT", "non_root", [("1_Day", 420)], "⚙️"),
            ("rapid_core", "RAPID CORE INJECTOR", "root", [("1_Day", 90)], "⚡"),
            ("migul_pro", "MIGUL PRO IOS", "ios", [("1_Day", 200)], "🍏"),
            ("br_mod_pc", "BR MOD PC", "pc", [("1_Day", 150)], "💻"),
            ("auto_like", "AUTO LIKE EVERY DAY", "likes", [("7_DAYS", 90)], "💎")
        ]
        for key, name, cat, prices, icon in initial_data:
            cursor.execute('''
                INSERT INTO products (prod_key, name, category, prices, download_link, icon, maintenance, stock_out)
                VALUES (?, ?, ?, ?, ?, ?, 0, 0)
            ''', (key, name, cat, json.dumps(prices), "", icon))
        conn.commit()
    conn.close()

init_db()
load_store_and_upi_settings()
db_seed_initial_products()
load_products_from_db()

def sanitize_product_key(raw_name):
    clean = re.sub(r'[^a-zA-Z0-9]', '_', raw_name).strip('_').lower()
    return clean if clean else "product_" + str(int(time.time()))

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

# ==========================================
# 🌐 LAUNCH WEB ADMIN PANEL
# ==========================================
def keep_alive():
    t = Thread(target=web_admin.run_web)
    t.daemon = True
    t.start()

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

        welcome_text = STORE_CONFIG["welcome_message"]

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
            render_url = os.environ.get("RENDER_EXTERNAL_URL", "http://localhost:8080")
            keyboard.append([InlineKeyboardButton("👑 Web Admin Panel", url=render_url)])

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
    supp_user = STORE_CONFIG.get("support_username", "@Athulsudin")
    text = f"📩 <b>Contact support:</b> {supp_user}"
    keyboard = [[InlineKeyboardButton("🔙 Back", callback_data="main_menu")]]
    await query.message.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))

async def how_to_use_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    text = (
        "═══════════════════════\n"
        "📖 <b>HOW TO USE — FF SERVICES SHOP</b>\n"
        "═══════════════════════\n\n"
        "1️⃣ Tap <b>🛒 Shop Now</b> to view store.\n"
        "2️⃣ Choose your product category.\n"
        "3️⃣ Pick your desired product and duration.\n"
        "4️⃣ Confirm your order.\n"
        "5️⃣ Key is delivered instantly! 🚀"
    )
    how_url = STORE_CONFIG.get("how_to_use_link", "https://t.me/chatelitehackers")
    keyboard = [
        [InlineKeyboardButton("🎬 Watch Tutorial Video", url=how_url)],
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
    keyboard = [[InlineKeyboardButton(f"{data.get('icon', '💎') if not data.get('icon', '').startswith('http') else '💎'} {data['name']}", callback_data=f"prod_likes_{key}")] for key, data in LIKE_PRODUCTS.items()]
    keyboard.append([InlineKeyboardButton("🔙 Back to Shop", callback_data="shop_now")])
    await query.message.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))

async def non_root_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    text = "<b>📱 NON-ROOT PANELS:</b>"
    keyboard = [[InlineKeyboardButton(f"{data.get('icon', '⚙️') if not data.get('icon', '').startswith('http') else '⚙️'} {data['name']}", callback_data=f"prod_nonroot_{key}")] for key, data in NON_ROOT_PRODUCTS.items()]
    keyboard.append([InlineKeyboardButton("🔙 Back to Categories", callback_data="cat_panels")])
    await query.message.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))

async def root_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    text = "<b>⚡ ROOT PANELS:</b>"
    keyboard = [[InlineKeyboardButton(f"{data.get('icon', '⚡') if not data.get('icon', '').startswith('http') else '⚡'} {data['name']}", callback_data=f"prod_root_{key}")] for key, data in ROOT_PRODUCTS.items()]
    keyboard.append([InlineKeyboardButton("🔙 Back to Categories", callback_data="cat_panels")])
    await query.message.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))

async def ios_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    text = "<b>🍏 IOS PANELS:</b>"
    keyboard = [[InlineKeyboardButton(f"{data.get('icon', '🍏') if not data.get('icon', '').startswith('http') else '🍏'} {data['name']}", callback_data=f"prod_ios_{key}")] for key, data in IOS_PRODUCTS.items()]
    keyboard.append([InlineKeyboardButton("🔙 Back to Categories", callback_data="cat_panels")])
    await query.message.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))

async def pc_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    text = "<b>💻 PC PANELS:</b>"
    keyboard = [[InlineKeyboardButton(f"{data.get('icon', '💻') if not data.get('icon', '').startswith('http') else '💻'} {data['name']}", callback_data=f"prod_pc_{key}")] for key, data in PC_PRODUCTS.items()]
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

    if STOCK_OUT_MODE.get(prod_key, False):
        s_text = (
            "<b>═══════════════════════</b>\n"
            "<b>❌ OUT OF STOCK</b>\n"
            "<b>═══════════════════════</b>\n\n"
            f"⚠️ <b>{prod['name']}</b> is currently out of stock!\n"
            "⏳ Admin will restock soon."
        )
        keyboard = [[InlineKeyboardButton("🔙 Back", callback_data=back_target)]]
        await query.message.edit_text(s_text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))
        return

    lines = ["<b>═══════════════════════</b>", f"<b>🛒 {prod['name']}</b>", "<b>═══════════════════════</b>\n", "🔥 <b>Choose a plan:</b>\n"]
    keyboard = []
    
    for plan, price in prod["prices"]:
        btn_text = f"{plan.replace('_', ' ')} — ₹{price}"
        cb = f"plan_{prod_type}_{prod_key}_{plan}_{price}"
        
        lines.append(f"• {btn_text}")
        keyboard.append([InlineKeyboardButton(btn_text, callback_data=cb)])

    if prod_key in PRODUCT_LINKS and PRODUCT_LINKS[prod_key]:
        keyboard.append([InlineKeyboardButton("📥 Download File/Apk", url=PRODUCT_LINKS[prod_key])])

    keyboard.append([InlineKeyboardButton("🔙 Back", callback_data=back_target)])
    await query.message.edit_text("\n".join(lines), parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))

# ==========================================
# 📋 ORDER SUMMARY & DIRECT CONFIRMATION
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

    prod = get_product_by_key(prod_key)
    prod_name = prod['name'] if prod else prod_key

    text = (
        "<b>═══════════════════════</b>\n"
        "<b>📋 ORDER SUMMARY</b>\n"
        "<b>═══════════════════════</b>\n\n"
        f"🔑 <b>Product:</b> {prod_name}\n"
        f"📄 <b>Plan:</b> {plan.replace('_', ' ')}\n"
        f"💵 <b>Price:</b> ₹{base_price:.2f}\n"
        "_______________________\n\n"
        "<b>Tap below to confirm your order.</b>"
    )

    context.user_data['pending_order'] = {
        'prod_type': prod_type, 
        'prod_key': prod_key, 
        'prod_name': prod_name, 
        'plan': plan, 
        'price': base_price
    }
    keyboard = [
        [InlineKeyboardButton("✅ Confirm Order", callback_data="confirm_order_btn")],
        [InlineKeyboardButton("🔙 Back to Plans", callback_data=f"prod_{prod_type}_{prod_key}")]
    ]
    await query.message.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))

async def confirm_order_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = update.effective_user
    order = context.user_data.get('pending_order')

    if not order:
        await query.message.reply_text("⚠️ No active order found. Please start over.")
        return

    prod_key = order['prod_key']
    plan = order['plan']
    
    # കസ്റ്റമർക്ക് ഓട്ടോമാറ്റിക് ആയി കീ ലഭ്യമാക്കുന്നു
    delivered_key = db_pop_auto_key(prod_key, plan)

    if delivered_key:
        time_now = get_ist_time()
        db_add_order(user.id, order['prod_name'], plan, delivered_key, order['price'], "CONFIRMED", time_now)

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
        await query.message.edit_text(cust_text, parse_mode="HTML")
        await start_command_for_user(context.bot, user.id)

        admin_text = (
            "🟢 <b>NEW ORDER DELIVERED</b>\n\n"
            f"👤 <b>Customer:</b> {user.first_name} (@{user.username if user.username else 'N/A'})\n"
            f"🆔 <b>User ID:</b> <code>{user.id}</code>\n"
            f"🔮 <b>Product:</b> {order['prod_name']}\n"
            f"⏱️ <b>Plan:</b> {plan.replace('_', ' ')}\n"
            f"💰 <b>Amount:</b> ₹{order['price']:.2f}\n"
            f"🔑 <b>Delivered Key:</b> <code>{delivered_key}</code>"
        )
        try:
            await context.bot.send_message(chat_id=ADMIN_ID, text=admin_text, parse_mode="HTML")
        except Exception:
            pass
    else:
        admin_text = (
            "🚨 <b>NEW ORDER (MANUAL APPROVAL REQUIRED - NO KEYS IN STOCK)</b> 🚨\n\n"
            f"👤 <b>Customer:</b> {user.first_name} (@{user.username if user.username else 'N/A'})\n"
            f"🆔 <b>User ID:</b> <code>{user.id}</code>\n"
            f"🔮 <b>Product:</b> {order['prod_name']}\n"
            f"⏱️ <b>Plan:</b> {plan.replace('_', ' ')}\n"
            f"💰 <b>Amount:</b> ₹{order['price']:.2f}\n\n"
            "⚠️ Tap Approve below to send key to customer."
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
        await query.message.edit_text("✅ <b>Order Placed!</b> Your order is being processed by admin, please wait...", parse_mode="HTML")

# ==========================================
# 📩 MESSAGES & KEY DISPATCH HANDLERS
# ==========================================
async def handle_user_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    text = update.message.text.strip() if update.message.text else ""

    if user.id == ADMIN_ID and context.user_data.get('admin_state') == 'AWAITING_KEY':
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

    restart_btn = [[InlineKeyboardButton("🔄 Click /start to Restart", callback_data="main_menu")]]
    await update.message.reply_text(
        "❌ <b>Unknown Command or Message!</b>\n\nPlease restart bot by clicking 👉 /start",
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
    app.add_handler(CallbackQueryHandler(confirm_order_handler, pattern="^confirm_order_btn$"))
    app.add_handler(CallbackQueryHandler(handle_admin_action, pattern="^admin_approve$"))

    app.add_handler(MessageHandler(filters.TEXT | filters.PHOTO | filters.VIDEO | filters.VOICE, handle_user_message))

    print("🤖 Telegram Bot is running...")
    app.run_polling(drop_pending_updates=True)

def main():
    # 🌐 വെബ് അഡ്മിൻ പാനൽ ബാക്ക്ഗ്രൗണ്ടിൽ ഓൺ ആക്കുന്നു
    print("🌐 Launching Web Admin Panel in Background Thread...")
    keep_alive()

    # 🤖 ടെലിഗ്രാം ബോട്ട് ലൂപ്പ് റൺ ചെയ്യുന്നു
    while True:
        try:
            start_bot()
        except Exception as e:
            print(f"Crash prevented: {e}. Auto-restarting in 1 second...")
            time.sleep(1)

if __name__ == "__main__":
    main()






