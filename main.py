import logging
import asyncio
import os
import time
from threading import Thread
from datetime import datetime
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
# 🌐 FLASK KEEP-ALIVE SERVER (FOR UPTIMEROBOT)
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
# ⚙️ BOT CONFIGURATION
# ==========================================
BOT_TOKEN = "8892856619:AAGZhdOv389_AaKvbcbInlJAiDMOwQxOeHc"
ADMIN_ID = 7616127905

ACTIVE_ORDERS = {}       # admin_msg_id -> order_data

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

def format_amt_simple(amount):
    if amount >= 1000:
        return f"{amount:,}"
    return str(amount)

# COMMAND: /start
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user = update.effective_user
        if not user:
            return

        welcome_text = "<b>WELCOME TO FF SERVICES SHOP! 🛒</b>\n\nPlease select an option from below to continue:"
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
            await update.callback_query.message.edit_text(welcome_text, parse_mode="HTML", reply_markup=reply_markup)
    except Exception as e:
        logger.error(f"Error in start_command: {e}")

# 1️⃣ PROFILE HANDLER
async def profile_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = update.effective_user
    
    text = (
        "___________________________\n\n"
        "<b>👤 YOUR PROFILE</b>\n"
        "___________________________\n\n"
        f"🛡️ <b>Name:</b> {user.full_name}\n"
        f"🔗 <b>Username:</b> @{user.username if user.username else 'N/A'}\n"
        f"🆔 <b>User ID:</b> {user.id}\n"
        f"📅 <b>Member Since:</b> {datetime.now().strftime('%d %b %Y')}\n"
        f"🪪 <b>Account Type:</b> 🟢 Regular\n"
        f"🛒 <b>Total Orders:</b> 0\n"
        "___________________________"
    )
    keyboard = [
        [InlineKeyboardButton("🛒 Shop Now", callback_data="shop_now"), InlineKeyboardButton("📦 My Orders", callback_data="my_orders")],
        [InlineKeyboardButton("↩️ Back to Menu", callback_data="main_menu")]
    ]
    await query.message.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))

# 2️⃣ MY ORDERS HANDLER
async def my_orders_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    text = (
        "___________________________\n\n"
        "<b>🔑 MY ORDERS (Last 5)</b>\n"
        "___________________________\n\n"
        "No purchase history found yet!\n"
        "___________________________"
    )
    keyboard = [
        [InlineKeyboardButton("🛒 Shop Now", callback_data="shop_now")],
        [InlineKeyboardButton("↩️ Back to Menu", callback_data="main_menu")]
    ]
    await query.message.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))

# 3️⃣ SUPPORT HANDLER
async def support_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    text = "📩 <b>Contact support:</b> @Athulsudin"
    keyboard = [[InlineKeyboardButton("🔙 Back", callback_data="main_menu")]]
    await query.message.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))

# 5️⃣ HOW TO USE HANDLER
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
        "4️⃣ Scan the UPI QR provided or copy details.\n"
        "5️⃣ Pay the <b>exact amount</b> shown.\n"
        "6️⃣ Tap <b>⚙️ I Have Paid</b> and submit 12-digit UTR.\n"
        "7️⃣ Send payment screenshot as final step.\n\n"
        "Your payment will be verified by admin and key will be delivered instantly! 🚀\n\n"
        "🎬 <b>Watch full tutorial video below:</b>"
    )
    keyboard = [
        [InlineKeyboardButton("🎬 Watch Tutorial Video", url="https://t.me/chatelitehackers")],
        [InlineKeyboardButton("🔙 Back", callback_data="main_menu")]
    ]
    await query.message.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))

# STORE NAVIGATION
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

async def show_product_prices(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    cb_data = query.data

    if cb_data.startswith("prod_nonroot_"):
        prod_type, prod_key, prod, back_target = "nonroot", cb_data.replace("prod_nonroot_", ""), NON_ROOT_PRODUCTS.get(cb_data.replace("prod_nonroot_", "")), "non_root_list"
    elif cb_data.startswith("prod_root_"):
        prod_type, prod_key, prod, back_target = "root", cb_data.replace("prod_root_", ""), ROOT_PRODUCTS.get(cb_data.replace("prod_root_", "")), "root_list"
    elif cb_data.startswith("prod_ios_"):
        prod_type, prod_key, prod, back_target = "ios", cb_data.replace("prod_ios_", ""), IOS_PRODUCTS.get(cb_data.replace("prod_ios_", "")), "ios_list"
    elif cb_data.startswith("prod_pc_"):
        prod_type, prod_key, prod, back_target = "pc", cb_data.replace("prod_pc_", ""), PC_PRODUCTS.get(cb_data.replace("prod_pc_", "")), "pc_list"
    else:
        prod_type, prod_key, prod, back_target = "likes", cb_data.replace("prod_likes_", ""), LIKE_PRODUCTS.get(cb_data.replace("prod_likes_", "")), "cat_likes"

    if not prod:
        return

    lines = ["<b>═══════════════════════</b>", f"<b>🛒 {prod['name']}</b>", "<b>═══════════════════════</b>\n", "🔥 <b>Choose a plan:</b>\n"]
    keyboard = []
    for plan, price in prod["prices"]:
        formatted_price = format_amt_simple(price)
        lines.append(f"• {plan} — ₹{formatted_price}.00")
        keyboard.append([InlineKeyboardButton(f"{plan} — ₹{formatted_price}.00", callback_data=f"plan_{prod_type}_{prod_key}_{plan}_{price}")])

    keyboard.append([InlineKeyboardButton("🔙 Back", callback_data=back_target)])
    await query.message.edit_text("\n".join(lines), parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))

async def order_summary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    data_str = query.data.replace("plan_", "")
    parts = data_str.split("_")
    prod_type = parts[0]
    prod_key = parts[1]
    price = int(parts[-1])
    plan = "_".join(parts[2:-1])

    if prod_type == "nonroot":
        prod, back_data = NON_ROOT_PRODUCTS.get(prod_key), f"prod_nonroot_{prod_key}"
    elif prod_type == "root":
        prod, back_data = ROOT_PRODUCTS.get(prod_key), f"prod_root_{prod_key}"
    elif prod_type == "ios":
        prod, back_data = IOS_PRODUCTS.get(prod_key), f"prod_ios_{prod_key}"
    elif prod_type == "pc":
        prod, back_data = PC_PRODUCTS.get(prod_key), f"prod_pc_{prod_key}"
    else:
        prod, back_data = LIKE_PRODUCTS.get(prod_key), f"prod_likes_{prod_key}"

    prod_name = prod['name'] if prod else "Service"
    formatted_price = format_amt_simple(price)

    text = (
        "<b>═══════════════════════</b>\n"
        "<b>📋 ORDER SUMMARY</b>\n"
        "<b>═══════════════════════</b>\n\n"
        f"🔑 <b>Product:</b> {prod_name}\n"
        f"📄 <b>Plan:</b> {plan}\n"
        f"💵 <b>Price:</b> ₹{formatted_price}.00\n"
        "_______________________\n\n"
        f"💰 <b>Final Total:</b> ₹{formatted_price}.00"
    )

    context.user_data['pending_order'] = {'prod_type': prod_type, 'prod_key': prod_key, 'prod_name': prod_name, 'plan': plan, 'price': price}
    keyboard = [[InlineKeyboardButton("✅ Confirm & Pay", callback_data="confirm_pay")], [InlineKeyboardButton("🔙 Back to Plans", callback_data=back_data)]]
    await query.message.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))

async def confirm_pay(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    order = context.user_data.get('pending_order')
    if not order:
        return

    formatted_price = format_amt_simple(order['price'])
    qr_image_url = "https://i.ibb.co/kg2jT6ZF/qr.jpg"
    back_target = f"prod_{order['prod_type']}_{order['prod_key']}"

    context.user_data['timer_seconds'] = 300
    context.user_data['order_cancelled'] = False

    def get_caption(seconds):
        m, s = divmod(max(0, seconds), 60)
        return (
            "<b>═══════════════════════</b>\n"
            "<b>💼 ORDER CREATED</b>\n"
            "<b>═══════════════════════</b>\n\n"
            f"🔮 <b>Product:</b> {order['prod_name']}\n"
            f"⏲️ <b>Duration:</b> {order['plan']}\n"
            f"💰 <b>Amount:</b> ₹{formatted_price}.00\n\n"
            "📲 <b>Scan the QR above to pay</b>\n"
            f"⚠️ <b>Pay EXACTLY ₹{formatted_price}.00</b>\n"
            f"⏳ <b>Expires in: {m:02d}:{s:02d} minutes</b>\n"
            "<b>═══════════════════════</b>"
        )

    keyboard = [
        [InlineKeyboardButton("⚙️ I Have Paid", callback_data="i_have_paid")],
        [InlineKeyboardButton("❌ Cancel", callback_data="cancel_order")],
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

    async def timer_loop():
        msg_id = sent_msg.message_id
        chat_id = sent_msg.chat_id
        for remaining in range(300, 0, -20):
            await asyncio.sleep(20)
            if context.user_data.get('order_cancelled'):
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
        
        if not context.user_data.get('order_cancelled'):
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

async def cancel_order(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data['order_cancelled'] = True
    try:
        await query.message.delete()
    except Exception:
        pass
    await start_command(update, context)

async def i_have_paid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    keyboard = [[InlineKeyboardButton("🔢 Enter UTR Number", callback_data="prompt_utr")]]
    await query.message.reply_text("<b>Please click below to enter your 12-digit UTR/Transaction Number:</b>", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))

async def prompt_utr(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data['state'] = 'WAITING_UTR'
    await query.message.reply_text("<b>🔢 Enter your 12-Digit UTR/Transaction ID (Digits only):</b>", parse_mode="HTML")

async def handle_user_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = context.user_data.get('state')
    user = update.effective_user

    if user.id == ADMIN_ID and context.user_data.get('admin_state') == 'AWAITING_KEY':
        key_text = update.message.text.strip()
        target_msg_id = context.user_data.get('active_admin_msg_id')
        order_info = ACTIVE_ORDERS.get(target_msg_id)

        if order_info:
            cust_id = order_info['user_id']
            prod_name = order_info['prod_name']
            plan = order_info['plan']

            cust_text = (
                "<b>═══════════════════════</b>\n"
                "<b>🎉 YOUR ORDER IS READY!</b>\n"
                "<b>═══════════════════════</b>\n\n"
                f"🔮 <b>Product:</b> {prod_name}\n"
                f"⏲️ <b>Duration:</b> {plan}\n\n"
                "🔑 <b>Key (Tap on Key to Copy):</b>\n"
                f"<code>{key_text}</code>\n"
                "<b>═══════════════════════</b>\n"
                "Thank you for shopping with us!"
            )
            await context.bot.send_message(chat_id=cust_id, text=cust_text, parse_mode="HTML")
            await update.message.reply_text("✅ Key sent to customer successfully!")
            context.user_data['admin_state'] = None
            context.user_data['active_admin_msg_id'] = None
        return

    if state == 'WAITING_UTR':
        if not update.message.text:
            await update.message.reply_text("⚠️ Please send a valid UTR text number!")
            return

        utr = update.message.text.strip()
        
        if not utr.isdigit() or len(utr) != 12:
            await update.message.reply_text("⚠️ <b>Invalid UTR Format!</b> UTR must be exactly <b>12 digits</b> (numbers only). Please enter again:", parse_mode="HTML")
            return

        context.user_data['utr'] = utr
        context.user_data['state'] = 'WAITING_SCREENSHOT'
        await update.message.reply_text("<b>📸 Now please send your Payment Screenshot image:</b>", parse_mode="HTML")
        return

    if state == 'WAITING_SCREENSHOT':
        if not update.message.photo:
            await update.message.reply_text("⚠️ Invalid input! Please send a valid payment screenshot image.")
            return

        photo_id = update.message.photo[-1].file_id
        order = context.user_data.get('pending_order')
        utr = context.user_data.get('utr')

        await update.message.reply_text("⏳ <b>Payment Received!</b> Please wait while admin verifies your payment.", parse_mode="HTML")
        formatted_price = format_amt_simple(order['price'])

        admin_text = (
            "🚨 <b>NEW ORDER RECEIVED</b> 🚨\n\n"
            "<b>👤 User Details:</b>\n"
            f"• Name: {user.first_name}\n"
            f"• Username: @{user.username if user.username else 'N/A'}\n"
            f"• Telegram ID: {user.id}\n\n"
            "<b>🛒 Order Details:</b>\n"
            f"• Product: {order['prod_name']}\n"
            f"• Duration: {order['plan']}\n"
            f"• Price: ₹{formatted_price}.00\n"
            f"• UTR Number: {utr}"
        )

        admin_keyboard = [[InlineKeyboardButton("✅ Approve", callback_data="admin_approve"), InlineKeyboardButton("❌ Reject", callback_data="admin_reject")]]
        
        try:
            admin_msg = await context.bot.send_photo(
                chat_id=ADMIN_ID,
                photo=photo_id,
                caption=admin_text,
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(admin_keyboard)
            )
            ACTIVE_ORDERS[admin_msg.message_id] = {
                'user_id': user.id,
                'prod_name': order['prod_name'],
                'plan': order['plan'],
                'price': order['price'],
                'utr': utr
            }
        except Exception as e:
            logger.error(f"Failed to send order to admin DM: {e}")
            await update.message.reply_text("⚠️ Error sending order to admin. Please make sure the admin has started the bot.")

        context.user_data['state'] = None
        return

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

    elif query.data == "admin_reject":
        await context.bot.send_message(chat_id=cust_id, text="❌ <b>Your Order Has Been Rejected.</b>", parse_mode="HTML")
        await query.message.reply_text("❌ Order Rejected notification sent.")

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
    app.add_handler(CallbackQueryHandler(confirm_pay, pattern="^confirm_pay$"))
    app.add_handler(CallbackQueryHandler(i_have_paid, pattern="^i_have_paid$"))
    app.add_handler(CallbackQueryHandler(prompt_utr, pattern="^prompt_utr$"))
    app.add_handler(CallbackQueryHandler(handle_admin_action, pattern="^admin_"))
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

