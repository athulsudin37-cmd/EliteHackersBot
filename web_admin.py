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
# 🗄️ DATABASE INITIALIZATION
# ==========================================
def init_admin_database():
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
    conn.commit()
    conn.close()

def get_current_password():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('SELECT password FROM admin_auth WHERE id = 1')
    row = c.fetchone()
    conn.close()
    return row[0] if row else DEFAULT_ADMIN_PWD

init_admin_database()

# Dynamic Bot DP Fetcher
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
# 🎨 MASTER ADMIN PANEL HTML TEMPLATE
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
            --card-glass: rgba(22, 13, 44, 0.92);
            --neon-border: rgba(147, 51, 234, 0.35);
            --accent-purple: #8b5cf6;
            --accent-cyan: #38bdf8;
            --sidebar-w: 270px;
        }
        body {
            background: radial-gradient(circle at top center, #1b0c3f 0%, #0c051d 60%, #05020c 100%);
            background-attachment: fixed; color: #f8fafc; font-family: -apple-system, system-ui, sans-serif;
            min-height: 100vh; margin: 0; overflow-x: hidden;
        }
        body::before {
            content: ''; position: fixed; top: 0; left: 0; width: 100%; height: 100%;
            background-image: linear-gradient(rgba(147, 51, 234, 0.05) 1px, transparent 1px), linear-gradient(90deg, rgba(147, 51, 234, 0.05) 1px, transparent 1px);
            background-size: 36px 36px; pointer-events: none; z-index: 0;
        }

        /* 🔐 LOGIN SCREEN */
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
        .badge-pill { border-radius: 20px; padding: 4px 12px; font-size: 0.78rem; font-weight: 600; }
    </style>
</head>
<body>

    {% if not session.get('admin_logged') %}
    <!-- 🔐 LOGIN SCREEN -->
    <div class="login-box">
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
                    <span class="eye-btn" onclick="togglePwd()"><i id="eyeI" class="fa-regular fa-eye"></i></span>
                </div>
                <button type="submit" class="btn-unlock">Unlock</button>
            </form>
            <div style="margin-top:20px;font-size:0.78rem;color:#d8b4fe;">🔐 Owner-only · session remembered for 30 days</div>
        </div>
    </div>
    <script>
        function togglePwd() {
            let p = document.getElementById('pInput');
            let e = document.getElementById('eyeI');
            p.type = p.type === 'password' ? 'text' : 'password';
            e.className = p.type === 'password' ? 'fa-regular fa-eye' : 'fa-regular fa-eye-slash';
        }
    </script>
    {% else %}

    <!-- 🌟 SIDEBAR DRAWER -->
    <div class="sidebar" id="sidebar">
        <div class="px-3 pb-3 border-bottom border-secondary d-flex align-items-center gap-2">
            <span class="fs-4">⚡</span>
            <strong class="text-info">{{ bot_info.name }}</strong>
        </div>
        <div class="mt-3">
            <a class="sidebar-link active" onclick="showTab('dashboard', this)"><i class="fas fa-chart-line"></i> Dashboard</a>
            <a class="sidebar-link" onclick="showTab('products', this)"><i class="fas fa-boxes-stacked"></i> Manage Product</a>
            <a class="sidebar-link" onclick="showTab('keys', this)"><i class="fas fa-key"></i> Manage Keys</a>
            <a class="sidebar-link" onclick="showTab('pending_keys', this)"><i class="fas fa-clock"></i> Pending Keys Queue</a>
            <a class="sidebar-link" onclick="showTab('id_stock', this)"><i class="fas fa-id-badge"></i> ID Stock</a>
            <a class="sidebar-link" onclick="showTab('members', this)"><i class="fas fa-users"></i> Members & Wallets</a>
            <a class="sidebar-link" onclick="showTab('resellers', this)"><i class="fas fa-handshake"></i> Resellers</a>
            <a class="sidebar-link" onclick="showTab('broadcast', this)"><i class="fas fa-bullhorn"></i> Broadcast</a>
            <a class="sidebar-link" onclick="showTab('coupons', this)"><i class="fas fa-ticket"></i> Coupon Manager</a>
            <a class="sidebar-link" onclick="showTab('upi', this)"><i class="fas fa-credit-card"></i> UPI Payment Setup</a>
            <a class="sidebar-link" onclick="showTab('topups', this)"><i class="fas fa-wallet"></i> Top-ups Queue</a>
            <a class="sidebar-link" onclick="showTab('links', this)"><i class="fas fa-link"></i> Product Links</a>
            <a class="sidebar-link" onclick="showTab('security', this)"><i class="fas fa-shield-halved"></i> Security & Sessions</a>
            <a class="sidebar-link" onclick="showTab('store', this)"><i class="fas fa-sliders"></i> Store Settings</a>
            <a href="/logout" class="sidebar-link text-danger mt-4"><i class="fas fa-lock"></i> Logout</a>
        </div>
    </div>

    <div class="main-content">
        <div class="d-flex justify-content-between align-items-center mb-4">
            <div class="d-flex align-items-center gap-3">
                <button class="btn btn-dark d-md-none" onclick="document.getElementById('sidebar').classList.toggle('active')"><i class="fas fa-bars"></i></button>
                <h4 class="fw-bold m-0 title-grad">Bot Control Center</h4>
            </div>
            <a href="/logout" class="btn btn-outline-danger btn-sm"><i class="fas fa-arrow-right-from-bracket"></i></a>
        </div>

        <!-- 1️⃣ 📊 DASHBOARD -->
        <div class="tab-pane-content" id="tab-dashboard">
            <div class="card p-4 mb-4" style="background:linear-gradient(135deg,#1f1042,#110729);">
                <small class="text-info fw-bold">⚡ BOT CONTROL CENTER</small>
                <h3 class="fw-bold mt-1">Welcome back.</h3>
                <span class="text-muted">Here's how {{ bot_info.username }} is doing right now.</span>
            </div>
            <div class="row g-3">
                <div class="col-6 col-md-3"><div class="stat-card"><h6>MEMBERS</h6><h2>{{ stats.users }}</h2></div></div>
                <div class="col-6 col-md-3"><div class="stat-card" style="border-left-color:#10b981;"><h6>WALLET (ALL)</h6><h2>₹{{ stats.total_wallet }}</h2></div></div>
                <div class="col-6 col-md-3"><div class="stat-card" style="border-left-color:#38bdf8;"><h6>CATALOG ITEMS</h6><h2>{{ stats.products_count }}</h2></div></div>
                <div class="col-6 col-md-3"><div class="stat-card" style="border-left-color:#10b981;"><h6>ORDERS FULFILLED</h6><h2>{{ stats.orders }}</h2></div></div>
                <div class="col-6 col-md-3"><div class="stat-card" style="border-left-color:#facc15;"><h6>LIFETIME REVENUE</h6><h2>₹{{ stats.revenue }}</h2></div></div>
                <div class="col-6 col-md-3"><div class="stat-card" style="border-left-color:#06b6d4;"><h6>KEYS IN STOCK</h6><h2>{{ stats.keys }}</h2></div></div>
            </div>
        </div>

        <!-- 2️⃣ 📦 MANAGE PRODUCTS -->
        <div class="tab-pane-content" id="tab-products" style="display:none;">
            <div class="card p-4">
                <div class="d-flex justify-content-between align-items-center mb-3">
                    <h5 class="m-0 fw-bold">📦 Manage Products</h5>
                    <button class="btn btn-custom btn-sm" onclick="document.getElementById('addProdBox').style.display='block'"><i class="fas fa-plus me-1"></i> Add Product</button>
                </div>
                <div id="addProdBox" class="p-3 mb-4 card" style="display:none;background:#13082e;">
                    <h6>➕ Add New Product Specification</h6>
                    <div class="row g-3 mt-1">
                        <div class="col-md-4"><label class="small text-muted">Product Name</label><input type="text" id="ap_name" class="form-control" placeholder="BALA MOD NON ROOT"></div>
                        <div class="col-md-4">
                            <label class="small text-muted">Category</label>
                            <select id="ap_cat" class="form-select">
                                <option value="non_root">Non-Root Mobile</option><option value="root">Root Mobile</option>
                                <option value="ios">iOS Panels</option><option value="pc">PC Panels</option><option value="likes">8 Level ID / Likes</option>
                            </select>
                        </div>
                        <div class="col-md-4"><label class="small text-muted">Icon / Sticker</label><input type="text" id="ap_icon" class="form-control" placeholder="⚡"></div>
                        <div class="col-md-4"><label class="small text-muted">Plan Name</label><input type="text" id="ap_plan" class="form-control" placeholder="10_Days"></div>
                        <div class="col-md-4"><label class="small text-muted">Regular Price (₹)</label><input type="number" id="ap_price" class="form-control" placeholder="400"></div>
                        <div class="col-md-4"><label class="small text-muted">Reseller Price (₹)</label><input type="number" id="ap_rprice" class="form-control" placeholder="250"></div>
                        <div class="col-12"><label class="small text-muted">Download / Demo Preview Link</label><input type="text" id="ap_link" class="form-control" placeholder="https://t.me/..."></div>
                        <div class="col-12"><label class="small text-muted">Bulk Serial Keys (One per line)</label><textarea id="ap_keys" class="form-control" rows="3" placeholder="KEY-001&#10;KEY-002"></textarea></div>
                        <div class="col-12"><button class="btn btn-custom w-100" onclick="saveProduct()"><i class="fas fa-floppy-disk me-2"></i> Save Product to Store</button></div>
                    </div>
                </div>
                <div class="row g-3">
                    {% for p in products %}
                    <div class="col-md-6">
                        <div class="card p-3" style="background:#12072c;border:1px solid #3b1d6e;">
                            <div class="d-flex justify-content-between align-items-center">
                                <span class="fs-4">{{ p.icon }}</span>
                                <span class="badge bg-secondary">{{ p.category }}</span>
                            </div>
                            <h5 class="fw-bold mt-2">{{ p.name }}</h5>
                            <div class="text-muted small">Prices: {{ p.prices }}</div>
                            <div class="d-flex gap-2 mt-3">
                                <button class="btn btn-sm btn-outline-danger" onclick="deleteProduct('{{ p.prod_key }}')"><i class="fas fa-trash"></i> Delete</button>
                            </div>
                        </div>
                    </div>
                    {% endfor %}
                </div>
            </div>
        </div>

        <!-- 3️⃣ 🗝️ MANAGE KEYS (ANALYTICS & SEARCH) -->
        <div class="tab-pane-content" id="tab-keys" style="display:none;">
            <div class="card p-4">
                <h5>🗝️ Manage Keys & Sales Analytics</h5>
                <input type="text" id="keySearch" class="form-control mt-3 mb-3" placeholder="🔍 Search by Telegram ID, User, Product, Key..." onkeyup="filterTable('keySearch', 'keysTable')">
                <div class="table-responsive">
                    <table class="table table-dark align-middle" id="keysTable">
                        <thead><tr><th>User ID</th><th>Product</th><th>Plan</th><th>Paid</th><th>Delivered Key</th><th>Date (IST)</th></tr></thead>
                        <tbody>
                            {% for o in all_orders %}
                            <tr>
                                <td><code>{{ o[1] }}</code></td>
                                <td>{{ o[2] }}</td>
                                <td>{{ o[3] }}</td>
                                <td>₹{{ o[5] }}</td>
                                <td><code>{{ o[4] }}</code></td>
                                <td>{{ o[7] }}</td>
                            </tr>
                            {% else %}
                            <tr><td colspan="6" class="text-center text-muted">No key delivery history logged yet.</td></tr>
                            {% endfor %}
                        </tbody>
                    </table>
                </div>
            </div>
        </div>

        <!-- 4️⃣ ⏳ PENDING KEYS (MANUAL DISPATCH QUEUE) -->
        <div class="tab-pane-content" id="tab-pending_keys" style="display:none;">
            <div class="card p-4">
                <h5>⏳ Pending Keys Queue (Out-of-Stock Dispatch)</h5>
                <p class="text-muted small">Orders waiting for stock. Enter fresh key and click send to dispatch directly to customer Telegram!</p>
                <div class="table-responsive">
                    <table class="table table-dark">
                        <thead><tr><th>User ID</th><th>Product</th><th>Plan</th><th>Paid</th><th>Action</th></tr></thead>
                        <tbody>
                            {% for pk in pending_orders %}
                            <tr>
                                <td><code>{{ pk[1] }}</code></td>
                                <td>{{ pk[2] }}</td>
                                <td>{{ pk[3] }}</td>
                                <td>₹{{ pk[5] }}</td>
                                <td>
                                    <div class="d-flex gap-2">
                                        <input type="text" id="key_disp_{{ pk[0] }}" class="form-control form-control-sm" placeholder="Paste Fresh Key">
                                        <button class="btn btn-success btn-sm" onclick="dispatchKey({{ pk[0] }}, '{{ pk[1] }}')">Dispatch</button>
                                    </div>
                                </td>
                            </tr>
                            {% else %}
                            <tr><td colspan="5" class="text-center text-muted">No pending orders in queue. All keys delivered!</td></tr>
                            {% endfor %}
                        </tbody>
                    </table>
                </div>
            </div>
        </div>

        <!-- 5️⃣ 🆔 ID STOCK -->
        <div class="tab-pane-content" id="tab-id_stock" style="display:none;">
            <div class="card p-4">
                <h5>🆔 ID Stock (Accounts Management)</h5>
                <div class="row g-3 mt-1">
                    <div class="col-md-4"><label class="small text-muted">Account Type</label>
                        <select id="id_cat" class="form-select">
                            <option value="8 Level ID">8 Level ID</option>
                            <option value="Facebook ID">Facebook ID</option>
                            <option value="Gmail ID">Gmail ID</option>
                        </select>
                    </div>
                    <div class="col-md-4"><label class="small text-muted">Price (₹)</label><input type="number" id="id_price" class="form-control" placeholder="45"></div>
                    <div class="col-md-4"><label class="small text-muted">Credentials (Username/Number:Pass)</label><input type="text" id="id_cred" class="form-control" placeholder="9847123456:Pass@123"></div>
                    <div class="col-12"><button class="btn btn-custom" onclick="saveIdStock()"><i class="fas fa-plus me-1"></i> Add Account to Stock</button></div>
                </div>
            </div>
        </div>

        <!-- 6️⃣ 👥 MEMBERS & WALLETS (INSPECTOR) -->
        <div class="tab-pane-content" id="tab-members" style="display:none;">
            <div class="card p-4">
                <h5>👥 Members & Wallets Management</h5>
                <input type="text" id="memSearch" class="form-control mt-2 mb-3" placeholder="🔍 Search by name, @username, or Telegram ID..." onkeyup="filterTable('memSearch', 'memTable')">
                <div class="table-responsive">
                    <table class="table table-dark align-middle" id="memTable">
                        <thead><tr><th>Telegram ID</th><th>Name</th><th>Role</th><th>Balance</th><th>Orders</th><th>Adjust Balance</th></tr></thead>
                        <tbody>
                            {% for u in users_list %}
                            <tr>
                                <td><code>{{ u[0] }}</code></td>
                                <td>{{ u[1] }}</td>
                                <td><span class="badge {{ 'bg-warning text-dark' if u[9]=='Reseller' else 'bg-secondary' }}">{{ u[9] }}</span></td>
                                <td><strong>₹{{ u[5] }}</strong></td>
                                <td>{{ u[4] }}</td>
                                <td>
                                    <div class="d-flex gap-1">
                                        <input type="number" id="bal_adj_{{ u[0] }}" class="form-control form-control-sm" style="width:85px;" placeholder="+50/-20">
                                        <button class="btn btn-sm btn-custom" onclick="adjustBalance({{ u[0] }})">Apply</button>
                                    </div>
                                </td>
                            </tr>
                            {% endfor %}
                        </tbody>
                    </table>
                </div>
            </div>
        </div>

        <!-- 7️⃣ 🤝 RESELLERS -->
        <div class="tab-pane-content" id="tab-resellers" style="display:none;">
            <div class="card p-4">
                <h5>🤝 Resellers Management</h5>
                <div class="table-responsive mt-3">
                    <table class="table table-dark align-middle">
                        <thead><tr><th>Telegram ID</th><th>Name</th><th>Wallet</th><th>Promote / Demote</th></tr></thead>
                        <tbody>
                            {% for u in users_list %}
                            <tr>
                                <td><code>{{ u[0] }}</code></td>
                                <td>{{ u[1] }}</td>
                                <td>₹{{ u[5] }}</td>
                                <td>
                                    {% if u[9] == 'Reseller' %}
                                        <button class="btn btn-sm btn-outline-warning" onclick="toggleResellerRole({{ u[0] }}, 'Regular')">Revoke Reseller</button>
                                    {% else %}
                                        <button class="btn btn-sm btn-custom" onclick="toggleResellerRole({{ u[0] }}, 'Reseller')"><i class="fas fa-handshake me-1"></i> Make Reseller</button>
                                    {% endif %}
                                </td>
                            </tr>
                            {% endfor %}
                        </tbody>
                    </table>
                </div>
            </div>
        </div>

        <!-- 8️⃣ 📢 BROADCAST -->
        <div class="tab-pane-content" id="tab-broadcast" style="display:none;">
            <div class="card p-4">
                <h5>📢 Broadcast Announcement to All Users</h5>
                <textarea id="bc_msg" class="form-control mt-3" rows="6" placeholder="Type announcement message (HTML supported)..."></textarea>
                <button class="btn btn-custom mt-3" onclick="sendBroadcast()"><i class="fas fa-paper-plane me-2"></i> Send to All Users</button>
            </div>
        </div>

        <!-- 9️⃣ 🎟️ COUPON MANAGER -->
        <div class="tab-pane-content" id="tab-coupons" style="display:none;">
            <div class="card p-4">
                <h5>🎟️ Coupon & Promo Codes Manager</h5>
                <div class="row g-3 mt-1">
                    <div class="col-md-4"><label class="small text-muted">Promo Code</label><input type="text" id="cp_code" class="form-control" placeholder="OFF50"></div>
                    <div class="col-md-4"><label class="small text-muted">Discount Value (₹)</label><input type="number" id="cp_val" class="form-control" placeholder="50"></div>
                    <div class="col-md-4"><label class="small text-muted">Usage Limit (Users)</label><input type="number" id="cp_limit" class="form-control" placeholder="50"></div>
                    <div class="col-12"><button class="btn btn-custom" onclick="saveCoupon()"><i class="fas fa-plus me-1"></i> Create Promo Code</button></div>
                </div>
            </div>
        </div>

        <!-- 🔟 💳 UPI PAYMENT SETUP -->
        <div class="tab-pane-content" id="tab-upi" style="display:none;">
            <div class="card p-4">
                <h5>💳 UPI Gateway Setup (FamPay & Paytm)</h5>
                <div class="row g-3 mt-1">
                    <div class="col-md-6"><label class="small text-muted">FamPay Receiver UPI ID</label><input type="text" id="u_fam" class="form-control" value="{{ upi.fampay_token }}"></div>
                    <div class="col-md-6"><label class="small text-muted">Paytm Gateway Token / UPI</label><input type="text" id="u_paytm" class="form-control" value="{{ upi.paytm_token }}"></div>
                    <div class="col-12"><button class="btn btn-custom" onclick="saveUpi()"><i class="fas fa-save me-1"></i> Save Gateway Settings</button></div>
                </div>
            </div>
        </div>

        <!-- 1️⃣1️⃣ 💳 TOP-UPS QUEUE -->
        <div class="tab-pane-content" id="tab-topups" style="display:none;">
            <div class="card p-4">
                <h5>💳 Review Pending Deposit Requests</h5>
                <p class="text-muted small">Customer deposit transactions awaiting verification.</p>
                <div class="table-responsive">
                    <table class="table table-dark">
                        <thead><tr><th>User ID</th><th>Amount</th><th>UTR</th><th>Action</th></tr></thead>
                        <tbody>
                            <tr><td colspan="4" class="text-center text-muted">No pending deposit requests.</td></tr>
                        </tbody>
                    </table>
                </div>
            </div>
        </div>

        <!-- 1️⃣2️⃣ 🔗 PRODUCT LINKS -->
        <div class="tab-pane-content" id="tab-links" style="display:none;">
            <div class="card p-4">
                <h5>🔗 Product Direct Deep Links</h5>
                <div class="table-responsive mt-3">
                    <table class="table table-dark align-middle">
                        <thead><tr><th>Product</th><th>Direct Buy Link</th><th>Action</th></tr></thead>
                        <tbody>
                            {% for p in products %}
                            <tr>
                                <td>{{ p.name }}</td>
                                <td><code>https://t.me/{{ bot_info.username.replace('@','') }}?start=buy_{{ p.prod_key }}</code></td>
                                <td><button class="btn btn-sm btn-custom" onclick="navigator.clipboard.writeText('https://t.me/{{ bot_info.username.replace('@','') }}?start=buy_{{ p.prod_key }}'); alert('Link Copied!');">Copy</button></td>
                            </tr>
                            {% endfor %}
                        </tbody>
                    </table>
                </div>
            </div>
        </div>

        <!-- 1️⃣3️⃣ 🛡️ SECURITY & PASSWORD -->
        <div class="tab-pane-content" id="tab-security" style="display:none;">
            <div class="card p-4">
                <h5>🛡️ Change Master Admin Password</h5>
                <div class="row g-3 mt-1">
                    <div class="col-md-6"><label class="small text-muted">New Password</label><input type="password" id="sec_pwd" class="form-control" placeholder="New Password"></div>
                    <div class="col-12"><button class="btn btn-warning" onclick="changePwd()">Update Password</button></div>
                </div>
            </div>
        </div>

        <!-- 1️⃣4️⃣ ⚙️ STORE SETTINGS -->
        <div class="tab-pane-content" id="tab-store" style="display:none;">
            <div class="card p-4">
                <h5>⚙️ Store Branding & Configuration</h5>
                <div class="row g-3 mt-1">
                    <div class="col-md-6"><label class="small text-muted">Support Username</label><input type="text" id="st_supp" class="form-control" value="{{ store.support_username }}"></div>
                    <div class="col-md-6"><label class="small text-muted">Tutorial Video Link</label><input type="text" id="st_how" class="form-control" value="{{ store.how_to_use_link }}"></div>
                    <div class="col-12"><button class="btn btn-custom" onclick="saveStore()">Save Store Settings</button></div>
                </div>
            </div>
        </div>
    </div>
    {% endif %}

    <script>
        function showTab(t, el) {
            document.querySelectorAll('.tab-pane-content').forEach(d => d.style.display = 'none');
            let target = document.getElementById('tab-' + t);
            if (target) target.style.display = 'block';
            document.querySelectorAll('.sidebar-link').forEach(l => l.classList.remove('active'));
            if (el) el.classList.add('active');
            if (window.innerWidth < 769) document.getElementById('sidebar').classList.remove('active');
        }
        function filterTable(inputId, tableId) {
            let filter = document.getElementById(inputId).value.toLowerCase();
            let rows = document.querySelectorAll('#' + tableId + ' tbody tr');
            rows.forEach(r => r.style.display = r.innerText.toLowerCase().includes(filter) ? '' : 'none');
        }
        function saveProduct() {
            let name = document.getElementById('ap_name').value;
            let cat = document.getElementById('ap_cat').value;
            let icon = document.getElementById('ap_icon').value;
            let plan = document.getElementById('ap_plan').value;
            let price = document.getElementById('ap_price').value;
            let rprice = document.getElementById('ap_rprice').value;
            let link = document.getElementById('ap_link').value;
            let keys = document.getElementById('ap_keys').value;
            if(!name || !plan || !price) { alert('Fill required fields!'); return; }
            fetch('/api/product/save', {
                method: 'POST', headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({name: name, category: cat, icon: icon, prices: [[plan, parseFloat(price)]], reseller_price: parseFloat(rprice || price), download_link: link, keys: keys})
            }).then(r => r.json()).then(d => { alert(d.message); location.reload(); });
        }
        function deleteProduct(k) {
            if(!confirm('Delete this product?')) return;
            fetch('/api/product/delete', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({prod_key: k}) })
            .then(r => r.json()).then(d => { alert(d.message); location.reload(); });
        }
        function dispatchKey(oid, uid) {
            let key = document.getElementById('key_disp_' + oid).value;
            if(!key) return alert('Enter key!');
            fetch('/api/pending/dispatch', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({order_id: oid, user_id: uid, key: key}) })
            .then(r => r.json()).then(d => { alert(d.message); location.reload(); });
        }
        function adjustBalance(uid) {
            let val = parseFloat(document.getElementById('bal_adj_' + uid).value);
            if(isNaN(val)) return alert('Enter valid amount!');
            fetch('/api/user/adjust_balance', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({user_id: uid, delta: val}) })
            .then(r => r.json()).then(d => { alert(d.message); location.reload(); });
        }
        function toggleResellerRole(uid, role) {
            fetch('/api/user/set_role', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({user_id: uid, role: role}) })
            .then(r => r.json()).then(d => { alert(d.message); location.reload(); });
        }
        function saveIdStock() {
            let cat = document.getElementById('id_cat').value;
            let pr = document.getElementById('id_price').value;
            let cred = document.getElementById('id_cred').value;
            if(!pr || !cred) return alert('Fill fields!');
            fetch('/api/id_stock/add', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({category: cat, price: parseFloat(pr), credentials: cred}) })
            .then(r => r.json()).then(d => { alert(d.message); location.reload(); });
        }
        function saveCoupon() {
            let c = document.getElementById('cp_code').value;
            let v = document.getElementById('cp_val').value;
            let l = document.getElementById('cp_limit').value;
            if(!c || !v) return alert('Fill fields!');
            fetch('/api/coupon/save', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({code: c, val: parseFloat(v), limit: parseInt(l||100)}) })
            .then(r => r.json()).then(d => { alert(d.message); location.reload(); });
        }
        function sendBroadcast() {
            let msg = document.getElementById('bc_msg').value;
            if(!msg) return alert('Enter message!');
            fetch('/api/broadcast/send', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({message: msg}) })
            .then(r => r.json()).then(d => alert(d.message));
        }
        function saveUpi() {
            fetch('/api/upi/save', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({fampay_token: document.getElementById('u_fam').value, paytm_token: document.getElementById('u_paytm').value}) })
            .then(r => r.json()).then(d => alert(d.message));
        }
        function saveStore() {
            fetch('/api/store/save', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({support_username: document.getElementById('st_supp').value, how_to_use_link: document.getElementById('st_how').value}) })
            .then(r => r.json()).then(d => alert(d.message));
        }
        function changePwd() {
            let p = document.getElementById('sec_pwd').value;
            if(!p) return alert('Enter new password!');
            fetch('/api/security/change_pwd', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({password: p}) })
            .then(r => r.json()).then(d => alert(d.message));
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
    c.execute('SELECT COUNT(*) FROM products'); tot_prods = c.fetchone()[0]
    c.execute('SELECT COUNT(*) FROM keys_inventory WHERE is_used = 0'); tot_keys = c.fetchone()[0]
    
    c.execute('SELECT prod_key, name, category, prices, icon, download_link FROM products'); prods = [{"prod_key": p[0], "name": p[1], "category": p[2], "prices": json.loads(p[3]), "icon": p[4] or "⚡", "download_link": p[5]} for p in c.fetchall()]
    c.execute('SELECT id, user_id, prod_name, plan, key_delivered, amount, utr, timestamp FROM order_history ORDER BY id DESC LIMIT 50'); all_orders = c.fetchall()
    c.execute('SELECT id, user_id, prod_name, plan, key_delivered, amount, timestamp FROM order_history WHERE key_delivered LIKE "PENDING%" ORDER BY id DESC'); pending = c.fetchall()
    c.execute('SELECT user_id, full_name, username, joined_date, orders_count, wallet_balance, total_spent, total_referrals, referral_earnings, account_type FROM users ORDER BY user_id DESC LIMIT 50'); users_list = c.fetchall()
    c.execute('SELECT fampay_token, paytm_token FROM upi_settings WHERE id = 1'); upi_r = c.fetchone() or ("", "")
    c.execute('SELECT support_username, how_to_use_link FROM store_settings WHERE id = 1'); st_r = c.fetchone() or ("@Athulsudin", "")
    conn.close()

    return render_template_string(
        ADMIN_HTML, bot_info=BOT_INFO,
        stats={"users": tot_users, "total_wallet": f"{tot_wallet:,.2f}", "products_count": tot_prods, "orders": tot_orders, "revenue": f"{tot_rev:,.2f}", "keys": tot_keys},
        products=prods, all_orders=all_orders, pending_orders=pending, users_list=users_list,
        upi={"fampay_token": upi_r[0], "paytm_token": upi_r[1]},
        store={"support_username": st_r[0], "how_to_use_link": st_r[1]}
    )

@app.route('/api/product/save', methods=['POST'])
def api_save_p():
    if not session.get('admin_logged'): return jsonify({"message": "Unauthorized"}), 401
    d = request.json
    k = re.sub(r'[^a-zA-Z0-9]', '_', d['name']).strip('_').lower()
    conn = sqlite3.connect(DB_FILE); c = conn.cursor()
    c.execute('INSERT OR REPLACE INTO products (prod_key, name, category, prices, download_link, icon, maintenance, stock_out) VALUES (?, ?, ?, ?, ?, ?, 0, 0)',
              (k, d['name'], d['category'], json.dumps(d['prices']), d.get('download_link',''), d.get('icon','⚡')))
    keys_txt = d.get('keys', '')
    if keys_txt:
        for key_item in keys_txt.split('\n'):
            if key_item.strip():
                c.execute('INSERT INTO keys_inventory (prod_key, plan, item_key, is_used) VALUES (?, ?, ?, 0)', (k, d['prices'][0][0], key_item.strip()))
    conn.commit(); conn.close()
    return jsonify({"message": "Product and keys saved successfully!"})

@app.route('/api/product/delete', methods=['POST'])
def api_del_p():
    if not session.get('admin_logged'): return jsonify({"message": "Unauthorized"}), 401
    conn = sqlite3.connect(DB_FILE); c = conn.cursor()
    c.execute('DELETE FROM products WHERE prod_key = ?', (request.json.get('prod_key'),))
    conn.commit(); conn.close()
    return jsonify({"message": "Product deleted!"})

@app.route('/api/pending/dispatch', methods=['POST'])
def api_dispatch():
    if not session.get('admin_logged'): return jsonify({"message": "Unauthorized"}), 401
    d = request.json
    oid = d['order_id']; uid = d['user_id']; key = d['key'].strip()
    conn = sqlite3.connect(DB_FILE); c = conn.cursor()
    c.execute('UPDATE order_history SET key_delivered = ? WHERE id = ?', (key, oid))
    conn.commit(); conn.close()
    try:
        msg = f"✅ <b>Payment verified — here's your key!</b>\n⏩ ~~~~~~~~~~~~~~~~~~~~~~~~~~\n\n🗝️ Your Key:\n<code>{key}</code>\n\nThank you for shopping with us!"
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        payload = json.dumps({"chat_id": uid, "text": msg, "parse_mode": "HTML"}).encode()
        urllib.request.urlopen(urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"}), timeout=5)
    except Exception: pass
    return jsonify({"message": "Key dispatched directly to customer!"})

@app.route('/api/user/adjust_balance', methods=['POST'])
def api_adj_bal():
    if not session.get('admin_logged'): return jsonify({"message": "Unauthorized"}), 401
    d = request.json
    conn = sqlite3.connect(DB_FILE); c = conn.cursor()
    c.execute('UPDATE users SET wallet_balance = wallet_balance + ? WHERE user_id = ?', (d['delta'], d['user_id']))
    conn.commit(); conn.close()
    return jsonify({"message": "Wallet balance updated!"})

@app.route('/api/user/set_role', methods=['POST'])
def api_set_role():
    if not session.get('admin_logged'): return jsonify({"message": "Unauthorized"}), 401
    d = request.json
    conn = sqlite3.connect(DB_FILE); c = conn.cursor()
    c.execute('UPDATE users SET account_type = ? WHERE user_id = ?', (d['role'], d['user_id']))
    conn.commit(); conn.close()
    return jsonify({"message": f"Role updated to {d['role']}!"})

@app.route('/api/id_stock/add', methods=['POST'])
def api_id_stock():
    if not session.get('admin_logged'): return jsonify({"message": "Unauthorized"}), 401
    d = request.json
    conn = sqlite3.connect(DB_FILE); c = conn.cursor()
    c.execute('INSERT INTO id_accounts (category_name, account_data, price, is_sold) VALUES (?, ?, ?, 0)',
              (d['category'], d['credentials'], d['price']))
    conn.commit(); conn.close()
    return jsonify({"message": "Account added to ID Stock!"})

@app.route('/api/coupon/save', methods=['POST'])
def api_coupon():
    if not session.get('admin_logged'): return jsonify({"message": "Unauthorized"}), 401
    d = request.json
    conn = sqlite3.connect(DB_FILE); c = conn.cursor()
    c.execute('INSERT OR REPLACE INTO coupons (code, discount_type, discount_val, usage_limit, used_count, is_active) VALUES (?, "flat", ?, ?, 0, 1)',
              (d['code'].upper(), d['val'], d['limit']))
    conn.commit(); conn.close()
    return jsonify({"message": "Promo code activated!"})

@app.route('/api/broadcast/send', methods=['POST'])
def api_bc():
    if not session.get('admin_logged'): return jsonify({"message": "Unauthorized"}), 401
    msg = request.json.get('message', '')
    def _run():
        conn = sqlite3.connect(DB_FILE); c = conn.cursor()
        c.execute('SELECT user_id FROM users'); uids = [r[0] for r in c.fetchall()]
        conn.close()
        for u in uids:
            try:
                url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
                payload = json.dumps({"chat_id": u, "text": msg, "parse_mode": "HTML"}).encode()
                urllib.request.urlopen(urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"}), timeout=4)
            except Exception: pass
    Thread(target=_run).start()
    return jsonify({"message": "Broadcast sent to all users!"})

@app.route('/api/upi/save', methods=['POST'])
def api_save_upi():
    if not session.get('admin_logged'): return jsonify({"message": "Unauthorized"}), 401
    d = request.json
    conn = sqlite3.connect(DB_FILE); c = conn.cursor()
    c.execute('UPDATE upi_settings SET fampay_token=?, paytm_token=? WHERE id = 1', (d.get('fampay_token',''), d.get('paytm_token','')))
    conn.commit(); conn.close()
    return jsonify({"message": "UPI settings saved!"})

@app.route('/api/store/save', methods=['POST'])
def api_save_st():
    if not session.get('admin_logged'): return jsonify({"message": "Unauthorized"}), 401
    d = request.json
    conn = sqlite3.connect(DB_FILE); c = conn.cursor()
    c.execute('UPDATE store_settings SET support_username=?, how_to_use_link=? WHERE id = 1', (d.get('support_username',''), d.get('how_to_use_link','')))
    conn.commit(); conn.close()
    return jsonify({"message": "Store settings saved!"})

@app.route('/api/security/change_pwd', methods=['POST'])
def api_pwd():
    if not session.get('admin_logged'): return jsonify({"message": "Unauthorized"}), 401
    np = request.json.get('password', '').strip()
    if np:
        conn = sqlite3.connect(DB_FILE); c = conn.cursor()
        c.execute('UPDATE admin_auth SET password = ? WHERE id = 1', (np,))
        conn.commit(); conn.close()
        return jsonify({"message": "Password updated successfully!"})
    return jsonify({"message": "Password cannot be empty!"}), 400

def run_web():
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)

if __name__ == "__main__":
    run_web()
