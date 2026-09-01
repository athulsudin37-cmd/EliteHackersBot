import asyncio, email, imaplib, json, logging, os, random, re, sqlite3, time, urllib.parse
from datetime import datetime
from threading import Thread
import pytz
from flask import Flask, jsonify, redirect, render_template_string, request, session
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler,
                          ContextTypes, MessageHandler, filters)

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = "8892856619:AAGZhdOv389_AaKvbcbInlJAiDMOwQxOeHc"
ADMIN_ID = 7616127905
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "athulsudin1234")
RECEIVER_UPI_ID = "9544113089@fam"
GMAIL_USER = os.environ.get("GMAIL_USER", "athulsudin37@gmail.com")
GMAIL_APP_PASS = os.environ.get("GMAIL_APP_PASS", "")

ACTIVE_ORDERS, MAINTENANCE_MODE, STOCK_OUT_MODE, PRODUCT_LINKS, PRODUCT_ICONS, KEYS_STOCK = {}, {}, {}, {}, {}, {}

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
        "— 🏦 Direct deals with every supplier\n— 💧 Instant delivery after payment\n"
        "— 🪙 Guaranteed discounted prices\n— 📞 24/7 admin support\n\n"
        "<b>Tap any button below to begin.</b>"
    )
}
UPI_CONFIG = {"paytm_token": "", "paytm_qr": "", "fampay_token": "", "fampay_qr": ""}
DB_FILE = "bot_database.db"

def init_db():
    with sqlite3.connect(DB_FILE) as conn:
        c = conn.cursor()
        c.execute('CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, full_name TEXT, username TEXT, joined_date TEXT, orders_count INTEGER DEFAULT 0)')
        c.execute('CREATE TABLE IF NOT EXISTS order_history (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, prod_name TEXT, plan TEXT, key_delivered TEXT, amount REAL, utr TEXT, timestamp TEXT)')
        c.execute('CREATE TABLE IF NOT EXISTS products (prod_key TEXT PRIMARY KEY, name TEXT, category TEXT, prices TEXT, download_link TEXT, icon TEXT DEFAULT "⚡", maintenance INTEGER DEFAULT 0, stock_out INTEGER DEFAULT 0, channel_link TEXT, reseller_price REAL DEFAULT 0, remap_id TEXT)')
        c.execute('CREATE TABLE IF NOT EXISTS keys_inventory (id INTEGER PRIMARY KEY AUTOINCREMENT, prod_key TEXT, plan TEXT, item_key TEXT, is_used INTEGER DEFAULT 0)')
        c.execute('CREATE TABLE IF NOT EXISTS store_settings (id INTEGER PRIMARY KEY DEFAULT 1, support_username TEXT, how_to_use_link TEXT, welcome_message TEXT)')
        c.execute('CREATE TABLE IF NOT EXISTS upi_settings (id INTEGER PRIMARY KEY DEFAULT 1, paytm_token TEXT, paytm_qr TEXT, fampay_token TEXT, fampay_qr TEXT)')
        conn.commit()

def load_store_and_upi_settings():
    with sqlite3.connect(DB_FILE) as conn:
        c = conn.cursor()
        c.execute('SELECT support_username, how_to_use_link, welcome_message FROM store_settings WHERE id = 1')
        if row := c.fetchone():
            STORE_CONFIG.update({"support_username": row[0] or STORE_CONFIG["support_username"], "how_to_use_link": row[1] or STORE_CONFIG["how_to_use_link"], "welcome_message": row[2] or STORE_CONFIG["welcome_message"]})
        else:
            c.execute('INSERT INTO store_settings VALUES (1, ?, ?, ?)', (STORE_CONFIG["support_username"], STORE_CONFIG["how_to_use_link"], STORE_CONFIG["welcome_message"]))
        
        c.execute('SELECT paytm_token, paytm_qr, fampay_token, fampay_qr FROM upi_settings WHERE id = 1')
        if row := c.fetchone():
            UPI_CONFIG.update({"paytm_token": row[0] or "", "paytm_qr": row[1] or "", "fampay_token": row[2] or "", "fampay_qr": row[3] or ""})
        else:
            c.execute('INSERT INTO upi_settings VALUES (1, "", "", "", "")')
        conn.commit()

def db_add_or_update_user(user_id, full_name, username, joined_date):
    with sqlite3.connect(DB_FILE) as conn:
        conn.cursor().execute('INSERT INTO users VALUES (?, ?, ?, ?, 0) ON CONFLICT(user_id) DO UPDATE SET full_name=?, username=?', (user_id, full_name, username, joined_date, full_name, username))
        conn.commit()

def db_get_user(user_id):
    with sqlite3.connect(DB_FILE) as conn:
        return conn.cursor().execute('SELECT full_name, username, joined_date, orders_count FROM users WHERE user_id = ?', (user_id,)).fetchone()

def db_add_order(user_id, prod_name, plan, key_delivered, amount, utr, timestamp):
    with sqlite3.connect(DB_FILE) as conn:
        c = conn.cursor()
        c.execute('INSERT INTO order_history (user_id, prod_name, plan, key_delivered, amount, utr, timestamp) VALUES (?, ?, ?, ?, ?, ?, ?)', (user_id, prod_name, plan, key_delivered, amount, utr, timestamp))
        c.execute('UPDATE users SET orders_count = orders_count + 1 WHERE user_id = ?', (user_id,))
        conn.commit()

def db_get_user_history(user_id, limit=5):
    with sqlite3.connect(DB_FILE) as conn:
        return conn.cursor().execute('SELECT prod_name, plan, key_delivered, timestamp FROM order_history WHERE user_id = ? ORDER BY id DESC LIMIT ?', (user_id, limit)).fetchall()

def db_add_keys_to_inventory(prod_key, plan, keys_list):
    with sqlite3.connect(DB_FILE) as conn:
        c = conn.cursor()
        for k in keys_list:
            if k.strip(): c.execute('INSERT INTO keys_inventory (prod_key, plan, item_key, is_used) VALUES (?, ?, ?, 0)', (prod_key, plan, k.strip()))
        conn.commit()

def db_pop_auto_key(prod_key, plan):
    with sqlite3.connect(DB_FILE) as conn:
        c = conn.cursor()
        c.execute('SELECT id, item_key FROM keys_inventory WHERE prod_key = ? AND plan = ? AND is_used = 0 ORDER BY id ASC LIMIT 1', (prod_key, plan))
        if row := c.fetchone():
            c.execute('UPDATE keys_inventory SET is_used = 1 WHERE id = ?', (row[0],))
            conn.commit()
            return row[1]
    return None

def db_get_key_count(prod_key, plan):
    with sqlite3.connect(DB_FILE) as conn:
        return conn.cursor().execute('SELECT COUNT(*) FROM keys_inventory WHERE prod_key = ? AND plan = ? AND is_used = 0', (prod_key, plan)).fetchone()[0]

NON_ROOT_PRODUCTS, ROOT_PRODUCTS, IOS_PRODUCTS, PC_PRODUCTS, LIKE_PRODUCTS = {}, {}, {}, {}, {}
ALL_CATEGORIES = [NON_ROOT_PRODUCTS, ROOT_PRODUCTS, IOS_PRODUCTS, PC_PRODUCTS, LIKE_PRODUCTS]

def load_products_from_db():
    with sqlite3.connect(DB_FILE) as conn:
        rows = conn.cursor().execute('SELECT prod_key, name, category, prices, download_link, icon, maintenance, stock_out FROM products').fetchall()
    
    cat_map = {"non_root": NON_ROOT_PRODUCTS, "root": ROOT_PRODUCTS, "ios": IOS_PRODUCTS, "pc": PC_PRODUCTS, "likes": LIKE_PRODUCTS}
    for cat in cat_map.values(): cat.clear()
    
    for p_key, name, category, prices_json, d_link, icon, maint, stockout in rows:
        prices = json.loads(prices_json)
        p_icon = icon or "⚡"
        if category in cat_map: cat_map[category][p_key] = {"name": name, "prices": [tuple(p) for p in prices], "icon": p_icon}
        if d_link: PRODUCT_LINKS[p_key] = d_link
        PRODUCT_ICONS[p_key] = p_icon
        MAINTENANCE_MODE[p_key] = bool(maint)
        STOCK_OUT_MODE[p_key] = bool(stockout)

def db_seed_initial_products():
    with sqlite3.connect(DB_FILE) as conn:
        c = conn.cursor()
        if c.execute('SELECT count(*) FROM products').fetchone()[0] == 0:
            for key, name, cat, prices, icon in [("bala_mod", "BALA MOD NON ROOT", "non_root", [("1_Day", 420)], "⚙️"), ("rapid_core", "RAPID CORE INJECTOR", "root", [("1_Day", 90)], "⚡"), ("migul_pro", "MIGUL PRO IOS", "ios", [("1_Day", 200)], "🍏"), ("br_mod_pc", "BR MOD PC", "pc", [("1_Day", 150)], "💻"), ("auto_like", "AUTO LIKE EVERY DAY", "likes", [("7_DAYS", 90)], "💎")]:
                c.execute('INSERT INTO products VALUES (?, ?, ?, ?, ?, ?, 0, 0, NULL, 0, NULL)', (key, name, cat, json.dumps(prices), "", icon))
            conn.commit()

init_db()
load_store_and_upi_settings()
db_seed_initial_products()
load_products_from_db()

def sanitize_product_key(raw_name):
    clean = re.sub(r'[^a-zA-Z0-9]', '_', raw_name).strip('_').lower()
    return clean if clean else f"product_{int(time.time())}"

def get_product_by_key(prod_key):
    for cat in ALL_CATEGORIES:
        if prod_key in cat: return cat[prod_key]
    return None

def get_ist_time(): return datetime.now(pytz.timezone('Asia/Kolkata')).strftime("%d %b %Y, %I:%M %p (IST)")
def generate_dynamic_qr_url(upi_id, amount, note="FF Service"):
    if UPI_CONFIG.get("fampay_qr"): return UPI_CONFIG["fampay_qr"]
    return f"https://api.qrserver.com/v1/create-qr-code/?size=500x500&data={urllib.parse.quote(f'upi://pay?pa={upi_id}&pn=ELITE_HACKERS&am={amount:.2f}&cu=INR&tn={urllib.parse.quote(note)}')}"

async def check_email_once(expected_amount, utr=None):
    def _imap_check():
        try:
            mail = imaplib.IMAP4_SSL("imap.gmail.com")
            mail.login(GMAIL_USER, GMAIL_APP_PASS)
            mail.select("inbox")
            _, messages = mail.search(None, "ALL")
            if not messages[0]:
                mail.logout()
                return False, "No emails found."
            expected_str = f"{float(expected_amount):.2f}"
            for msg_id in reversed(messages[0].split()[-40:]):
                _, msg_data = mail.fetch(msg_id, "(RFC822)")
                for response_part in msg_data:
                    if isinstance(response_part, tuple):
                        body = email.message_from_bytes(response_part[1]).get_payload(decode=True).decode('utf-8', errors='ignore')
                        body = ' '.join(re.sub(r'<[^>]+>', ' ', body).split())
                        if expected_str in body or (utr and str(utr).strip() in body):
                            mail.logout()
                            return True, "SUCCESS"
            mail.logout()
            return False, "Not found."
        except Exception as e:
            return False, str(e)
    return await asyncio.to_thread(_imap_check)

async def verify_fampay_gmail_payment(expected_amount, utr=None, retries=2, delay=2):
    for attempt in range(retries):
        success, _ = await check_email_once(expected_amount, utr)
        if success: return True, "SUCCESS"
        if attempt < retries - 1: await asyncio.sleep(delay)
    return False, "Payment notification not received."

# Flask Dashboard Setup
flask_app = Flask(__name__)
flask_app.secret_key = os.urandom(24)

ADMIN_HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>ELITE HACKERS - Admin Dashboard</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
    <link href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css" rel="stylesheet">
    <style>
        body { background-color: #0f172a; color: #f8fafc; font-family: sans-serif; }
        .card { background-color: #1e293b; border: 1px solid #334155; color: #f8fafc; border-radius: 12px; margin-bottom: 20px; }
        .btn-custom { background-color: #6366f1; color: white; border: none; }
        .form-control, .form-select { background-color: #0f172a; border: 1px solid #334155; color: white; }
    </style>
</head>
<body class="p-4">
    <div class="container">
        <h2 class="text-primary mb-4">👑 ELITE HACKERS Admin Dashboard</h2>
        <div class="card p-4">
            <h4 class="mb-3">Quick Actions</h4>
            <a href="/logout" class="btn btn-outline-danger btn-sm">Logout</a>
        </div>
    </div>
</body>
</html>
"""

@flask_app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        if request.form.get('password') == ADMIN_PASSWORD:
            session['logged_in'] = True
            return redirect('/admin')
        return "Invalid Password! <a href='/login'>Try Again</a>"
    return '<form method="POST">Password: <input type="password" name="password"><button type="submit">Login</button></form>'

@flask_app.route('/logout')
def logout():
    session.clear()
    return redirect('/login')

@flask_app.route('/')
@flask_app.route('/admin')
def admin_dashboard():
    if not session.get('logged_in'): return redirect('/login')
    with sqlite3.connect(DB_FILE) as conn:
        c = conn.cursor()
        tot_users = c.execute('SELECT COUNT(*) FROM users').fetchone()[0]
        ord_info = c.execute('SELECT COUNT(*), SUM(amount) FROM order_history').fetchone()
        tot_keys = c.execute('SELECT COUNT(*) FROM keys_inventory WHERE is_used = 0').fetchone()[0]
    return f"Admin Panel Active! Total Users: {tot_users}, Total Orders: {ord_info[0] or 0}, Total Revenue: ₹{ord_info[1] or 0.0}, Active Keys: {tot_keys}. <a href='/logout'>Logout</a>"

def keep_alive():
    Thread(target=lambda: flask_app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 8080))), daemon=True).start()

# Telegram Bot Handlers
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user = update.effective_user
        if not user: return
        if not db_get_user(user.id):
            db_add_or_update_user(user.id, user.full_name, f"@{user.username}" if user.username else "N/A", get_ist_time())
        
        keyboard = [
            [InlineKeyboardButton("🛒 Shop Now", callback_data="shop_now")],
            [InlineKeyboardButton("📦 My Orders", callback_data="my_orders"), InlineKeyboardButton("👤 Profile", callback_data="profile")],
            [InlineKeyboardButton("💬 Support", callback_data="support"), InlineKeyboardButton("ℹ️ How to Use", callback_data="how_to_use")]
        ]
        if user.id == ADMIN_ID:
            keyboard.append([InlineKeyboardButton("👑 Admin Panel", callback_data="admin_panel_home")])
        
        markup = InlineKeyboardMarkup(keyboard)
        if update.message: await update.message.reply_text(STORE_CONFIG["welcome_message"], parse_mode="HTML", reply_markup=markup)
        elif update.callback_query:
            await update.callback_query.answer()
            try: await update.callback_query.message.edit_text(STORE_CONFIG["welcome_message"], parse_mode="HTML", reply_markup=markup)
            except: await update.callback_query.message.reply_text(STORE_CONFIG["welcome_message"], parse_mode="HTML", reply_markup=markup)
    except Exception as e: logger.error(f"Error in start: {e}")

async def admin_panel_home(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.from_user.id != ADMIN_ID: return
    keyboard = [
        [InlineKeyboardButton("➕ Add Key", callback_data="admin_opt_addkey"), InlineKeyboardButton("🔑 View Stock", callback_data="admin_opt_viewkeys")],
        [InlineKeyboardButton("🔙 Main Menu", callback_data="main_menu")]
    ]
    await query.message.edit_text("👑 <b>Admin Control Panel</b>", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))

async def admin_option_click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.from_user.id != ADMIN_ID: return
    flows = {"admin_opt_addkey": ("WAITING_ADDKEY", "Format: <code>prod_key plan_name key1, key2</code>"), "admin_opt_viewkeys": ("WAITING_VIEWKEYS", "Format: <code>prod_key plan_name</code>")}
    if query.data in flows:
        context.user_data['admin_flow'] = flows[query.data][0]
        await query.message.edit_text(flows[query.data][1], parse_mode="HTML", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="admin_panel_home")]]))

async def profile_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = update.effective_user
    u = db_get_user(user.id)
    text = f"👤 <b>PROFILE</b>\n\nName: {u[0] if u else user.full_name}\nID: <code>{user.id}</code>\nOrders: {u[3] if u else 0}"
    await query.message.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="main_menu")]]))

async def my_orders_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    history = db_get_user_history(update.effective_user.id)
    text = "<b>📦 MY ORDERS</b>\n\n" + ("No orders yet!" if not history else "\n".join([f"• <b>{i[0]}</b> ({i[1]})\nKey: <code>{i[2]}</code>\nDate: {i[3]}\n" for i in history]))
    await query.message.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="main_menu")]]))

async def support_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.message.edit_text(f"📩 <b>Support:</b> {STORE_CONFIG['support_username']}", parse_mode="HTML", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="main_menu")]]))

async def how_to_use_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.message.edit_text("📖 <b>How to Use:</b> Select item, scan QR, pay exact amount, and tap Verify!", parse_mode="HTML", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="main_menu")]]))

async def store_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.message.edit_text("<b>🛒 SELECT CATEGORY:</b>", parse_mode="HTML", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔥 Panels", callback_data="cat_panels")], [InlineKeyboardButton("💎 Likes", callback_data="cat_likes")], [InlineKeyboardButton("🏠 Menu", callback_data="main_menu")]]))

async def category_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.message.edit_text("<b>📱 SELECT PANEL TYPE:</b>", parse_mode="HTML", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Non-Root", callback_data="non_root_list")], [InlineKeyboardButton("Root", callback_data="root_list")], [InlineKeyboardButton("iOS", callback_data="ios_list")], [InlineKeyboardButton("PC", callback_data="pc_list")], [InlineKeyboardButton("🔙 Shop", callback_data="shop_now")]]))

async def make_list_handler(update: Update, context: ContextTypes.DEFAULT_TYPE, product_dict, back_target):
    query = update.callback_query
    await query.answer()
    keyboard = [[InlineKeyboardButton(f"{d.get('icon', '⚡')} {d['name']}", callback_data=f"prod_{key}")] for key, d in product_dict.items()]
    keyboard.append([InlineKeyboardButton("🔙 Back", callback_data=back_target)])
    await query.message.edit_text("<b>Select Product:</b>", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))

async def show_product_prices(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    pkey = query.data.replace("prod_", "")
    prod = get_product_by_key(pkey)
    if not prod: return
    keyboard = [[InlineKeyboardButton(f"{plan.replace('_', ' ')} — ₹{price}", callback_data=f"plan_{pkey}_{plan}_{price}")] for plan, price in prod["prices"]]
    if PRODUCT_LINKS.get(pkey): keyboard.append([InlineKeyboardButton("📥 Download", url=PRODUCT_LINKS[pkey])])
    keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="shop_now")])
    await query.message.edit_text(f"<b>🛒 {prod['name']}</b>\n\nChoose plan:", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))

async def order_summary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, pkey, plan, price = query.data.split("_", 3)
    final_price = round(float(price) + random.randint(1, 99) / 100.0, 2)
    prod = get_product_by_key(pkey)
    context.user_data['pending_order'] = {'prod_key': pkey, 'prod_name': prod['name'] if prod else pkey, 'plan': plan, 'price': final_price}
    await query.message.edit_text(f"<b>📋 ORDER SUMMARY</b>\n\nProduct: {prod['name'] if prod else pkey}\nPlan: {plan}\nTotal: <b>₹{final_price:.2f}</b>", parse_mode="HTML", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ Confirm & Pay", callback_data="confirm_pay")], [InlineKeyboardButton("🔙 Back", callback_data=f"prod_{pkey}")]]))

async def confirm_pay(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    order = context.user_data.get('pending_order')
    if not order: return
    context.user_data.update({'order_cancelled': False, 'payment_complete': False})
    
    keyboard = [[InlineKeyboardButton("VERIFY PAYMENT", callback_data="verify_payment_btn")], [InlineKeyboardButton("➡️ Cancel", callback_data="cancel_order")]]
    try: await query.message.delete()
    except: pass
    
    sent = await context.bot.send_photo(chat_id=query.message.chat_id, photo=generate_dynamic_qr_url(UPI_CONFIG.get("fampay_token") or RECEIVER_UPI_ID, order['price']), caption=f"Scan & Pay: <b>₹{order['price']:.2f}</b>\nExpires in 5 mins.", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))
    context.user_data['qr_msg_id'] = sent.message_id

async def verify_payment_btn_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("Checking payment...")
    order = context.user_data.get('pending_order')
    if not order: return
    
    v_msg = await query.message.reply_text("🔄 Verifying payment...", parse_mode="HTML")
    verified, _ = await verify_fampay_gmail_payment(order['price'])
    try: await v_msg.delete()
    except: pass
    
    if verified:
        context.user_data['payment_complete'] = True
        try: await context.bot.delete_message(chat_id=query.message.chat_id, message_id=context.user_data.get('qr_msg_id'))
        except: pass
        
        key = db_pop_auto_key(order['prod_key'], order['plan'])
        if key:
            db_add_order(update.effective_user.id, order['prod_name'], order['plan'], key, order['price'], "AUTO", get_ist_time())
            await context.bot.send_message(chat_id=update.effective_user.id, text=f"🎉 <b>Success!</b>\nKey: <code>{key}</code>", parse_mode="HTML")
        else:
            await query.message.reply_text("✅ Payment verified! Admin will send your key shortly.", parse_mode="HTML")
    else:
        await query.message.reply_text("❌ Payment not found yet. Please try again after paying.", parse_mode="HTML")

async def cancel_order(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data['order_cancelled'] = True
    try: await query.message.delete()
    except: pass
    await start_command(update, context)

async def handle_user_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id == ADMIN_ID and (flow := context.user_data.get('admin_flow')):
        text = update.message.text.strip() if update.message.text else ""
        try:
            parts = text.split(" ")
            if flow == 'WAITING_ADDKEY':
                db_add_keys_to_inventory(parts[0], parts[1], [k.strip() for k in " ".join(parts[2:]).split(",")])
                await update.message.reply_text("✅ Keys added!")
            elif flow == 'WAITING_VIEWKEYS':
                await update.message.reply_text(f"🔑 Stock: {db_get_key_count(parts[0], parts[1])}")
        except Exception as e: await update.message.reply_text(f"❌ Error: {e}")
        context.user_data['admin_flow'] = None

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
    app.add_handler(CallbackQueryHandler(lambda u, c: make_list_handler(u, c, LIKE_PRODUCTS, "shop_now"), pattern="^cat_likes$"))
    app.add_handler(CallbackQueryHandler(lambda u, c: make_list_handler(u, c, NON_ROOT_PRODUCTS, "cat_panels"), pattern="^non_root_list$"))
    app.add_handler(CallbackQueryHandler(lambda u, c: make_list_handler(u, c, ROOT_PRODUCTS, "cat_panels"), pattern="^root_list$"))
    app.add_handler(CallbackQueryHandler(lambda u, c: make_list_handler(u, c, IOS_PRODUCTS, "cat_panels"), pattern="^ios_list$"))
    app.add_handler(CallbackQueryHandler(lambda u, c: make_list_handler(u, c, PC_PRODUCTS, "cat_panels"), pattern="^pc_list$"))
    app.add_handler(CallbackQueryHandler(show_product_prices, pattern="^prod_"))
    app.add_handler(CallbackQueryHandler(order_summary, pattern="^plan_"))
    app.add_handler(CallbackQueryHandler(confirm_pay, pattern="^confirm_pay$"))
    app.add_handler(CallbackQueryHandler(verify_payment_btn_handler, pattern="^verify_payment_btn$"))
    app.add_handler(CallbackQueryHandler(cancel_order, pattern="^cancel_order$"))
    app.add_handler(MessageHandler(filters.ALL, handle_user_message))
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    keep_alive()
    while True:
        try: start_bot()
        except Exception as e:
            print(f"Restarting: {e}")
            time.sleep(1)






