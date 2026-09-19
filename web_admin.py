import os
import json
import sqlite3
import re
import urllib.request
import urllib.parse
from datetime import timedelta, datetime
from threading import Thread
import pytz
from flask import Flask, render_template_string, request, jsonify, redirect, session

DB_FILE = "bot_database.db"
BOT_TOKEN = "8892856619:AAGZhdOv389_AaKvbcbInlJAiDMOwQxOeHc"
DEFAULT_ADMIN_PWD = os.environ.get("ADMIN_PASSWORD", "athulsudin1234")
IST = pytz.timezone('Asia/Kolkata')

app = Flask(__name__)
app.secret_key = os.urandom(32)
app.permanent_session_lifetime = timedelta(days=30)

def get_ist_time():
    return datetime.now(IST).strftime("%d %b %Y, %I:%M %p (IST)")

# ==========================================
# 🗄️ DATABASE SCHEMA & INITIALIZATION
# ==========================================
def init_all_database_tables():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS admin_auth (
            id INTEGER PRIMARY KEY DEFAULT 1,
            password TEXT
        )
    ''')
    c.execute('SELECT password FROM admin_auth WHERE id = 1')
    if not c.fetchone():
        c.execute('INSERT INTO admin_auth (id, password) VALUES (1, ?)', (DEFAULT_ADMIN_PWD,))

    c.execute('''
        CREATE TABLE IF NOT EXISTS coupons (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT UNIQUE,
            discount_type TEXT,
            discount_val REAL,
            usage_limit INTEGER,
            used_count INTEGER DEFAULT 0,
            is_active INTEGER DEFAULT 1
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS id_accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            category_name TEXT,
            account_data TEXT,
            price REAL,
            is_sold INTEGER DEFAULT 0
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS broadcast_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            message TEXT,
            media_type TEXT,
            sent_at TEXT,
            recipients INTEGER
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS pending_deposits (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            username TEXT,
            amount REAL,
            utr TEXT,
            order_id TEXT,
            created_at TEXT,
            status TEXT DEFAULT 'Pending'
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS gateway_config (
            id INTEGER PRIMARY KEY DEFAULT 1,
            active_gateway TEXT DEFAULT 'fampay',
            fampay_api_key TEXT DEFAULT '',
            fampay_upi_id TEXT DEFAULT '9544113089@fam',
            fampay_base_url TEXT DEFAULT 'https://xyzcheats.com/gateway',
            paytm_base_url TEXT DEFAULT 'https://xyzcheats.com',
            paytm_upi_id TEXT DEFAULT '',
            paytm_merchant_id TEXT DEFAULT ''
        )
    ''')
    c.execute('SELECT id FROM gateway_config WHERE id = 1')
    if not c.fetchone():
        c.execute('INSERT INTO gateway_config VALUES (1, "fampay", "", "9544113089@fam", "https://xyzcheats.com/gateway", "https://xyzcheats.com", "", "")')

    c.execute('''
        CREATE TABLE IF NOT EXISTS store_settings (
            id INTEGER PRIMARY KEY DEFAULT 1,
            support_username TEXT DEFAULT '@Athulsudin',
            how_to_use_link TEXT DEFAULT 'https://t.me/chatelitehackers',
            welcome_message TEXT DEFAULT '',
            shop_name TEXT DEFAULT 'ELITE HACKERS',
            tagline TEXT DEFAULT 'Best Free Fire Panel Services',
            min_deposit REAL DEFAULT 10.0,
            max_deposit REAL DEFAULT 50000.0,
            qr_expiry_min INTEGER DEFAULT 5,
            referral_bonus REAL DEFAULT 5.0,
            spin_min REAL DEFAULT 2.0,
            spin_max REAL DEFAULT 10.0,
            spin_cooldown_hours INTEGER DEFAULT 24,
            monthly_users TEXT DEFAULT '3,074 monthly users',
            pending_notice_template TEXT DEFAULT '✅ Payment Verified & Received!\\n\\n⚠️ NOTICE: Auto-stock for {product} ({plan}) is currently restocking!\\n\\n🛡️ 100% SECURE: Admin is preparing your fresh key right now. It will be delivered directly to this chat shortly!'
        )
    ''')
    c.execute('SELECT id FROM store_settings WHERE id = 1')
    if not c.fetchone():
        c.execute('''
            INSERT INTO store_settings (id, support_username, how_to_use_link, welcome_message, shop_name, tagline, min_deposit, max_deposit, qr_expiry_min, referral_bonus, spin_min, spin_max, spin_cooldown_hours, monthly_users)
            VALUES (1, "@Athulsudin", "https://t.me/chatelitehackers", "", "ELITE HACKERS", "Best Free Fire Panel Services", 10.0, 50000.0, 5, 5.0, 2.0, 10.0, 24, "3,074 monthly users")
        ''')
        
    # Safely adding new columns for Product features without crashing existing DB
    try: c.execute("ALTER TABLE products ADD COLUMN reseller_price REAL DEFAULT 0")
    except: pass
    try: c.execute("ALTER TABLE products ADD COLUMN remote_pid TEXT DEFAULT ''")
    except: pass
    try: c.execute("ALTER TABLE products ADD COLUMN remote_duration TEXT DEFAULT ''")
    except: pass

    conn.commit()
    conn.close()

def get_current_password():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('SELECT password FROM admin_auth WHERE id = 1')
    row = c.fetchone()
    conn.close()
    return row[0] if row else DEFAULT_ADMIN_PWD

init_all_database_tables()

# Dynamic Bot Avatar & Username Fetcher
BOT_INFO = {"name": "Bot Control Center", "username": "@EliteBot", "avatar": None}
def refresh_bot_meta():
    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/getMe"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            d = json.loads(resp.read().decode())
            if d.get("ok"):
                BOT_INFO["name"] = d["result"].get("first_name", "Bot Control Center")
                BOT_INFO["username"] = f"@{d['result'].get('username', 'Bot')}"
        p_url = f"https://api.telegram.org/bot{BOT_TOKEN}/getUserProfilePhotos?user_id={BOT_TOKEN.split(':')[0]}&limit=1"
        with urllib.request.urlopen(urllib.request.Request(p_url, headers={"User-Agent": "Mozilla/5.0"}), timeout=5) as resp2:
            pd = json.loads(resp2.read().decode())
            if pd.get("ok") and pd["result"]["total_count"] > 0:
                fid = pd["result"]["photos"][0][-1]["file_id"]
                with urllib.request.urlopen(urllib.request.Request(f"https://api.telegram.org/bot{BOT_TOKEN}/getFile?file_id={fid}"), timeout=5) as resp3:
                    fd = json.loads(resp3.read().decode())
                    if fd.get("ok"):
                        BOT_INFO["avatar"] = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{fd['result']['file_path']}"
    except Exception:
        pass

Thread(target=refresh_bot_meta, daemon=True).start()

# ==========================================
# 🎨 COMPLETE CYBER ADMIN DASHBOARD HTML
# ==========================================
ADMIN_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{{ bot_info.name }} - Control Center</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
    <link href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css" rel="stylesheet">
    <style>
        :root {
            --bg-deep: #080318;
            --card-glass: rgba(22, 13, 44, 0.94);
            --neon-border: rgba(147, 51, 234, 0.35);
            --sidebar-w: 270px;
            --text-main: #f8fafc;
        }
        body {
            background: radial-gradient(circle at top center, #1b0c3f 0%, #0c051d 60%, #05020c 100%);
            background-attachment: fixed; color: var(--text-main); font-family: -apple-system, system-ui, sans-serif;
            min-height: 100vh; margin: 0; overflow-x: hidden;
        }
        body::before {
            content: ''; position: fixed; top: 0; left: 0; width: 100%; height: 100%;
            background-image: linear-gradient(rgba(147, 51, 234, 0.05) 1px, transparent 1px), linear-gradient(90deg, rgba(147, 51, 234, 0.05) 1px, transparent 1px);
            background-size: 36px 36px; pointer-events: none; z-index: 0;
        }

        /* 🌟 Animations */
        .fade-in { animation: fadeIn 0.3s ease-in-out; }
        @keyframes fadeIn { from { opacity: 0; transform: translateY(10px); } to { opacity: 1; transform: translateY(0); } }

        /* 🔐 SCREENSHOT-EXACT CYBER LOGIN */
        .login-box { min-height: 100vh; display: flex; align-items: center; justify-content: center; padding: 20px; position: relative; z-index: 10; }
        .login-card { background: var(--card-glass); backdrop-filter: blur(25px); border: 1px solid var(--neon-border); border-radius: 28px; padding: 42px 32px; width: 100%; max-width: 400px; text-align: center; box-shadow: 0 0 50px rgba(139, 92, 246, 0.25); }
        .avatar-ring { width: 88px; height: 88px; margin: 0 auto 20px; border-radius: 50%; padding: 3px; background: linear-gradient(135deg, #06b6d4, #a855f7, #f59e0b); display: flex; align-items: center; justify-content: center; }
        .avatar-inner { width: 100%; height: 100%; background: #0d0622; border-radius: 50%; display: flex; align-items: center; justify-content: center; overflow: hidden; }
        .title-grad { font-size: 1.65rem; font-weight: 800; background: linear-gradient(90deg, #a78bfa, #38bdf8, #facc15); -webkit-background-clip: text; -webkit-text-fill-color: transparent; }
        .pwd-field { position: relative; margin-bottom: 20px; }
        .pwd-field input { width: 100%; background: rgba(14, 7, 33, 0.9); border: 1px solid rgba(139, 92, 246, 0.5); border-radius: 14px; padding: 13px 45px 13px 18px; color: white; outline: none; }
        .eye-btn { position: absolute; right: 15px; top: 50%; transform: translateY(-50%); color: #a78bfa; cursor: pointer; }
        .btn-unlock { width: 100%; padding: 13px; border: none; border-radius: 14px; background: linear-gradient(135deg, #7c3aed, #6366f1); color: white; font-weight: 700; cursor: pointer; }

        /* 💻 DASHBOARD LAYOUT */
        .sidebar { position: fixed; top: 0; left: -270px; width: var(--sidebar-w); height: 100vh; background: #0f0724; border-right: 1px solid var(--neon-border); padding-top: 20px; z-index: 1050; transition: 0.3s; overflow-y: auto; }
        .sidebar.active { left: 0; }
        .sidebar-link { padding: 12px 20px; color: #94a3b8; display: flex; align-items: center; gap: 12px; cursor: pointer; text-decoration: none; border-left: 4px solid transparent; }
        .sidebar-link:hover, .sidebar-link.active { background: #1a0b3b; color: #38bdf8; border-left-color: #38bdf8; }
        .main-content { padding: 25px; transition: 0.3s; position: relative; z-index: 10; }
        @media (min-width: 769px) { .sidebar { left: 0; } .main-content { margin-left: var(--sidebar-w); } }
        
        .stat-card { background: var(--card-glass); border: 1px solid var(--neon-border); border-radius: 16px; padding: 20px; border-left: 4px solid #8b5cf6; }
        .card { background: var(--card-glass); border: 1px solid var(--neon-border); border-radius: 18px; margin-bottom: 20px; }
        .btn-custom { background: linear-gradient(135deg, #7c3aed, #6366f1); color: white; border: none; border-radius: 10px; padding: 10px 18px; font-weight: 600; }
        .form-control, .form-select { background-color: #0d0622; border: 1px solid var(--neon-border); color: white; border-radius: 10px; padding: 10px; }
        .form-control:focus { background-color: #0d0622; color: white; border-color: #a855f7; box-shadow: none; }
        
        /* Product Cards & Delete Notification */
        .del-notify { background: #7c3aed; color: white; padding: 10px 20px; border-radius: 8px; font-weight: bold; position: fixed; top: 20px; left: 50%; transform: translateX(-50%); z-index: 9999; display: none; box-shadow: 0 4px 15px rgba(124,58,237,0.5); }
        .prod-card { background: #12072c; border: 1px solid #3b1d6e; border-radius: 16px; padding: 20px; }
        .prod-icon { background: linear-gradient(135deg, #facc15, #f59e0b); -webkit-background-clip: text; -webkit-text-fill-color: transparent; font-size: 2rem; }
    </style>
</head>
<body>

    {% if not session.get('admin_logged') %}
    <!-- 🔐 LOGIN SCREEN -->
    <div class="login-box fade-in">
        <div class="login-card">
            <div class="avatar-ring">
                <div class="avatar-inner">
                    {% if bot_info.avatar %}<img src="{{ bot_info.avatar }}" style="width:100%;height:100%;object-fit:cover;">{% else %}<span style="font-size:2.2rem;">🔒</span>{% endif %}
                </div>
            </div>
            <div class="title-grad">Bot Control Center</div>
            <div style="color:#38bdf8;font-size:0.9rem;margin-bottom:12px;">{{ bot_info.username }}</div>
            <div style="color:#94a3b8;font-size:0.85rem;margin-bottom:24px;">Restricted owner access — enter your key</div>
            {% if error %}<div class="alert alert-danger py-2 small">{{ error }}</div>{% endif %}
            <form method="POST" action="/login">
                <div class="pwd-field">
                    <input type="password" id="pInput" name="password" placeholder="Admin password" required autofocus>
                    <span class="eye-btn" onclick="let p=document.getElementById('pInput');p.type=p.type==='password'?'text':'password';"><i class="fa-regular fa-eye"></i></span>
                </div>
                <button type="submit" class="btn-unlock">Unlock</button>
            </form>
            <div style="margin-top:20px;font-size:0.78rem;color:#d8b4fe;">🔐 Owner-only · session remembered for 30 days</div>
        </div>
    </div>
    {% else %}

    <!-- Notification Banner -->
    <div id="delNotify" class="del-notify fade-in"><i class="fas fa-trash me-2"></i> Product deleted.</div>

    <!-- 🌟 SIDEBAR DRAWER -->
    <div class="sidebar" id="sidebar">
        <div class="px-3 pb-3 border-bottom border-secondary d-flex justify-content-between align-items-center">
            <div class="d-flex align-items-center gap-2">
                <span class="fs-4">⚡</span><strong class="text-info">{{ bot_info.name }}</strong>
            </div>
            <button class="btn-close btn-close-white d-md-none" onclick="document.getElementById('sidebar').classList.remove('active')"></button>
        </div>
        <div class="mt-3">
            <a class="sidebar-link active" onclick="showTab('dashboard', this)"><i class="fas fa-chart-pie"></i> Dashboard</a>
            <a class="sidebar-link" onclick="showTab('products', this)"><i class="fas fa-box-open"></i> Manage Product</a>
            <a class="sidebar-link" onclick="showTab('keys_log', this)"><i class="fas fa-scroll"></i> Live Key Delivery Logs</a>
            <a class="sidebar-link" onclick="showTab('coupons', this)"><i class="fas fa-ticket"></i> Coupon Manager</a>
            <a class="sidebar-link" onclick="showTab('upi', this)"><i class="fas fa-credit-card"></i> UPI Payment Setup</a>
            <a class="sidebar-link" onclick="showTab('store', this)"><i class="fas fa-sliders"></i> Store Settings</a>
            <a href="/logout" class="sidebar-link text-danger mt-4"><i class="fas fa-right-from-bracket"></i> Logout</a>
        </div>
    </div>

    <div class="main-content">
        <div class="d-flex justify-content-between align-items-center mb-4">
            <div class="d-flex align-items-center gap-3">
                <button class="btn btn-dark d-md-none" onclick="document.getElementById('sidebar').classList.add('active')"><i class="fas fa-bars"></i></button>
                <h4 class="fw-bold m-0 title-grad">Bot Control Center</h4>
            </div>
            <a href="/logout" class="btn btn-outline-danger btn-sm"><i class="fas fa-lock me-1"></i></a>
        </div>

        <!-- 📊 1. DASHBOARD -->
        <div class="tab-pane-content fade-in" id="tab-dashboard">
            <div class="card p-4 mb-4" style="background:linear-gradient(135deg,#1f1042,#110729);">
                <small class="text-info fw-bold">⚡ BOT CONTROL CENTER</small>
                <h3 class="fw-bold mt-1">Welcome back.</h3>
                <span class="text-muted">Here's how {{ bot_info.username }} is doing right now.</span>
            </div>
            <div class="row g-3">
                <div class="col-6 col-md-3"><div class="stat-card"><h6>MEMBERS</h6><h2>{{ stats.users }}</h2></div></div>
                <div class="col-6 col-md-3"><div class="stat-card" style="border-left-color:#10b981;"><h6>WALLET (ALL)</h6><h2>₹{{ stats.total_wallet }}</h2></div></div>
                <div class="col-6 col-md-3"><div class="stat-card" style="border-left-color:#38bdf8;"><h6>CATALOG ITEMS</h6><h2>{{ stats.products_count }}</h2></div></div>
                <div class="col-6 col-md-3"><div class="stat-card" style="border-left-color:#facc15;"><h6>LIFETIME REVENUE</h6><h2>₹{{ stats.revenue }}</h2></div></div>
            </div>
        </div>

        <!-- 📦 2. MANAGE PRODUCTS (CARDS + ADD FORM) -->
        <div class="tab-pane-content fade-in" id="tab-products" style="display:none;">
            <div class="d-flex justify-content-between align-items-center mb-3">
                <div>
                    <h5 class="m-0 fw-bold"><i class="fas fa-box me-2 text-warning"></i> Manage Product</h5>
                    <small class="text-muted">Add products, plans, pricing, and API auto-restock links.</small>
                </div>
                <button class="btn btn-custom" onclick="document.getElementById('addProdBox').style.display='block'; window.scrollTo(0,0);"><i class="fas fa-plus me-1"></i> Add Product</button>
            </div>
            
            <!-- ADD PRODUCT FORM -->
            <div id="addProdBox" class="p-4 mb-4 card" style="display:none;background:#13082e;">
                <div class="d-flex justify-content-between">
                    <h6 class="fw-bold text-info"><i class="fas fa-plus me-2"></i>Add Product</h6>
                    <button class="btn-close btn-close-white" onclick="document.getElementById('addProdBox').style.display='none'"></button>
                </div>
                <div class="row g-3 mt-1">
                    <div class="col-md-6"><label class="small text-muted">Product Name</label><input type="text" id="ap_name" class="form-control" placeholder="e.g. Netflix Premium"></div>
                    <div class="col-md-6">
                        <label class="small text-muted">Device Category (optional)</label>
                        <select id="ap_cat" class="form-select">
                            <option value="non_root">Non-Root Mobile</option><option value="root">Root Mobile</option>
                            <option value="ios">iOS Panels</option><option value="pc">PC Panels</option><option value="likes">8 Level ID / Likes</option>
                        </select>
                    </div>
                    <div class="col-md-12"><label class="small text-muted">Channel Link (optional)</label><input type="text" id="ap_link" class="form-control" placeholder="e.g. https://t.me/yourchannel"></div>
                    
                    <div class="col-12 mt-4"><h6 class="text-secondary m-0">➕ Plans (optional)</h6></div>
                    <div class="col-md-6"><label class="small text-muted">Plan Name</label><input type="text" id="ap_plan" class="form-control" placeholder="e.g. 1 Day / 30 Days"></div>
                    <div class="col-md-6"><label class="small text-muted">Price (₹)</label><input type="number" id="ap_price" class="form-control" placeholder="Price (₹)"></div>
                    <div class="col-md-12"><label class="small text-warning"><i class="fas fa-handshake me-1"></i> Reseller Price (₹) (optional)</label><input type="number" id="ap_rprice" class="form-control" placeholder="optional"></div>
                    
                    <div class="col-md-6"><label class="small text-info">Remote Product ID (API)</label><input type="text" id="ap_pid" class="form-control" placeholder="e.g. PID_123"></div>
                    <div class="col-md-6"><label class="small text-info">Remote Duration (API)</label><input type="text" id="ap_rdur" class="form-control" placeholder="e.g. 1 Day"></div>
                    
                    <div class="col-12"><button class="btn btn-custom w-100 mt-2" onclick="saveProduct()">➕ Add Product</button></div>
                </div>
            </div>

            <!-- PRODUCT CARDS GRID -->
            <div class="row g-3">
                {% for p in products %}
                <div class="col-md-6">
                    <div class="prod-card">
                        <div class="d-flex justify-content-between align-items-center mb-3">
                            <div class="prod-icon"><i class="fas fa-folder text-warning"></i></div>
                            <span class="badge" style="border: 1px solid #facc15; color: #facc15; background: transparent;">❓ NOT CATEGORIZED</span>
                        </div>
                        <div class="d-flex justify-content-between">
                            <div>
                                <small class="text-muted">PRODUCT</small>
                                <h5 class="fw-bold m-0">{{ p.name }}</h5>
                            </div>
                            <div class="text-end">
                                <small class="text-muted">STOCK</small>
                                <h5 class="fw-bold m-0">{{ p.stock_count }} key(s)</h5>
                            </div>
                        </div>
                        <div class="text-end mt-1"><small class="text-success fw-bold">🟢 ACTIVE</small></div>
                        <div class="d-flex flex-wrap gap-2 mt-3">
                            <button class="btn btn-sm btn-outline-light"><i class="fas fa-pen"></i> Edit</button>
                            <button class="btn btn-sm btn-outline-warning"><i class="fas fa-pause"></i> Disable</button>
                            <button class="btn btn-sm btn-outline-secondary"><i class="fas fa-tools"></i> Maintenance</button>
                            <button class="btn btn-sm btn-outline-danger" onclick="deleteProduct('{{ p.prod_key }}')"><i class="fas fa-trash"></i> Delete</button>
                        </div>
                    </div>
                </div>
                {% endfor %}
            </div>
        </div>

        <!-- 📜 3. LIVE KEY DELIVERY LOGS (SALES ANALYTICS) -->
        <div class="tab-pane-content fade-in" id="tab-keys_log" style="display:none;">
            <h5 class="fw-bold mb-3"><i class="fas fa-scroll text-warning me-2"></i> Live Key Delivery & Global Orders</h5>
            <p class="text-muted small">Track all API keys delivered to customers with exact timestamps.</p>
            
            <div class="row g-3 mb-4">
                <div class="col-6 col-md-3"><div class="stat-card" style="border-left-color:#38bdf8;"><h6>🛍️ TOTAL SOLD</h6><h3 class="m-0 mt-1">{{ stats.orders }}</h3></div></div>
                <div class="col-6 col-md-3"><div class="stat-card" style="border-left-color:#10b981;"><h6>📈 SOLD TODAY</h6><h3 class="m-0 mt-1">{{ stats.today_orders }}</h3></div></div>
                <div class="col-6 col-md-3"><div class="stat-card" style="border-left-color:#facc15;"><h6>💎 TODAY'S REVENUE</h6><h3 class="m-0 mt-1">₹{{ stats.today_rev }}</h3></div></div>
                <div class="col-6 col-md-3"><div class="stat-card" style="border-left-color:#8b5cf6;"><h6>📦 LIVE PRODUCTS</h6><h3 class="m-0 mt-1">{{ stats.products_count }}</h3></div></div>
            </div>

            <div class="card p-4">
                <h6 class="text-success fw-bold mb-3">🔑 Delivered Keys & Orders Log</h6>
                <input type="text" id="logSearch" class="form-control mb-3" placeholder="🔍 Search by Telegram ID, User, Product, or Key..." onkeyup="filterTable('logSearch', 'logTable')">
                <div class="table-responsive">
                    <table class="table table-dark align-middle" id="logTable">
                        <thead><tr class="text-muted small"><th>USER (ID)</th><th>PRODUCT & PLAN</th><th>PRICE PAID</th><th>DELIVERED KEY</th><th>DATE & TIME</th></tr></thead>
                        <tbody>
                            {% for o in all_orders %}
                            <tr>
                                <td>
                                    <strong>{{ o.user_name }}</strong><br>
                                    <a href="tg://user?id={{ o.user_id }}" class="text-info text-decoration-none small"><i class="fas fa-comment-dots"></i> {{ o.user_id }}</a>
                                </td>
                                <td><strong>{{ o.prod_name }}</strong><br><small class="text-muted">⏱️ {{ o.plan }}</small></td>
                                <td><strong class="text-success">₹{{ o.amount }}</strong></td>
                                <td>
                                    <span class="badge bg-secondary mb-1">{% if 'API' in o.key %}🌐 API{% else %}📦 LOCAL{% endif %}</span><br>
                                    <code class="text-light">{{ o.key }}</code>
                                    <i class="fas fa-copy text-muted ms-2" style="cursor:pointer;" onclick="navigator.clipboard.writeText('{{ o.key }}'); alert('Key Copied!');"></i>
                                </td>
                                <td class="small">{{ o.date }}</td>
                            </tr>
                            {% else %}
                            <tr><td colspan="5" class="text-center text-muted py-4">No key delivery history logged yet.</td></tr>
                            {% endfor %}
                        </tbody>
                    </table>
                </div>
            </div>
        </div>

    </div>
    {% endif %}

    <script>
        function showTab(t, el) {
            document.querySelectorAll('.tab-pane-content').forEach(d => d.style.display = 'none');
            let target = document.getElementById('tab-' + t);
            if(target) target.style.display = 'block';
            document.querySelectorAll('.sidebar-link').forEach(l => l.classList.remove('active'));
            if(el) el.classList.add('active');
            if(window.innerWidth < 769) document.getElementById('sidebar').classList.remove('active');
        }
        function filterTable(inputId, tableId) {
            let filter = document.getElementById(inputId).value.toLowerCase();
            let rows = document.querySelectorAll('#' + tableId + ' tbody tr');
            rows.forEach(r => r.style.display = r.innerText.toLowerCase().includes(filter) ? '' : 'none');
        }
        function saveProduct() {
            let name = document.getElementById('ap_name').value;
            let cat = document.getElementById('ap_cat').value;
            let plan = document.getElementById('ap_plan').value;
            let price = document.getElementById('ap_price').value;
            let rprice = document.getElementById('ap_rprice').value;
            let link = document.getElementById('ap_link').value;
            let pid = document.getElementById('ap_pid').value;
            let rdur = document.getElementById('ap_rdur').value;
            
            if(!name || !plan || !price) return alert('Fill required fields!');
            
            fetch('/api/product/save', {
                method: 'POST', headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({name: name, category: cat, prices: [[plan, parseFloat(price)]], reseller_price: parseFloat(rprice||price), download_link: link, pid: pid, remote_duration: rdur})
            }).then(r => r.json()).then(d => { 
                alert(d.message); 
                location.reload(); 
            });
        }
        function deleteProduct(k) {
            if(!confirm('Are you sure you want to delete this product?')) return;
            fetch('/api/product/delete', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({prod_key: k}) })
            .then(r => r.json()).then(d => { 
                // Show notification and reload
                let notif = document.getElementById('delNotify');
                notif.style.display = 'block';
                setTimeout(() => { location.reload(); }, 1500);
            });
        }
    </script>
</body>
</html>
"""

# ==========================================
# 🌐 FLASK BACKEND ENDPOINTS
# ==========================================
@app.route('/login', methods=['POST'])
def login():
    if request.form.get('password') == get_current_password():
        session.permanent = True
        session['admin_logged'] = True
        return redirect('/')
    return render_template_string(ADMIN_HTML, bot_info=BOT_INFO, error="Invalid Admin Password!")

@app.route('/logout')
def logout():
    session.clear()
    return redirect('/')

@app.route('/')
def dashboard():
    if not session.get('admin_logged'):
        return render_template_string(ADMIN_HTML, bot_info=BOT_INFO)

    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('SELECT COUNT(*), SUM(wallet_balance) FROM users'); r_u = c.fetchone(); tot_users = r_u[0] or 0; tot_wallet = r_u[1] or 0.0
    c.execute('SELECT COUNT(*), SUM(amount) FROM order_history'); r_o = c.fetchone(); tot_orders = r_o[0] or 0; tot_rev = r_o[1] or 0.0
    c.execute('SELECT COUNT(*) FROM products'); p_cnt = c.fetchone()[0]
    c.execute('SELECT COUNT(*) FROM keys_inventory WHERE is_used = 0'); k_cnt = c.fetchone()[0]
    
    # Products with stock count
    prods = []
    c.execute('SELECT prod_key, name, category, prices, icon FROM products')
    for p in c.fetchall():
        c.execute('SELECT COUNT(*) FROM keys_inventory WHERE prod_key=? AND is_used=0', (p[0],))
        scount = c.fetchone()[0]
        prods.append({"prod_key": p[0], "name": p[1], "category": p[2], "prices": json.loads(p[3]), "icon": p[4] or "⚡", "stock_count": scount})
    
    # Today's Analytics
    today_str = datetime.now(IST).strftime("%d %b %Y")
    c.execute("SELECT COUNT(*), SUM(amount) FROM order_history WHERE timestamp LIKE ?", (f"%{today_str}%",))
    r_today = c.fetchone()
    today_orders = r_today[0] or 0
    today_rev = r_today[1] or 0.0

    # All Orders for Delivery Logs
    c.execute('''
        SELECT o.user_id, u.full_name, o.prod_name, o.plan, o.key_delivered, o.amount, o.timestamp 
        FROM order_history o 
        LEFT JOIN users u ON o.user_id = u.user_id 
        ORDER BY o.id DESC LIMIT 100
    ''')
    all_orders = [{"user_id": r[0], "user_name": r[1] or "Unknown", "prod_name": r[2], "plan": r[3], "key": r[4], "amount": r[5], "date": r[6]} for r in c.fetchall()]

    conn.close()

    return render_template_string(
        ADMIN_HTML, bot_info=BOT_INFO,
        stats={"users": tot_users, "total_wallet": f"{tot_wallet:,.2f}", "products_count": p_cnt, "orders": tot_orders, "revenue": f"{tot_rev:,.2f}", "keys": k_cnt, "today_orders": today_orders, "today_rev": f"{today_rev:,.2f}"},
        products=prods, all_orders=all_orders
    )

@app.route('/api/product/save', methods=['POST'])
def api_save_p():
    if not session.get('admin_logged'): return jsonify({"message": "Unauthorized"}), 401
    d = request.json
    k = re.sub(r'[^a-zA-Z0-9]', '_', d['name']).strip('_').lower()
    conn = sqlite3.connect(DB_FILE); c = conn.cursor()
    c.execute('''INSERT OR REPLACE INTO products (prod_key, name, category, prices, download_link, icon, maintenance, stock_out, reseller_price, remote_pid, remote_duration)
                 VALUES (?, ?, ?, ?, ?, "⚡", 0, 0, ?, ?, ?)''',
              (k, d['name'], d['category'], json.dumps(d['prices']), d.get('download_link',''), d.get('reseller_price',0), d.get('pid',''), d.get('remote_duration','')))
    conn.commit(); conn.close()
    return jsonify({"message": "Product saved successfully!"})

@app.route('/api/product/delete', methods=['POST'])
def api_del_p():
    if not session.get('admin_logged'): return jsonify({"message": "Unauthorized"}), 401
    conn = sqlite3.connect(DB_FILE); c = conn.cursor()
    c.execute('DELETE FROM products WHERE prod_key = ?', (request.json.get('prod_key'),))
    conn.commit(); conn.close()
    return jsonify({"message": "Product deleted!"})

def run_web():
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)

if __name__ == "__main__":
    run_web()

    
    
    
    
