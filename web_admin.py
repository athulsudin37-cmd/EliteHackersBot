import os
import json
import sqlite3
import re
import urllib.parse
from threading import Thread
import requests
from flask import Flask, render_template_string, request, jsonify, redirect, session

DB_FILE = "bot_database.db"
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "athulsudin1234")
BOT_TOKEN = "8892856619:AAGZhdOv389_AaKvbcbInlJAiDMOwQxOeHc"

app = Flask(__name__)
app.secret_key = os.urandom(24)

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
        :root { --bg-main: #0f172a; --bg-card: #1e293b; --border-color: #334155; --accent: #6366f1; --sidebar-w: 260px; }
        body { background-color: var(--bg-main); color: #f8fafc; font-family: system-ui, sans-serif; margin: 0; }
        .card { background-color: var(--bg-card); border: 1px solid var(--border-color); border-radius: 12px; margin-bottom: 20px; }
        .btn-custom { background-color: var(--accent); color: white; border: none; border-radius: 8px; padding: 10px 18px; }
        .btn-custom:hover { background-color: #4f46e5; color: white; }
        .form-control, .form-select { background-color: #0b1120; border: 1px solid var(--border-color); color: white; border-radius: 8px; padding: 10px; }
        .form-control:focus, .form-select:focus { background-color: #0b1120; color: white; border-color: var(--accent); box-shadow: none; }
        .sidebar { position: fixed; top: 0; left: 0; width: var(--sidebar-w); height: 100vh; background: var(--bg-card); border-right: 1px solid var(--border-color); padding-top: 20px; z-index: 1050; }
        .sidebar-brand { padding: 0 20px 20px; font-weight: 700; color: #38bdf8; border-bottom: 1px solid var(--border-color); }
        .sidebar-link { padding: 12px 20px; color: #94a3b8; display: flex; align-items: center; gap: 12px; cursor: pointer; text-decoration: none; border-left: 4px solid transparent; }
        .sidebar-link:hover, .sidebar-link.active { background: var(--bg-main); color: #38bdf8; border-left-color: #38bdf8; }
        .main-content { margin-left: var(--sidebar-w); padding: 25px; }
        @media (max-width: 768px) { .sidebar { left: -260px; } .sidebar.active { left: 0; } .main-content { margin-left: 0; } }
        .stat-card { background: var(--bg-card); border: 1px solid var(--border-color); border-radius: 12px; padding: 20px; }
    </style>
</head>
<body>
    {% if not session.get('admin_logged') %}
    <div class="d-flex align-items-center justify-content-center" style="min-height: 100vh;">
        <div class="col-11 col-sm-8 col-md-4">
            <div class="card p-4 text-center">
                <i class="fas fa-shield-halved fa-3x text-primary mb-3"></i>
                <h4 class="fw-bold mb-3">ADMIN CONTROL</h4>
                {% if error %}<div class="alert alert-danger py-1 small">{{ error }}</div>{% endif %}
                <form method="POST" action="/login">
                    <input type="password" name="password" class="form-control text-center mb-3" placeholder="Enter Password" required>
                    <button type="submit" class="btn btn-custom w-100">Unlock Panel</button>
                </form>
            </div>
        </div>
    </div>
    {% else %}
    <div class="sidebar" id="sidebar">
        <div class="sidebar-brand"><i class="fas fa-bolt text-warning me-2"></i>ELITE CONTROL</div>
        <div class="mt-3">
            <a class="sidebar-link active" onclick="showTab('dashboard', this)"><i class="fas fa-chart-pie"></i> Dashboard</a>
            <a class="sidebar-link" onclick="showTab('products', this)"><i class="fas fa-box-open"></i> Manage Products</a>
            <a class="sidebar-link" onclick="showTab('stock', this)"><i class="fas fa-key"></i> Key Stock</a>
            <a class="sidebar-link" onclick="showTab('broadcast', this)"><i class="fas fa-bullhorn"></i> Broadcast</a>
            <a class="sidebar-link" onclick="showTab('upi', this)"><i class="fas fa-credit-card"></i> UPI Setup</a>
            <a class="sidebar-link" onclick="showTab('store', this)"><i class="fas fa-sliders"></i> Store Settings</a>
            <a href="/logout" class="sidebar-link text-danger mt-4"><i class="fas fa-right-from-bracket"></i> Logout</a>
        </div>
    </div>

    <div class="main-content">
        <div class="d-flex justify-content-between align-items-center mb-4">
            <h4 class="fw-bold m-0">⚙️ Bot Control Center</h4>
            <a href="/logout" class="btn btn-outline-danger btn-sm"><i class="fas fa-lock me-1"></i>Logout</a>
        </div>

        <div class="tab-pane-content" id="tab-dashboard">
            <div class="row g-3 mb-4">
                <div class="col-md-3"><div class="stat-card"><h6>USERS</h6><h2 class="text-primary">{{ stats.users }}</h2></div></div>
                <div class="col-md-3"><div class="stat-card"><h6>ORDERS</h6><h2 class="text-success">{{ stats.orders }}</h2></div></div>
                <div class="col-md-3"><div class="stat-card"><h6>REVENUE</h6><h2 class="text-warning">₹{{ stats.revenue }}</h2></div></div>
                <div class="col-md-3"><div class="stat-card"><h6>KEYS IN STOCK</h6><h2 class="text-info">{{ stats.keys }}</h2></div></div>
            </div>
        </div>

        <div class="tab-pane-content" id="tab-products" style="display: none;">
            <div class="card p-4">
                <h5><i class="fas fa-plus-circle me-2"></i>Add / Update Product</h5>
                <div class="row g-3 mt-1">
                    <div class="col-md-4"><input type="text" id="p_name" class="form-control" placeholder="Product Name"></div>
                    <div class="col-md-4">
                        <select id="p_cat" class="form-select">
                            <option value="non_root">Non-Root</option><option value="root">Root</option>
                            <option value="ios">iOS</option><option value="pc">PC</option><option value="likes">Likes</option>
                        </select>
                    </div>
                    <div class="col-md-4"><input type="text" id="p_icon" class="form-control" placeholder="Icon (⚡)"></div>
                    <div class="col-md-4"><input type="text" id="p_plan" class="form-control" placeholder="Plan (1_Day)"></div>
                    <div class="col-md-4"><input type="number" id="p_price" class="form-control" placeholder="Price (₹)"></div>
                    <div class="col-md-4"><input type="text" id="p_link" class="form-control" placeholder="Download Link"></div>
                    <div class="col-12"><button onclick="saveProduct()" class="btn btn-custom w-100">Save Product</button></div>
                </div>
            </div>
            <div class="card p-4">
                <h5><i class="fas fa-list me-2"></i>Product List</h5>
                <div class="table-responsive">
                    <table class="table table-dark align-middle mt-2">
                        <thead><tr><th>Icon</th><th>Name</th><th>Category</th><th>Prices</th><th>Action</th></tr></thead>
                        <tbody>
                            {% for p in products %}
                            <tr>
                                <td>{{ p.icon }}</td><td><strong>{{ p.name }}</strong></td><td>{{ p.category }}</td><td>{{ p.prices }}</td>
                                <td><button onclick="deleteProduct('{{ p.prod_key }}')" class="btn btn-sm btn-outline-danger"><i class="fas fa-trash"></i></button></td>
                            </tr>
                            {% endfor %}
                        </tbody>
                    </table>
                </div>
            </div>
        </div>

        <div class="tab-pane-content" id="tab-stock" style="display: none;">
            <div class="card p-4">
                <h5><i class="fas fa-key me-2"></i>Add Keys to Stock</h5>
                <div class="row g-3 mt-1">
                    <div class="col-md-6"><input type="text" id="k_prod" class="form-control" placeholder="Product Key (e.g. bala_mod)"></div>
                    <div class="col-md-6"><input type="text" id="k_plan" class="form-control" placeholder="Plan (e.g. 1_Day)"></div>
                    <div class="col-12"><textarea id="k_keys" class="form-control" rows="5" placeholder="KEY1&#10;KEY2"></textarea></div>
                    <div class="col-12"><button onclick="uploadKeys()" class="btn btn-custom">Upload Keys</button></div>
                </div>
            </div>
        </div>

        <div class="tab-pane-content" id="tab-broadcast" style="display: none;">
            <div class="card p-4">
                <h5><i class="fas fa-bullhorn me-2"></i>Broadcast Message</h5>
                <textarea id="bc_text" class="form-control mt-2" rows="5" placeholder="Type message for all users..."></textarea>
                <button onclick="sendBroadcast()" class="btn btn-danger mt-3">Send Broadcast</button>
            </div>
        </div>

        <div class="tab-pane-content" id="tab-upi" style="display: none;">
            <div class="card p-4">
                <h5><i class="fas fa-credit-card me-2"></i>UPI Gateway Settings</h5>
                <div class="row g-3 mt-1">
                    <div class="col-md-6"><label class="small text-muted">FamPay UPI ID</label><input type="text" id="u_fampay_upi" class="form-control" value="{{ upi.fampay_token }}"></div>
                    <div class="col-md-6"><label class="small text-muted">FamPay QR Link</label><input type="text" id="u_fampay_qr" class="form-control" value="{{ upi.fampay_qr }}"></div>
                    <div class="col-12"><button onclick="saveUpi()" class="btn btn-custom">Save UPI</button></div>
                </div>
            </div>
        </div>

        <div class="tab-pane-content" id="tab-store" style="display: none;">
            <div class="card p-4">
                <h5><i class="fas fa-gear me-2"></i>Store Settings</h5>
                <div class="row g-3 mt-1">
                    <div class="col-md-6"><label class="small text-muted">Support Username</label><input type="text" id="st_support" class="form-control" value="{{ store.support_username }}"></div>
                    <div class="col-md-6"><label class="small text-muted">How to Use Link</label><input type="text" id="st_how" class="form-control" value="{{ store.how_to_use_link }}"></div>
                    <div class="col-12"><label class="small text-muted">Welcome Message</label><textarea id="st_welc" class="form-control" rows="4">{{ store.welcome_message }}</textarea></div>
                    <div class="col-12"><button onclick="saveStore()" class="btn btn-custom">Save Settings</button></div>
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
                body: JSON.stringify({name: document.getElementById('p_name').value, category: document.getElementById('p_cat').value, icon: document.getElementById('p_icon').value, prices: [[document.getElementById('p_plan').value, parseFloat(document.getElementById('p_price').value)]], download_link: document.getElementById('p_link').value})
            }).then(r => r.json()).then(d => { alert(d.message); location.reload(); });
        }
        function deleteProduct(k) {
            if(!confirm("Are you sure?")) return;
            fetch('/api/product/delete', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({prod_key: k}) })
            .then(r => r.json()).then(d => { alert(d.message); location.reload(); });
        }
        function uploadKeys() {
            fetch('/api/stock/add', {
                method: 'POST', headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({prod_key: document.getElementById('k_prod').value, plan: document.getElementById('k_plan').value, keys: document.getElementById('k_keys').value})
            }).then(r => r.json()).then(d => { alert(d.message); location.reload(); });
        }
        function sendBroadcast() {
            fetch('/api/broadcast/send', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({message: document.getElementById('bc_text').value}) })
            .then(r => r.json()).then(d => alert(d.message));
        }
        function saveUpi() {
            fetch('/api/upi/save', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({fampay_token: document.getElementById('u_fampay_upi').value, fampay_qr: document.getElementById('u_fampay_qr').value}) })
            .then(r => r.json()).then(d => alert(d.message));
        }
        function saveStore() {
            fetch('/api/store/save', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({support_username: document.getElementById('st_support').value, how_to_use_link: document.getElementById('st_how').value, welcome_message: document.getElementById('st_welc').value}) })
            .then(r => r.json()).then(d => alert(d.message));
        }
    </script>
</body>
</html>
"""

@app.route('/login', methods=['POST'])
def login():
    if request.form.get('password') == ADMIN_PASSWORD:
        session['admin_logged'] = True
        return redirect('/')
    return render_template_string(ADMIN_HTML, error="Wrong Password!")

@app.route('/logout')
def logout():
    session.clear()
    return redirect('/')

@app.route('/')
def dashboard():
    if not session.get('admin_logged'): return render_template_string(ADMIN_HTML)
    conn = sqlite3.connect(DB_FILE); c = conn.cursor()
    c.execute('SELECT COUNT(*) FROM users'); users = c.fetchone()[0]
    c.execute('SELECT COUNT(*), SUM(amount) FROM order_history'); r = c.fetchone(); orders = r[0] or 0; rev = r[1] or 0.0
    c.execute('SELECT COUNT(*) FROM keys_inventory WHERE is_used = 0'); keys = c.fetchone()[0]
    c.execute('SELECT prod_key, name, category, prices, icon FROM products'); prods = [{"prod_key": p[0], "name": p[1], "category": p[2], "prices": json.loads(p[3]), "icon": p[4]} for p in c.fetchall()]
    c.execute('SELECT support_username, how_to_use_link, welcome_message FROM store_settings WHERE id = 1'); st = c.fetchone() or ("@Athulsudin", "", "")
    c.execute('SELECT fampay_token, fampay_qr FROM upi_settings WHERE id = 1'); up = c.fetchone() or ("", "")
    conn.close()
    return render_template_string(ADMIN_HTML, stats={"users": users, "orders": orders, "revenue": f"{rev:,.2f}", "keys": keys}, products=prods, store={"support_username": st[0], "how_to_use_link": st[1], "welcome_message": st[2]}, upi={"fampay_token": up[0], "fampay_qr": up[1]})

@app.route('/api/product/save', methods=['POST'])
def api_prod():
    d = request.json; key = re.sub(r'[^a-zA-Z0-9]', '_', d['name']).strip('_').lower()
    conn = sqlite3.connect(DB_FILE); c = conn.cursor()
    c.execute('INSERT OR REPLACE INTO products (prod_key, name, category, prices, download_link, icon, maintenance, stock_out) VALUES (?, ?, ?, ?, ?, ?, 0, 0)',
              (key, d['name'], d['category'], json.dumps(d['prices']), d.get('download_link',''), d.get('icon','⚡')))
    conn.commit(); conn.close()
    return jsonify({"message": "Product saved successfully!"})

@app.route('/api/product/delete', methods=['POST'])
def api_pdel():
    key = request.json.get('prod_key')
    conn = sqlite3.connect(DB_FILE); c = conn.cursor()
    c.execute('DELETE FROM products WHERE prod_key = ?', (key,))
    conn.commit(); conn.close()
    return jsonify({"message": "Product deleted!"})

@app.route('/api/stock/add', methods=['POST'])
def api_stock():
    d = request.json; keys = [k.strip() for k in d['keys'].split('\n') if k.strip()]
    conn = sqlite3.connect(DB_FILE); c = conn.cursor()
    for k in keys: c.execute('INSERT INTO keys_inventory (prod_key, plan, item_key, is_used) VALUES (?, ?, ?, 0)', (d['prod_key'], d['plan'], k))
    conn.commit(); conn.close()
    return jsonify({"message": f"Added {len(keys)} keys!"})

@app.route('/api/broadcast/send', methods=['POST'])
def api_bc():
    msg = request.json.get('message', '')
    def _send():
        conn = sqlite3.connect(DB_FILE); c = conn.cursor(); c.execute('SELECT user_id FROM users'); users = [r[0] for r in c.fetchall()]; conn.close()
        for u in users:
            try:
                requests.post(
                    f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                    json={"chat_id": u, "text": msg, "parse_mode": "HTML"},
                    timeout=5
                )
            except Exception:
                pass
    Thread(target=_send).start()
    return jsonify({"message": "Broadcast started in background!"})

@app.route('/api/upi/save', methods=['POST'])
def api_upi():
    d = request.json; conn = sqlite3.connect(DB_FILE); c = conn.cursor()
    c.execute('UPDATE upi_settings SET fampay_token=?, fampay_qr=? WHERE id = 1', (d['fampay_token'], d['fampay_qr']))
    conn.commit(); conn.close()
    return jsonify({"message": "UPI Settings saved!"})

@app.route('/api/store/save', methods=['POST'])
def api_store():
    d = request.json; conn = sqlite3.connect(DB_FILE); c = conn.cursor()
    c.execute('UPDATE store_settings SET support_username=?, how_to_use_link=?, welcome_message=? WHERE id = 1', (d['support_username'], d['how_to_use_link'], d['welcome_message']))
    conn.commit(); conn.close()
    return jsonify({"message": "Store Settings saved!"})

def run_web():
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)
