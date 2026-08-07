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

flask_app = Flask('')
@flask_app.route('/')
def home():
    return "Bot is alive!"

def run_flask():
    port = int(os.environ.get("PORT", 8080))
    flask_app.run(host='0.0.0.0', port=port)

def keep_alive():
    t = Thread(target=run_flask)
    t.daemon = True
    t.start()

BOT_TOKEN = "8892856619:AAGZhdOv389_AaKvbcbInlJAiDMOwQxOeHc"
ADMIN_ID = 7616127905
QR_IMAGE_URL = "https://i.ibb.co/kg2jT6ZF/qr.jpg"

USED_UTRS = set()
ACTIVE_ORDERS = {}       
USER_PROFILES = {}       
USER_ORDER_HISTORY = {}  

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
    "auto_like_everyday": {"name": "AUTO LIKE EVERY DAY", "prices": [("7 DAYS", 90), ("15 DAYS", 160), ("30 DAYS", 275), ("90 DAYS", 730)]}
}

ALL_PRODUCTS = {**NON_ROOT_PRODUCTS, **ROOT_PRODUCTS, **IOS_PRODUCTS, **PC_PRODUCTS, **LIKE_PRODUCTS}

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id not in USER_PROFILES:
        USER_PROFILES[user.id] = {'joined_date': datetime.now().strftime("%d %b %Y"), 'total_orders': 0}
    text = "<b>WELCOME TO FF SERVICES SHOP! 🛒</b>\n\nPlease select an option from below:"
    keyboard = [
        [InlineKeyboardButton("🛒 Shop Now", callback_data="shop_now")],
        [InlineKeyboardButton("📦 My Orders", callback_data="my_orders"), InlineKeyboardButton("👤 Profile", callback_data="profile")],
        [InlineKeyboardButton("💬 Support", callback_data="support"), InlineKeyboardButton("ℹ️ How to Use", callback_data="how_to_use")]
    ]
    if update.message:
        await update.message.reply_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))
    else:
        await update.callback_query.message.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))

async def profile_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = update.effective_user
    p = USER_PROFILES.get(user.id, {'joined_date': 'N/A', 'total_orders': 0})
    text = f"<b>👤 PROFILE</b>\n\nName: {user.full_name}\nID: {user.id}\nOrders: {p['total_orders']}"
    await query.message.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Menu", callback_data="main_menu")]]))

async def my_orders_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    history = USER_ORDER_HISTORY.get(update.effective_user.id, [])
    text = "<b>📦 MY ORDERS</b>\n\n" + ("No orders yet." if not history else "\n".join([f"- {i['prod_name']} ({i['plan']})" for i in history]))
    await query.message.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Menu", callback_data="main_menu")]]))

async def support_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.message.edit_text("💬 Support: @Athulsudin", parse_mode="HTML", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Menu", callback_data="main_menu")]]))

async def how_to_use_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.message.edit_text("📖 Select product -> Pay via QR -> Send UTR & Screenshot.", parse_mode="HTML", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Menu", callback_data="main_menu")]]))

async def store_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    keyboard = [
        [InlineKeyboardButton("🔥 Panels", callback_data="cat_panels")],
        [InlineKeyboardButton("💎 Likes", callback_data="cat_likes")],
        [InlineKeyboardButton("🔙 Menu", callback_data="main_menu")]
    ]
    await query.message.edit_text("<b>🛒 SELECT CATEGORY:</b>", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))

async def category_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    keyboard = [
        [InlineKeyboardButton("Non-Root", callback_data="non_root_list")],
        [InlineKeyboardButton("Root", callback_data="root_list")],
        [InlineKeyboardButton("iOS", callback_data="ios_list")],
        [InlineKeyboardButton("PC", callback_data="pc_list")],
        [InlineKeyboardButton("🔙 Shop", callback_data="shop_now")]
    ]
    await query.message.edit_text("<b>📱 PANEL TYPES:</b>", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))

async def non_root_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    keyboard = [[InlineKeyboardButton(data['name'], callback_data=f"p_{key}")] for key, data in NON_ROOT_PRODUCTS.items()]
    keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="cat_panels")])
    await query.message.edit_text("<b>Non-Root Panels:</b>", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))

async def root_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    keyboard = [[InlineKeyboardButton(data['name'], callback_data=f"p_{key}")] for key, data in ROOT_PRODUCTS.items()]
    keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="cat_panels")])
    await query.message.edit_text("<b>Root Panels:</b>", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))

async def ios_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    keyboard = [[InlineKeyboardButton(data['name'], callback_data=f"p_{key}")] for key, data in IOS_PRODUCTS.items()]
    keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="cat_panels")])
    await query.message.edit_text("<b>iOS Panels:</b>", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))

async def pc_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    keyboard = [[InlineKeyboardButton(data['name'], callback_data=f"p_{key}")] for key, data in PC_PRODUCTS.items()]
    keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="cat_panels")])
    await query.message.edit_text("<b>PC Panels:</b>", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))

async def likes_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    keyboard = [[InlineKeyboardButton(data['name'], callback_data=f"p_{key}")] for key, data in LIKE_PRODUCTS.items()]
    keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="shop_now")])
    await query.message.edit_text("<b>Likes Services:</b>", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))

async def show_product_prices(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    prod_key = query.data.replace("p_", "")
    prod = ALL_PRODUCTS.get(prod_key)
    if not prod:
        return
    context.user_data['current_prod_key'] = prod_key
    keyboard = []
    for idx, (plan, price) in enumerate(prod["prices"]):
        keyboard.append([InlineKeyboardButton(f"{plan} — ₹{price}", callback_data=f"price_{prod_key}_{idx}")])
    keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="shop_now")])
    await query.message.edit_text(f"<b>{prod['name']}</b>\nSelect Plan:", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))

async def order_summary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    parts = query.data.split("_")
    prod_key = parts[1]
    price_idx = int(parts[2])
    
    prod = ALL_PRODUCTS.get(prod_key)
    plan, price = prod["prices"][price_idx]
    
    context.user_data['pending_order'] = {'prod_name': prod['name'], 'plan': plan, 'price': price, 'prod_key': prod_key}
    
    text = f"<b>📋 ORDER SUMMARY</b>\n\nProduct: {prod['name']}\nPlan: {plan}\nPrice: ₹{price}.00"
    keyboard = [
        [InlineKeyboardButton("✅ Confirm & Pay", callback_data="confirm_pay")],
        [InlineKeyboardButton("🔙 Back", callback_data=f"p_{prod_key}")]
    ]
    await query.message.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))

async def confirm_pay(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    order = context.user_data.get('pending_order')
    if not order:
        return
    
    caption = f"<b>💼 PAY TO QR</b>\n\nProduct: {order['prod_name']}\nPlan: {order['plan']}\nAmount: ₹{order['price']}.00\n\n<b>Scan QR and Pay, then click below:</b>"
    keyboard = [
        [InlineKeyboardButton("⚙️ I Have Paid", callback_data="i_have_paid")],
        [InlineKeyboardButton("❌ Cancel", callback_data="cancel_order")]
    ]
    try:
        await query.message.delete()
    except:
        pass
    
    await context.bot.send_photo(chat_id=query.message.chat_id, photo=QR_IMAGE_URL, caption=caption, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))

async def i_have_paid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data['state'] = 'WAITING_UTR'
    await query.message.reply_text("<b>🔢 Enter your 12-digit UTR Number:</b>", parse_mode="HTML")

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
            if cust_id not in USER_ORDER_HISTORY:
                USER_ORDER_HISTORY[cust_id] = []
            USER_ORDER_HISTORY[cust_id].append({'prod_name': order_info['prod_name'], 'plan': order_info['plan'], 'date': datetime.now().strftime("%d %b %Y"), 'key': key_text})
            USER_PROFILES[cust_id]['total_orders'] += 1
            await context.bot.send_message(chat_id=cust_id, text=f"<b>🎉 YOUR KEY:</b>\n<code>{key_text}</code>", parse_mode="HTML")
            await update.message.reply_text("✅ Key sent successfully!")
            context.user_data['admin_state'] = None
        return

    if state == 'WAITING_UTR':
        utr = update.message.text.strip()
        if utr in USED_UTRS:
            await update.message.reply_text("⚠️ UTR already used!")
            return
        context.user_data['utr'] = utr
        context.user_data['state'] = 'WAITING_SCREENSHOT'
        await update.message.reply_text("<b>📸 Send Payment Screenshot image:</b>", parse_mode="HTML")
        return

    if state == 'WAITING_SCREENSHOT':
        if not update.message.photo:
            await update.message.reply_text("⚠️ Please send a screenshot image!")
            return
        photo_id = update.message.photo[-1].file_id
        order = context.user_data.get('pending_order')
        utr = context.user_data.get('utr')
        USED_UTRS.add(utr)

        admin_text = f"🚨 <b>NEW ORDER</b>\nUser: {user.first_name} ({user.id})\nProduct: {order['prod_name']} - {order['plan']}\nPrice: ₹{order['price']}\nUTR: {utr}"
        admin_kb = [[InlineKeyboardButton("✅ Approve", callback_data="admin_approve"), InlineKeyboardButton("❌ Reject", callback_data="admin_reject")]]
        admin_msg = await context.bot.send_photo(chat_id=ADMIN_ID, photo=photo_id, caption=admin_text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(admin_kb))
        
        ACTIVE_ORDERS[admin_msg.message_id] = {'user_id': user.id, 'prod_name': order['prod_name'], 'plan': order['plan'], 'price': order['price']}
        context.user_data['state'] = None
        await update.message.reply_text("⏳ Payment submitted! Waiting for admin verification.")

async def handle_admin_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.from_user.id != ADMIN_ID:
        return
    admin_msg_id = query.message.message_id
    order_info = ACTIVE_ORDERS.get(admin_msg_id)
    if not order_info:
        return
    if query.data == "admin_approve":
        context.user_data['admin_state'] = 'AWAITING_KEY'
        context.user_data['active_admin_msg_id'] = admin_msg_id
        await query.message.reply_text("🔑 Send the KEY:")
    elif query.data == "admin_reject":
        await context.bot.send_message(chat_id=order_info['user_id'], text="❌ Order Rejected.")
        await query.message.reply_text("❌ Rejected.")

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
    app.add_handler(CallbackGroup := CallbackQueryHandler(ios_list, pattern="^ios_list$"))
    app.add_handler(CallbackQueryHandler(pc_list, pattern="^pc_list$"))
    app.add_handler(CallbackQueryHandler(show_product_prices, pattern="^p_"))
    app.add_handler(CallbackQueryHandler(order_summary, pattern="^price_"))
    app.add_handler(CallbackQueryHandler(confirm_pay, pattern="^confirm_pay$"))
    app.add_handler(CallbackQueryHandler(i_have_paid, pattern="^i_have_paid$"))
    app.add_handler(CallbackQueryHandler(cancel_order, pattern="^cancel_order$"))
    app.add_handler(CallbackQueryHandler(handle_admin_action, pattern="^admin_"))
    app.add_handler(MessageHandler(filters.TEXT | filters.PHOTO, handle_user_message))
    app.run_polling()

def main():
    keep_alive()
    while True:
        try:
            start_bot()
        except Exception as e:
            print(f"Error: {e}")
            time.sleep(1)

if __name__ == "__main__":
    main()
