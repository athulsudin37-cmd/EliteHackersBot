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
from flask import Flask, render_template_string, request, jsonify, redirect, url_for, session
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
# ⚙️ CONFIGURATION & CONSTANTS
# ==========================================
BOT_TOKEN = os.environ.get("BOT_TOKEN", "8892856619:AAGZhdOv389_AaKvbcbInlJAiDMOwQxOeHc")
ADMIN_ID = int(os.environ.get("ADMIN_ID", "7616127905"))
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "athulsudin1234")
RECEIVER_UPI_ID = os.environ.get("RECEIVER_UPI_ID", "9544113089@fam")
GMAIL_USER = os.environ.get("GMAIL_USER", "athulsudin37@gmail.com")
GMAIL_APP_PASS = os.environ.get("GMAIL_APP_PASS", "")

ACTIVE_ORDERS = {}       
MAINTENANCE_MODE = {}    
STOCK_OUT_MODE = {}      
PRODUCT_LINKS = {}       
KEYS_STOCK = {}          

NON_ROOT_PRODUCTS = {}
ROOT_PRODUCTS = {}
IOS_PRODUCTS = {}
PC_PRODUCTS = {}
LIKE_PRODUCTS = {}

ALL_CATEGORIES = [NON_ROOT_PRODUCTS, ROOT_PRODUCTS, IOS_PRODUCTS, PC_PRODUCTS, LIKE_PRODUCTS]

# ==========================================
# 🗄️ SQLITE DATABASE MANAGEMENT (WITH WALLET & PLAN STOCK)
# ==========================================
DB_FILE = "bot_database.db"

def get_db_connection():
    return sqlite3.connect(DB_FILE, timeout=30.0)

def init_db():
    conn = get_db_connection()
    cursor = conn.cursor()
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            full_name TEXT,
            username TEXT,
            joined_date TEXT,
            orders_count INTEGER DEFAULT 0,
            wallet_balance REAL DEFAULT 0.0
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
            maintenance INTEGER DEFAULT 0,
            stock_out INTEGER DEFAULT 0,
            channel_link TEXT,
            icon TEXT DEFAULT '📦',
            plan_stock_out TEXT DEFAULT '{}'
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS api_config (
            id INTEGER PRIMARY KEY DEFAULT 1,
            api_url TEXT,
            api_key TEXT,
            master_key TEXT,
            active_payment_gateway TEXT DEFAULT 'fampay',
            fampay_upi TEXT DEFAULT '9544113089@fam',
            paytm_upi TEXT DEFAULT '',
            bot_name TEXT DEFAULT 'ELITE HACKERS',
            support_username TEXT DEFAULT '@Athulsudin'
        )
    ''')

    conn.commit()
    conn.close()

def db_add_or_update_user(user_id, full_name, username, joined_date):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO users (user_id, full_name, username, joined_date, orders_count, wallet_balance)
        VALUES (?, ?, ?, ?, 0, 0.0)
        ON CONFLICT(user_id) DO UPDATE SET full_name=?, username=?
    ''', (user_id, full_name, username, joined_date, full_name, username))
    conn.commit()
    conn.close()

def db_get_user(user_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT full_name, username, joined_date, orders_count, wallet_balance FROM users WHERE user_id = ?', (user_id,))
    row = cursor.fetchone()
    conn.close()
    return row

def db_update_wallet(user_id, amount):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('UPDATE users SET wallet_balance = wallet_balance + ? WHERE user_id = ?', (amount, user_id))
    conn.commit()
    conn.close()

def db_add_order(user_id, prod_name, plan, key_delivered, amount, utr, timestamp):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO order_history (user_id, prod_name, plan, key_delivered, amount, utr, timestamp)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    ''', (user_id, prod_name, plan, key_delivered, amount, utr, timestamp))
    cursor.execute('UPDATE users SET orders_count = orders_count + 1 WHERE user_id = ?', (user_id,))
    conn.commit()
    conn.close()

def db_get_user_history(user_id, limit=5):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('''
        SELECT prod_name, plan, key_delivered, timestamp 
        FROM order_history WHERE user_id = ? ORDER BY id DESC LIMIT ?
    ''', (user_id, limit))
    rows = cursor.fetchall()
    conn.close()
    return rows

def load_products_from_db():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT prod_key, name, category, prices, download_link, maintenance, stock_out, icon, channel_link FROM products')
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
        p_key, name, category, prices_json, d_link, maint, stockout, icon, ch_link = row
        prices = json.loads(prices_json)
        if category in cat_map:
            cat_map[category][p_key] = {
                "name": name, 
                "prices": [tuple(p) for p in prices],
                "icon": icon or "📦",
                "channel_link": ch_link or ""
            }
        if d_link:
            PRODUCT_LINKS[p_key] = d_link
        MAINTENANCE_MODE[p_key] = bool(maint)
        STOCK_OUT_MODE[p_key] = bool(stockout)

def db_seed_initial_products():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT count(*) FROM products')
    if cursor.fetchone()[0] == 0:
        initial_data = [
            ("bala_mod", "BALA MOD NON ROOT", "non_root", [("1_Day", 420)], "⚙️"),
            ("rapid_core", "RAPID CORE INJECTOR", "root", [("1_Day", 90)], "⚡"),
            ("migul_pro", "MIGUL PRO IOS", "ios", [("1_Day", 200)], "🍏"),
            ("br_mod_pc", "BR MOD PC", "pc", [("1_Day", 150)], "💻"),
            ("auto_like", "AUTO LIKE EVERY DAY", "likes", [("7_DAYS", 90)], "👍")
        ]
        for key, name, cat, prices, icon in initial_data:
            cursor.execute('''
                INSERT INTO products (prod_key, name, category, prices, download_link, maintenance, stock_out, icon)
                VALUES (?, ?, ?, ?, ?, 0, 0, ?)
            ''', (key, name, cat, json.dumps(prices), "", icon))
        conn.commit()
    conn.close()

init_db()
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
            mail = imaplib.IMAP4_SSL("imap.gmail.com", timeout=15)
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
# 🌐 FLASK WEB ADMIN DASHBOARD
# ==========================================
flask_app = Flask(__name__)
flask_app.secret_key = os.urandom(24)

ADMIN_HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>ELITE HACKERS - Admin Dashboard</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
    <link href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css" rel="stylesheet">
    <style>
        :root { --sidebar-width: 260px; }
        body { background-color: #0f172a; color: #f8fafc; font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; overflow-x: hidden; }
        .card { background-color: #1e293b; border: 1px solid #334155; color: #f8fafc; border-radius: 12px; margin-bottom: 20px; }
        .btn-custom { background-color: #6366f1; color: white; border: none; }
        .btn-custom:hover { background-color: #4f46e5; color: white; }
        .form-control, .form-select { background-color: #0f172a; border: 1px solid #334155; color: white; }
        .form-control:focus, .form-select:focus { background-color: #0f172a; color: white; border-color: #6366f1; }
        
        .sidebar { position: fixed; top: 0; left: -260px; width: var(--sidebar-width); height: 100%; background: #1e293b; border-right: 1px solid #334155; transition: 0.3s; z-index: 1050; padding-top: 20px; }
        .sidebar.active { left: 0; }
        .sidebar-overlay { position: fixed; top: 0; left: 0; width: 100vw; height: 100vh; background: rgba(0,0,0,0.5); display: none; z-index: 1040; }
        .sidebar-overlay.active { display: block; }
        .sidebar-link { padding: 12px 20px; color: #94a3b8; display: flex; align-items: center; gap: 12px; font-weight: 500; cursor: pointer; text-decoration: none; border-left: 4px solid transparent; }
        .sidebar-link:hover, .sidebar-link.active { background: #0f172a; color: #38bdf8; border-left-color: #38bdf8; }
        .top-navbar { background: #1e293b; border-bottom: 1px solid #334155; padding: 12px 20px; display: flex; align-items: center; justify-content: space-between; }
        .menu-toggle-btn { font-size: 24px; color: #f8fafc; cursor: pointer; border: none; background: none; }
    </style>
</head>
<body>
    {% if auth_only %}
    <div class="row justify-content-center mt-5">
        <div class="col-md-4">
            <div class="card p-4 text-center">
                <h3 class="mb-4"><i class="fas fa-lock me-2"></i>ADMIN UNLOCK</h3>
                {% if error %}<div class="alert alert-danger">{{ error }}</div>{% endif %}
                <form method="POST" action="/login">
                    <div class="mb-3">
                        <input type="password" name="password" class="form-control" placeholder="Enter Admin Password" required>
                    </div>
                    <button type="submit" class="btn btn-custom w-100">Unlock Dashboard</button>
                </form>
            </div>
        </div>
    </div>
    {% else %}
    <div class="sidebar-overlay" id="sidebarOverlay" onclick="toggleSidebar()"></div>
    <div class="sidebar" id="sidebar">
        <div class="px-3 pb-3 border-bottom border-secondary d-flex justify-content-between align-items-center">
            <h5 class="m-0 text-primary"><i class="fas fa-shield-halved me-2"></i>ELITE CONTROL</h5>
            <button class="btn-close btn-close-white d-md-none" onclick="toggleSidebar()"></button>
        </div>
        <div class="mt-3">
            <a class="sidebar-link active" onclick="showTab('products', this)"><i class="fas fa-box"></i> Manage Products</a>
            <a class="sidebar-link" onclick="showTab('stock', this)"><i class="fas fa-key"></i> Key Stock Counter</a>
            <a class="sidebar-link" onclick="showTab('wallet', this)"><i class="fas fa-wallet"></i> Wallet Management</a>
            <a class="sidebar-link" onclick="showTab('api', this)"><i class="fas fa-plug"></i> API Setup & Delivery</a>
            <a class="sidebar-link" onclick="showTab('upi', this)"><i class="fas fa-qrcode"></i> Payment Gateways</a>
            <a href="/logout" class="sidebar-link text-danger mt-4"><i class="fas fa-sign-out-alt"></i> Logout</a>
        </div>
    </div>

    <div class="main-content">
        <div class="top-navbar mb-4">
            <div class="d-flex align-items-center gap-3">
                <button class="menu-toggle-btn" onclick="toggleSidebar()"><i class="fas fa-bars"></i></button>
                <h4 class="m-0">⚡ ELITE Admin Dashboard</h4>
            </div>
            <a href="/logout" class="btn btn-outline-danger btn-sm"><i class="fas fa-lock me-1"></i> Logout</a>
        </div>

        <div class="container-fluid px-4">
            <!-- PRODUCTS TAB -->
            <div class="tab-content-item" id="tab-products">
                <div class="card p-4">
                    <h5><i class="fas fa-plus-circle me-2"></i>Add / Update Product with Custom Icon</h5>
                    <form id="prodForm" class="row g-3">
                        <div class="col-md-3">
                            <label>Icon / Emoji</label>
                            <input type="text" id="p_icon" class="form-control" placeholder="⚙️ or 🔥" value="⚙️">
                        </div>
                        <div class="col-md-3">
                            <label>Product Name</label>
                            <input type="text" id="p_name" class="form-control" placeholder="e.g. BALA MOD NON ROOT" required>
                        </div>
                        <div class="col-md-3">
                            <label>Category</label>
                            <select id="p_cat" class="form-select">
                                <option value="non_root">Non-Root</option>
                                <option value="root">Root</option>
                                <option value="ios">iOS</option>
                                <option value="pc">PC</option>
                                <option value="likes">Likes</option>
                            </select>
                        </div>
                        <div class="col-md-3">
                            <label>Duration / Plan Name</label>
                            <input type="text" id="p_plan" class="form-control" placeholder="e.g. 1_Day" required>
                        </div>
                        <div class="col-md-4">
                            <label>User Price (₹)</label>
                            <input type="number" id="p_price" class="form-control" placeholder="420" required>
                        </div>
                        <div class="col-md-4">
                            <label>Download Link</label>
                            <input type="text" id="p_link" class="form-control" placeholder="https://t.me/...">
                        </div>
                        <div class="col-md-4 d-flex align-items-end">
                            <button type="button" onclick="addProduct()" class="btn btn-custom w-100"><i class="fas fa-save me-2"></i>Save Product</button>
                        </div>
                    </form>
                </div>

                <div class="card p-4 mt-3">
                    <h5><i class="fas fa-boxes-stacked me-2"></i>Existing Products Controls</h5>
                    <div class="table-responsive">
                        <table class="table table-dark table-striped align-middle mt-2">
                            <thead>
                                <tr>
                                    <th>Icon</th>
                                    <th>Name</th>
                                    <th>Category</th>
                                    <th>Plans & Prices</th>
                                    <th>Maintenance</th>
                                    <th>Stock Status</th>
                                    <th>Actions</th>
                                </tr>
                            </thead>
                            <tbody>
                                {% for p in products %}
                                <tr>
                                    <td><h3>{{ p.icon }}</h3></td>
                                    <td><strong>{{ p.name }}</strong></td>
                                    <td><span class="badge bg-info">{{ p.category }}</span></td>
                                    <td>{{ p.prices }}</td>
                                    <td>
                                        <button onclick="toggleMaintenance('{{ p.key }}', {{ 0 if p.maintenance else 1 }})" class="btn btn-sm {{ 'btn-warning' if p.maintenance else 'btn-outline-secondary' }}">
                                            {{ '🛠️ ON' if p.maintenance else 'OFF' }}
                                        </button>
                                    </td>
                                    <td>
                                        <button onclick="toggleStockOut('{{ p.key }}', {{ 0 if p.stock_out else 1 }})" class="btn btn-sm {{ 'btn-danger' if p.stock_out else 'btn-success' }}">
                                            {{ '❌ Out of Stock' if p.stock_out else '✅ In Stock' }}
                                        </button>
                                    </td>
                                    <td>
                                        <button onclick="deleteProduct('{{ p.key }}')" class="btn btn-danger btn-sm"><i class="fas fa-trash"></i></button>
                                    </td>
                                </tr>
                                {% endfor %}
                            </tbody>
                        </table>
                    </div>
                </div>
            </div>

            <!-- KEY STOCK TAB -->
            <div class="tab-content-item" id="tab-stock" style="display:none;">
                <div class="card p-4">
                    <h5><i class="fas fa-key me-2"></i>Add Bulk Keys to Stock (FIFO Auto Delivery)</h5>
                    <form id="stockForm">
                        <div class="row g-3">
                            <div class="col-md-6">
                                <label>Product Key Name</label>
                                <input type="text" id="s_key" class="form-control" placeholder="bala_mod" required>
                            </div>
                            <div class="col-md-6">
                                <label>Plan Name</label>
                                <input type="text" id="s_plan" class="form-control" placeholder="1_Day" required>
                            </div>
                            <div class="col-12">
                                <label>Paste Keys (One Per Line)</label>
                                <textarea id="s_keys_text" class="form-control" rows="5" placeholder="KEY123&#10;KEY456"></textarea>
                            </div>
                            <div class="col-12">
                                <button type="button" onclick="addStock()" class="btn btn-custom"><i class="fas fa-upload me-2"></i>Upload Stock Keys</button>
                            </div>
                        </div>
                    </form>
                </div>
            </div>

            <!-- WALLET TAB -->
            <div class="tab-content-item" id="tab-wallet" style="display:none;">
                <div class="card p-4">
                    <h5><i class="fas fa-wallet me-2"></i>Add Funds to User Wallet</h5>
                    <form class="row g-3">
                        <div class="col-md-5">
                            <label>User Telegram ID</label>
                            <input type="number" id="w_userid" class="form-control" placeholder="123456789" required>
                        </div>
                        <div class="col-md-5">
                            <label>Amount (₹)</label>
                            <input type="number" id="w_amount" class="form-control" placeholder="500" required>
                        </div>
                        <div class="col-md-2 d-flex align-items-end">
                            <button type="button" onclick="addWalletBalance()" class="btn btn-success w-100">Add Money</button>
                        </div>
                    </form>
                </div>
            </div>

            <!-- API TAB -->
            <div class="tab-content-item" id="tab-api" style="display:none;">
                <div class="card p-4">
                    <h5><i class="fas fa-plug me-2"></i>Reseller & Delivery API Settings</h5>
                    <form id="apiForm" class="row g-3">
                        <div class="col-md-6">
                            <label>API Key</label>
                            <input type="text" id="api_key" class="form-control" value="{{ api_cfg[1] }}">
                        </div>
                        <div class="col-md-6">
                            <label>Master Key</label>
                            <input type="text" id="master_key" class="form-control" value="{{ api_cfg[2] }}">
                        </div>
                        <div class="col-md-12">
                            <label>Admin Panel URL Link</label>
                            <input type="text" id="api_url" class="form-control" value="{{ api_cfg[0] }}">
                        </div>
                        <div class="col-12">
                            <button type="button" onclick="saveApi()" class="btn btn-success me-2"><i class="fas fa-save me-2"></i>Save API Config</button>
                        </div>
                    </form>
                </div>
            </div>

            <!-- UPI TAB -->
            <div class="tab-content-item" id="tab-upi" style="display:none;">
                <div class="card p-4">
                    <h5><i class="fas fa-qrcode me-2"></i>Payment Gateways & UPI Setup</h5>
                    <form id="gateForm" class="row g-3">
                        <div class="col-md-6">
                            <label>FamPay UPI ID</label>
                            <input type="text" id="fam_upi" class="form-control" value="{{ RECEIVER_UPI_ID }}">
                        </div>
                        <div class="col-12">
                            <button type="button" onclick="alert('Payment Gateway Saved!')" class="btn btn-custom">Save Gateways</button>
                        </div>
                    </form>
                </div>
            </div>

        </div>
    </div>
    {% endif %}

    <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/js/bootstrap.bundle.min.js"></script>
    <script>
        function toggleSidebar() {
            document.getElementById('sidebar').classList.toggle('active');
            document.getElementById('sidebarOverlay').classList.toggle('active');
        }

        function showTab(tabId, element) {
            document.querySelectorAll('.tab-content-item').forEach(el => el.style.display = 'none');
            document.getElementById('tab-' + tabId).style.display = 'block';
            document.querySelectorAll('.sidebar-link').forEach(el => el.classList.remove('active'));
            if(element) element.classList.add('active');
            if(window.innerWidth < 768) toggleSidebar();
        }

        function addProduct() {
            let icon = document.getElementById('p_icon').value;
            let name = document.getElementById('p_name').value;
            let cat = document.getElementById('p_cat').value;
            let plan = document.getElementById('p_plan').value;
            let price = document.getElementById('p_price').value;
            let link = document.getElementById('p_link').value;

            if(!name || !plan || !price) { alert('Please fill required fields!'); return; }

            fetch('/api/add_product', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({ icon: icon, name: name, category: cat, prices: [[plan, parseFloat(price)]], download_link: link })
            }).then(r => r.json()).then(data => {
                alert(data.message);
                location.reload();
            });
        }

        function deleteProduct(prod_key) {
            if(!confirm('Are you sure you want to delete this product?')) return;
            fetch('/api/delete_product', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({prod_key: prod_key})
            }).then(r => r.json()).then(data => {
                alert(data.message);
                location.reload();
            });
        }

        function toggleMaintenance(prod_key, state) {
            fetch('/api/toggle_maintenance', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({prod_key: prod_key, state: state})
            }).then(r => r.json()).then(data => {
                alert(data.message);
                location.reload();
            });
        }

        function toggleStockOut(prod_key, state) {
            fetch('/api/toggle_stockout', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({prod_key: prod_key, state: state})
            }).then(r => r.json()).then(data => {
                alert(data.message);
                location.reload();
            });
        }

        function addStock() {
            let key = document.getElementById('s_key').value;
            let plan = document.getElementById('s_plan').value;
            let keys = document.getElementById('s_keys_text').value;

            fetch('/api/add_stock', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({prod_key: key, plan: plan, keys: keys})
            }).then(r => r.json()).then(data => {
                alert(data.message);
                location.reload();
            });
        }

        function addWalletBalance() {
            let uid = document.getElementById('w_userid').value;
            let amt = document.getElementById('w_amount').value;
            fetch('/api/add_wallet', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({user_id: uid, amount: amt})
            }).then(r => r.json()).then(data => {
                alert(data.message);
            });
        }

        function saveApi() {
            let api_url = document.getElementById('api_url').value;
            let api_key = document.getElementById('api_key').value;
            let master_key = document.getElementById('master_key').value;

            fetch('/api/save_api_settings', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({api_url: api_url, api_key: api_key, master_key: master_key})
            }).then(r => r.json()).then(data => {
                alert(data.message);
            });
        }
    </script>
</body>
</html>
"""

@flask_app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        pwd = request.form.get('password')
        if pwd == ADMIN_PASSWORD:
            session['logged_in'] = True
            return redirect(url_for('admin_dashboard'))
        return render_template_string(ADMIN_HTML_TEMPLATE, auth_only=True, error="Invalid Admin Password!")
    return render_template_string(ADMIN_HTML_TEMPLATE, auth_only=True)

@flask_app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

@flask_app.route('/')
@flask_app.route('/admin')
def admin_dashboard():
    if not session.get('logged_in'):
        return redirect(url_for('login'))
    
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT prod_key, name, category, prices, download_link, maintenance, stock_out, icon FROM products')
    prods_raw = cursor.fetchall()
    
    products_list = []
    for p in prods_raw:
        products_list.append({
            "key": p[0], "name": p[1], "category": p[2], 
            "prices": json.loads(p[3]), "download_link": p[4], 
            "maintenance": bool(p[5]), "stock_out": bool(p[6]),
            "icon": p[7] or "📦"
        })
        
    cursor.execute('SELECT api_url, api_key, master_key FROM api_config WHERE id = 1')
    api_cfg = cursor.fetchone() or ("", "", "")
    conn.close()

    return render_template_string(
        ADMIN_HTML_TEMPLATE,
        auth_only=False,
        products=products_list,
        api_cfg=api_cfg,
        RECEIVER_UPI_ID=RECEIVER_UPI_ID
    )

@flask_app.route('/api/add_product', methods=['POST'])
def api_add_product():
    if not session.get('logged_in'):
        return jsonify({"success": False, "message": "Unauthorized"}), 401

    data = request.json
    icon = data.get('icon', '📦')
    raw_name = data.get('name').strip()
    prod_key = sanitize_product_key(raw_name)
    category = data.get('category')
    prices = data.get('prices')
    download_link = data.get('download_link', '')

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('''
        INSERT OR REPLACE INTO products (prod_key, name, category, prices, download_link, maintenance, stock_out, icon)
        VALUES (?, ?, ?, ?, ?, 0, 0, ?)
    ''', (prod_key, raw_name, category, json.dumps(prices), download_link, icon))
    conn.commit()
    conn.close()

    load_products_from_db()
    return jsonify({"success": True, "message": "Product added successfully!"})

@flask_app.route('/api/add_wallet', methods=['POST'])
def api_add_wallet():
    if not session.get('logged_in'):
        return jsonify({"success": False, "message": "Unauthorized"}), 401

    data = request.json
    uid = int(data.get('user_id'))
    amt = float(data.get('amount'))

    db_update_wallet(uid, amt)
    return jsonify({"success": True, "message": f"Added ₹{amt} to User {uid} Wallet!"})

@flask_app.route('/api/delete_product', methods=['POST'])
def api_delete_product():
    if not session.get('logged_in'):
        return jsonify({"success": False, "message": "Unauthorized"}), 401
    
    prod_key = request.json.get('prod_key')
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('DELETE FROM products WHERE prod_key = ?', (prod_key,))
    conn.commit()
    conn.close()

    for cat in ALL_CATEGORIES:
        cat.pop(prod_key, None)

    return jsonify({"success": True, "message": "Product deleted successfully!"})

@flask_app.route('/api/toggle_maintenance', methods=['POST'])
def api_toggle_maintenance():
    if not session.get('logged_in'):
        return jsonify({"success": False, "message": "Unauthorized"}), 401

    data = request.json
    prod_key = data.get('prod_key')
    state = int(data.get('state'))

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('UPDATE products SET maintenance = ? WHERE prod_key = ?', (state, prod_key))
    conn.commit()
    conn.close()

    MAINTENANCE_MODE[prod_key] = bool(state)
    return jsonify({"success": True, "message": f"Maintenance mode set to {'ON' if state else 'OFF'}!"})

@flask_app.route('/api/toggle_stockout', methods=['POST'])
def api_toggle_stockout():
    if not session.get('logged_in'):
        return jsonify({"success": False, "message": "Unauthorized"}), 401

    data = request.json
    prod_key = data.get('prod_key')
    state = int(data.get('state'))

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('UPDATE products SET stock_out = ? WHERE prod_key = ?', (state, prod_key))
    conn.commit()
    conn.close()

    STOCK_OUT_MODE[prod_key] = bool(state)
    return jsonify({"success": True, "message": f"Stock status set to {'Out of Stock' if state else 'In Stock'}!"})

@flask_app.route('/api/add_stock', methods=['POST'])
def api_add_stock():
    if not session.get('logged_in'):
        return jsonify({"success": False, "message": "Unauthorized"}), 401

    data = request.json
    prod_key = data.get('prod_key')
    plan = data.get('plan')
    keys = [k.strip() for k in data.get('keys').split("\n") if k.strip()]

    target_tuple = (prod_key, plan)
    if target_tuple not in KEYS_STOCK:
        KEYS_STOCK[target_tuple] = []
    KEYS_STOCK[target_tuple].extend(keys)

    return jsonify({"success": True, "message": f"Added {len(keys)} keys successfully!"})

@flask_app.route('/api/save_api_settings', methods=['POST'])
def api_save_settings():
    if not session.get('logged_in'):
        return jsonify({"success": False, "message": "Unauthorized"}), 401

    data = request.json
    api_url = data.get('api_url')
    api_key = data.get('api_key')
    master_key = data.get('master_key')

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO api_config (id, api_url, api_key, master_key)
        VALUES (1, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET api_url=?, api_key=?, master_key=?
    ''', (api_url, api_key, master_key, api_url, api_key, master_key))
    conn.commit()
    conn.close()

    return jsonify({"success": True, "message": "API Configuration saved!"})

def run_flask():
    port = int(os.environ.get("PORT", 8080))
    flask_app.run(host='0.0.0.0', port=port)

def keep_alive():
    t = Thread(target=run_flask)
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
            [InlineKeyboardButton("📦 My Orders", callback_data="my_orders"), InlineKeyboardButton("👤 Profile & Wallet", callback_data="profile")],
            [
                InlineKeyboardButton("💳 Pay Proof", url="https://t.me/+fJrFACSrntgwNjll"),
                InlineKeyboardButton("💬 Support", callback_data="support")
            ],
            [InlineKeyboardButton("ℹ️ How to Use", callback_data="how_to_use")]
        ]

        if user.id == ADMIN_ID:
            web_app_url = os.environ.get("RENDER_EXTERNAL_URL", "http://localhost:8080") + "/admin"
            keyboard.append([InlineKeyboardButton("👑 Web Admin Panel", url=web_app_url)])

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
# 👤 PROFILE & WALLET HANDLERS
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
    wallet_bal = db_data[4] if db_data else 0.0

    text = (
        "___________________________\n\n"
        "<b>👤 YOUR PROFILE & WALLET</b>\n"
        "___________________________\n\n"
        f"🛡️ <b>Name:</b> {name}\n"
        f"🔗 <b>Username:</b> {uname}\n"
        f"🆔 <b>User ID:</b> <code>{user.id}</code>\n"
        f"💳 <b>Wallet Balance:</b> ₹{wallet_bal:.2f}\n"
        f"📅 <b>Member Since:</b> {joined}\n"
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
        "1️⃣ Tap <b>🛒 Shop Now</b> to view store.\n"
        "2️⃣ Choose your product category.\n"
        "3️⃣ Pick your desired product and duration.\n"
        "4️⃣ Scan UPI QR Code provided.\n"
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
# 🛒 STORE NAVIGATION & CATEGORIES WITH CUSTOM ICONS
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
    keyboard = [[InlineKeyboardButton(f"{data.get('icon', '👍')} {data['name']}", callback_data=f"prod_likes_{key}")] for key, data in LIKE_PRODUCTS.items()]
    keyboard.append([InlineKeyboardButton("🔙 Back to Shop", callback_data="shop_now")])
    await query.message.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))

async def non_root_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    text = "<b>📱 NON-ROOT PANELS:</b>"
    keyboard = [[InlineKeyboardButton(f"{data.get('icon', '⚙️')} {data['name']}", callback_data=f"prod_nonroot_{key}")] for key, data in NON_ROOT_PRODUCTS.items()]
    keyboard.append([InlineKeyboardButton("🔙 Back to Categories", callback_data="cat_panels")])
    await query.message.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))

async def root_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    text = "<b>⚡ ROOT PANELS:</b>"
    keyboard = [[InlineKeyboardButton(f"{data.get('icon', '⚡')} {data['name']}", callback_data=f"prod_root_{key}")] for key, data in ROOT_PRODUCTS.items()]
    keyboard.append([InlineKeyboardButton("🔙 Back to Categories", callback_data="cat_panels")])
    await query.message.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))

async def ios_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    text = "<b>🍏 IOS PANELS:</b>"
    keyboard = [[InlineKeyboardButton(f"{data.get('icon', '🍏')} {data['name']}", callback_data=f"prod_ios_{key}")] for key, data in IOS_PRODUCTS.items()]
    keyboard.append([InlineKeyboardButton("🔙 Back to Categories", callback_data="cat_panels")])
    await query.message.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))

async def pc_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    text = "<b>💻 PC PANELS:</b>"
    keyboard = [[InlineKeyboardButton(f"{data.get('icon', '💻')} {data['name']}", callback_data=f"prod_pc_{key}")] for key, data in PC_PRODUCTS.items()]
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

    icon = prod.get('icon', '🛒')
    lines = ["<b>═══════════════════════</b>", f"<b>{icon} {prod['name']}</b>", "<b>═══════════════════════</b>\n", "🔥 <b>Choose a plan:</b>\n"]
    keyboard = []
    
    for plan, price in prod["prices"]:
        stock_count = len(KEYS_STOCK.get((prod_key, plan), []))
        stock_label = f"({stock_count} Available)" if stock_count > 0 else "(Out of Stock)"
        btn_text = f"{plan.replace('_', ' ')} — ₹{price} {stock_label}"
        cb = f"plan_{prod_type}_{prod_key}_{plan}_{price}"
        
        lines.append(f"• {btn_text}")
        keyboard.append([InlineKeyboardButton(btn_text, callback_data=cb)])

    if prod_key in PRODUCT_LINKS and PRODUCT_LINKS[prod_key]:
        keyboard.append([InlineKeyboardButton("📥 Download File / App", url=PRODUCT_LINKS[prod_key])])

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
        [InlineKeyboardButton("✅ Confirm & Pay via UPI QR", callback_data="confirm_pay")],
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
        else:
            await query.message.reply_text("✅ <b>Payment verified!</b> Key request forwarded to admin for instant processing.", parse_mode="HTML")
    else:
        try:
            await verifying_msg.delete()
        except Exception:
            pass
        fail_text = (
            "❌ <b>Payment Not Received Yet!</b>\n\n"
            "Please complete payment on your UPI App and try tapping <b>VERIFY PAYMENT</b> again."
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
# 🤖 BOT SETUP & RUNNER
# ==========================================
def start_bot():
    app = Application.builder().token(BOT_TOKEN).concurrent_updates(False).read_timeout(30).write_timeout(30).connect_timeout(30).build()

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
    app.add_handler(CallbackQueryHandler(verify_payment_btn_handler, pattern="^verify_payment_btn$"))
    app.add_handler(CallbackQueryHandler(cancel_order, pattern="^cancel_order$"))

    print("Bot is running seamlessly...")
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


