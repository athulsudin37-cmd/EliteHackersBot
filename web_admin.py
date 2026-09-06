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
# 🗄️ DATABASE & AUTH
# ==========================================
def init_db_extended():
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
        CREATE TABLE IF NOT EXISTS store_customization (
            id INTEGER PRIMARY KEY DEFAULT 1,
            monthly_users TEXT DEFAULT '3,074 monthly users',
            header_badge TEXT DEFAULT '🔥 FAST • TRUSTED • ACTIVE 🔥'
        )
    ''')
    c.execute('SELECT id FROM store_customization WHERE id = 1')
    if not c.fetchone():
        c.execute('INSERT INTO store_customization VALUES (1, "3,074 monthly users", "🔥 FAST • TRUSTED • ACTIVE 🔥")')
    conn.commit()
    conn.close()

def get_current_password():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('SELECT password FROM admin_auth WHERE id = 1')
    row = c.fetchone()
    conn.close()
    return row[0] if row else DEFAULT_ADMIN_PWD

init_db_extended()

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
# 🎨 COMPLETE CYBER ADMIN DASHBOARD HTML
# ==========================================
ADMIN_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Bot Control Center</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
    <link href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css" rel="stylesheet">
    <style>
        :root { --bg: #080318; --card: rgba(22, 13, 44, 0.88); --neon: rgba(147, 51, 234, 0.35); --sidebar: 270px; }
        body { background: radial-gradient(circle at top center, #1b0c3f 0%, #0c051d 60%, #05020c 100%); color: #f8fafc; font-family: -apple-system, system-ui, sans-serif; min-height: 100vh; margin: 0; }
        body::before { content: ''; position: fixed; top: 0; left: 0; width: 100%; height: 100%; background-image: linear-gradient(rgba(147, 51, 234, 0.05) 1px, transparent 1px), linear-gradient(90deg, rgba(147, 51, 234, 0.05) 1px, transparent 1px); background-size: 36px 36px; pointer-events: none; }
        
        /* 🔐 LOGIN SCREEN */
        .login-box { min-height: 100vh; display: flex; align-items: center; justify-content: center; padding: 20px; }
        .login-card { background: var(--card); backdrop-filter: blur(25px); border: 1px solid var(--neon); border-radius: 28px; padding: 42px 32px; width: 100%; max-width: 400px; text-align: center; box-shadow: 0 0 50px rgba(139, 92, 246, 0.25); }
        .avatar-ring { width: 88px; height: 88px; margin: 0 auto 20px; border-radius: 50%; padding: 3px; background: linear-gradient(135deg, #06b6d4, #a855f7, #f59e0b); display: flex; align-items: center; justify-content: center; }
        .avatar-inner { width: 100%; height: 100%; background: #0d0622; border-radius: 50%; display: flex; align-items: center; justify-content: center; overflow: hidden; }
        .title-grad { font-size: 1.65rem; font-weight: 800; background: linear-gradient(90deg, #a78bfa, #38bdf8, #facc15); -webkit-background-clip: text; -webkit-text-fill-color: transparent; }
        .pwd-field { position: relative; margin-bottom: 20px; }
        .pwd-field input { width: 100%; background: rgba(14, 7, 33, 0.9); border: 1px solid rgba(139, 92, 246, 0.5); border-radius: 14px; padding: 13px 45px 13px 18px; color: white; outline: none; }
        .eye-btn { position: absolute; right: 15px; top: 50%; transform: translateY(-50%); color: #a78bfa; cursor: pointer; }
        .btn-unlock { width: 100%; padding: 13px; border: none; border-radius: 14px; background: linear-gradient(135deg, #7c3aed, #6366f1); color: white; font-weight: 700; cursor: pointer; }

        /* 💻 DASHBOARD */
        .sidebar { position: fixed; top: 0; left: -270px; width: var(--sidebar); height: 100vh; background: #0f0724; border-right: 1px solid var(--neon); padding-top: 20px; z-index: 1050; transition: 0.3s; overflow-y: auto; }
        .sidebar.active { left: 0; }
        .sidebar-link { padding: 12px 20px; color: #94a3b8; display: flex; align-items: center; gap: 12px; cursor: pointer; text-decoration: none; border-left: 4px solid transparent; }
        .sidebar-link:hover, .sidebar-link.active { background: #1a0b3b; color: #38bdf8; border-left-color: #38bdf8; }
        .main-content { padding: 25px; transition: 0.3s; }
        @media (min-width: 769px) { .sidebar { left: 0; } .main-content { margin-left: var(--sidebar); } }
        .stat-card { background: var(--card); border: 1px solid var(--neon); border-radius: 16px; padding: 20px; border-left: 4px solid #8b5cf6; }
        .card { background: var(--card); border: 1px solid var(--neon); border-radius: 18px; margin-bottom: 20px; }
        .btn-custom { background: linear-gradient(135deg, #7c3aed, #6366f1); color: white; border: none; border-radius: 10px; padding: 10px 18px; font-weight: 600; }
        .form-control, .form-select { background: #0d0622; border: 1px solid var(--neon); color: white; border-radius: 10px; padding: 10px; }
        .form-control:focus { background: #0d0622; color: white; border-color: #a855f7; box-shadow: none; }
        .squircle-icon { width: 42px; height: 42px; border-radius: 12px; background: rgba(139, 92, 246, 0.2); display: flex; align-items: center; justify-content: center; }
    </style>
</head>
<body>
    {% if not session.get('admin_logged') %}
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

    <div class="sidebar" id="sidebar">
        <div class="px-3 pb-3 border-bottom border-secondary d-flex align-items-center gap-2">
            <span class="fs-4">⚡</span>
            <strong class="text-info">{{ bot_info.name }}</strong>
        </div>
        <div class="mt-3">
            <a class="sidebar-link active" onclick="showTab('dashboard', this)"><i class="fas fa-chart-line"></i> Dashboard</a>
            <a class="sidebar-link" onclick="showTab('products', this)"><i class="fas fa-boxes-stacked"></i> Manage Product</a>
            <a class="sidebar-link" onclick="showTab('keys', this)"><i class="fas fa-key"></i> Manage Keys</a>
            <a class="sidebar-link" onclick="showTab('pending_keys', this)"><i class="fas fa-clock"></i> Pending Keys</a>
            <a class="sidebar-link" onclick="showTab('id_stock', this)"><i class="fas fa-id-badge"></i> ID Stock</a>
            <a class="sidebar-link" onclick="showTab('members', this)"><i class="fas fa-users"></i> Members & Wallets</a>
            <a class="sidebar-link" onclick="showTab('resellers', this)"><i class="fas fa-handshake"></i> Resellers</a>
            <a class="sidebar-link" onclick="showTab('broadcast', this)"><i class="fas fa-bullhorn"></i> Broadcast</a>
            <a class="sidebar-link" onclick="showTab('coupons', this)"><i class="fas fa-ticket"></i> Coupon Manager</a>
            <a class="sidebar-link" onclick="showTab('upi', this)"><i class="fas fa-credit-card"></i> UPI Payment Setup</a>
            <a class="sidebar-link" onclick="showTab('topups', this)"><i class="fas fa-wallet"></i> Top-ups Queue</a>
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

        <!-- 📊 1. DASHBOARD -->
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

        <!-- 📦 2. MANAGE PRODUCT -->
        <div class="tab-pane-content" id="tab-products" style="display:none;">
            <div class="card p-4">
                <div class="d-flex justify-content-between align-items-center mb-3">
                    <h5 class="m-0 fw-bold">📦 Manage Products</h5>
                    <button class="btn btn-custom btn-sm" onclick="document.getElementById('addProdBox').style.display='block'"><i class="fas fa-plus me-1"></i> Add Product</button>
                </div>
                <div id="addProdBox" class="p-3 mb-4 card" style="display:none;background:#13082e;">
                    <h6>➕ Add Product</h6>
                    <div class="row g-3 mt-1">
                        <div class="col-md-4"><label class="small">Product Name</label><input type="text" id="ap_name" class="form-control" placeholder="BALA MOD"></div>
                        <div class="col-md-4">
                            <label class="small">Category</label>
                            <select id="ap_cat" class="form-select">
                                <option value="non_root">Non-Root Mobile</option><option value="root">Root Mobile</option>
                                <option value="ios">iOS Panels</option><option value="pc">PC Panels</option><option value="likes">8 Level ID / Likes</option>
                            </select>
                        </div>
                        <div class="col-md-4"><label class="small">Icon / Emoji</label><input type="text" id="ap_icon" class="form-control" placeholder="⚡"></div>
                        <div class="col-md-4"><label class="small">Plan Name</label><input type="text" id="ap_plan" class="form-control" placeholder="10_Days"></div>
                        <div class="col-md-4"><label class="small">Regular Price (₹)</label><input type="number" id="ap_price" class="form-control" placeholder="400"></div>
                        <div class="col-md-4"><label class="small">Reseller Price (₹)</label><input type="number" id="ap_rprice" class="form-control" placeholder="250"></div>
                        <div class="col-12"><label class="small">Download Link</label><input type="text" id="ap_link" class="form-control"></div>
                        <div class="col-12"><button class="btn btn-custom w-100" onclick="saveProduct()">Save Product</button></div>
                    </div>
                </div>
                <div class="row g-3">
                    {% for p in products %}
                    <div class="col-md-6">
                        <div class="card p-3" style="background:#12072c;">
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

        <!-- ⏳ 3. PENDING KEYS -->
        <div class="tab-pane-content" id="tab-pending_keys" style="display:none;">
            <div class="card p-4">
                <h5>⏳ Pending Keys Queue (Out-of-Stock Manual Dispatch)</h5>
                <p class="text-muted small">Orders waiting for keys. Enter key and click send to dispatch directly to customer Telegram.</p>
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
                                        <input type="text" id="key_disp_{{ pk[0] }}" class="form-control form-control-sm" placeholder="Paste Key">
                                        <button class="btn btn-success btn-sm" onclick="dispatchKey({{ pk[0] }}, '{{ pk[1] }}')">Send</button>
                                    </div>
                                </td>
                            </tr>
                            {% else %}
                            <tr><td colspan="5" class="text-center text-muted">No pending keys in queue. All orders fulfilled!</td></tr>
                            {% endfor %}
                        </tbody>
                    </table>
                </div>
            </div>
        </div>

        <!-- 👥 4. MEMBERS & WALLETS (INSPECTOR) -->
        <div class="tab-pane-content" id="tab-members" style="display:none;">
            <div class="card p-4">
                <h5>👥 Members & Wallets Management</h5>
                <div class="table-responsive mt-3">
                    <table class="table table-dark align-middle">
                        <thead><tr><th>Telegram ID</th><th>Name</th><th>Role</th><th>Balance</th><th>Adjust Balance</th></tr></thead>
                        <tbody>
                            {% for u in users_list %}
                            <tr>
                                <td><code>{{ u[0] }}</code></td>
                                <td>{{ u[1] }}</td>
                                <td><span class="badge {{ 'bg-warning text-dark' if u[9]=='Reseller' else 'bg-secondary' }}">{{ u[9] }}</span></td>
                                <td><strong>₹{{ u[5] }}</strong></td>
                                <td>
                                    <div class="d-flex gap-1">
                                        <input type="number" id="bal_adj_{{ u[0] }}" class="form-control form-control-sm" style="width:90px;" placeholder="+50 / -20">
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

        <!-- 📢 5. BROADCAST -->
        <div class="tab-pane-content" id="tab-broadcast" style="display:none;">
            <div class="card p-4">
                <h5>📢 Broadcast Message to All Users</h5>
                <textarea id="bc_msg" class="form-control mt-3" rows="6" placeholder="Type broadcast message (HTML supported)..."></textarea>
                <button class="btn btn-custom mt-3" onclick="sendBroadcast()"><i class="fas fa-paper-plane me-2"></i> Send to All Users</button>
            </div>
        </div>

        <!-- 💳 6. UPI SETTINGS -->
        <div class="tab-pane-content" id="tab-upi" style="display:none;">
            <div class="card p-4">
                <h5>💳 UPI Gateway Setup</h5>
                <div class="row g-3 mt-1">
                    <div class="col-md-6"><label class="small">FamPay UPI ID</label><input type="text" id="u_fam" class="form-control" value="{{ upi.fampay_token }}"></div>
                    <div class="col-md-6"><label class="small">Paytm Token / UPI</label><input type="text" id="u_paytm" class="form-control" value="{{ upi.paytm_token }}"></div>
                    <div class="col-12"><button class="btn btn-custom" onclick="saveUpi()">Save UPI Settings</button></div>
                </div>
            </div>
        </div>

        <!-- ⚙️ 7. STORE SETTINGS -->
        <div class="tab-pane-content" id="tab-store" style="display:none;">
            <div class="card p-4">
                <h5>⚙️ Store Branding & Limits</h5>
                <div class="row g-3 mt-1">
                    <div class="col-md-6"><label class="small">Support Username</label><input type="text" id="st_supp" class="form-control" value="{{ store.support_username }}"></div>
                    <div class="col-md-6"><label class="small">Tutorial Link</label><input type="text" id="st_how" class="form-control" value="{{ store.how_to_use_link }}"></div>
                    <div class="col-12"><button class="btn btn-custom" onclick="saveStore()">Save Settings</button></div>
                </div>
            </div>
        </div>

        <!-- 🛡️ 8. SECURITY -->
        <div class="tab-pane-content" id="tab-security" style="display:none;">
            <div class="card p-4">
                <h5>🛡️ Change Admin Password</h5>
                <div class="row g-3 mt-1">
                    <div class="col-md-6"><input type="password" id="sec_pwd" class="form-control" placeholder="New Password"></div>
                    <div class="col-12"><button class="btn btn-warning" onclick="changePwd()">Update Password</button></div>
                </div>
            </div>
        </div>
    </div>
    {% endif %}

    <script>
        function showTab(t, el) {
            document.querySelectorAll('.tab-pane-content').forEach(d => d.style.display = 'none');
            document.getElementById('tab-' + t).style.display = 'block';
            document.querySelectorAll('.sidebar-link').forEach(l => l.classList.remove('active'));
            if(el) el.classList.add('active');
        }
        function saveProduct() {
            fetch('/api/product/save', {
                method: 'POST', headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({
                    name: document.getElementById('ap_name').value, category: document.getElementById('ap_cat').value,
                    icon: document.getElementById('ap_icon').value, prices: [[document.getElementById('ap_plan').value, parseFloat(document.getElementById('ap_price').value)]],
                    reseller_price: parseFloat(document.getElementById('ap_rprice').value || 0), download_link: document.getElementById('ap_link').value
                })
            }).then(r => r.json()).then(d => { alert(d.message); location.reload(); });
        }
        function deleteProduct(k) {
            if(!confirm('Delete this product?')) return;
            fetch('/api/product/delete', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({prod_key: k}) })
            .then(r => r.json()).then(d => { alert(d.message); location.reload(); });
        }
        function dispatchKey(oid, uid) {
            let key = document.getElementById('key_disp_' + oid).value;
            if(!key) { alert('Please enter key!'); return; }
            fetch('/api/pending/dispatch', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({order_id: oid, user_id: uid, key: key}) })
            .then(r => r.json()).then(d => { alert(d.message); location.reload(); });
        }
        function adjustBalance(uid) {
            let val = parseFloat(document.getElementById('bal_adj_' + uid).value);
            if(isNaN(val)) { alert('Invalid amount!'); return; }
            fetch('/api/user/adjust_balance', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({user_id: uid, delta: val}) })
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
            fetch('/api/security/change_pwd', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({password: document.getElementById('sec_pwd').value}) })
            .then(r => r.json()).then(d => alert(d.message));
        }
    </script>
</body>
</html>
"""

# ==========================================
# 🌐 BACKEND ROUTES
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
    c.execute('SELECT id, user_id, prod_name, plan, key_delivered, amount, timestamp FROM order_history WHERE key_delivered LIKE "PENDING%" ORDER BY id DESC'); pending = c.fetchall()
    c.execute('SELECT user_id, full_name, username, joined_date, orders_count, wallet_balance, total_spent, total_referrals, referral_earnings, account_type FROM users ORDER BY user_id DESC LIMIT 50'); users_list = c.fetchall()
    c.execute('SELECT fampay_token, paytm_token FROM upi_settings WHERE id = 1'); upi_r = c.fetchone() or ("", "")
    c.execute('SELECT support_username, how_to_use_link FROM store_settings WHERE id = 1'); st_r = c.fetchone() or ("@Athulsudin", "")
    conn.close()

    return render_template_string(
        ADMIN_HTML, bot_info=BOT_INFO,
        stats={"users": tot_users, "total_wallet": f"{tot_wallet:,.2f}", "products_count": tot_prods, "orders": tot_orders, "revenue": f"{tot_rev:,.2f}", "keys": tot_keys},
        products=prods, pending_orders=pending, users_list=users_list,
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
    conn.commit(); conn.close()
    return jsonify({"message": "Product saved successfully!"})

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
    
    # Send Key to Customer Telegram Chat
    try:
        msg = f"✅ <b>Payment verified — here's your key!</b>\n⏩ ~~~~~~~~~~~~~~~~~~~~~~~~~~\n\n🗝️ Your Key:\n<code>{key}</code>\n\nThank you for shopping with us!"
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        payload = json.dumps({"chat_id": uid, "text": msg, "parse_mode": "HTML"}).encode()
        urllib.request.urlopen(urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"}), timeout=5)
    except Exception:
        pass
    return jsonify({"message": "Key dispatched directly to customer!"})

@app.route('/api/user/adjust_balance', methods=['POST'])
def api_adj_bal():
    if not session.get('admin_logged'): return jsonify({"message": "Unauthorized"}), 401
    d = request.json
    conn = sqlite3.connect(DB_FILE); c = conn.cursor()
    c.execute('UPDATE users SET wallet_balance = wallet_balance + ? WHERE user_id = ?', (d['delta'], d['user_id']))
    conn.commit(); conn.close()
    return jsonify({"message": "Wallet balance updated!"})

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
            except Exception:
                pass
    Thread(target=_run).start()
    return jsonify({"message": "Broadcast sending in background!"})

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
