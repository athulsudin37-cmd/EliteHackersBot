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

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
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
# ⚙️ BOT CONFIGURATION & DATABASE
# ==========================================
BOT_TOKEN = "8892856619:AAGZhdOv389_AaKvbcbInlJAiDMOwQxOeHc"
ADMIN_ID = 7616127905
QR_IMAGE_URL = "https://i.ibb.co/kg2jT6ZF/qr.jpg"

USED_UTRS = set()
ACTIVE_ORDERS = {}       
USER_PROFILES = {}       
USER_ORDER_HISTORY = {}  

# Products Data
NON_ROOT_PRODUCTS = {
    "bala_mod": {"name": "BALA MOD NON ROOT", "prices": [("1 Hour", 45), ("2 Hour", 85), ("4 Hour", 150), ("6 Hour", 220), ("12 Hour", 300), ("1 Day", 420), ("3 Day", 1050)]},
    "tm_pannel": {"name": "TM PANNEL NON ROOT", "prices": [("1 Day", 70), ("7 Day", 210), ("15 Day", 310), ("31 Day", 450), ("Lifetime Permanent", 1100)]},
    "drip_client": {"name": "DRIP CLIENT APK MOD", "prices": [("1 Day", 80), ("3 Day", 140), ("7 Day", 250), ("15 Day", 360), ("31 Day", 500)]},
    "prime_hook": {"name": "PRIME HOOK APK MOD", "prices": [("1 Day", 80), ("3 Day", 170), ("7 Day", 320), ("10 Day", 420)]},
    "hg_cheat": {"name": "HG CHEAT APK MOD", "prices": [("1 Day", 100), ("7 Day", 230), ("10 Day", 330), ("30 Day", 690)]},
    "silent_cheat": {"name": "SILENT CHEAT SAFE", "prices": [("1 Day", 90), ("3 Day", 190), ("7 Day", 320), ("15 Day", 550), ("30 Day", 830)]},
    "drip_proxy": {"name": "DRIP CLIENT PROXY", "prices": [("1 Day", 65), ("3 Day", 140), ("7 Day", 260), ("31 Day", 650)]}
}
ROOT_PRODUCTS = {
    "rapid_core": {"name": "RAPID CORE INJECTOR", "prices": [("1 Day", 90), ("7 Day", 310), ("15 Day", 470), ("30 Day", 690)]},
    "neo_strike": {"name": "NEO STRIKE BRUTAL", "prices": [("1 Day", 90), ("3 Day", 180), ("7 Day", 310), ("14 Day", 590), ("28 Day", 899)]},
    "haxx_cker": {"name": "HAXX-CKER PRO", "prices": [("10 Day", 550)]},
    "xytron_pro": {"name": "XYTRON PRO", "prices": [("1 Day", 100), ("7 Day", 310), ("15 Day", 550), ("31 Day", 830)]},
    "br_mod": {"name": "BR MOD INJECTOR", "prices": [("1 Day", 90), ("7 Day", 250), ("15 Day", 420), ("31 Day", 570)]},
    "angry_mod": {"name": "ANGRY MOD", "prices": [("1 Day", 70), ("7 Day", 130), ("15 Day", 170), ("31 Day", 290)]},
    "xyz_cheats": {"name": "XYZ CHEATS", "prices": [("1 Day", 80), ("3 Day", 160), ("7 Day", 310), ("15 Day", 520), ("30 Day", 880)]}
}
IOS_PRODUCTS = {
    "migul_pro": {"name": "MIGUL PRO IOS", "prices": [("1 Day", 200), ("7 Day", 480), ("31 Day", 900)]},
    "flourite_ios": {"name": "FLOURITE IOS", "prices": [("1 Day", 270), ("7 Day", 780), ("31 Day", 1600)]}
}
PC_PRODUCTS = {
    "br_mod_pc": {"name": "BR MOD PC", "prices": [("1 Day", 150), ("10 Day", 550), ("31 Day", 900)]},
    "internal_pc": {"name": "INTERNAL PC", "prices": [("1 Day", 99), ("3 Day", 199), ("7 Day", 370), ("15 Day", 650), ("30 Day", 900), ("Lifetime Permanent", 2100)]}
}
LIKE_PRODUCTS = {
    "auto_like_everyday": {"name": "AUTO LIKE EVERY DAY", "prices": [("7 DAYS (220+ Likes/day)", 90), ("15 DAYS (220+ Likes/day)", 160), ("30 DAYS (220+ Likes/day)", 275), ("90 DAYS (220+ Likes/day)", 730)]}
}

ALL_PRODUCTS = {**NON_ROOT_PRODUCTS, **ROOT_PRODUCTS, **IOS_PRODUCTS, **PC_PRODUCTS, **LIKE_PRODUCTS}

def format_amt_simple(amount):
    if amount >= 1000:
        return f"{amount:,}"
    return str(amount)

# COMMAND: /start
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id not in USER_PROFILES:
        USER_PROFILES[user.id] = {'joined_date': datetime.now().strftime("%d %b %Y"), 'total_orders': 0}

    welcome_text = "<b>WELCOME TO FF SERVICES SHOP! 🛒</b>\n\nPlease select an option from below to continue:"
    keyboard = [
        [InlineKeyboardButton("🛒 Shop Now", callback_data="shop_now")],
        [InlineKeyboardButton("📦 My Orders", callback_data="my_orders"), InlineKeyboardButton("👤 Profile", callback_data="profile")],
        [InlineKeyboardButton("💳 Pay Proof", url="https://t.me/+fJrFACSrntgwNjll"), InlineKeyboardButton("💬 Support", callback_data="support")],
        [InlineKeyboardButton("ℹ️ How to Use", callback_data="how_to_use")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    if update.message:
        await update.message.reply_text(welcome_text, parse_mode="HTML", reply_markup=reply_markup)
    elif update.callback_query:
        await update.callback_query.message.edit_text(welcome_text, parse_mode="HTML", reply_markup=reply_markup)

# PROFILE HANDLER
async def profile_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = update.effective_user
    profile_data = USER_PROFILES.get(user.id, {'joined_date': datetime.now().strftime("%d %b %Y"), 'total_orders': 0})

    text = (
        "___________________________\n\n"
        "<b>👤 YOUR PROFILE</b>\n"
        "___________________________\n\n"
        f"🛡️ <b>Name:</b> {user.full_name}\n"
        f"🔗 <b>Username:</b> @{user.username if user.username else 'N/A'}\n"
        f"🆔 <b>User ID:</b> {user.id}\n"
        f"📅 <b>Member Since:</b> {profile_data['joined_date']}\n"
        f"🪪 <b>Account Type:</b> 🟢 Regular\n"
        f"🛒 <b>Total Orders:</b> {profile_data['total_orders']}\n"
        "___________________________"
    )
    keyboard = [
        [InlineKeyboardButton("🛒 Shop Now", callback_data="shop_now"), InlineKeyboardButton("📦 My Orders", callback_data="my_orders")],
        [InlineKeyboardButton("↩️ Back to Menu", callback_data="main_menu")]
    ]
    await query.message.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))

# MY ORDERS HANDLER
async def my_orders_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    history = USER_ORDER_HISTORY.get(user_id, [])

    if not history:
        text = "___________________________\n\n<b>🔑 MY ORDERS (Last 5)</b>\n___________________________\n\nNo purchase history found yet!\n___________________________"
        keyboard = [[InlineKeyboardButton("🛒 Shop Now", callback_data="shop_now")], [InlineKeyboardButton("↩️ Back to Menu", callback_data="main_menu")]]
    else:
        lines = ["___________________________\n", "<b>🔑 MY ORDERS (Last 5)</b>\n", "___________________________\n"]
        for idx, item in enumerate(reversed(history[-5:]), start=1):
            lines.append(f"<b>{idx}. ⚙️ {item['prod_name']}</b>\n   ⏲️ <b>Duration:</b> {item['plan']}\n   📅 <b>Date:</b> {item['date']}\n   🔑 <b>Key:</b> <code>{item['key']}</code>\n")
        lines.append("___________________________")
        text = "\n".join(lines)
        keyboard = [[InlineKeyboardButton("🛒 Shop Again", callback_data="shop_now")], [InlineKeyboardButton("↩️ Back to Menu", callback_data="main_menu")]]

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
    text = "📖 <b>HOW TO USE:</b>\n1. Select Product\n2. Pay via QR\n3. Send UTR & Screenshot."
    keyboard = [[InlineKeyboardButton("🔙 Back", callback_data="main_menu")]]
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
    keyboard = [[InlineKeyboardButton(f"👍 {data['name']}", callback_data=f"p_{key}")] for key, data in LIKE_PRODUCTS.items()]
    keyboard.append([InlineKeyboardButton("🔙 Back to Shop", callback_data="shop_now")])
    await query.message.edit_text("<b>💎 FREE FIRE LIKE SERVICES:</b>", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))

async def non_root_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    keyboard = [[InlineKeyboardButton(f"⚙️ {data['name']}", callback_data=f"p_{key}")] for key, data in NON_ROOT_PRODUCTS.items()]
    keyboard.append([InlineKeyboardButton("🔙 Back to Categories", callback_data="cat_panels")])
    await query.message.edit_text("<b>📱 NON-ROOT PANELS:</b>", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))

async def root_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    keyboard = [[InlineKeyboardButton(f"⚡ {data['name']}", callback_data=f"p_{key}")] for key, data in ROOT_PRODUCTS.items()]
    keyboard.append([InlineKeyboardButton("🔙 Back to Categories", callback_data="cat_panels")])
    await query.message.edit_text("<b>⚡ ROOT PANELS:</b>", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))

async def ios_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    keyboard = [[InlineKeyboardButton(f"🍏 {data['name']}", callback_data=f"p_{key}")] for key, data in IOS_PRODUCTS.items()]
    keyboard.append([InlineKeyboardButton("🔙 Back to Categories", callback_data="cat_panels")])
    await query.message.edit_text("<b>🍏 IOS PANELS:</b>", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))

async def pc_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    keyboard = [[InlineKeyboardButton(f"💻 {data['name']}", callback_data=f"p_{key}")] for key, data in PC_PRODUCTS.items()]
    keyboard.append([InlineKeyboardButton("🔙 Back to Categories", callback_data="cat_panels")])
    await query.message.edit_text("<b>💻 PC PANELS:</b>", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))

async def show_product_prices(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    prod_key = query.data.replace("p_", "")
    prod = ALL_PRODUCTS.get(prod_key)
    if not prod:
        return
    
    lines = ["<b>═══════════════════════</b>", f"<b>🛒 {prod['name']}</b>", "<b>═══════════════════════</b>\n", "🔥 <b>Choose a plan:</b>\n"]
    keyboard = []
    for idx, (plan, price) in enumerate(prod["prices"]):
        formatted_price = format_amt_simple(price)
        lines.append(f"• {plan} — ₹{formatted_price}.00")
        keyboard.append([InlineKeyboardButton(f"{plan} — ₹{formatted_price}.00", callback_data=f"price_{prod_key}_{idx}")])

    keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="shop_now")])
    await query.message.edit_text("\n".join(lines), parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))

async def order_summary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    parts = query.data.split("_")
    prod_key = parts[1]
    price_idx = int(parts[2])
    
    prod = ALL_PRODUCTS.get(prod_key)
    plan, price = prod["prices"][price_idx]
    formatted_price = format_amt_simple(price)

    text = (
        "<b>═══════════════════════</b>\n"
        "<b>📋 ORDER SUMMARY</b>\n"
        "<b>═══════════════════════</b>\n\n"
        f"🔑 <b>Product:</b> {prod['name']}\n"
        f"📄 <b>Plan:</b> {plan}\n"
        f"💵 <b>Price:</b> ₹{formatted_price}.00\n"
        "_______________________\n\n"
        f"💰 <b>Final Total:</b> ₹{formatted_price}.00"
    )

    context.user_data['pending_order'] = {'prod_key': prod_key, 'prod_name': prod['name'], 'plan': plan, 'price': price}
    keyboard = [
        [InlineKeyboardButton("✅ Confirm & Pay", callback_data="confirm_pay")],
        [InlineKeyboardButton("🔙 Back to Plans", callback_data=f"p_{prod_key}")]
    ]
    await query.message.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))

async def confirm_pay(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    order = context.user_data.get('pending_order')
    if not order:
        return

    formatted_price = format_amt_simple(order['price'])
    caption = (
        "<b>═══════════════════════</b>\n"
        "<b>💼 ORDER CREATED</b>\n"
        "<b>═══════════════════════</b>\n\n"
        f"🔮 <b>Product:</b> {order['prod_name']}\n"
        f"⏲️ <b>Duration:</b> {order['plan']}\n"
        f"💰 <b>Amount:</b> ₹{formatted_price}.00\n\n"
        "📲 <b>Scan the QR above to pay</b>\n"
        f"⚠️ <b>Pay EXACTLY ₹{formatted_price}.00</b>\n"
        "<b>═══════════════════════</b>"
    )

    keyboard = [
        [InlineKeyboardButton("⚙️ I Have Paid", callback_data="i_have_paid")],
        [InlineKeyboardButton("❌ Cancel", callback_data="cancel_order")]
    ]

    try:
        await query.message.delete()
    except Exception:
        pass

    sent_msg = await context.bot.send_photo(
        chat_id=query.message.chat_id,
        photo=QR_IMAGE_URL,
        caption=caption,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

    async def auto_delete():
        await asyncio.sleep(300)
        try:
            await context.bot.delete_message(chat_id=sent_msg.chat_id, message_id=sent_msg.message_id)
        except Exception:
            pass

    asyncio.create_task(auto_delete())

async def i_have_paid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data['state'] = 'WAITING_UTR'
    await query.message.reply_text("<b>🔢 Enter your 12-digit UTR/Transaction Number:</b>", parse_mode="HTML")

async def cancel_order(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.message.reply_text("❌ Order cancelled.")
    await start_command(update, context)

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
            curr_date = datetime.now().strftime("%d %b %Y, %I:%M %p")

            if cust_id in USER_PROFILES:
                USER_PROFILES[cust_id]['total_orders'] += 1
            else:
                USER_PROFILES[cust_id] = {'joined_date': datetime.now().strftime("%d %b %Y"), 'total_orders': 1}

            if cust_id not in USER_ORDER_HISTORY:
                USER_ORDER_HISTORY[cust_id] = []

            USER_ORDER_HISTORY[cust_id].append({'prod_name': prod_name, 'plan': plan, 'date': curr_date, 'key': key_text})

            cust_text = (
                "<b>═══════════════════════</b>\n"
                "<b>🎉 YOUR ORDER IS READY!</b>\n"
                "<b>═══════════════════════</b>\n\n"
                f"🔮 <b>Product:</b> {prod_name}\n"
                f"⏲️ <b>Duration:</b> {plan}\n\n"
                "🔑 <b>Key:</b>\n"
                f"<code>{key_text}</code>\n"
                "<b>═══════════════════════</b>"
            )
            await context.bot.send_message(chat_id=cust_id, text=cust_text, parse_mode="HTML")
            await update.message.reply_text("✅ Key sent successfully!")
            context.user_data['admin_state'] = None
            context.user_data['active_admin_msg_id'] = None
        return

    if state == 'WAITING_UTR':
        if not update.message.text:
            await update.message.reply_text("⚠️ Please send a valid UTR text number!")
            return

        utr = update.message.text.strip()
        if utr in USED_UTRS:
            await update.message.reply_text("⚠️ This UTR has already been used!")
            return

        context.user_data['utr'] = utr
        context.user_data['state'] = 'WAITING_SCREENSHOT'
        await update.message.reply_text("<b>📸 Now please send your Payment Screenshot image:</b>", parse_mode="HTML")
        return

    if state == 'WAITING_SCREENSHOT':
        if not update.message.photo:
            await update.message.reply_text("⚠️ Please send a valid payment screenshot image.")
            return

        photo_id = update.message.photo[-1].file_id
        order = context.user_data.get('pending_order')
        utr = context.user_data.get('utr')
        USED_UTRS.add(utr)

        await update.message.reply_text("⏳ <b>Payment Received!</b> Please wait while admin verifies.", parse_mode="HTML")
        formatted_price = format_amt_simple(order['price'])

        admin_text = (
            "🚨 <b>NEW ORDER RECEIVED</b> 🚨\n\n"
            f"• User: {user.first_name} ({user.id})\n"
            f"• Product: {order['prod_name']}\n"
            f"• Plan: {order['plan']}\n"
            f"• Price: ₹{formatted_price}.00\n"
            f"• UTR: {utr}"
        )

        admin_keyboard = [[InlineKeyboardButton("✅ Approve", callback_data="admin_approve"), InlineKeyboardButton("❌ Reject", callback_data="admin_reject")]]
        admin_msg = await context.bot.send_photo(chat_id=ADMIN_ID, photo=photo_id, caption=admin_text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(admin_keyboard))

        ACTIVE_ORDERS[admin_msg.message_id] = {'user_id': user.id, 'prod_name': order['prod_name'], 'plan': order['plan'], 'price': order['price'], 'utr': utr}
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
        await query.message.reply_text(f"🔑 Send the <b>KEY</b> for {order_info['prod_name']} ({order_info['plan']}):", parse_mode="HTML")

    elif query.data == "admin_reject":
        await context.bot.send_message(chat_id=cust_id, text="❌ <b>Your Order Has Been Rejected.</b>", parse_mode="HTML")
        await query.message.reply_text("❌ Order Rejected.")

def start_bot():
    app = Application.builder().token(BOT_TOKEN).build()

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
    app.add_handler(CallbackQueryHandler(show_product_prices, pattern="^p_"))
    app.add_handler(CallbackQueryHandler(order_summary, pattern="^price_"))
    app.add_handler(CallbackQueryHandler(confirm_pay, pattern="^confirm_pay$"))
    app.add_handler(CallbackQueryHandler(i_have_paid, pattern="^i_have_paid$"))
    app.add_handler(CallbackQueryHandler(cancel_order, pattern="^cancel_order$"))
    app.add_handler(CallbackQueryHandler(handle_admin_action, pattern="^admin_"))

    app.add_handler(MessageHandler(filters.TEXT | filters.PHOTO, handle_user_message))

    print("Bot is running...")
    app.run_polling()

def main():
    keep_alive()
    while True:
        try:
            start_bot()
        except Exception as e:
            print(f"Crash prevented: {e}. Auto-restarting...")
            time.sleep(1)

if __name__ == "__main__":
    main()
