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
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, ContextTypes, filters

# 🔗 വെബ് അഡ്മിൻ പാനൽ ഫയൽ ഇംപോർട്ട് ചെയ്യുന്നു
import web_admin

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ==========================================
# ⚙️ CONFIGURATION
# ==========================================
BOT_TOKEN = "8892856619:AAGZhdOv389_AaKvbcbInlJAiDMOwQxOeHc"
ADMIN_ID = 7616127905
GMAIL_USER = os.environ.get("GMAIL_USER", "athulsudin37@gmail.com")
GMAIL_APP_PASS = os.environ.get("GMAIL_APP_PASS", "")
DB_FILE = "bot_database.db"

STORE_CONFIG = {"support_username": "@Athulsudin", "how_to_use_link": "https://t.me/chatelitehackers", "welcome_message": "🚀 <b>Welcome to ELITE HACKERS</b>\n\nTap below to begin."}
UPI_CONFIG = {"fampay_token": "9544113089@fam", "fampay_qr": ""}

def init_db():
    conn = sqlite3.connect(DB_FILE); c = conn.cursor()
    c.execute('CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, full_name TEXT, username TEXT, joined_date TEXT, orders_count INTEGER DEFAULT 0)')
    c.execute('CREATE TABLE IF NOT EXISTS order_history (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, prod_name TEXT, plan TEXT, key_delivered TEXT, amount REAL, utr TEXT, timestamp TEXT)')
    c.execute('CREATE TABLE IF NOT EXISTS products (prod_key TEXT PRIMARY KEY, name TEXT, category TEXT, prices TEXT, download_link TEXT, icon TEXT DEFAULT "⚡", maintenance INTEGER DEFAULT 0, stock_out INTEGER DEFAULT 0, channel_link TEXT, reseller_price REAL DEFAULT 0, remap_id TEXT)')
    c.execute('CREATE TABLE IF NOT EXISTS keys_inventory (id INTEGER PRIMARY KEY AUTOINCREMENT, prod_key TEXT, plan TEXT, item_key TEXT, is_used INTEGER DEFAULT 0)')
    c.execute('CREATE TABLE IF NOT EXISTS store_settings (id INTEGER PRIMARY KEY DEFAULT 1, support_username TEXT, how_to_use_link TEXT, welcome_message TEXT)')
    c.execute('CREATE TABLE IF NOT EXISTS upi_settings (id INTEGER PRIMARY KEY DEFAULT 1, paytm_token TEXT, paytm_qr TEXT, fampay_token TEXT, fampay_qr TEXT)')
    c.execute('CREATE TABLE IF NOT EXISTS api_config (id INTEGER PRIMARY KEY DEFAULT 1, api_url TEXT, api_key TEXT, master_key TEXT, active_payment_gateway TEXT DEFAULT "paytm", fampay_token TEXT, paytm_merchant_id TEXT, paytm_upi_id TEXT, bot_name TEXT, support_username TEXT)')
    conn.commit(); conn.close()

def load_settings():
    conn = sqlite3.connect(DB_FILE); c = conn.cursor()
    c.execute('SELECT support_username, how_to_use_link, welcome_message FROM store_settings WHERE id = 1'); r = c.fetchone()
    if r: STORE_CONFIG["support_username"], STORE_CONFIG["how_to_use_link"], STORE_CONFIG["welcome_message"] = r[0] or "@Athulsudin", r[1] or "", r[2] or ""
    else: c.execute('INSERT INTO store_settings VALUES (1, "@Athulsudin", "", "Welcome!")')
    c.execute('SELECT fampay_token, fampay_qr FROM upi_settings WHERE id = 1'); r2 = c.fetchone()
    if r2: UPI_CONFIG["fampay_token"], UPI_CONFIG["fampay_qr"] = r2[0] or "9544113089@fam", r2[1] or ""
    else: c.execute('INSERT INTO upi_settings VALUES (1, "", "", "9544113089@fam", "")')
    conn.commit(); conn.close()

init_db(); load_settings()

def get_ist_time():
    return datetime.now(pytz.timezone('Asia/Kolkata')).strftime("%d %b %Y, %I:%M %p (IST)")

def db_pop_key(prod_key, plan):
    conn = sqlite3.connect(DB_FILE); c = conn.cursor()
    c.execute('SELECT id, item_key FROM keys_inventory WHERE prod_key = ? AND plan = ? AND is_used = 0 ORDER BY id ASC LIMIT 1', (prod_key, plan))
    r = c.fetchone()
    if r:
        c.execute('UPDATE keys_inventory SET is_used = 1 WHERE id = ?', (r[0],))
        conn.commit(); conn.close(); return r[1]
    conn.close(); return None

def get_prods_by_cat(category):
    conn = sqlite3.connect(DB_FILE); c = conn.cursor()
    c.execute('SELECT prod_key, name, prices, icon, download_link FROM products WHERE category = ? AND maintenance = 0', (category,))
    rows = c.fetchall(); conn.close()
    return {r[0]: {"name": r[1], "prices": json.loads(r[2]), "icon": r[3] or "⚡", "link": r[4]} for r in rows}

def get_prod(prod_key):
    conn = sqlite3.connect(DB_FILE); c = conn.cursor()
    c.execute('SELECT name, prices, icon, download_link FROM products WHERE prod_key = ?', (prod_key,))
    r = c.fetchone(); conn.close()
    return {"name": r[0], "prices": json.loads(r[1]), "icon": r[2] or "⚡", "link": r[3]} if r else None

async def verify_payment(amount):
    def _imap():
        try:
            m = imaplib.IMAP4_SSL("imap.gmail.com"); m.login(GMAIL_USER, GMAIL_APP_PASS); m.select("inbox")
            st, msgs = m.search(None, "ALL")
            if st != "OK" or not msgs[0]: m.logout(); return False
            for mid in reversed(msgs[0].split()[-30:]):
                _, d = m.fetch(mid, "(RFC822)")
                for part in d:
                    if isinstance(part, tuple):
                        msg = email.message_from_bytes(part[1])
                        b = ""
                        if msg.is_multipart():
                            for p in msg.walk():
                                if p.get_content_type() in ["text/plain", "text/html"]:
                                    b += " " + p.get_payload(decode=True).decode('utf-8', errors='ignore')
                        else: b = msg.get_payload(decode=True).decode('utf-8', errors='ignore')
                        if f"{float(amount):.2f}" in b: m.logout(); return True
            m.logout(); return False
        except Exception: return False
    return await asyncio.to_thread(_imap)

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    load_settings()
    conn = sqlite3.connect(DB_FILE); c = conn.cursor()
    c.execute('INSERT INTO users VALUES (?, ?, ?, ?, 0) ON CONFLICT(user_id) DO NOTHING', (u.id, u.full_name, f"@{u.username}" if u.username else "N/A", get_ist_time()))
    conn.commit(); conn.close()
    
    kb = [
        [InlineKeyboardButton("🛒 Shop Now", callback_data="shop_now")],
        [InlineKeyboardButton("📦 My Orders", callback_data="my_orders"), InlineKeyboardButton("👤 Profile", callback_data="profile")],
        [InlineKeyboardButton("💬 Support", callback_data="support"), InlineKeyboardButton("ℹ️ How to Use", callback_data="how_to_use")]
    ]
    if u.id == ADMIN_ID:
        r_url = os.environ.get("RENDER_EXTERNAL_URL", "http://localhost:8080")
        kb.append([InlineKeyboardButton("👑 Web Admin Panel", url=r_url)])
        
    rm = InlineKeyboardMarkup(kb)
    if update.message: await update.message.reply_text(STORE_CONFIG["welcome_message"], parse_mode="HTML", reply_markup=rm)
    elif update.callback_query: await update.callback_query.answer(); await update.callback_query.message.edit_text(STORE_CONFIG["welcome_message"], parse_mode="HTML", reply_markup=rm)

async def profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer(); u = update.effective_user
    conn = sqlite3.connect(DB_FILE); c = conn.cursor(); c.execute('SELECT joined_date, orders_count FROM users WHERE user_id = ?', (u.id,)); r = c.fetchone(); conn.close()
    text = f"👤 <b>PROFILE</b>\n\nName: {u.full_name}\nID: <code>{u.id}</code>\nJoined: {r[0] if r else 'N/A'}\nOrders: {r[1] if r else 0}"
    await q.message.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="main_menu")]]))

async def my_orders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer(); u = update.effective_user
    conn = sqlite3.connect(DB_FILE); c = conn.cursor(); c.execute('SELECT prod_name, plan, key_delivered, timestamp FROM order_history WHERE user_id = ? ORDER BY id DESC LIMIT 5', (u.id,)); rows = c.fetchall(); conn.close()
    text = "🔑 <b>MY ORDERS</b>\n\n" + ("\n\n".join([f"📦 {r[0]} ({r[1]})\nKey: <code>{r[2]}</code>\nDate: {r[3]}" for r in rows]) if rows else "No orders yet!")
    await q.message.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="main_menu")]]))

async def support(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer(); load_settings()
    await q.message.edit_text(f"📩 <b>Support:</b> {STORE_CONFIG['support_username']}", parse_mode="HTML", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="main_menu")]]))

async def how_to_use(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer(); load_settings()
    await q.message.edit_text("📖 <b>How to Use:</b>\n1. Select Product\n2. Pay dynamic amount\n3. Tap Verify for instant key!", parse_mode="HTML", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="main_menu")]]))

async def shop_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    kb = [[InlineKeyboardButton("📱 Non-Root", callback_data="cat_non_root"), InlineKeyboardButton("⚡ Root", callback_data="cat_root")], [InlineKeyboardButton("🍏 iOS", callback_data="cat_ios"), InlineKeyboardButton("💻 PC", callback_data="cat_pc")], [InlineKeyboardButton("💎 Likes", callback_data="cat_likes")], [InlineKeyboardButton("🏠 Menu", callback_data="main_menu")]]
    await q.message.edit_text("🛒 <b>Select Category:</b>", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(kb))

async def show_cat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer(); cat = q.data.replace("cat_", "")
    prods = get_prods_by_cat(cat)
    kb = [[InlineKeyboardButton(f"{p['icon']} {p['name']}", callback_data=f"p_{k}")] for k, p in prods.items()]
    kb.append([InlineKeyboardButton("🔙 Back", callback_data="shop_now")])
    await q.message.edit_text("🔥 <b>Select Product:</b>", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(kb))

async def show_prod(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer(); pkey = q.data.replace("p_", ""); prod = get_prod(pkey)
    if not prod: return
    kb = [[InlineKeyboardButton(f"{pl.replace('_',' ')} — ₹{pr}", callback_data=f"buy_{pkey}_{pl}_{pr}")] for pl, pr in prod["prices"]]
    if prod.get("link"): kb.append([InlineKeyboardButton("📥 Download File", url=prod["link"])])
    kb.append([InlineKeyboardButton("🔙 Back", callback_data="shop_now")])
    await q.message.edit_text(f"🛒 <b>{prod['name']}</b>\nChoose Plan:", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(kb))

async def buy_plan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    _, pkey, plan, price = q.data.split("_", 3); base = float(price); final = round(base + (random.randint(1,99)/100.0), 2)
    prod = get_prod(pkey); name = prod['name'] if prod else pkey
    context.user_data['order'] = {"key": pkey, "name": name, "plan": plan, "price": final}
    kb = [[InlineKeyboardButton("✅ Confirm & Pay", callback_data="confirm_pay")], [InlineKeyboardButton("🔙 Back", callback_data=f"p_{pkey}")]]
    await q.message.edit_text(f"📋 <b>ORDER SUMMARY</b>\n\nProduct: {name}\nPlan: {plan}\nTotal: ₹{final:.2f}", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(kb))

async def confirm_pay(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer(); o = context.user_data.get('order'); load_settings()
    if not o: return
    upi = UPI_CONFIG.get("fampay_token") or "9544113089@fam"
    qr_url = UPI_CONFIG.get("fampay_qr") or f"https://api.qrserver.com/v1/create-qr-code/?size=500x500&data={urllib.parse.quote(f'upi://pay?pa={upi}&pn=ELITE&am={o[\"price\"]:.2f}&cu=INR')}"
    kb = [[InlineKeyboardButton("VERIFY PAYMENT", callback_data="verify_pay")], [InlineKeyboardButton("❌ Cancel", callback_data="main_menu")]]
    await q.message.delete()
    await context.bot.send_photo(chat_id=q.message.chat_id, photo=qr_url, caption=f"💰 <b>Scan & Pay ₹{o['price']:.2f}</b>\n\nTap Verify below after paying.", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(kb))

async def verify_btn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer("Checking payment..."); u = update.effective_user; o = context.user_data.get('order')
    if not o: return
    v_msg = await q.message.reply_text("🔄 Verifying payment...")
    if await verify_payment(o['price']):
        await v_msg.delete(); key = db_pop_key(o['key'], o['plan'])
        conn = sqlite3.connect(DB_FILE); c = conn.cursor()
        c.execute('INSERT INTO order_history (user_id, prod_name, plan, key_delivered, amount, utr, timestamp) VALUES (?, ?, ?, ?, ?, "AUTO", ?)', (u.id, o['name'], o['plan'], key or "MANUAL_DELIVERY", o['price'], get_ist_time()))
        c.execute('UPDATE users SET orders_count = orders_count + 1 WHERE user_id = ?', (u.id,))
        conn.commit(); conn.close()
        if key:
            await context.bot.send_message(chat_id=u.id, text=f"🎉 <b>ORDER READY!</b>\n\nProduct: {o['name']}\nPlan: {o['plan']}\nKey: <code>{key}</code>", parse_mode="HTML")
            await context.bot.send_message(chat_id=ADMIN_ID, text=f"🟢 <b>AUTO DELIVERED:</b> {u.first_name} bought {o['name']} (₹{o['price']})")
        else:
            await context.bot.send_message(chat_id=u.id, text="✅ <b>Payment verified!</b> Key will be sent manually by admin soon.")
            await context.bot.send_message(chat_id=ADMIN_ID, text=f"🚨 <b>OUT OF STOCK:</b> {u.first_name} paid ₹{o['price']} for {o['name']} ({o['plan']})")
    else:
        await v_msg.delete(); await q.message.reply_text("❌ Payment not found yet. Please try again after completing payment.")

def start_bot_polling():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CallbackQueryHandler(start_command, pattern="^main_menu$"))
    app.add_handler(CallbackQueryHandler(profile, pattern="^profile$"))
    app.add_handler(CallbackQueryHandler(my_orders, pattern="^my_orders$"))
    app.add_handler(CallbackQueryHandler(support, pattern="^support$"))
    app.add_handler(CallbackQueryHandler(how_to_use, pattern="^how_to_use$"))
    app.add_handler(CallbackQueryHandler(shop_menu, pattern="^shop_now$"))
    app.add_handler(CallbackQueryHandler(show_cat, pattern="^cat_"))
    app.add_handler(CallbackQueryHandler(show_prod, pattern="^p_"))
    app.add_handler(CallbackQueryHandler(buy_plan, pattern="^buy_"))
    app.add_handler(CallbackQueryHandler(confirm_pay, pattern="^confirm_pay$"))
    app.add_handler(CallbackQueryHandler(verify_btn, pattern="^verify_pay$"))
    
    print("🤖 Telegram Bot Polling Started...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    # 1. വെബ് അഡ്മിൻ പാനൽ ബാക്ക്ഗ്രൗണ്ടിൽ ഓൺ ആക്കുന്നു
    print("🌐 Launching Web Admin Panel in Background Thread...")
    web_thread = Thread(target=web_admin.run_web)
    web_thread.daemon = True
    web_thread.start()

    # 2. ടെലിഗ്രാം ബോട്ട് റൺ ചെയ്യുന്നു
    while True:
        try:
            start_bot_polling()
        except Exception as e:
            print(f"Bot restarting due to error: {e}")
            time.sleep(1)







