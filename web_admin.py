import os
import json
import sqlite3
import logging
from threading import Thread
import asyncio
from flask import Flask, render_template_string, request, jsonify, redirect, url_for, session
from telegram import Application

# Logging setup
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("WebAdmin")

# ==========================================
# ⚙️ CONFIGURATION
# ==========================================
DB_FILE = "bot_database.db"
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "athulsudin1234")  # നിങ്ങളുടെ വെബ് പാസ്‌വേർഡ്
BOT_TOKEN = "8892856619:AAGZhdOv389_AaKvbcbInlJAiDMOwQxOeHc"

app = Flask(__name__)
app.secret_key = os.urandom(24)

# ==========================================
# 🎨 VIDEO THEME HTML TEMPLATE (DARK SLATE & INDIGO ACCENTS)
# ==========================================
ADMIN_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Elite Hackers - Bot Control Panel</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
    <link href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css" rel="stylesheet">
    <style>
        :root {
            --bg-main: #0f172a;
            --bg-card: #1e293b;
            --border-color: #334155;
            --accent-primary: #6366f1;
            --accent-hover: #4f46e5;
            --text-main: #f8fafc;
            --text-muted: #94a3b8;
            --sidebar-w: 260px;
        }
        body { background-color: var(--bg-main); color: var(--text-main); font-family: 'Inter', system-ui, -apple-system, sans-serif; overflow-x: hidden; margin: 0; }
        .card { background-color: var(--bg-card); border: 1px solid var(--border-color); color: var(--text-main); border-radius: 12px; margin-bottom: 20px; box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.2); }
        .btn-custom { background-color: var(--accent-primary); color: white; border: none; font-weight: 500; border-radius: 8px; padding: 10px 18px; }
        .btn-custom:hover { background-color: var(--accent-hover); color: white; }
        .form-control, .form-select { background-color: #0b1120; border: 1px solid var(--border-color); color: white; border-radius: 8px; padding: 10px; }
        .form-control:focus, .form-select:focus { background-color: #0b1120; color: white; border-color: var(--accent-primary); box-shadow: 0 0 0 0.2rem rgba(99, 102, 241, 0.25); }
        
        /* Sidebar styling */
        .sidebar { position: fixed; top: 0; left: 0; width: var(--sidebar-w); height: 100vh; background: var(--bg-card); border-right: 1px solid var(--border-color); z-index: 1050; padding-top: 20px; overflow-y: auto; transition: 0.3s; }
        .sidebar-brand { padding: 0 20px 20px; font-weight: 700; font-size: 1.1rem; color: #38bdf8; display: flex; align-items: center; gap: 10px; border-bottom: 1px solid var(--border-color); }
        .sidebar-link { padding: 12px 20px; color: var(--text-muted); display: flex; align-items: center; gap: 12px; font-weight: 500; cursor: pointer; text-decoration: none; border-left: 4px solid transparent; transition: 0.2s; }
        .sidebar-link:hover, .sidebar-link.active { background: var(--bg-main); color: #38bdf8; border-left-color: #38bdf8; }
        
        .main-content { margin-left: var(--sidebar-w); padding: 25px; min-height: 100vh; }
        @media (max-width: 768px) {
            .sidebar { left: -260px; }
            .sidebar.active { left: 0; }
            .main-content { margin-left: 0; }
        }
        .stat-card { background: var(--bg-card); border: 1px solid var(--border-color); border-radius: 12px; padding: 20px; }
        .stat-card h6 { font-size: 0.8rem; letter-spacing: 0.5px; color: var(--text-muted); font-weight: 600; }
        .stat-card h2 { font-size: 1.8rem; font-weight: 700; margin: 5px 0 0; }
    </style>
</head>
<body>

    {% if not session.get('admin_logged') %}
    <!-- 🔒 LOGIN GATEWAY -->
    <div class="d-flex align-items-center justify-content-center" style="min-height: 100vh;">
        <div class="col-11 col-sm-8 col-md-5 col-lg-4">
            <div class="card p-4 text-center">
                <div class="mb-3">
                    <i class="fas fa-shield-halved fa-3x text-primary"></i>
                </div>
                <h4 class="fw-bold mb-3">ADMIN CONTROL UNLOCK</h4>
                <p class="text-muted small">Enter your master password to access dashboard</p>
                {% if error %}
                    <div class="alert alert-danger py-2 small">{{ error }}</div>
                {% endif %}
                <form method="POST" action="/login">
                    <div class="mb-3">
                        <input type="password" name="password" class="form-control text-center" placeholder="••••••••••••" required>
                    </div>
                    <button type="submit" class="btn btn-custom w-100"><i class="fas fa-lock-open me-2"></i>Unlock Dashboard</button>
                </form>
            </div>
        </div>
    </div>
    {% else %}

    <!-- 🌟 SIDEBAR MENU -->
    <div class="sidebar" id="sidebar">
        <div class="sidebar-brand">
            <i class="fas fa-bolt text-warning"></i>
            <span>ELITE HACKERS PANEL</span>
        </div>
        <div class="mt-3">
            <a class="sidebar-link active" onclick="showTab('dashboard', this)"><i class="fas fa-chart-pie"></i> Dashboard</a>
            <a class="sidebar-link" onclick="showTab('products', this)"><i class="fas fa-box-open"></i> Manage Products</a>
            <a class="sidebar-link" onclick="showTab('stock', this)"><i class="fas fa-key"></i> Keys / ID Stock</a>
            <a class="sidebar-link" onclick="showTab('broadcast', this)"><i class="fas fa-bullhorn"></i> Broadcast</a>
            <a class="sidebar-link" onclick="showTab('upi', this)"><i class="fas fa-credit-card"></i> UPI Payment Setup</a>
            <a class="sidebar-link" onclick="showTab('api', this)"><i class="fas fa-plug"></i> Key Delivery API</a>
            <a class="sidebar-link" onclick="showTab('store', this)"><i class="fas fa-sliders"></i> Store Settings</a>
            <a href="/logout" class="sidebar-link text-danger mt-4"><i class="fas fa-right-from-bracket"></i> Logout</a>
        </div>
    </div>

    <!-- 💻 MAIN CONTENT -->
    <div class="main-content">
        <!-- TOPBAR -->
        <div class="d-flex justify-content-between align-items-center mb-4">
            <div class="d-flex align-items-center gap-3">
                <button class="btn btn-dark d-md-none" onclick="toggleSidebar()"><i class="fas fa-bars"></i></button>
                <h4 class="fw-bold m-0">⚙️ Control Center</h4>
            </div>
            <div class="d-flex align-items-center gap-2">
                <span class="badge bg-success py-2 px-3"><i class="fas fa-circle-check me-1"></i> Bot Online</span>
                <a href="/logout" class="btn btn-outline-danger btn-sm"><i class="fas fa-lock"></i></a>
            </div>
        </div>

        <!-- 📊 TAB 1: DASHBOARD -->
        <div class="tab-pane-content" id="tab-dashboard">
            <div class="row g-3 mb-4">
                <div class="col-md-3">
                    <div class="stat-card">
                        <h6>TOTAL USERS</h6>
                        <h2 class="text-primary">{{ stats.users }}</h2>
                    </div>
                </div>
                <div class="col-md-3">
                    <div class="stat-card">
                        <h6>TOTAL ORDERS</h6>
                        <h2 class="text-success">{{ stats.orders }}</h2>
                    </div>
                </div>
                <div class="col-md-3">
                    <div class="stat-card">
                        <h6>TOTAL REVENUE</h6>
                        <h2 class="text-warning">₹{{ stats.revenue }}</h2>
                    </div>
                </div>
                <div class="col-md-3">
                    <div class="stat-card">
                        <h6>KEYS IN STOCK</h6>
                        <h2 class="text-info">{{ stats.keys }}</h2>
                    </div>
                </div>
            </div>

            <div class="card p-4">
                <h5 class="fw-bold mb-3"><i class="fas fa-chart-line text-info me-2"></i>System Overview</h5>
                <p class="text-muted">Use the left sidebar options to easily add/edit products, restock keys, send broadcasts, and update UPI configs live without restarting the bot.</p>
            </div>
        </div>

        <!-- 📦 TAB 2: MANAGE PRODUCTS -->
        <div class="tab-pane-content" id="tab-products" style="display: none;">
            <div class="card p-4">
                <h5 class="fw-bold mb-3"><i class="fas fa-plus-circle text-primary me-2"></i>Add / Update Product</h5>
                <div class="row g-3">
                    <div class="col-md-4">
                        <label class="form-label small text-muted">Product Name</label>
                        <input type="text" id="p_name" class="form-control" placeholder="e.g. BALA MOD NON ROOT">
                    </div>
                    <div class="col-md-4">
                        <label class="form-label small text-muted">Category</label>
                        <select id="p_cat" class="form-select">
                            <option value="non_root">Non-Root Panel</option>
                            <option value="root">Root Panel</option>
                            <option value="ios">iOS Panel</option>
                            <option value="pc">PC Panel</option>
                            <option value="likes">Like Service</option>
                        </select>
                    </div>
                    <div class="col-md-4">
                        <label class="form-label small text-muted">Emoji / Icon</label>
                        <input type="text" id="p_icon" class="form-control" placeholder="⚡ or 🍏">
                    </div>
                    <div class="col-md-4">
                        <label class="form-label small text-muted">Plan Name</label>
                        <input type="text" id="p_plan" class="form-control" placeholder="1_Day">
                    </div>
                    <div class="col-md-4">
                        <label class="form-label small text-muted">Price (₹)</label>
                        <input type="number" id="p_price" class="form-control" placeholder="120">
                    </div>
                    <div class="col-md-4">
                        <label class="form-label small text-muted">Download / Channel Link</label>
                        <input type="text" id="p_link" class="form-control" placeholder="https://t.me/...">
                    </div>
                    <div class="col-12">
                        <button onclick="saveProduct()" class="btn btn-custom w-100"><i class="fas fa-floppy-disk me-2"></i>Save Product to Database</button>
                    </div>
                </div>
            </div>

            <!-- Products List Table -->
            <div class="card p-4">
                <h5 class="fw-bold mb-3"><i class="fas fa-list text-info me-2"></i>Existing Products</h5>
                <div class="table-responsive">
                    <table class="table table-dark table-hover align-middle">
                        <thead>
                            <tr class="text-secondary">
                                <th>Icon</th>
                                <th>Name</th>
                                <th>Category</th>
                                <th>Plans & Prices</th>
                                <th>Maintenance</th>
                                <th>Stock Status</th>
                                <th>Action</th>
                            </tr>
                        </thead>
                        <tbody>
                            {% for p in products %}
                            <tr>
                                <td><span style="font-size: 1.4rem;">{{ p.icon }}</span></td>
                                <td><strong>{{ p.name }}</strong></td>
                                <td><span class="badge bg-secondary">{{ p.category }}</span></td>
                                <td>{{ p.prices }}</td>
                                <td>
                                    <button onclick="toggleMaint('{{ p.prod_key }}', {{ 0 if p.maintenance else 1 }})" class="btn btn-sm {{ 'btn-warning' if p.maintenance else 'btn-outline-secondary' }}">
                                        {{ '🛠️ Active' if p.maintenance else 'Off' }}
                                    </button>
                                </td>
                                <td>
                                    <button onclick="toggleStock('{{ p.prod_key }}', {{ 0 if p.stock_out else 1 }})" class="btn btn-sm {{ 'btn-danger' if p.stock_out else 'btn-success' }}">
                                        {{ '❌ Out' if p.stock_out else '✅ In Stock' }}
                                    </button>
                                </td>
                                <td>
                                    <button onclick="deleteProduct('{{ p.prod_key }}')" class="btn btn-outline-danger btn-sm"><i class="fas fa-trash"></i></button>
                                </td>
                            </tr>
                            {% endfor %}
                        </tbody>
                    </table>
                </div>
            </div>
        </div>

        <!-- 🔑 TAB 3: STOCK & KEYS -->
        <div class="tab-pane-content" id="tab-stock" style="display: none;">
            <div class="card p-4">
                <h5 class="fw-bold mb-3"><i class="fas fa-key text-warning me-2"></i>Add Serial Keys (Auto sequential delivery)</h5>
                <div class="row g-3">
                    <div class="col-md-6">
                        <label class="form-label small text-muted">Product Key Identifier</label>
                        <input type="text" id="k_prod" class="form-control" placeholder="e.g. bala_mod">
                    </div>
                    <div class="col-md-6">
                        <label class="form-label small text-muted">Plan Name</label>
                        <input type="text" id="k_plan" class="form-control" placeholder="e.g. 1_Day">
                    </div>
                    <div class="col-12">
                        <label class="form-label small text-muted">Paste Keys (One Key Per Line)</label>
                        <textarea id="k_keys" class="form-control" rows="6" placeholder="KEY-1234&#10;KEY-5678&#10;KEY-9012"></textarea>
                    </div>
                    <div class="col-12">
                        <button onclick="uploadKeys()" class="btn btn-custom"><i class="fas fa-cloud-arrow-up me-2"></i>Upload Keys to Stock</button>
                    </div>
                </div>
            </div>
        </div>

        <!-- 📢 TAB 4: BROADCAST -->
        <div class="tab-pane-content" id="tab-broadcast" style="display: none;">
            <div class="card p-4">
                <h5 class="fw-bold mb-3"><i class="fas fa-bullhorn text-danger me-2"></i>Broadcast Message to All Telegram Users</h5>
                <div class="row g-3">
                    <div class="col-12">
                        <label class="form-label small text-muted">Message (HTML Tags supported like &lt;b&gt;, &lt;i&gt;, &lt;code&gt;)</label>
                        <textarea id="bc_text" class="form-control" rows="6" placeholder="🚀 Big update available now!"></textarea>
                    </div>
                    <div class="col-12">
                        <button onclick="sendBroadcast()" class="btn btn-danger"><i class="fas fa-paper-plane me-2"></i>Send Broadcast Now</button>
                    </div>
                </div>
            </div>
        </div>

        <!-- 💳 TAB 5: UPI SETUP -->
        <div class="tab-pane-content" id="tab-upi" style="display: none;">
            <div class="card p-4">
                <h5 class="fw-bold mb-3"><i class="fas fa-wallet text-success me-2"></i>UPI Gateway Settings</h5>
                <div class="row g-3">
                    <div class="col-md-6">
                        <label class="form-label small text-muted">FamPay UPI ID</label>
                        <input type="text" id="u_fampay_upi" class="form-control" value="{{ upi.fampay_token }}" placeholder="9544113089@fam">
                    </div>
                    <div class="col-md-6">
                        <label class="form-label small text-muted">Custom FamPay QR Image URL</label>
                        <input type="text" id="u_fampay_qr" class="form-control" value="{{ upi.fampay_qr }}" placeholder="https://i.imgur.com/...">
                    </div>
                    <div class="col-md-6">
                        <label class="form-label small text-muted">Paytm API Token</label>
                        <input type="text" id="u_paytm_token" class="form-control" value="{{ upi.paytm_token }}" placeholder="Paytm Token">
                    </div>
                    <div class="col-md-6">
                        <label class="form-label small text-muted">Paytm QR Image URL</label>
                        <input type="text" id="u_paytm_qr" class="form-control" value="{{ upi.paytm_qr }}" placeholder="https://i.imgur.com/...">
                    </div>
                    <div class="col-12">
                        <button onclick="saveUpi()" class="btn btn-custom w-100"><i class="fas fa-floppy-disk me-2"></i>Save UPI Settings</button>
                    </div>
                </div>
            </div>
        </div>

        <!-- 🔌 TAB 6: KEY DELIVERY API -->
        <div class="tab-pane-content" id="tab-api" style="display: none;">
            <div class="card p-4">
                <h5 class="fw-bold mb-3"><i class="fas fa-plug text-warning me-2"></i>Key Delivery API Configuration</h5>
                <div class="row g-3">
                    <div class="col-md-6">
                        <label class="form-label small text-muted">API Key</label>
                        <input type="text" id="api_key" class="form-control" value="{{ api_cfg[1] }}">
                    </div>
                    <div class="col-md-6">
                        <label class="form-label small text-muted">Master Key</label>
                        <input type="text" id="api_master" class="form-control" value="{{ api_cfg[2] }}">
                    </div>
                    <div class="col-12">
                        <label class="form-label small text-muted">Supplier Panel URL</label>
                        <input type="text" id="api_url" class="form-control" value="{{ api_cfg[0] }}">
                    </div>
                    <div class="col-12">
                        <button onclick="saveApi()" class="btn btn-success"><i class="fas fa-save me-2"></i>Save API Settings</button>
                    </div>
                </div>
            </div>
        </div>

        <!-- ⚙️ TAB 7: STORE SETTINGS -->
        <div class="tab-pane-content" id="tab-store" style="display: none;">
            <div class="card p-4">
                <h5 class="fw-bold mb-3"><i class="fas fa-gear text-info me-2"></i>Store Configurations</h5>
                <div class="row g-3">
                    <div class="col-md-6">
                        <label class="form-label small text-muted">Support Telegram Username</label>
                        <input type="text" id="st_support" class="form-control" value="{{ store.support_username }}" placeholder="@Athulsudin">
                    </div>
                    <div class="col-md-6">
                        <label class="form-label small text-muted">How to Use Tutorial Link</label>
                        <input type="text" id="st_how" class="form-control" value="{{ store.how_to_use_link }}" placeholder="https://t.me/...">
                    </div>
                    <div class="col-12">
                        <label class="form-label small text-muted">Welcome Message Text</label>
                        <textarea id="st_welc" class="form-control" rows="6">{{ store.welcome_message }}</textarea>
                    </div>
                    <div class="col-12">
                        <button onclick="saveStore()" class="btn btn-custom"><i class="fas fa-floppy-disk me-2"></i>Save Store Settings</button>
                    </div>
                </div>
            </div>
        </div>
    </div>
    {% endif %}

    <script>
        function toggleSidebar() {
            document.getElementById('sidebar').classList.toggle('active');
        }

        function showTab(tabName, el) {
            document.querySelectorAll('.tab-pane-content').forEach(d => d.style.display = 'none');
            document.getElementById('tab-' + tabName).style.display = 'block';
            document.querySelectorAll('.sidebar-link').forEach(l => l.classList.remove('active'));
            if(el) el.classList.add('active');
        }

        function saveProduct() {
            let name = document.getElementById('p_name').value;
            let cat = document.getElementById('p_cat').value;
            let icon = document.getElementById('p_icon').value;
            let plan = document.getElementById('p_plan').value;
            let price = document.getElementById('p_price').value;
            let link = document.getElementById('p_link').value;

            if(!name || !plan || !price) { alert("Please fill required fields!"); return; }

            fetch('/api/product/save', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({name: name, category: cat, icon: icon, prices: [[plan, parseFloat(price)]], download_link: link})
            }).then(r => r.json()).then(d => { alert(d.message); location.reload(); });
        }

        function deleteProduct(prod_key) {
            if(!confirm("Are you sure?")) return;
            fetch('/api/product/delete', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({prod_key: prod_key})
            }).then(r => r.json()).then(d => { alert(d.message); location.reload(); });
        }

        function toggleMaint(prod_key, state) {
            fetch('/api/product/maintenance', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({prod_key: prod_key, state: state})
            }).then(r => r.json()).then(d => { location.reload(); });
        }

        function toggleStock(prod_key, state) {
            fetch('/api/product/stockout', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({prod_key: prod_key, state: state})
            }).then(r => r.json()).then(d => { location.reload(); });
        }

        function uploadKeys() {
            let prod = document.getElementById('k_prod').value;
            let plan = document.getElementById('k_plan').value;
            let keys = document.getElementById('k_keys').value;

            fetch('/api/stock/add', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({prod_key: prod, plan: plan, keys: keys})
            }).then(r => r.json()).then(d => { alert(d.message); location.reload(); });
        }

        function sendBroadcast() {
            let text = document.getElementById('bc_text').value;
            if(!text) { alert("Please type a message!"); return; }

            fetch('/api/broadcast/send', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({message: text})
            }).then(r => r.json()).then(d => { alert(d.message); });
        }

        function saveUpi() {
            fetch('/api/upi/save', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({
                    fampay_token: document.getElementById('u_fampay_upi').value,
                    fampay_qr: document.getElementById('u_fampay_qr').value,
                    paytm_token: document.getElementById('u_paytm_token').value,
                    paytm_qr: document.getElementById('u_paytm_qr').value
                })
            }).then(r => r.json()).then(d => { alert(d.message); });
        }

        function saveApi() {
            fetch('/api/api_cfg/save', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({
                    api_url: document.getElementById('api_url').value,
                    api_key: document.getElementById('api_key').value,
                    master_key: document.getElementById('api_master').value
                })
            }).then(r => r.json()).then(d => { alert(d.message); });
        }

        function saveStore() {
            fetch('/api/store/save', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({
                    support_username: document.getElementById('st_support').value,
                    how_to_use_link: document.getElementById('st_how').value,
                    welcome_message: document.getElementById('st_welc').value
                })
            }).then(r => r.json()).then(d => { alert(d.message); });
        }
    </script>
</body>
</html>
"""

# ==========================================
# 🌐 FLASK ROUTES & AUTH
# ==========================================
@app.route('/login', methods=['POST'])
def login():
    pwd = request.form.get('password')
    if pwd == ADMIN_PASSWORD:
        session['admin_logged'] = True
        return redirect('/')
    return render_template_string(ADMIN_HTML, error="Incorrect Admin Password!")

@app.route('/logout')
def logout():
    session.clear()
    return redirect('/')

@app.route('/')
def dashboard():
    if not session.get('admin_logged'):
        return render_template_string(ADMIN_HTML)

    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()

    c.execute('SELECT COUNT(*) FROM users')
    tot_users = c.fetchone()[0]

    c.execute('SELECT COUNT(*), SUM(amount) FROM order_history')
    row = c.fetchone()
    tot_orders = row[0] or 0
    tot_rev = row[1] or 0.0

    c.execute('SELECT COUNT(*) FROM keys_inventory WHERE is_used = 0')
    tot_keys = c.fetchone()[0]

    c.execute('SELECT prod_key, name, category, prices, download_link, icon, maintenance, stock_out FROM products')
    prods = [{"prod_key": p[0], "name": p[1], "category": p[2], "prices": json.loads(p[3]), "download_link": p[4], "icon": p[5], "maintenance": bool(p[6]), "stock_out": bool(p[7])} for p in c.fetchall()]

    c.execute('SELECT support_username, how_to_use_link, welcome_message FROM store_settings WHERE id = 1')
    st_row = c.fetchone() or ("@Athulsudin", "", "")
    store = {"support_username": st_row[0], "how_to_use_link": st_row[1], "welcome_message": st_row[2]}

    c.execute('SELECT paytm_token, paytm_qr, fampay_token, fampay_qr FROM upi_settings WHERE id = 1')
    up_row = c.fetchone() or ("", "", "", "")
    upi = {"paytm_token": up_row[0], "paytm_qr": up_row[1], "fampay_token": up_row[2], "fampay_qr": up_row[3]}

    c.execute('SELECT api_url, api_key, master_key FROM api_config WHERE id = 1')
    api_cfg = c.fetchone() or ("", "", "")
    conn.close()

    stats = {
        "users": tot_users,
        "orders": tot_orders,
        "revenue": f"{tot_rev:,.2f}",
        "keys": tot_keys
    }

    return render_template_string(ADMIN_HTML, stats=stats, products=prods, store=store, upi=upi, api_cfg=api_cfg)

# ==========================================
# 🛠️ BACKEND API CALLS
# ==========================================
@app.route('/api/product/save', methods=['POST'])
def api_save_product():
    if not session.get('admin_logged'): return jsonify({"success": False, "message": "Unauthorized"}), 401
    d = request.json
    name = d['name'].strip()
    key = re.sub(r'[^a-zA-Z0-9]', '_', name).strip('_').lower()
    
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''
        INSERT OR REPLACE INTO products (prod_key, name, category, prices, download_link, icon, maintenance, stock_out)
        VALUES (?, ?, ?, ?, ?, ?, 0, 0)
    ''', (key, name, d['category'], json.dumps(d['prices']), d.get('download_link', ''), d.get('icon', '⚡')))
    conn.commit()
    conn.close()
    return jsonify({"success": True, "message": "Product saved successfully!"})

@app.route('/api/product/delete', methods=['POST'])
def api_delete_product():
    if not session.get('admin_logged'): return jsonify({"success": False, "message": "Unauthorized"}), 401
    key = request.json.get('prod_key')
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('DELETE FROM products WHERE prod_key = ?', (key,))
    conn.commit()
    conn.close()
    return jsonify({"success": True, "message": "Product deleted successfully!"})

@app.route('/api/product/maintenance', methods=['POST'])
def api_maint_product():
    if not session.get('admin_logged'): return jsonify({"success": False, "message": "Unauthorized"}), 401
    d = request.json
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('UPDATE products SET maintenance = ? WHERE prod_key = ?', (d['state'], d['prod_key']))
    conn.commit()
    conn.close()
    return jsonify({"success": True})

@app.route('/api/product/stockout', methods=['POST'])
def api_stockout_product():
    if not session.get('admin_logged'): return jsonify({"success": False, "message": "Unauthorized"}), 401
    d = request.json
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('UPDATE products SET stock_out = ? WHERE prod_key = ?', (d['state'], d['prod_key']))
    conn.commit()
    conn.close()
    return jsonify({"success": True})

@app.route('/api/stock/add', methods=['POST'])
def api_stock_add():
    if not session.get('admin_logged'): return jsonify({"success": False, "message": "Unauthorized"}), 401
    d = request.json
    keys = [k.strip() for k in d['keys'].split('\n') if k.strip()]
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    for k in keys:
        c.execute('INSERT INTO keys_inventory (prod_key, plan, item_key, is_used) VALUES (?, ?, ?, 0)', (d['prod_key'], d['plan'], k))
    conn.commit()
    conn.close()
    return jsonify({"success": True, "message": f"Added {len(keys)} keys to stock!"})

@app.route('/api/broadcast/send', methods=['POST'])
def api_broadcast_send():
    if not session.get('admin_logged'): return jsonify({"success": False, "message": "Unauthorized"}), 401
    msg = request.json.get('message', '').strip()
    
    def _send_sync():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute('SELECT user_id FROM users')
        users = [r[0] for r in c.fetchall()]
        conn.close()
        
        bot = Application.builder().token(BOT_TOKEN).build().bot
        for u in users:
            try:
                loop.run_until_complete(bot.send_message(chat_id=u, text=msg, parse_mode="HTML"))
            except Exception:
                pass

    Thread(target=_send_sync).start()
    return jsonify({"success": True, "message": "Broadcast sending in background to all users!"})

@app.route('/api/upi/save', methods=['POST'])
def api_upi_save():
    if not session.get('admin_logged'): return jsonify({"success": False, "message": "Unauthorized"}), 401
    d = request.json
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''
        INSERT INTO upi_settings (id, paytm_token, paytm_qr, fampay_token, fampay_qr)
        VALUES (1, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET paytm_token=?, paytm_qr=?, fampay_token=?, fampay_qr=?
    ''', (d['paytm_token'], d['paytm_qr'], d['fampay_token'], d['fampay_qr'], d['paytm_token'], d['paytm_qr'], d['fampay_token'], d['fampay_qr']))
    conn.commit()
    conn.close()
    return jsonify({"success": True, "message": "UPI settings updated!"})

@app.route('/api/api_cfg/save', methods=['POST'])
def api_cfg_save():
    if not session.get('admin_logged'): return jsonify({"success": False, "message": "Unauthorized"}), 401
    d = request.json
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''
        INSERT INTO api_config (id, api_url, api_key, master_key)
        VALUES (1, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET api_url=?, api_key=?, master_key=?
    ''', (d['api_url'], d['api_key'], d['master_key'], d['api_url'], d['api_key'], d['master_key']))
    conn.commit()
    conn.close()
    return jsonify({"success": True, "message": "API config saved!"})

@app.route('/api/store/save', methods=['POST'])
def api_store_save():
    if not session.get('admin_logged'): return jsonify({"success": False, "message": "Unauthorized"}), 401
    d = request.json
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''
        INSERT INTO store_settings (id, support_username, how_to_use_link, welcome_message)
        VALUES (1, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET support_username=?, how_to_use_link=?, welcome_message=?
    ''', (d['support_username'], d['how_to_use_link'], d['welcome_message'], d['support_username'], d['how_to_use_link'], d['welcome_message']))
    conn.commit()
    conn.close()
    return jsonify({"success": True, "message": "Store settings updated!"})

# ==========================================
# 🚀 RUNNER
# ==========================================
if __name__ == '__main__':
    port = int(os.environ.get("PORT", 8080))
    print(f"🚀 Web Admin running at http://0.0.0.0:{port}")
    app.run(host='0.0.0.0', port=port)
