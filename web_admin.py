import os
import json
import re
import time
import hashlib
import hmac
import urllib.request
from datetime import timedelta, timezone
from threading import Thread
from flask import Flask, render_template_string, request, jsonify, redirect, session

from database import db, init_db, parse_prices
# Filled in by main.py at start-up (the payment logic lives in main.py).
parse_sms = None
gmail_status = None
gmail_detect = None

# Secrets come from Render Environment Variables (never hard-code them in GitHub).
BOT_TOKEN = "".join(os.environ.get("BOT_TOKEN", "").split())

app = Flask(__name__)
# Stable secret so admin sessions survive restarts / redeploys.
app.secret_key = os.environ.get("SECRET_KEY") or hashlib.sha256(
    ("admin-session:" + BOT_TOKEN).encode()
).hexdigest()
app.permanent_session_lifetime = timedelta(days=30)

# ==========================================
# 🗄️ DATABASE HELPERS (PostgreSQL via database.py)
# ==========================================
def q_all(sql, params=None):
    """Run a SELECT and return rows as tuples (the HTML template reads rows by position)."""
    return [tuple(r.values()) for r in db.fetchall(sql, params)]

def q_one(sql, params=None):
    row = db.fetchone(sql, params)
    return tuple(row.values()) if row else None

def tg_send(chat_id, text):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = json.dumps({"chat_id": chat_id, "text": text, "parse_mode": "HTML"}).encode()
    urllib.request.urlopen(
        urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"}),
        timeout=5,
    )

# Dynamic Bot Avatar & Username Fetcher
BOT_INFO = {"name": "Bot Control Center", "username": "@EliteBot", "avatar": None}
def refresh_bot_meta():
    if not BOT_TOKEN:
        return
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
        
        .gold-box { border: 1px solid rgba(250, 204, 21, 0.4); border-radius: 16px; padding: 20px; background: rgba(22, 13, 44, 0.95); margin-bottom: 20px; }
        .cyan-box { border: 1px solid rgba(56, 189, 248, 0.4); border-radius: 16px; padding: 20px; background: rgba(22, 13, 44, 0.95); margin-bottom: 20px; }
        
        .modal-bg { position: fixed; top: 0; left: 0; width: 100vw; height: 100vh; background: rgba(0, 0, 0, 0.8); backdrop-filter: blur(10px); z-index: 2000; display: none; align-items: center; justify-content: center; padding: 20px; }
        .modal-card { background: #13072e; border: 1px solid #8b5cf6; border-radius: 24px; padding: 30px; width: 100%; max-width: 600px; max-height: 90vh; overflow-y: auto; box-shadow: 0 0 50px rgba(139, 92, 246, 0.4); }
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

    <!-- 🌟 1. CUSTOMER PROFILE INSPECTOR MODAL -->
    <div class="modal-bg" id="userModal">
        <div class="modal-card">
            <div class="d-flex justify-content-between align-items-center mb-3">
                <h5 class="fw-bold m-0 text-info"><i class="fas fa-user-shield me-2"></i>Customer Profile Inspector</h5>
                <button class="btn-close btn-close-white" onclick="document.getElementById('userModal').style.display='none'"></button>
            </div>
            <div class="card p-3 mb-3" style="background:#1a0c3d;">
                <div class="row g-2 small">
                    <div class="col-6"><strong>Name:</strong> <span id="m_name"></span></div>
                    <div class="col-6"><strong>Telegram ID:</strong> <code id="m_id"></code></div>
                    <div class="col-6"><strong>Username:</strong> <span id="m_user"></span></div>
                    <div class="col-6"><strong>Account Role:</strong> <span id="m_role" class="badge bg-warning text-dark"></span></div>
                    <div class="col-12"><strong>Joined:</strong> <span id="m_joined"></span></div>
                </div>
            </div>
            <div class="card p-3 mb-3" style="background:#1a0c3d;">
                <h6 class="text-success fw-bold">💰 Financials & Balance</h6>
                <div class="row g-2 mb-3">
                    <div class="col-6">Current Balance: <h4 class="text-warning m-0">₹<span id="m_bal"></span></h4></div>
                    <div class="col-6">Lifetime Spent: <h5 class="text-muted m-0">₹<span id="m_spent"></span></h5></div>
                </div>
                <div class="d-flex gap-2 mb-2">
                    <button class="btn btn-sm btn-outline-success" onclick="quickAddBal(50)">+₹50</button>
                    <button class="btn btn-sm btn-outline-success" onclick="quickAddBal(100)">+₹100</button>
                    <button class="btn btn-sm btn-outline-success" onclick="quickAddBal(500)">+₹500</button>
                    <button class="btn btn-sm btn-outline-danger" onclick="quickAddBal(-50)">-₹50</button>
                </div>
                <div class="d-flex gap-2">
                    <input type="number" id="m_custom_bal" class="form-control form-control-sm" placeholder="Custom amount (+150 or -50)">
                    <button class="btn btn-sm btn-custom" onclick="applyCustomBal()">Apply</button>
                </div>
            </div>
            <div class="d-flex justify-content-between">
                <a id="m_dm_link" href="#" target="_blank" class="btn btn-primary btn-sm"><i class="fab fa-telegram me-1"></i> Send DM on Telegram</a>
                <button class="btn btn-secondary btn-sm" onclick="document.getElementById('userModal').style.display='none'">Close</button>
            </div>
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

        <!-- 1️⃣ 📊 TAB: DASHBOARD -->
        <div class="tab-pane-content" id="tab-dashboard">
            <div class="card p-4 mb-4" style="background:linear-gradient(135deg,#1f1042,#110729);">
                <small class="text-info fw-bold">⚡ BOT CONTROL CENTER</small>
                <h3 class="fw-bold mt-1">Welcome back.</h3>
                <span class="text-muted">Here's how {{ bot_info.username }} is doing right now.</span>
            </div>
            <div class="row g-3 mb-4">
                <div class="col-6 col-md-3"><div class="stat-card"><h6>MEMBERS</h6><h2>{{ stats.users }}</h2></div></div>
                <div class="col-6 col-md-3"><div class="stat-card" style="border-left-color:#10b981;"><h6>WALLET (ALL)</h6><h2>₹{{ stats.total_wallet }}</h2></div></div>
                <div class="col-6 col-md-3"><div class="stat-card" style="border-left-color:#38bdf8;"><h6>CATALOG ITEMS</h6><h2>{{ stats.products_count }}</h2></div></div>
                <div class="col-6 col-md-3"><div class="stat-card" style="border-left-color:#10b981;"><h6>ORDERS FULFILLED</h6><h2>{{ stats.orders }}</h2></div></div>
                <div class="col-6 col-md-3"><div class="stat-card" style="border-left-color:#facc15;"><h6>LIFETIME REVENUE</h6><h2>₹{{ stats.revenue }}</h2></div></div>
                <div class="col-6 col-md-3"><div class="stat-card" style="border-left-color:#06b6d4;"><h6>KEYS IN STOCK</h6><h2>{{ stats.keys }}</h2></div></div>
            </div>
        </div>

        <!-- 2️⃣ 📦 TAB: MANAGE PRODUCTS -->
        <div class="tab-pane-content" id="tab-products" style="display:none;">
            <div class="card p-4">
                <div class="d-flex justify-content-between align-items-center mb-3">
                    <h5 class="m-0 fw-bold">📦 Manage Products</h5>
                    <button class="btn btn-custom btn-sm" onclick="document.getElementById('addProdBox').style.display='block'"><i class="fas fa-plus me-1"></i> Add Product</button>
                </div>
                <div id="addProdBox" class="p-3 mb-4 card" style="display:none;background:#13082e;">
                    <h6>➕ Add Product Specification</h6>
                    <div class="row g-3 mt-1">
                        <div class="col-md-4"><label class="small text-muted">Product Name</label><input type="text" id="ap_name" class="form-control" placeholder="BALA MOD NON ROOT"></div>
                        <div class="col-md-4">
                            <label class="small text-muted">Category</label>
                            <select id="ap_cat" class="form-select">
                                <option value="non_root">Non-Root Mobile</option><option value="root">Root Mobile</option>
                                <option value="ios">iOS Panels</option><option value="pc">PC Panels</option><option value="likes">8 Level ID / Likes</option>
                            </select>
                        </div>
                        <div class="col-md-4"><label class="small text-muted">Icon / Emoji</label><input type="text" id="ap_icon" class="form-control" placeholder="⚡"></div>
                        <div class="col-md-4"><label class="small text-muted">Plan Name</label><input type="text" id="ap_plan" class="form-control" placeholder="10_Days"></div>
                        <div class="col-md-4"><label class="small text-muted">Regular Price (₹)</label><input type="number" id="ap_price" class="form-control" placeholder="400"></div>
                        <div class="col-md-4"><label class="small text-muted">Reseller Price (₹)</label><input type="number" id="ap_rprice" class="form-control" placeholder="250"></div>
                        <div class="col-12"><label class="small text-muted">Download Link</label><input type="text" id="ap_link" class="form-control"></div>
                        <div class="col-12"><label class="small text-muted">Bulk Keys (One per line)</label><textarea id="ap_keys" class="form-control" rows="3" placeholder="KEY1&#10;KEY2"></textarea></div>
                        <div class="col-12"><button class="btn btn-custom w-100" onclick="saveProduct()">Save Product</button></div>
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

        <!-- 3️⃣ 🗝️ TAB: MANAGE KEYS & SALES ANALYTICS -->
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

        <!-- 4️⃣ ⏳ TAB: PENDING KEYS QUEUE -->
        <div class="tab-pane-content" id="tab-pending_keys" style="display:none;">
            <div class="card p-4">
                <h5>⏳ Pending Keys Queue (Out-of-Stock Manual Dispatch)</h5>
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
                                        <button class="btn btn-success btn-sm" onclick="dispatchKey({{ pk[0] }}, '{{ pk[1] }}')">Send</button>
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

        <!-- 5️⃣ 🆔 TAB: ID STOCK -->
        <div class="tab-pane-content" id="tab-id_stock" style="display:none;">
            <div class="card p-4">
                <h5>🆔 ID Stock (Accounts Management)</h5>
                <div class="row g-3 mt-1">
                    <div class="col-md-4"><label class="small text-muted">Account Type</label>
                        <select id="id_cat" class="form-select">
                            <option value="8 Level ID">8 Level ID (Ranked Ready)</option>
                            <option value="Facebook ID">Facebook ID</option>
                            <option value="Gmail ID">Gmail ID</option>
                            <option value="Instagram ID">Instagram ID</option>
                            <option value="WhatsApp ID">WhatsApp ID</option>
                        </select>
                    </div>
                    <div class="col-md-4"><label class="small text-muted">Price (₹)</label><input type="number" id="id_price" class="form-control" placeholder="45"></div>
                    <div class="col-md-4"><label class="small text-muted">Credentials (Username/Number:Pass)</label><input type="text" id="id_cred" class="form-control" placeholder="9847123456:Pass@123"></div>
                    <div class="col-12"><button class="btn btn-custom" onclick="saveIdStock()"><i class="fas fa-plus me-1"></i> Add Account to Stock</button></div>
                </div>
            </div>
        </div>

        <!-- 6️⃣ 👥 TAB: MEMBERS & WALLETS (INSPECTOR) -->
        <div class="tab-pane-content" id="tab-members" style="display:none;">
            <div class="card p-4">
                <h5>👥 Members & Wallets Management</h5>
                <input type="text" id="memSearch" class="form-control mt-2 mb-3" placeholder="🔍 Search by name, @username, or Telegram ID..." onkeyup="filterTable('memSearch', 'memTable')">
                <div class="table-responsive">
                    <table class="table table-dark align-middle" id="memTable">
                        <thead><tr><th>Telegram ID</th><th>Name</th><th>Role</th><th>Balance</th><th>Orders</th><th>Action</th></tr></thead>
                        <tbody>
                            {% for u in users_list %}
                            <tr>
                                <td><code>{{ u[0] }}</code></td>
                                <td>{{ u[1] }} <small class="text-muted">({{ u[2] }})</small></td>
                                <td><span class="badge {{ 'bg-warning text-dark' if u[9]=='Reseller' else 'bg-secondary' }}">{{ u[9] }}</span></td>
                                <td><strong>₹{{ u[5] }}</strong></td>
                                <td>{{ u[4] }}</td>
                                <td>
                                    <button class="btn btn-sm btn-custom" onclick='openInspector({{ u[0] }}, {{ u[1]|tojson }}, {{ u[2]|tojson }}, {{ u[3]|tojson }}, {{ u[5] }}, {{ u[6] }}, {{ u[9]|tojson }})'><i class="fas fa-eye me-1"></i> Inspect Profile</button>
                                </td>
                            </tr>
                            {% endfor %}
                        </tbody>
                    </table>
                </div>
            </div>
        </div>

        <!-- 7️⃣ 🤝 TAB: RESELLERS -->
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

        <!-- 8️⃣ 📢 TAB: BROADCAST -->
        <div class="tab-pane-content" id="tab-broadcast" style="display:none;">
            <div class="card p-4">
                <h5>📢 Broadcast Announcement to All Users</h5>
                <textarea id="bc_msg" class="form-control mt-3" rows="6" placeholder="Type announcement message (HTML supported)..."></textarea>
                <button class="btn btn-custom mt-3" onclick="sendBroadcast()"><i class="fas fa-paper-plane me-2"></i> Send to All Users</button>
            </div>
        </div>

        <!-- 9️⃣ 🎟️ TAB: COUPON MANAGER -->
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

        <!-- 🔟 💳 TAB: UPI PAYMENT SETUP -->
        <div class="tab-pane-content" id="tab-upi" style="display:none;">
            <div class="cyan-box">
                <h5 class="text-info fw-bold mb-3"><i class="fas fa-qrcode me-2"></i>UPI Payment Setup</h5>
                <p class="small text-muted">Customers scan a QR made from your UPI ID (FamPay, GPay, PhonePe... any UPI ID). Changes apply to the bot <b>instantly</b>.</p>
                <div class="row g-3">
                    <div class="col-md-6">
                        <label class="small text-muted">UPI ID (where you receive the money)</label>
                        <input type="text" id="upi_id" class="form-control" value="{{ pay_cfg.upi_id }}" placeholder="yourname@fam" oninput="updateUpiPreview()">
                    </div>
                    <div class="col-md-6">
                        <label class="small text-muted">Payee / Shop Name</label>
                        <input type="text" id="upi_name" class="form-control" value="{{ pay_cfg.payee_name }}" placeholder="Elite Hackers Store" oninput="updateUpiPreview()">
                    </div>
                    <div class="col-12">
                        <div class="form-check form-switch">
                            <input class="form-check-input" type="checkbox" id="auto_verify" {% if pay_cfg.auto_verify %}checked{% endif %}>
                            <label class="form-check-label fw-bold" for="auto_verify">Automatic payment confirmation</label>
                        </div>
                        <div class="small text-muted mt-1">ON: every customer pays an exact amount (for example &#8377;100.07). The bot reads your FamPay "You received..." e-mail in Gmail every 5 seconds, checks amount, UTR and time, and confirms the payment by itself. The customer can tap "I have paid" once, but does not have to. OFF: you approve each payment in Telegram.</div>
                    </div>
                    <div class="col-12">
                        <button class="btn btn-custom w-100" onclick="saveUpiSettings()"><i class="fas fa-save me-1"></i> Save UPI Settings</button>
                    </div>
                    <div class="col-12 text-center">
                        <div class="small text-muted mb-2">Live QR preview (&#8377;1 test amount)</div>
                        <img id="upi_qr_preview" alt="QR preview" style="width:220px;height:220px;background:#fff;padding:8px;border-radius:12px;display:none;">
                        <div id="upi_qr_empty" class="small text-warning">Enter your UPI ID to see the QR.</div>
                    </div>
                </div>
            </div>

            <div class="gold-box">
                <h5 class="text-warning fw-bold mb-3"><i class="fas fa-envelope me-2"></i>FamPay e-mail connection (Gmail)</h5>
                <ol class="small text-muted ps-3 mb-3">
                    <li>Use the Gmail that receives your FamPay "You received &#8377;..." e-mails.</li>
                    <li>Google Account &rarr; Security &rarr; turn on <b>2-Step Verification</b> &rarr; <b>App passwords</b> &rarr; create one (16 letters).</li>
                    <li>Fill the Gmail address and the App Password below and press <b>Save</b>.</li>
                    <li>Press <b>Connect &amp; detect sender</b>. It finds FamPay's sender domain by itself.</li>
                    <li>Switch on <b>Automatic payment confirmation</b> above, Save, and test with &#8377;10.</li>
                </ol>
                <label class="small text-muted">Gmail address</label>
                <input type="email" id="gm_user" class="form-control mb-2" value="{{ gmail.user }}" placeholder="you@gmail.com" autocomplete="off">
                <label class="small text-muted">App Password {% if gmail.has_password %}(saved, leave empty to keep it){% endif %}</label>
                <input type="password" id="gm_pass" class="form-control mb-2" placeholder="{% if gmail.has_password %}saved{% else %}16-letter App Password{% endif %}" autocomplete="new-password">
                <label class="small text-muted">FamPay sender domain (filled by the detect button)</label>
                <input type="text" id="gm_domain" class="form-control mb-3" value="{{ gmail.domain }}" placeholder="for example famapp.in" autocomplete="off">
                <div class="d-flex gap-2">
                    <button class="btn btn-custom flex-fill" onclick="saveGmail()"><i class="fas fa-save me-1"></i> Save</button>
                    <button class="btn btn-outline-warning flex-fill" onclick="detectGmail()"><i class="fas fa-magnifying-glass me-1"></i> Connect &amp; detect sender</button>
                </div>
                <div class="small mt-3" id="gm_status"><b>Status:</b> {{ gmail.status }}</div>
                <hr>
                <label class="small text-muted">Test an e-mail (nothing is credited)</label>
                <textarea id="test_sms" class="form-control mb-2" rows="3" placeholder="Paste the text of a FamPay e-mail, for example: You received 10.01 in your FamX account ... UTR 632512726024"></textarea>
                <button class="btn btn-custom w-100" onclick="testParse()"><i class="fas fa-vial me-1"></i> Test this message</button>
                <div id="test_result" class="small mt-2 text-info"></div>
            </div>

            <div class="card p-4 mb-4">
                <h5 class="fw-bold mb-3"><i class="fas fa-list me-2"></i>Recent payment e-mails (last 20)</h5>
                <div class="table-responsive">
                    <table class="table table-dark align-middle">
                        <thead><tr><th>Time (IST)</th><th>Amount</th><th>UTR</th><th>Result</th><th>Message</th></tr></thead>
                        <tbody>
                            {% for a in alerts %}
                            <tr>
                                <td>{{ a.time_txt }}</td>
                                <td>{% if a.amount is not none %}&#8377;{{ '%.2f'|format(a.amount) }}{% else %}-{% endif %}</td>
                                <td><code>{{ a.utr or '-' }}</code></td>
                                <td>{{ a.status }}{% if a.order_id %}<br><small><code>{{ a.order_id }}</code></small>{% endif %}</td>
                                <td class="small text-muted">{{ a.snippet }}</td>
                            </tr>
                            {% else %}
                            <tr><td colspan="5" class="text-center text-muted">No payment e-mails read yet.</td></tr>
                            {% endfor %}
                        </tbody>
                    </table>
                </div>
                <div class="small text-muted">matched = credited automatically &middot; unmatched = money arrived but no order has that exact amount (check manually) &middot; ignored = not a "money received" e-mail &middot; logged = read while automatic mode was OFF</div>
            </div>
        </div>

        <!-- 1️⃣1️⃣ 💳 TAB: TOP-UPS QUEUE -->
        <div class="tab-pane-content" id="tab-topups" style="display:none;">
            <div class="card p-4">
                <h5>💳 Review Pending Deposit Requests</h5>
                <div class="table-responsive mt-3">
                    <table class="table table-dark">
                        <thead><tr><th>User ID</th><th>Amount</th><th>UTR</th><th>Action</th></tr></thead>
                        <tbody>
                            <tr><td colspan="4" class="text-center text-muted">No pending deposit requests.</td></tr>
                        </tbody>
                    </table>
                </div>
            </div>
        </div>

        <!-- 1️⃣2️⃣ 🔗 TAB: PRODUCT LINKS -->
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

        <!-- 1️⃣3️⃣ 🛡️ TAB: SECURITY & SESSIONS -->
        <div class="tab-pane-content" id="tab-security" style="display:none;">
            <div class="card p-4">
                <h5>🛡️ Change Master Admin Password</h5>
                <div class="row g-3 mt-1">
                    <div class="col-md-6"><label class="small text-muted">New Password</label><input type="password" id="sec_pwd" class="form-control" placeholder="New Password"></div>
                    <div class="col-12"><button class="btn btn-warning" onclick="changePwd()">Update Password</button></div>
                </div>
            </div>
        </div>

        <!-- 1️⃣4️⃣ ⚙️ TAB: STORE SETTINGS -->
        <div class="tab-pane-content" id="tab-store" style="display:none;">
            <div class="card p-4">
                <h5>⚙️ Store Branding & Limits</h5>
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
        let currentInspectUid = null;
        function openInspector(uid, name, user, joined, bal, spent, role) {
            currentInspectUid = uid;
            document.getElementById('m_id').innerText = uid;
            document.getElementById('m_name').innerText = name;
            document.getElementById('m_user').innerText = user;
            document.getElementById('m_joined').innerText = joined;
            document.getElementById('m_bal').innerText = parseFloat(bal).toFixed(2);
            document.getElementById('m_spent').innerText = parseFloat(spent).toFixed(2);
            document.getElementById('m_role').innerText = role;
            document.getElementById('m_dm_link').href = 'tg://user?id=' + uid;
            document.getElementById('userModal').style.display = 'flex';
        }
        function quickAddBal(delta) {
            if(!currentInspectUid) return;
            fetch('/api/user/adjust_balance', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({user_id: currentInspectUid, delta: delta}) })
            .then(r => r.json()).then(d => { alert(d.message); location.reload(); });
        }
        function applyCustomBal() {
            let val = parseFloat(document.getElementById('m_custom_bal').value);
            if(isNaN(val)) return alert('Enter valid number');
            quickAddBal(val);
        }
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
            if(!name || !plan || !price) return alert('Fill required fields!');
            fetch('/api/product/save', {
                method: 'POST', headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({name: name, category: cat, icon: icon, prices: [[plan, parseFloat(price)]], reseller_price: parseFloat(rprice||price), download_link: link, keys: keys})
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
        function updateUpiPreview() {
            let up = document.getElementById('upi_id');
            if (!up) return;
            let upi = up.value.trim();
            let name = document.getElementById('upi_name').value.trim() || 'Shop';
            let img = document.getElementById('upi_qr_preview');
            let empty = document.getElementById('upi_qr_empty');
            if (!upi) { img.style.display = 'none'; empty.style.display = 'block'; return; }
            let uri = 'upi://pay?pa=' + upi + '&pn=' + encodeURIComponent(name) + '&am=1.00&cu=INR&tn=TEST';
            img.src = 'https://api.qrserver.com/v1/create-qr-code/?size=300x300&data=' + encodeURIComponent(uri);
            img.style.display = 'inline-block';
            empty.style.display = 'none';
        }
        function saveUpiSettings() {
            let upi = document.getElementById('upi_id').value.trim();
            let name = document.getElementById('upi_name').value.trim();
            let auto = document.getElementById('auto_verify').checked;
            fetch('/api/payment/save', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({upi_id: upi, payee_name: name, auto_verify: auto}) })
            .then(r => r.json()).then(d => alert(d.message));
        }
        function saveGmail() {
            let body = {gmail_user: document.getElementById('gm_user').value.trim(),
                        app_password: document.getElementById('gm_pass').value,
                        bank_domain: document.getElementById('gm_domain').value.trim()};
            return fetch('/api/gmail/save', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body) })
            .then(r => r.json()).then(d => { alert(d.message); document.getElementById('gm_pass').value = ''; return d; });
        }
        function detectGmail() {
            let st = document.getElementById('gm_status');
            st.innerText = 'Connecting to Gmail...';
            let body = {gmail_user: document.getElementById('gm_user').value.trim(),
                        app_password: document.getElementById('gm_pass').value,
                        bank_domain: document.getElementById('gm_domain').value.trim()};
            fetch('/api/gmail/save', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body) })
            .then(r => r.json()).then(d => {
                if (d.error) { st.innerText = d.message; return; }
                return fetch('/api/gmail/detect', { method: 'POST' }).then(r => r.json()).then(x => {
                    st.innerText = x.message;
                    if (x.ok && x.domain) {
                        document.getElementById('gm_domain').value = x.domain;
                        fetch('/api/gmail/save', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({gmail_user: body.gmail_user, app_password: '', bank_domain: x.domain}) })
                        .then(r => r.json()).then(y => { st.innerText = x.message + '. Sender domain saved: ' + x.domain; });
                    }
                });
            });
        }
        function testParse() {
            let t = document.getElementById('test_sms').value;
            fetch('/api/payment/test_parse', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({text: t}) })
            .then(r => r.json()).then(d => { document.getElementById('test_result').innerText = d.message; });
        }
        updateUpiPreview();
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
        function toggleResellerRole(uid, role) {
            fetch('/api/user/set_role', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({user_id: uid, role: role}) })
            .then(r => r.json()).then(d => { alert(d.message); location.reload(); });
        }
        function sendBroadcast() {
            let msg = document.getElementById('bc_msg').value;
            if(!msg) return alert('Enter message!');
            fetch('/api/broadcast/send', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({message: msg}) })
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
# 🌐 BACKEND API ENDPOINTS
# ==========================================
def admin_required():
    return bool(session.get('admin_logged'))

def unauthorized():
    return jsonify({"message": "Unauthorized"}), 401

@app.route('/health')
def health():
    # Lightweight endpoint for uptime monitors (keeps the free Render instance awake).
    return "ok", 200

@app.route('/login', methods=['POST'])
def login():
    if db.check_admin_password(request.form.get('password', '').strip()):
        session.permanent = True
        session['admin_logged'] = True
        return redirect('/')
    time.sleep(1)  # slow down password guessing
    return render_template_string(ADMIN_HTML, bot_info=BOT_INFO, error="Invalid Admin Password!")

@app.route('/logout')
def logout():
    session.clear()
    return redirect('/')

@app.route('/')
def dashboard():
    if not admin_required():
        return render_template_string(ADMIN_HTML, bot_info=BOT_INFO)

    r_u = q_one('SELECT COUNT(*), COALESCE(SUM(wallet_balance), 0) FROM users')
    tot_users, tot_wallet = r_u[0] or 0, float(r_u[1] or 0)
    r_o = q_one('SELECT COUNT(*), COALESCE(SUM(amount), 0) FROM order_history')
    tot_orders, tot_rev = r_o[0] or 0, float(r_o[1] or 0)
    tot_prods = q_one('SELECT COUNT(*) FROM products')[0]
    tot_keys = q_one('SELECT COUNT(*) FROM keys_inventory WHERE is_used = 0')[0]

    prods = [
        {"prod_key": p[0], "name": p[1], "category": p[2], "prices": parse_prices(p[3]),
         "icon": p[4] or "⚡", "download_link": p[5]}
        for p in q_all('SELECT prod_key, name, category, prices, icon, download_link FROM products ORDER BY name')
    ]
    all_orders = q_all('SELECT id, user_id, prod_name, plan, key_delivered, amount, utr, timestamp FROM order_history ORDER BY id DESC LIMIT 50')
    pending = q_all("SELECT id, user_id, prod_name, plan, key_delivered, amount, timestamp FROM order_history WHERE LEFT(key_delivered, 7) = 'PENDING' ORDER BY id DESC")
    users_list = q_all('SELECT user_id, full_name, username, joined_date, orders_count, wallet_balance, total_spent, total_referrals, referral_earnings, account_type FROM users ORDER BY user_id DESC LIMIT 50')
    st_r = q_one('SELECT support_username, how_to_use_link FROM store_settings WHERE id = 1') or ("@Athulsudin", "")
    pay = db.get_payment_settings()
    base = (os.environ.get("RENDER_EXTERNAL_URL") or request.host_url).rstrip("/")
    if base.startswith("http://") and "localhost" not in base and "127.0.0.1" not in base:
        base = "https://" + base[len("http://"):]
    pay_cfg = {
        "upi_id": pay["upi_id"], "payee_name": pay["payee_name"], "auto_verify": bool(pay["auto_verify"]),
    }
    gm = db.get_gmail_config()
    gm_state = (gmail_status() if gmail_status else None) or {}
    gmail_cfg = {
        "user": gm.get("gmail_user") or "", "has_password": bool(gm.get("gmail_app_password")),
        "domain": gm.get("bank_domain") or "",
        "status": (gm_state.get("detail") or "Not connected.")
                  + (f" Last check: {gm_state['last_check']}." if gm_state.get("last_check") else "")
                  + (f" Last e-mail: {gm_state['last_mail']}." if gm_state.get("last_mail") else ""),
    }
    ist = timezone(timedelta(hours=5, minutes=30))
    alerts = [
        {
            "time_txt": r["received_at"].astimezone(ist).strftime("%d %b, %I:%M %p") if r["received_at"] else "",
            "amount": r["amount"], "utr": r["utr"], "status": r["status"], "order_id": r["order_id"],
            "snippet": (r["raw_text"] or "")[:90],
        }
        for r in db.recent_bank_credits(20)
    ]

    return render_template_string(
        ADMIN_HTML, bot_info=BOT_INFO,
        stats={"users": tot_users, "total_wallet": f"{tot_wallet:,.2f}", "products_count": tot_prods, "orders": tot_orders, "revenue": f"{tot_rev:,.2f}", "keys": tot_keys},
        products=prods, all_orders=all_orders, pending_orders=pending, users_list=users_list,
        pay_cfg=pay_cfg, gmail=gmail_cfg, alerts=alerts, store={"support_username": st_r[0], "how_to_use_link": st_r[1]}
    )

UPI_RE = re.compile(r'^[a-zA-Z0-9.\-_]{2,64}@[a-zA-Z0-9]{2,32}$')

@app.route('/api/payment/save', methods=['POST'])
def api_payment_save():
    if not admin_required(): return unauthorized()
    d = request.json or {}
    upi_id = (d.get('upi_id') or '').strip()
    payee = re.sub(r'[^A-Za-z0-9 .&\-]', '', (d.get('payee_name') or '').strip())[:40].strip() or 'Elite Hackers Store'
    if not UPI_RE.match(upi_id):
        return jsonify({"message": "Invalid UPI ID! Example: yourname@okhdfcbank"}), 400
    auto = bool(d.get('auto_verify'))
    db.set_payment_settings(upi_id, payee, auto)
    mode = "AUTOMATIC verification is ON." if auto else "Manual approval mode (you approve in Telegram)."
    return jsonify({"message": f"UPI settings saved! New payments use this QR immediately. {mode}"})

@app.route('/api/payment/test_parse', methods=['POST'])
def api_test_parse():
    if not admin_required(): return unauthorized()
    parsed = parse_sms((request.json or {}).get('text', ''))
    if not parsed['is_credit']:
        msg = "❌ Not detected as a 'money received' message."
    elif parsed['amount'] is None:
        msg = "⚠️ Looks like a credit, but no amount was found."
    else:
        match = db.find_open_payment_by_amount(parsed['amount'])
        msg = f"✅ Credit detected: ₹{parsed['amount']:,.2f}"
        msg += f" · UTR {parsed['utr']}" if parsed['utr'] else " · no UTR found (fine for UPI app notifications)"
        msg += " · matches an open order" if match else " · no open order has this exact amount (normal for a test)"
    return jsonify({"message": msg})

GMAIL_RE = re.compile(r'^[A-Za-z0-9._%+\-]+@(gmail\.com|googlemail\.com)$', re.I)
DOMAIN_RE = re.compile(r'^[a-z0-9\-]+(\.[a-z0-9\-]+)+$')

@app.route('/api/gmail/save', methods=['POST'])
def api_gmail_save():
    if not admin_required(): return unauthorized()
    d = request.json or {}
    user = (d.get('gmail_user') or '').strip()
    password = "".join((d.get('app_password') or '').split()) or None
    domain = (d.get('bank_domain') or '').strip().lower().lstrip('@*. ')
    if user and not GMAIL_RE.match(user):
        return jsonify({"message": "Enter a Gmail address like you@gmail.com", "error": True}), 400
    if domain and not DOMAIN_RE.match(domain):
        return jsonify({"message": "The sender domain must look like famapp.in (a full domain).", "error": True}), 400
    db.set_gmail_config(user, password, domain)
    return jsonify({"message": "Saved! The bot reconnects to Gmail within a few seconds."})

@app.route('/api/gmail/detect', methods=['POST'])
def api_gmail_detect():
    if not admin_required(): return unauthorized()
    if not gmail_detect:
        return jsonify({"ok": False, "message": "The payment reader is still starting. Try again in a moment."})
    return jsonify(gmail_detect())

@app.route('/api/product/save', methods=['POST'])
def api_save_p():
    if not admin_required(): return unauthorized()
    d = request.json or {}
    name = (d.get('name') or '').strip()
    prices = parse_prices(d.get('prices'))
    k = re.sub(r'[^a-zA-Z0-9]', '_', name).strip('_').lower()[:32]
    if not name or not k or not prices:
        return jsonify({"message": "Product name and at least one plan are required!"}), 400

    key_lines = [x.strip() for x in (d.get('keys', '') or '').split('\n') if x.strip()]
    with db.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO products (prod_key, name, category, prices, download_link, icon, maintenance, stock_out)
                VALUES (%s, %s, %s, %s, %s, %s, 0, 0)
                ON CONFLICT (prod_key) DO UPDATE SET
                    name = EXCLUDED.name,
                    category = EXCLUDED.category,
                    prices = EXCLUDED.prices,
                    download_link = EXCLUDED.download_link,
                    icon = EXCLUDED.icon,
                    maintenance = 0,
                    stock_out = 0
                """,
                (k, name, d.get('category', 'non_root'), json.dumps(prices),
                 d.get('download_link', ''), d.get('icon') or '⚡')
            )
            if key_lines:
                cur.executemany(
                    'INSERT INTO keys_inventory (prod_key, plan, item_key, is_used) VALUES (%s, %s, %s, 0)',
                    [(k, prices[0][0], item) for item in key_lines]
                )
    return jsonify({"message": "Product and keys saved successfully!"})

@app.route('/api/product/delete', methods=['POST'])
def api_del_p():
    if not admin_required(): return unauthorized()
    db.execute('DELETE FROM products WHERE prod_key = %s', ((request.json or {}).get('prod_key'),))
    return jsonify({"message": "Product deleted!"})

@app.route('/api/pending/dispatch', methods=['POST'])
def api_dispatch():
    if not admin_required(): return unauthorized()
    d = request.json or {}
    key = (d.get('key') or '').strip()
    if not key:
        return jsonify({"message": "Enter a key!"}), 400
    try:
        order_id = int(d['order_id'])
    except (KeyError, TypeError, ValueError):
        return jsonify({"message": "Invalid order!"}), 400

    # Only orders that are still pending can be dispatched; the customer is taken from the order itself.
    row = db.fetchone(
        """
        UPDATE order_history SET key_delivered = %s
        WHERE id = %s AND LEFT(key_delivered, 7) = 'PENDING'
        RETURNING user_id, prod_name, plan
        """,
        (key, order_id)
    )
    if not row:
        return jsonify({"message": "This order is not pending (already delivered?)."}), 400

    sent = True
    try:
        safe_key = key.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        tg_send(
            row["user_id"],
            "✅ <b>Payment verified — here's your key!</b>\n⏩ ~~~~~~~~~~~~~~~~~~~~~~~~~~\n\n"
            f"🗝️ Your Key:\n<code>{safe_key}</code>\n\nThank you for shopping with us!"
        )
    except Exception:
        sent = False
    msg = "Key dispatched directly to customer!" if sent else "Key saved, but Telegram message failed (user may have blocked the bot)."
    return jsonify({"message": msg})

@app.route('/api/user/adjust_balance', methods=['POST'])
def api_adj_bal():
    if not admin_required(): return unauthorized()
    d = request.json or {}
    try:
        user_id, delta = int(d['user_id']), float(d['delta'])
    except (KeyError, TypeError, ValueError):
        return jsonify({"message": "Invalid values!"}), 400
    db.execute('UPDATE users SET wallet_balance = GREATEST(0, wallet_balance + %s) WHERE user_id = %s', (delta, user_id))
    return jsonify({"message": "Wallet balance updated!"})

@app.route('/api/user/set_role', methods=['POST'])
def api_set_role():
    if not admin_required(): return unauthorized()
    d = request.json or {}
    try:
        db.set_account_type(int(d['user_id']), d['role'])
    except (KeyError, TypeError, ValueError):
        return jsonify({"message": "Invalid role or user!"}), 400
    return jsonify({"message": f"Role updated to {d['role']}!"})

@app.route('/api/id_stock/add', methods=['POST'])
def api_id_stock():
    if not admin_required(): return unauthorized()
    d = request.json or {}
    try:
        price = float(d['price'])
    except (KeyError, TypeError, ValueError):
        return jsonify({"message": "Invalid price!"}), 400
    db.execute(
        'INSERT INTO id_accounts (category_name, account_data, price, is_sold) VALUES (%s, %s, %s, 0)',
        (d.get('category', ''), d.get('credentials', ''), price)
    )
    return jsonify({"message": "Account added to ID Stock!"})

@app.route('/api/coupon/save', methods=['POST'])
def api_coupon():
    if not admin_required(): return unauthorized()
    d = request.json or {}
    code = (d.get('code') or '').strip().upper()
    try:
        val, limit = float(d['val']), int(d.get('limit') or 100)
    except (KeyError, TypeError, ValueError):
        return jsonify({"message": "Invalid values!"}), 400
    if not code:
        return jsonify({"message": "Enter a promo code!"}), 400
    db.execute(
        """
        INSERT INTO coupons (code, discount_type, discount_val, usage_limit, used_count, is_active)
        VALUES (%s, 'flat', %s, %s, 0, 1)
        ON CONFLICT (code) DO UPDATE SET
            discount_type = 'flat',
            discount_val = EXCLUDED.discount_val,
            usage_limit = EXCLUDED.usage_limit,
            used_count = 0,
            is_active = 1
        """,
        (code, val, limit)
    )
    return jsonify({"message": "Promo code activated!"})

@app.route('/api/broadcast/send', methods=['POST'])
def api_bc():
    if not admin_required(): return unauthorized()
    msg = (request.json or {}).get('message', '')
    if not msg.strip():
        return jsonify({"message": "Enter a message!"}), 400

    def _run():
        try:
            uids = [r[0] for r in q_all('SELECT user_id FROM users')]
        except Exception:
            return
        for u in uids:
            try:
                tg_send(u, msg)
            except Exception:
                pass
            time.sleep(0.05)  # stay under Telegram's broadcast rate limit
    Thread(target=_run, daemon=True).start()
    return jsonify({"message": "Broadcast started! Messages are being sent to all users."})

@app.route('/api/store/save', methods=['POST'])
def api_save_st():
    if not admin_required(): return unauthorized()
    d = request.json or {}
    db.execute(
        'UPDATE store_settings SET support_username = %s, how_to_use_link = %s WHERE id = 1',
        (d.get('support_username', ''), d.get('how_to_use_link', ''))
    )
    return jsonify({"message": "Store settings saved!"})

@app.route('/api/security/change_pwd', methods=['POST'])
def api_pwd():
    if not admin_required(): return unauthorized()
    if os.environ.get('ADMIN_PASSWORD', '').strip():
        return jsonify({"message": "Password is managed by ADMIN_PASSWORD in Render Environment. Change it there and redeploy."}), 400
    new_pwd = ((request.json or {}).get('password') or '').strip()
    if len(new_pwd) < 6:
        return jsonify({"message": "Password must be at least 6 characters!"}), 400
    db.set_admin_password(new_pwd)
    return jsonify({"message": "Password updated successfully!"})

def run_web():
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)

if __name__ == "__main__":
    init_db()
    run_web()

