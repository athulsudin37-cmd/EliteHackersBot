import logging
import asyncio
import os
import time
import json
import imaplib
import email
import re
import random
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
# ⚙️ BOT CONFIGURATION & DATA STORAGE
# ==========================================
BOT_TOKEN = "8892856619:AAGZhdOv389_AaKvbcbInlJAiDMOwQxOeHc"
ADMIN_ID = 7616127905
RECEIVER_UPI_ID = "9544113089@fam"
GMAIL_USER = "athulsudin37@gmail.com"  
GMAIL_APP_PASS = "rxks jltg unqu gche"             

ACTIVE_ORDERS = {}       # admin_msg_id -> order_data
USERS_DATA = {}          # user_id -> {'name': ..., 'username': ..., 'joined': ..., 'orders_count': ..., 'history': []}
MAINTENANCE_MODE = {}    # prod_key -> True/False
PRODUCT_LINKS = {}       # prod_key -> download_url_string

# Key Management & Security Data Storage
KEYS_STOCK = {}          # (prod_key, plan_name) -> list of key strings
USED_UTRS = set()        # Used UTR list for double-spending protection

# Products Data
NON_ROOT_PRODUCTS = {
    "bala_mod": {
        "name": "BALA MOD NON ROOT",
        "prices": [("1 Hour", 45), ("2 Hour", 85), ("4 Hour", 150), ("6 Hour", 220), ("12 Hour", 300), ("1 Day", 420), ("3 Day", 1050)]
    },
    "tm_pannel": {
        "name": "TM PANNEL NON ROOT",
        "prices": [("1 Day", 70), ("7 Day", 210), ("15 Day", 310), ("31 Day", 450), ("Lifetime Permanent", 1100)]
    },
    "drip_client": {
        "name": "DRIP CLIENT APK MOD",
        "prices": [("1 Day", 80), ("3 Day", 140), ("7 Day", 250), ("15 Day", 360), ("31 Day", 500)]
    },
    "prime_hook": {
        "name": "PRIME HOOK APK MOD",
        "prices": [("1 Day", 80), ("3 Day", 170), ("7 Day", 320), ("10 Day", 420)]
    },
    "hg_cheat": {
        "name": "HG CHEAT APK MOD",
        "prices": [("1 Day", 100), ("7 Day", 230), ("10 Day", 330), ("30 Day", 690)]
    },
    "silent_cheat": {
        "name": "SILENT CHEAT SAFE",
        "prices": [("1 Day", 90), ("3 Day", 190), ("7 Day", 320), ("15 Day", 550), ("30 Day", 830)]
    },
    "drip_proxy": {
        "name": "DRIP CLIENT PROXY",
        "prices": [("1 Day", 65), ("3 Day", 140), ("7 Day", 260), ("31 Day", 650)]
    }
}

ROOT_PRODUCTS = {
    "rapid_core": {
        "name": "RAPID CORE INJECTOR",
        "prices": [("1 Day", 90), ("7 Day", 310), ("15 Day", 470), ("30 Day", 690)]
    },
    "neo_strike": {
        "name": "NEO STRIKE BRUTAL",
        "prices": [("1 Day", 90), ("3 Day", 180), ("7 Day", 310), ("14 Day", 590), ("28 Day", 899)]
    },
    "haxx_cker": {
        "name": "HAXX-CKER PRO",
        "prices": [("10 Day", 550)]
    },
    "xytron_pro": {
        "name": "XYTRON PRO",
        "prices": [("1 Day", 100), ("7 Day", 310), ("15 Day", 550), ("31 Day", 830)]
    },
    "br_mod": {
        "name": "BR MOD INJECTOR",
        "prices": [("1 Day", 90), ("7 Day", 250), ("15 Day", 420), ("31 Day", 570)]
    },
    "angry_mod": {
        "name": "ANGRY MOD",
        "prices": [("1 Day", 70), ("7 Day", 130), ("15 Day", 170), ("31 Day", 290)]
    },
    "xyz_cheats": {
        "name": "XYZ CHEATS",
        "prices": [("1 Day", 80), ("3 Day", 160), ("7 Day", 310), ("15 Day", 520), ("30 Day", 880)]
    }
}

IOS_PRODUCTS = {
    "migul_pro": {
        "name": "MIGUL PRO IOS",
        "prices": [("1 Day", 200), ("7 Day", 480), ("31 Day", 900)]
    },
    "flourite_ios": {
        "name": "FLOURITE IOS",
        "prices": [("1 Day", 270), ("7 Day", 780), ("31 Day", 1600)]
    }
}

PC_PRODUCTS = {
    "br_mod_pc": {
        "name": "BR MOD PC",
        "prices": [("1 Day", 150), ("10 Day", 550), ("31 Day", 900)]
    },
    "internal_pc": {
        "name": "INTERNAL PC",
        "prices": [("1 Day", 99), ("3 Day", 199), ("7 Day", 370), ("15 Day", 650), ("30 Day", 900), ("Lifetime Permanent", 2100)]
    }
}

LIKE_PRODUCTS = {
    "auto_like_everyday": {
        "name": "AUTO LIKE EVERY DAY",
        "prices": [("7 DAYS (220+ Likes/day)", 90), ("15 DAYS (220+ Likes/day)", 160), ("30 DAYS (220+ Likes/day)", 275), ("90 DAYS (220+ Likes/day)", 730)]
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

async def check_email_once(utr, expected_amount):
    def _imap_check():
        try:
            mail = imaplib.IMAP4_SSL("imap.gmail.com")
            mail.login(GMAIL_USER, GMAIL_APP_PASS)
            mail.select("inbox")

            status, messages = mail.search(None, "ALL")
            if status != "OK" or not messages[0]:
                mail.logout()
                return False

            msg_ids = messages[0].split()[-35:]
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

                        if utr in body:
                            amounts = re.findall(r'(?:₹|Rs\.?|INR)\s*(\d+(?:\.\d{1,2})?)', body, re.IGNORECASE) or re.findall(r'(\d+(?:\.\d{1,2})?)', body)
                            for amt in amounts:
                                try:
                                    if abs(float(amt) - float(expected_amount)) < 0.05:
                                        mail.logout()
                                        return True
                                except ValueError:
                                    continue
            mail.logout()
        except Exception as e:
            logger.error(f"Gmail Verification Error: {e}")
        return False

    return await asyncio.to_thread(_imap_check)

async def verify_fampay_gmail_payment(utr, expected_amount, retries=6, delay=5):
    for attempt in range(retries):
        found = await check_email_once(utr, expected_amount)
        if found:
            return True
        if attempt < retries - 1:
            await asyncio.sleep(delay)
    return False

# ==========================================
# 🚀 START & WELCOME MESSAGE
# ==========================================
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user = update.effective_user
        if not user:
            return

        if user.id not in USERS_DATA:
            USERS_DATA[user.id] = {
                'name': user.full_name,
                'username': f"@{user.username}" if user.username else "N/A",
                'joined': get_ist_time(),
                'orders_count': 0,
                'history': []
            }
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
            "❗ I'm currently busy or offline at the moment.\n"
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
    u_data = USERS_DATA.get(user.id, {
        'name': user.full_name,
        'username': f"@{user.username}" if user.username else "N/A",
        'joined': get_ist_time(),
        'orders_count': 0
    })
    
    text = (
        "___________________________\n\n"
        "<b>👤 YOUR PROFILE</b>\n"
        "___________________________\n\n"
        f"🛡️ <b>Name:</b> {u_data['name']}\n"
        f"🔗 <b>Username:</b> {u_data['username']}\n"
        f"🆔 <b>User ID:</b> <code>{user.id}</code>\n"
        f"📅 <b>Member Since:</b> {u_data['joined']}\n"
        f"🪪 <b>Account Type:</b> 🟢 Regular\n"
        f"🛒 <b>Total Orders:</b> {u_data['orders_count']}\n"
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
    u_data = USERS_DATA.get(user.id, {'history': []})
    history = u_data.get('history', [])

    text = "___________________________\n\n<b>🔑 MY ORDERS (Last 5)</b>\n___________________________\n\n"
    if not history:
        text += "No purchase history found yet!\n"
    else:
        for idx, item in enumerate(reversed(history[-5:]), 1):
            text += (
                f"<b>{idx}️⃣ Product:</b> {item['prod_name']}\n"
                f"⏱️ <b>Plan:</b> {item['plan']}\n"
                f"🔑 <b>Key:</b> <code>{item['key']}</code>\n"
                f"📅 <b>Date & Time:</b> {item['time']}\n\n"
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
        "Here is how you can purchase from our bot:\n\n"
        "1️⃣ Tap <b>🛒 Shop Now</b> to view the store.\n"
        "2️⃣ Choose your product category.\n"
        "3️⃣ Pick your desired product and duration.\n"
        "4️⃣ Scan the Automatic Dynamic UPI QR provided.\n"
        "5️⃣ Amount will automatically pre-fill with unique decimal paisa!\n"
        "6️⃣ Pay the exact amount and type your 12-digit UTR in chat.\n"
        "7️⃣ Payment will auto-verify and key is delivered instantly! 🚀\n\n"
        "🎬 <b>Watch full tutorial video below:</b>"
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
        btn_text = f"{plan} — ₹{price}"
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

    # Dynamic decimal amount addition (Feature #2)
    random_paisa = round(random.randint(1, 99) / 100.0, 2)
    final_price = round(base_price + random_paisa, 2)

    prod = get_product_by_key(prod_key)
    prod_name = prod['name'] if prod else prod_key

    text = (
        "<b>═══════════════════════</b>\n"
        "<b>📋 ORDER SUMMARY</b>\n"
        "<b>═══════════════════════</b>\n\n"
        f"🔑 <b>Product:</b> {prod_name}\n"
        f"📄 <b>Plan:</b> {plan}\n"
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

async def confirm_pay(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    order = context.user_data.get('pending_order')
    if not order:
        return

    formatted_price = format_amt_simple(order['price'])
    qr_image_url = generate_dynamic_qr_url(RECEIVER_UPI_ID, order['price'], f"Order_{order['prod_key']}")
    back_target = f"prod_{order['prod_type']}_{order['prod_key']}"

    context.user_data['timer_seconds'] = 300
    context.user_data['order_cancelled'] = False
    context.user_data['state'] = 'WAITING_UTR'

    def get_caption(seconds):
        m, s = divmod(max(0, seconds), 60)
        return (
            "<b>═══════════════════════</b>\n"
            "<b>💼 ORDER CREATED</b>\n"
            "<b>═══════════════════════</b>\n\n"
            f"🔮 <b>Product:</b> {order['prod_name']}\n"
            f"⏲️ <b>Duration:</b> {order['plan']}\n"
            f"💰 <b>Amount:</b> ₹{formatted_price}\n\n"
            f"📲 <b>Scan Dynamic QR Code above to pay!</b>\n"
            f"<i>(Please pay the EXACT amount with paisa!)</i>\n\n"
            f"👇 <b>PLEASE TYPE YOUR 12-DIGIT UTR NUMBER BELOW AFTER PAYMENT:</b>\n\n"
            f"⏳ <b>Expires in: {m:02d}:{s:02d} minutes</b>\n"
            "<b>═══════════════════════</b>"
        )

    keyboard = [
        [InlineKeyboardButton("🔄 Retry / Enter UTR Again", callback_data="retry_utr")], # Feature #3
        [InlineKeyboardButton("❌ Cancel Order", callback_data="cancel_order")],
        [InlineKeyboardButton("🔙 Back to Shop", callback_data=back_target)]
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

    async def timer_loop():
        msg_id = sent_msg.message_id
        chat_id = sent_msg.chat_id
        for remaining in range(300, 0, -20):
            await asyncio.sleep(20)
            if context.user_data.get('order_cancelled') or context.user_data.get('payment_complete'):
                break
            try:
                await context.bot.edit_message_caption(
                    chat_id=chat_id,
                    message_id=msg_id,
                    caption=get_caption(remaining - 20),
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

async def retry_utr_handler(update: Update, context: ContextTypes.DEFAULT_TYPE): # Feature #3 Implementation
    query = update.callback_query
    await query.answer()
    context.user_data['state'] = 'WAITING_UTR'
    await query.message.reply_text("✍️ <b>Please type your 12-digit UTR number again below:</b>", parse_mode="HTML")

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
# 📩 MESSAGES & AUTOMATIC PAYMENT VERIFICATION
# ==========================================
async def handle_user_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = context.user_data.get('state')
    user = update.effective_user

    # Admin Broadcast with Photo + Text
    if user.id == ADMIN_ID and update.message and (update.message.caption or update.message.text):
        msg_text = update.message.caption or update.message.text
        if msg_text.startswith("/broadcast"):
            await cmd_broadcast(update, context)
            return

    # Admin Manual Key Entry
    if user.id == ADMIN_ID and context.user_data.get('admin_state') == 'AWAITING_KEY':
        key_text = update.message.text.strip() if update.message.text else ""
        target_msg_id = context.user_data.get('active_admin_msg_id')
        order_info = ACTIVE_ORDERS.get(target_msg_id)

        if order_info and key_text:
            cust_id = order_info['user_id']
            prod_name = order_info['prod_name']
            plan = order_info['plan']
            time_now = get_ist_time()

            if cust_id in USERS_DATA:
                USERS_DATA[cust_id]['orders_count'] += 1
                USERS_DATA[cust_id]['history'].append({
                    'prod_name': prod_name,
                    'plan': plan,
                    'key': key_text,
                    'time': time_now
                })

            cust_text = (
                "<b>═══════════════════════</b>\n"
                "<b>🎉 YOUR ORDER IS READY!</b>\n"
                "<b>═══════════════════════</b>\n\n"
                f"🔮 <b>Product:</b> {prod_name}\n"
                f"⏱️ <b>Duration:</b> {plan}\n\n"
                "🔑 <b>Key (Tap on Key to Copy):</b>\n"
                f"<code>{key_text}</code>\n"
                "<b>═══════════════════════</b>\n"
                "Thank you for shopping with us! 🛍️\n"
                "We hope to see you again soon."
            )
            await context.bot.send_message(chat_id=cust_id, text=cust_text, parse_mode="HTML")
            await start_command_for_user(context.bot, cust_id)
            await update.message.reply_text("✅ Key sent to customer successfully!")
            context.user_data['admin_state'] = None
            context.user_data['active_admin_msg_id'] = None
        return

    # UTR Check
    if state == 'WAITING_UTR':
        if not update.message.text:
            await update.message.reply_text("⚠️ Please send a valid 12-digit UTR text number!")
            return

        utr = update.message.text.strip()
        if not utr.isdigit() or len(utr) != 12:
            retry_btn = [[InlineKeyboardButton("🔄 Retry / Enter UTR Again", callback_data="retry_utr")]]
            await update.message.reply_text(
                "⚠️ <b>Invalid UTR Format!</b> UTR must be exactly <b>12 digits</b> (numbers only). Please try again:", 
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(retry_btn)
            )
            return

        # Double Spending Permanent Check
        if utr in USED_UTRS:
            await update.message.reply_text("❌ <b>This UTR has already been used!</b> Double spending is strictly prohibited.", parse_mode="HTML")
            return

        order = context.user_data.get('pending_order')
        if not order:
            await update.message.reply_text("⚠️ No active order session found. Please start again.")
            return

        verifying_msg = await update.message.reply_text("🔄 <b>Verifying your payment automatically with bank...</b>", parse_mode="HTML")

        is_verified = await verify_fampay_gmail_payment(utr, order['price'])

        if is_verified:
            USED_UTRS.add(utr) # Lock UTR permanently
            context.user_data['payment_complete'] = True
            
            try:
                await verifying_msg.edit_text("✅ <b>Payment Received & Verified Successfully!</b>", parse_mode="HTML")
                await asyncio.sleep(2)
                await verifying_msg.delete()
                qr_msg_id = context.user_data.get('qr_msg_id')
                if qr_msg_id:
                    await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=qr_msg_id)
            except Exception:
                pass

            prod_key = order['prod_key']
            plan = order['plan']
            keys_list = KEYS_STOCK.get((prod_key, plan), [])

            if keys_list:
                delivered_key = keys_list.pop(0)
                KEYS_STOCK[(prod_key, plan)] = keys_list
                time_now = get_ist_time()

                if user.id in USERS_DATA:
                    USERS_DATA[user.id]['orders_count'] += 1
                    USERS_DATA[user.id]['history'].append({
                        'prod_name': order['prod_name'],
                        'plan': plan,
                        'key': delivered_key,
                        'time': time_now
                    })

                cust_text = (
                    "<b>═══════════════════════</b>\n"
                    "<b>🎉 YOUR ORDER IS READY!</b>\n"
                    "<b>═══════════════════════</b>\n\n"
                    f"🔮 <b>Product:</b> {order['prod_name']}\n"
                    f"⏱️ <b>Duration:</b> {plan}\n\n"
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
                    f"⏱️ <b>Plan:</b> {plan}\n"
                    f"💰 <b>Amount:</b> ₹{order['price']:.2f}\n"
                    f"🔢 <b>UTR:</b> <code>{utr}</code>\n"
                    f"🔑 <b>Delivered Key:</b> <code>{delivered_key}</code>"
                )
                await context.bot.send_message(chat_id=ADMIN_ID, text=admin_text, parse_mode="HTML")
            else:
                admin_text = (
                    "🚨 <b>PAYMENT AUTO-VERIFIED (MANUAL APPROVAL REQUIRED)</b> 🚨\n\n"
                    f"👤 <b>Customer:</b> {user.first_name} (@{user.username if user.username else 'N/A'})\n"
                    f"🆔 <b>User ID:</b> <code>{user.id}</code>\n"
                    f"🔮 <b>Product:</b> {order['prod_name']}\n"
                    f"⏱️ <b>Plan:</b> {plan}\n"
                    f"💰 <b>Amount:</b> ₹{order['price']:.2f}\n"
                    f"🔢 <b>UTR:</b> <code>{utr}</code>\n\n"
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
                    'price': order['price'],
                    'utr': utr
                }
                await update.message.reply_text("✅ <b>Payment verified!</b> Your order is being processed by admin, please wait...", parse_mode="HTML")

        else:
            try:
                await verifying_msg.delete()
            except Exception:
                pass
            retry_keyboard = [
                [InlineKeyboardButton("🔄 Retry / Enter UTR Again", callback_data="retry_utr")],
                [InlineKeyboardButton("💬 Contact Support", callback_data="support")]
            ]
            await update.message.reply_text(
                "❌ <b>Payment Verification Failed!</b> UTR or Amount mismatch in bank statement. Please try again or tap retry button below.",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(retry_keyboard)
            )

        context.user_data['state'] = None
        return

    # Customer fallback message
    if user.id != ADMIN_ID:
        restart_btn = [[InlineKeyboardButton("🔄 Click /start to Restart", callback_data="main_menu")]]
        await update.message.reply_text(
            "❌ <b>Unknown Command or Message!</b>\n\n"
            "Please restart the bot by clicking 👉 /start",
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
# 🛠️ ADMIN CONTROLS (COMMANDS & HELP)
# ==========================================
async def cmd_admin_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    help_text = (
        "<b>👑 ADMIN CONTROL COMMANDS MENU & TUTORIAL</b>\n\n"
        "🛠️ <b>1. MAINTENANCE MODE</b>\n"
        "• `/maintain <prod_key> <on/off>`\n"
        "👉 <i>Ex: `/maintain bala_mod on`</i> (Disable for users)\n\n"
        "📦 <b>2. STOCK OUT / ADD KEYS</b>\n"
        "• Add: `/addkey <prod_key> <plan_name> <key1, key2...>`\n"
        "👉 <i>Ex: `/addkey bala_mod 1_Day KEY1, KEY2`</i>\n"
        "• Clear (Stock Out): `/clearstock <prod_key> <plan_name>`\n"
        "👉 <i>Ex: `/clearstock bala_mod 1_Day`</i>\n\n"
        "💵 <b>3. SET PRICE</b>\n"
        "• `/setprice <prod_key> <plan_name> <new_price>`\n"
        "👉 <i>Ex: `/setprice bala_mod 1 Day 450`</i>\n\n"
        "🔗 <b>4. ADD DOWNLOAD LINK</b>\n"
        "• `/addlink <prod_key> <url>`\n"
        "👉 <i>Ex: `/addlink bala_mod https://example.com/apk`</i>\n\n"
        "📢 <b>5. BROADCAST MESSAGE</b>\n"
        "• `/broadcast <your_message_or_photo>`\n\n"
        "🔍 <b>View Keys:</b> `/viewkeys <prod_key> <plan_name>`"
    )
    await update.message.reply_text(help_text, parse_mode="HTML")

async def cmd_addkey(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    try:
        args = context.args
        prod_key = args[0]
        plan = args[1]
        keys = [k.strip() for k in " ".join(args[2:]).split(",")]
        
        target_tuple = (prod_key, plan)
        if target_tuple not in KEYS_STOCK:
            KEYS_STOCK[target_tuple] = []
        KEYS_STOCK[target_tuple].extend(keys)
        
        await update.message.reply_text(f"✅ Added <b>{len(keys)} keys</b> to <b>{prod_key}</b> ({plan})!", parse_mode="HTML")
    except Exception:
        await update.message.reply_text("<b>Usage:</b> `/addkey <prod_key> <plan_name> <key1, key2...>`", parse_mode="HTML")

async def cmd_delkey(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    try:
        args = context.args
        prod_key = args[0]
        plan = args[1]
        key_to_del = args[2]
        
        target_tuple = (prod_key, plan)
        if target_tuple in KEYS_STOCK and key_to_del in KEYS_STOCK[target_tuple]:
            KEYS_STOCK[target_tuple].remove(key_to_del)
            await update.message.reply_text(f"✅ Key <code>{key_to_del}</code> deleted successfully!", parse_mode="HTML")
        else:
            await update.message.reply_text("⚠️ Key not found in stock!")
    except Exception:
        await update.message.reply_text("<b>Usage:</b> `/delkey <prod_key> <plan_name> <key_text>`", parse_mode="HTML")

async def cmd_clearstock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    try:
        prod_key = context.args[0]
        plan = context.args[1]
        KEYS_STOCK[(prod_key, plan)] = []
        await update.message.reply_text(f"✅ Cleared stock for <b>{prod_key}</b> ({plan})! Status is now <b>OUT OF STOCK</b>.", parse_mode="HTML")
    except Exception:
        await update.message.reply_text("<b>Usage:</b> `/clearstock <prod_key> <plan_name>`", parse_mode="HTML")

async def cmd_viewkeys(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    try:
        prod_key = context.args[0]
        plan = context.args[1]
        keys = KEYS_STOCK.get((prod_key, plan), [])
        if not keys:
            await update.message.reply_text("⚠️ No keys available in stock!")
        else:
            keys_text = "\n".join([f"• <code>{k}</code>" for k in keys])
            await update.message.reply_text(f"🔑 <b>Available Keys ({prod_key} - {plan}):</b>\n\n{keys_text}", parse_mode="HTML")
    except Exception:
        await update.message.reply_text("<b>Usage:</b> `/viewkeys <prod_key> <plan_name>`", parse_mode="HTML")

async def cmd_addproduct(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    try:
        category_type = context.args[0].lower()
        prod_key = context.args[1]
        prod_name = " ".join(context.args[2:])

        new_prod_data = {"name": prod_name, "prices": []}
        if category_type == "non_root":
            NON_ROOT_PRODUCTS[prod_key] = new_prod_data
        elif category_type == "root":
            ROOT_PRODUCTS[prod_key] = new_prod_data
        elif category_type == "ios":
            IOS_PRODUCTS[prod_key] = new_prod_data
        elif category_type == "pc":
            PC_PRODUCTS[prod_key] = new_prod_data

        await update.message.reply_text(f"✅ Added new product <b>{prod_name}</b> (`{prod_key}`)!", parse_mode="HTML")
    except Exception:
        await update.message.reply_text("<b>Usage:</b> `/addproduct <non_root/root/ios/pc> <prod_key> <prod_name>`", parse_mode="HTML")

async def cmd_addplan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    try:
        prod_key = context.args[0]
        price = int(context.args[-1])
        plan_name = " ".join(context.args[1:-1])

        prod = get_product_by_key(prod_key)
        if prod:
            prod["prices"].append((plan_name, price))
            await update.message.reply_text(f"✅ Plan <b>{plan_name}</b> (₹{price}) added to <b>{prod_key}</b>!", parse_mode="HTML")
        else:
            await update.message.reply_text("⚠️ Product key not found!")
    except Exception:
        await update.message.reply_text("<b>Usage:</b> `/addplan <prod_key> <plan_name> <price>`", parse_mode="HTML")

async def cmd_setprice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    try:
        args = context.args
        prod_key = args[0]
        new_price = int(args[-1])
        plan = " ".join(args[1:-1])
        prod = get_product_by_key(prod_key)
        
        if prod:
            for idx, (p_name, p_price) in enumerate(prod["prices"]):
                if p_name.lower() == plan.lower():
                    prod["prices"][idx] = (p_name, new_price)
                    await update.message.reply_text(f"✅ Price updated for <b>{prod_key}</b> ({p_name}) to <b>₹{new_price}</b>!", parse_mode="HTML")
                    return
        await update.message.reply_text("⚠️ Product or Plan not found!")
    except Exception:
        await update.message.reply_text("<b>Usage:</b> `/setprice <prod_key> <plan_name> <new_price>`", parse_mode="HTML")

async def cmd_maintain(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    try:
        prod_key = context.args[0]
        status = context.args[1].lower()
        if status == "on":
            MAINTENANCE_MODE[prod_key] = True
            await update.message.reply_text(f"🛠️ <b>{prod_key}</b> is now set to <b>Under Maintenance</b>!", parse_mode="HTML")
        else:
            MAINTENANCE_MODE[prod_key] = False
            await update.message.reply_text(f"✅ <b>{prod_key}</b> maintenance mode disabled!", parse_mode="HTML")
    except Exception:
        await update.message.reply_text("<b>Usage:</b> `/maintain <prod_key> <on/off>`", parse_mode="HTML")

async def cmd_addlink(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    try:
        prod_key = context.args[0]
        link_url = context.args[1]
        PRODUCT_LINKS[prod_key] = link_url
        await update.message.reply_text(f"✅ Download Link added for <b>{prod_key}</b>:\n{link_url}", parse_mode="HTML")
    except Exception:
        await update.message.reply_text("<b>Usage:</b> `/addlink <prod_key> <url>`", parse_mode="HTML")

async def cmd_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    
    users = list(USERS_DATA.keys())
    if not users:
        await update.message.reply_text("⚠️ No registered users found to broadcast!")
        return

    success = 0
    await update.message.reply_text(f"📢 <b>Broadcast started for {len(users)} users...</b>", parse_mode="HTML")

    if update.message.photo:
        photo_id = update.message.photo[-1].file_id
        raw_caption = update.message.caption or ""
        caption = raw_caption.replace("/broadcast", "").strip()
        
        for u_id in users:
            try:
                await context.bot.send_photo(
                    chat_id=u_id,
                    photo=photo_id,
                    caption=caption if caption else None,
                    parse_mode="HTML"
                )
                success += 1
            except Exception as e:
                logger.error(f"Failed photo broadcast to {u_id}: {e}")
    else:
        msg_text = " ".join(context.args)
        if not msg_text:
            await update.message.reply_text("⚠️ Please enter text or send a photo with `/broadcast` caption!", parse_mode="HTML")
            return
        for u_id in users:
            try:
                await context.bot.send_message(chat_id=u_id, text=msg_text, parse_mode="HTML")
                success += 1
            except Exception as e:
                logger.error(f"Failed text broadcast to {u_id}: {e}")

    await update.message.reply_text(f"✅ <b>Broadcast completed!</b>\n📊 Successfully sent to <b>{success}/{len(users)}</b> users.", parse_mode="HTML")

# ==========================================
# 🤖 BOT SETUP & RUNNER
# ==========================================
def start_bot():
    app = Application.builder().token(BOT_TOKEN).concurrent_updates(False).build()

    # Base Handlers
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("adminhelp", cmd_admin_help))
    app.add_handler(CommandHandler("help", cmd_admin_help))
    
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
    app.add_handler(CallbackQueryHandler(confirm_pay, pattern="^confirm_pay$"))
    app.add_handler(CallbackQueryHandler(retry_utr_handler, pattern="^retry_utr$"))
    app.add_handler(CallbackQueryHandler(handle_admin_action, pattern="^admin_"))
    app.add_handler(CallbackQueryHandler(cancel_order, pattern="^cancel_order$"))

    # Admin Key & Stock Commands
    app.add_handler(CommandHandler("addkey", cmd_addkey))
    app.add_handler(CommandHandler("delkey", cmd_delkey))
    app.add_handler(CommandHandler("clearstock", cmd_clearstock))
    app.add_handler(CommandHandler("viewkeys", cmd_viewkeys))

    # Admin Product & Links Commands
    app.add_handler(CommandHandler("addproduct", cmd_addproduct))
    app.add_handler(CommandHandler("addplan", cmd_addplan))
    app.add_handler(CommandHandler("addlink", cmd_addlink))

    # Admin Controls
    app.add_handler(CommandHandler("setprice", cmd_setprice))
    app.add_handler(CommandHandler("maintain", cmd_maintain))
    app.add_handler(CommandHandler("broadcast", cmd_broadcast))

    # Messages and Fallback
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
