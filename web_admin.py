import os
import json
import re
import time
import hashlib
import hmac
import csv
import io
import urllib.request
import urllib.error
from datetime import datetime, timedelta, timezone
from threading import Thread
from flask import Flask, render_template_string, request, jsonify, redirect, session, Response

from database import db, init_db, parse_prices, MENU_BUTTONS, BUTTON_GROUPS, BUTTON_COLORS
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
app.config["MAX_CONTENT_LENGTH"] = 55 * 1024 * 1024  # broadcast media (Telegram allows up to 50 MB)
IST = timezone(timedelta(hours=5, minutes=30))
ADMIN_ID = int(os.environ.get("ADMIN_ID", "7616127905"))

# ==========================================
# 🗄️ DATABASE HELPERS (PostgreSQL via database.py)
# ==========================================
def q_all(sql, params=None):
    """Run a SELECT and return rows as tuples (the HTML template reads rows by position)."""
    return [tuple(r.values()) for r in db.fetchall(sql, params)]

def q_one(sql, params=None):
    row = db.fetchone(sql, params)
    return tuple(row.values()) if row else None

# "Sold today" in Indian time. Old rows have no created_ts, so their IST text timestamp is matched too.
TODAY_SQL = ("((o.created_ts AT TIME ZONE 'Asia/Kolkata')::date = (now() AT TIME ZONE 'Asia/Kolkata')::date "
             "OR o.timestamp LIKE %s)")

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
<html lang="en" data-bs-theme="dark">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{{ bot_info.name }} - Control Center</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
    <link href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css" rel="stylesheet">
    <style>
        :root {
            --bg-deep: #080318;
            --card-glass: rgba(24, 14, 52, 0.95);
            --neon-border: rgba(147, 51, 234, 0.38);
            --sidebar-w: 290px;
            --txt: #eef2ff;
            --txt-soft: #b4bdd9;
            --bs-body-color: #eef2ff;
            --bs-body-bg: #080318;
            --bs-border-color: rgba(147, 51, 234, 0.38);
        }
        * { -webkit-tap-highlight-color: transparent; }
        body {
            background: radial-gradient(circle at top center, #1b0c3f 0%, #0c051d 60%, #05020c 100%);
            background-attachment: fixed; color: var(--txt); font-family: -apple-system, system-ui, 'Segoe UI', Roboto, sans-serif;
            min-height: 100vh; margin: 0; overflow-x: hidden;
        }
        body::before {
            content: ''; position: fixed; top: 0; left: 0; width: 100%; height: 100%;
            background-image: linear-gradient(rgba(147, 51, 234, 0.05) 1px, transparent 1px), linear-gradient(90deg, rgba(147, 51, 234, 0.05) 1px, transparent 1px);
            background-size: 36px 36px; pointer-events: none; z-index: 0;
        }

        /* ✅ READABILITY FIX: every text inside cards, tables, labels and inputs is light on dark */
        .card, .card h1, .card h2, .card h3, .card h4, .card h5, .card h6, .card label, .card p, .card td, .card th,
        .modal-card, .modal-card label, .modal-card h5, .modal-card h6, .table, .table td, .table th { color: var(--txt); }
        .card { --bs-card-color: var(--txt); --bs-card-bg: transparent; }
        .text-muted, .small.text-muted, label.small, .form-text { color: var(--txt-soft) !important; }
        .table { --bs-table-color: var(--txt); --bs-table-bg: transparent; --bs-table-border-color: rgba(147,51,234,.25); --bs-table-striped-bg: rgba(255,255,255,.03); }
        .table > :not(caption) > * > * { background-color: transparent; color: var(--txt); }
        .table thead th { color: #c4b5fd; font-size: .78rem; letter-spacing: .04em; text-transform: uppercase; }
        .form-control, .form-select { background-color: #0d0622; border: 1px solid var(--neon-border); color: #fff; border-radius: 12px; padding: 11px 13px; }
        .form-control:focus, .form-select:focus { background-color: #0d0622; color: #fff; border-color: #a855f7; box-shadow: 0 0 0 3px rgba(168,85,247,.2); }
        .form-control::placeholder { color: #8d96b5; opacity: 1; }
        .form-select option { background: #13072e; color: #fff; }
        code { color: #f9a8d4; }
        a { color: #7dd3fc; }
        .form-check-input { border-color: #a78bfa; }

        /* 🔐 LOGIN */
        .login-box { min-height: 100vh; display: flex; align-items: center; justify-content: center; padding: 20px; position: relative; z-index: 10; }
        .login-card { background: var(--card-glass); backdrop-filter: blur(25px); border: 1px solid var(--neon-border); border-radius: 28px; padding: 42px 32px; width: 100%; max-width: 400px; text-align: center; box-shadow: 0 0 50px rgba(139, 92, 246, 0.25); }
        .avatar-ring { width: 88px; height: 88px; margin: 0 auto 20px; border-radius: 50%; padding: 3px; background: linear-gradient(135deg, #06b6d4, #a855f7, #f59e0b); display: flex; align-items: center; justify-content: center; }
        .avatar-inner { width: 100%; height: 100%; background: #0d0622; border-radius: 50%; display: flex; align-items: center; justify-content: center; overflow: hidden; }
        .title-grad { font-size: 1.65rem; font-weight: 800; background: linear-gradient(90deg, #a78bfa, #38bdf8, #facc15); -webkit-background-clip: text; -webkit-text-fill-color: transparent; }
        .pwd-field { position: relative; margin-bottom: 20px; }
        .pwd-field input { width: 100%; background: rgba(14, 7, 33, 0.9); border: 1px solid rgba(139, 92, 246, 0.5); border-radius: 14px; padding: 13px 45px 13px 18px; color: white; outline: none; }
        .eye-btn { position: absolute; right: 15px; top: 50%; transform: translateY(-50%); color: #a78bfa; cursor: pointer; }
        .btn-unlock { width: 100%; padding: 13px; border: none; border-radius: 14px; background: linear-gradient(135deg, #7c3aed, #6366f1); color: white; font-weight: 700; cursor: pointer; }

        /* 💻 LAYOUT + DRAWER SIDEBAR */
        .sidebar { position: fixed; top: 0; left: calc(-1 * var(--sidebar-w) - 20px); width: var(--sidebar-w); height: 100vh; background: linear-gradient(180deg, #14082e, #0b0420); border-right: 1px solid var(--neon-border); padding: 18px 14px 30px; z-index: 1060; transition: left .28s ease; overflow-y: auto; }
        .sidebar.active { left: 0; box-shadow: 12px 0 40px rgba(0,0,0,.6); }
        .sb-overlay { position: fixed; inset: 0; background: rgba(0,0,0,.6); backdrop-filter: blur(3px); z-index: 1055; opacity: 0; pointer-events: none; transition: opacity .25s; }
        .sb-overlay.show { opacity: 1; pointer-events: auto; }
        .sb-brand { display: flex; align-items: center; gap: 10px; padding: 4px 8px 16px; border-bottom: 1px solid var(--neon-border); margin-bottom: 14px; font-weight: 800; color: #a78bfa; font-size: 1.1rem; }
        .sb-link { display: flex; align-items: center; gap: 14px; padding: 9px 10px; margin-bottom: 6px; border-radius: 16px; color: #dbe3ff; font-weight: 600; cursor: pointer; text-decoration: none; border: 1px solid transparent; }
        .sb-link:hover { background: rgba(124,58,237,.15); color: #fff; }
        .sb-link.active { background: linear-gradient(135deg, rgba(124,58,237,.55), rgba(99,102,241,.35)); border-color: #8b5cf6; color: #fff; }
        .sb-ic { width: 46px; height: 46px; border-radius: 14px; display: flex; align-items: center; justify-content: center; font-size: 1.35rem; flex-shrink: 0; box-shadow: inset 0 0 0 1px rgba(255,255,255,.12); }
        .g1 { background: linear-gradient(135deg, #7c3aed, #c026d3); } .g2 { background: linear-gradient(135deg, #1d4ed8, #3b82f6); }
        .g3 { background: linear-gradient(135deg, #0f766e, #14b8a6); } .g4 { background: linear-gradient(135deg, #b45309, #f59e0b); }
        .g5 { background: linear-gradient(135deg, #0e7490, #22d3ee); } .g6 { background: linear-gradient(135deg, #9d174d, #ec4899); }
        .g7 { background: linear-gradient(135deg, #4338ca, #818cf8); } .g8 { background: linear-gradient(135deg, #b91c1c, #f87171); }
        .main-content { padding: 18px 16px 60px; position: relative; z-index: 10; max-width: 1100px; }
        @media (min-width: 769px) { .sidebar { left: 0; } .sb-overlay { display: none; } .main-content { margin-left: var(--sidebar-w); padding: 25px; } }

        .topbar { display: flex; align-items: center; justify-content: space-between; padding: 6px 0 16px; margin-bottom: 18px; border-bottom: 1px solid var(--neon-border); }
        .menu-btn { width: 56px; height: 56px; border-radius: 18px; background: rgba(40,20,90,.7); border: 1px solid var(--neon-border); color: #a78bfa; font-size: 1.4rem; }
        .crown-ring { width: 56px; height: 56px; border-radius: 50%; padding: 3px; background: linear-gradient(135deg, #f59e0b, #ec4899, #8b5cf6); display: flex; }
        .crown-ring span { flex: 1; border-radius: 50%; background: #0d0622; display: flex; align-items: center; justify-content: center; font-size: 1.5rem; }

        .hero-card { background: linear-gradient(135deg, #2a1466, #120832); border: 1px solid var(--neon-border); border-radius: 26px; padding: 26px 24px; margin-bottom: 22px; box-shadow: 0 10px 40px rgba(88,28,135,.35); }
        .hero-kicker { color: #a78bfa; font-weight: 800; letter-spacing: .06em; font-size: .85rem; }
        .hero-card h2 { font-weight: 800; margin: 6px 0 8px; color: #fff; }
        .hero-card p { color: var(--txt-soft); margin: 0; }
        .stat-grid { display: grid; grid-template-columns: repeat(2, 1fr); gap: 14px; margin-bottom: 22px; }
        @media (min-width: 769px) { .stat-grid { grid-template-columns: repeat(4, 1fr); } }
        .stat-card { background: var(--card-glass); border: 1px solid var(--neon-border); border-left: 5px solid #8b5cf6; border-radius: 22px; padding: 18px 18px 16px; min-height: 150px; }
        .stat-card .si { font-size: 1.9rem; margin-bottom: 6px; }
        .stat-card .sv { font-size: 2rem; font-weight: 800; color: #fff; line-height: 1.1; word-break: break-word; }
        .stat-card .sl { color: var(--txt-soft); font-size: .82rem; letter-spacing: .05em; text-transform: uppercase; margin-top: 8px; }
        .c-green { border-left-color: #10b981; } .c-blue { border-left-color: #38bdf8; } .c-gold { border-left-color: #facc15; } .c-cyan { border-left-color: #22d3ee; } .c-pink { border-left-color: #f472b6; }

        .quick-box { background: var(--card-glass); border: 1px solid var(--neon-border); border-top: 4px solid #8b5cf6; border-radius: 26px; padding: 24px 20px; margin-bottom: 20px; }
        .quick-box h5 { color: #a78bfa; font-weight: 800; margin-bottom: 16px; }
        .qa { display: flex; align-items: center; gap: 14px; width: 100%; text-align: left; margin-bottom: 12px; padding: 16px 18px; border-radius: 20px; background: linear-gradient(135deg, rgba(88,50,180,.45), rgba(40,20,100,.55)); border: 1px solid rgba(139,92,246,.55); color: #fff; font-weight: 700; font-size: 1.02rem; }
        .qa:active { transform: scale(.985); }
        .qa .qi { font-size: 1.5rem; }
        .bot-link { color: var(--txt-soft); margin-top: 6px; }

        .page-head { display: flex; align-items: center; gap: 16px; margin-bottom: 20px; }
        .ph-ic { width: 66px; height: 66px; border-radius: 20px; background: rgba(60,30,140,.6); border: 1px solid var(--neon-border); display: flex; align-items: center; justify-content: center; font-size: 2rem; flex-shrink: 0; }
        .page-head h3 { margin: 0; font-weight: 800; color: #fff; }
        .page-head p { margin: 2px 0 0; color: var(--txt-soft); }
        .sec-title { color: #38bdf8; font-weight: 800; margin: 0; }
        .btn-grad { background: linear-gradient(135deg, #8b5cf6, #6366f1); color: #fff; border: none; border-radius: 14px; padding: 11px 18px; font-weight: 700; box-shadow: 0 6px 20px rgba(99,102,241,.4); }
        .btn-custom { background: linear-gradient(135deg, #7c3aed, #6366f1); color: white; border: none; border-radius: 12px; padding: 10px 18px; font-weight: 600; }
        .btn-custom:hover { color: #fff; filter: brightness(1.1); }

        .prod-wrap { background: var(--card-glass); border: 1px solid var(--neon-border); border-radius: 26px; padding: 16px; margin-bottom: 18px; }
        .prod-card { background: linear-gradient(135deg, #2a1466 0%, #3a1650 100%); border: 1px solid rgba(139,92,246,.5); border-radius: 22px; padding: 22px 20px; }
        .prod-icon { width: 62px; height: 52px; border-radius: 14px; background: linear-gradient(135deg, #fbbf24, #f97316); display: flex; align-items: center; justify-content: center; font-size: 1.7rem; margin-bottom: 18px; }
        .prod-label { color: var(--txt-soft); font-size: .78rem; letter-spacing: .1em; }
        .prod-name { font-size: 1.45rem; font-weight: 800; color: #fff; }
        .prod-meta { color: var(--txt-soft); font-size: .86rem; margin: 6px 0 14px; line-height: 1.7; }
        .status { font-weight: 800; font-size: .88rem; letter-spacing: .05em; white-space: nowrap; }
        .st-ok { color: #34d399; } .st-maint { color: #fbbf24; } .st-off { color: #f87171; }
        .pa { display: inline-flex; align-items: center; gap: 8px; padding: 10px 16px; margin: 0 8px 10px 0; border-radius: 14px; background: rgba(20,10,50,.6); border: 1px solid rgba(139,92,246,.5); color: #fff; font-weight: 700; }
        .pa.del { background: rgba(120,20,60,.35); border-color: #e11d48; color: #fda4af; }

        .gold-box { border: 1px solid rgba(250, 204, 21, 0.4); border-radius: 16px; padding: 20px; background: rgba(22, 13, 44, 0.95); margin-bottom: 20px; }
        .cyan-box { border: 1px solid rgba(56, 189, 248, 0.4); border-radius: 16px; padding: 20px; background: rgba(22, 13, 44, 0.95); margin-bottom: 20px; }
        .card { background: var(--card-glass); border: 1px solid var(--neon-border); border-radius: 20px; margin-bottom: 20px; }

        .modal-bg { position: fixed; top: 0; left: 0; width: 100vw; height: 100vh; background: rgba(0, 0, 0, 0.8); backdrop-filter: blur(10px); z-index: 2000; display: none; align-items: flex-start; justify-content: center; padding: 20px 14px; overflow-y: auto; }
        .modal-card { background: linear-gradient(160deg, #1f0f4d, #120730); border: 1px solid #8b5cf6; border-radius: 26px; padding: 24px 20px; width: 100%; max-width: 620px; margin: auto; box-shadow: 0 0 50px rgba(139, 92, 246, 0.4); }
        .modal-card h5 { color: #c4b5fd; font-weight: 800; }
        .plan-row { position: relative; border: 1px solid rgba(139,92,246,.5); border-radius: 18px; padding: 14px; margin-bottom: 12px; background: rgba(10,4,30,.55); }
        .plan-x { position: absolute; top: -10px; right: -6px; width: 28px; height: 28px; border-radius: 50%; border: 1px solid #e11d48; background: #3b0a1f; color: #fda4af; font-size: .8rem; line-height: 1; }

        .bc-group { background: var(--card-glass); border: 1px solid var(--neon-border); border-radius: 20px; margin-bottom: 14px; overflow: hidden; }
        .bc-group > summary { list-style: none; cursor: pointer; padding: 16px 18px; font-weight: 800; color: #fff; display: flex; justify-content: space-between; align-items: center; }
        .bc-group > summary::-webkit-details-marker { display: none; }
        .bc-group[open] > summary { border-bottom: 1px solid var(--neon-border); background: rgba(124,58,237,.18); }
        .bc-count { color: var(--txt-soft); font-weight: 600; font-size: .82rem; }
        .bc-all { display: flex; align-items: center; gap: 10px; padding: 12px 18px; background: rgba(0,0,0,.18); border-bottom: 1px solid rgba(147,51,234,.2); color: var(--txt-soft); font-size: .85rem; }
        .bc-row { padding: 14px 18px; border-bottom: 1px solid rgba(147,51,234,.15); }
        .bc-row:last-child { border-bottom: none; }
        .bc-prev { display: block; text-align: center; padding: 11px 12px; border-radius: 10px; font-weight: 700; color: #fff; margin-bottom: 10px; }
        .s-none { background: #3a4052; } .s-primary { background: #2f7de1; } .s-success { background: #2e9b44; } .s-danger { background: #c9424a; }
        .sw-wrap { display: flex; gap: 10px; align-items: center; }
        .sw { width: 38px; height: 38px; border-radius: 50%; border: 3px solid transparent; cursor: pointer; box-shadow: 0 0 0 1px rgba(255,255,255,.2); }
        .sw.on { border-color: #fff; box-shadow: 0 0 0 3px rgba(167,139,250,.8); }
        .sw.s-none { background: #5b6378; }
        .badge-st { padding: 4px 10px; border-radius: 10px; font-size: .75rem; font-weight: 700; }
        .b-verified { background: rgba(16,185,129,.2); color: #6ee7b7; } .b-pending, .b-awaiting_approval { background: rgba(250,204,21,.2); color: #fde68a; }
        .b-rejected, .b-cancelled, .b-expired { background: rgba(248,113,113,.2); color: #fca5a5; }
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
    <div class="sb-overlay" id="sbOverlay" onclick="toggleSidebar(false)"></div>
    <div class="sidebar" id="sidebar">
        <div class="sb-brand"><span style="font-size:1.4rem;">⚡</span><span>{{ bot_info.name }}</span></div>
        <a class="sb-link active" data-tab="dashboard" onclick="showTab('dashboard', this)"><span class="sb-ic g1">📈</span> Dashboard</a>
        <a class="sb-link" data-tab="products" onclick="showTab('products', this)"><span class="sb-ic g2">📦</span> Manage Product</a>
        <a class="sb-link" data-tab="keys" onclick="showTab('keys', this)"><span class="sb-ic g3">📜</span> Key Delivery Logs</a>
        <a class="sb-link" data-tab="keystock" onclick="showTab('keystock', this)"><span class="sb-ic g4">🔑</span> Manage Keys</a>
        <a class="sb-link" data-tab="pending_keys" onclick="showTab('pending_keys', this)"><span class="sb-ic g8">⏳</span> Pending Keys Queue</a>
        <a class="sb-link" data-tab="id_stock" onclick="showTab('id_stock', this)"><span class="sb-ic g4">🆔</span> ID Stock</a>
        <a class="sb-link" data-tab="links" onclick="showTab('links', this)"><span class="sb-ic g2">🔗</span> Product Links</a>
        <a class="sb-link" data-tab="members" onclick="showTab('members', this)"><span class="sb-ic g6">👥</span> Members &amp; Wallets</a>
        <a class="sb-link" data-tab="resellers" onclick="showTab('resellers', this)"><span class="sb-ic g5">🤝</span> Resellers</a>
        <a class="sb-link" data-tab="topups" onclick="showTab('topups', this)"><span class="sb-ic g8">💳</span> Top-ups</a>
        <a class="sb-link" data-tab="upi" onclick="showTab('upi', this)"><span class="sb-ic g3">💳</span> UPI Payment Setup</a>
        <a class="sb-link" data-tab="broadcast" onclick="showTab('broadcast', this)"><span class="sb-ic g8">📢</span> Broadcast</a>
        <a class="sb-link" data-tab="coupons" onclick="showTab('coupons', this)"><span class="sb-ic g4">🎟️</span> Coupon Manager</a>
        <a class="sb-link" data-tab="btncolors" onclick="showTab('btncolors', this)"><span class="sb-ic g1">🎨</span> Button Colors</a>
        <a class="sb-link" data-tab="store" onclick="showTab('store', this)"><span class="sb-ic g7">⚙️</span> Store Settings</a>
        <a class="sb-link" data-tab="security" onclick="showTab('security', this)"><span class="sb-ic g7">👤</span> My Profile</a>
        <a href="/logout" class="sb-link" style="color:#fca5a5;margin-top:10px;"><span class="sb-ic g8">🔒</span> Logout</a>
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
        <div class="topbar">
            <div class="d-flex align-items-center gap-3">
                <button class="menu-btn d-md-none" id="menuBtn" onclick="toggleSidebar()"><i id="menuIcon" class="fas fa-bars"></i></button>
                <div class="title-grad" style="font-size:1.5rem;">⚡ Bot Control Center</div>
            </div>
            <a href="/" class="crown-ring" title="Home"><span>👑</span></a>
        </div>

        <!-- 1️⃣ 📊 TAB: DASHBOARD -->
        <div class="tab-pane-content" id="tab-dashboard">
            <div class="hero-card">
                <div class="hero-kicker">⚡ BOT CONTROL CENTER</div>
                <h2>Welcome back.</h2>
                <p>Here's how <b style="color:#fff;">{{ bot_info.username }}</b> is doing right now.</p>
            </div>
            <div class="stat-grid">
                <div class="stat-card"><div class="si">👥</div><div class="sv">{{ stats.users }}</div><div class="sl">Members</div></div>
                <div class="stat-card c-green"><div class="si">💰</div><div class="sv">₹{{ stats.total_wallet }}</div><div class="sl">Wallet balance (all)</div></div>
                <div class="stat-card c-blue"><div class="si">📦</div><div class="sv">{{ stats.products_count }}</div><div class="sl">Catalog items</div></div>
                <div class="stat-card c-green"><div class="si">✅</div><div class="sv">{{ stats.orders }}</div><div class="sl">Orders fulfilled</div></div>
                <div class="stat-card c-gold"><div class="si">💎</div><div class="sv">₹{{ stats.revenue }}</div><div class="sl">Lifetime revenue</div></div>
                <div class="stat-card c-cyan"><div class="si">📅</div><div class="sv">₹{{ stats.today_deposit }}</div><div class="sl">Today's deposit</div></div>
                <div class="stat-card c-pink"><div class="si">⏳</div><div class="sv">{{ stats.awaiting }}</div><div class="sl">Awaiting review</div></div>
                <div class="stat-card c-blue"><div class="si">🔑</div><div class="sv">{{ stats.key_stock }}</div><div class="sl">Keys in stock</div></div>
            </div>
            <div class="quick-box">
                <h5>Quick Actions</h5>
                <button class="qa" onclick="showTab('products')"><span class="qi">📦</span> Manage Product</button>
                <button class="qa" onclick="showTab('keys')"><span class="qi">📜</span> Key Delivery Logs</button>
                <button class="qa" onclick="showTab('keystock')"><span class="qi">🔑</span> Manage Keys</button>
                <button class="qa" onclick="showTab('members')"><span class="qi">👥</span> Members &amp; Wallets</button>
                <button class="qa" onclick="showTab('topups')"><span class="qi">💳</span> Top-ups</button>
                <button class="qa" onclick="showTab('broadcast')"><span class="qi">📢</span> Broadcast</button>
                <button class="qa" onclick="showTab('btncolors')"><span class="qi">🎨</span> Button Colors</button>
                <button class="qa" onclick="showTab('store')"><span class="qi">⚙️</span> Store Settings</button>
                <div class="bot-link">🔗 Bot: <a href="https://t.me/{{ bot_info.username.replace('@','') }}" target="_blank">{{ bot_info.username }}</a></div>
            </div>
        </div>

        <!-- 2️⃣ 📦 TAB: MANAGE PRODUCTS -->
        <div class="tab-pane-content" id="tab-products" style="display:none;">
            <div class="page-head">
                <div class="ph-icon ph-ic">📦</div>
                <div><h3>Manage Product</h3><p>Add products, plans, pricing and keys.</p></div>
            </div>
            <div class="d-flex justify-content-between align-items-center mb-3">
                <h5 class="sec-title">📦 Manage Products</h5>
                <button class="btn-grad" onclick="openProductModal()">＋ Add Product</button>
            </div>
            {% for p in products %}
            <div class="prod-wrap">
                <div class="prod-card">
                    <div class="prod-icon">{{ p.icon }}</div>
                    <div class="prod-label">PRODUCT</div>
                    <div class="d-flex justify-content-between align-items-center gap-2">
                        <div class="prod-name">{{ p.name }}</div>
                        {% if p.maintenance %}<span class="status st-maint">● MAINTENANCE</span>
                        {% elif p.stock_out %}<span class="status st-off">● DISABLED</span>
                        {% else %}<span class="status st-ok">● ACTIVE</span>{% endif %}
                    </div>
                    <div class="prod-meta">
                        {{ p.category }}<br>
                        {% for pl in p.prices %}🕒 {{ pl[0]|replace('_', ' ') }} · ₹{{ '%g'|format(pl[1]) }} · stock {{ p.stock.get(pl[0], 0) }}<br>{% else %}No plans yet{% endfor %}
                    </div>
                    <div>
                        <button class="pa" onclick="openProductModal({{ loop.index0 }})">✏️ Edit</button>
                        <button class="pa" onclick="toggleProduct('{{ p.prod_key }}', 'stock_out')">{% if p.stock_out %}▶️ Enable{% else %}⏸️ Disable{% endif %}</button>
                        <button class="pa" onclick="toggleProduct('{{ p.prod_key }}', 'maintenance')">🛠️ {% if p.maintenance %}End Maintenance{% else %}Maintenance{% endif %}</button>
                        <button class="pa del" onclick="deleteProduct('{{ p.prod_key }}')">🗑️ Delete</button>
                    </div>
                </div>
            </div>
            {% else %}
            <div class="card p-4 text-center text-muted">No products yet. Tap “Add Product” to create the first one.</div>
            {% endfor %}
        </div>

        <!-- ➕ ADD / EDIT PRODUCT MODAL -->
        <div class="modal-bg" id="prodModal">
            <div class="modal-card">
                <div class="d-flex justify-content-between align-items-center mb-3">
                    <h5 class="m-0" id="pm_title">＋ Add Product</h5>
                    <button class="btn-close btn-close-white" onclick="closeProductModal()"></button>
                </div>
                <label class="small mb-1">Product Name</label>
                <input type="text" id="pm_name" class="form-control mb-3" placeholder="e.g. Netflix Premium">
                <div class="row g-2 mb-3">
                    <div class="col-8">
                        <label class="small mb-1">Category (where it shows in the bot)</label>
                        <select id="pm_cat" class="form-select">
                            <option value="non_root">Non-Root Mobile</option><option value="root">Root Mobile</option>
                            <option value="ios">iOS Panels</option><option value="pc">PC Panels</option><option value="likes">8 Level ID / Likes</option>
                        </select>
                    </div>
                    <div class="col-4"><label class="small mb-1">Icon</label><input type="text" id="pm_icon" class="form-control" placeholder="⚡"></div>
                </div>
                <label class="small mb-1">Channel / Download Link (optional)</label>
                <input type="text" id="pm_link" class="form-control mb-3" placeholder="e.g. https://t.me/yourchannel">
                <hr style="border-color:rgba(147,51,234,.4);">
                <div class="fw-bold mb-2" style="color:#c4b5fd;">＋ Plans</div>
                <div id="pm_plans"></div>
                <button type="button" class="pa" onclick="addPlanRow()">＋ Add More Plan</button>
                <div class="small text-muted mb-3">Reseller price is automatic (60% of the regular price). Keys are optional — you can add more later in “Manage Keys”.</div>
                <button class="btn-grad w-100 py-3" id="pm_submit" onclick="submitProduct()">＋ Add Product</button>
            </div>
        </div>

        <!-- 3️⃣ 🗝️ TAB: MANAGE KEYS & SALES ANALYTICS -->
        <div class="tab-pane-content" id="tab-keys" style="display:none;">
            <div class="card p-4">
                <h5>📜 Key Delivery Logs</h5>
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
            <div class="page-head">
                <div class="ph-ic">📢</div>
                <div><h3>Broadcast</h3><p>Send messages to all users.</p></div>
            </div>

            <div class="gcard">
                <span class="bc-premium">✨ PREMIUM</span>
                <h5 class="gt" style="margin-top:0;">Broadcast to Your Users</h5>

                <label class="small mb-1">Audience</label>
                <div class="bc-aud" id="bc_aud">
                    <button type="button" class="chip-btn on" data-aud="all" data-n="{{ bc_counts.all }}" onclick="bcAud(this)">👥 All ({{ bc_counts.all }})</button>
                    <button type="button" class="chip-btn" data-aud="customers" data-n="{{ bc_counts.customers }}" onclick="bcAud(this)">🛒 Buyers ({{ bc_counts.customers }})</button>
                    <button type="button" class="chip-btn" data-aud="non_buyers" data-n="{{ bc_counts.non_buyers }}" onclick="bcAud(this)">🆕 Not bought yet ({{ bc_counts.non_buyers }})</button>
                    <button type="button" class="chip-btn" data-aud="resellers" data-n="{{ bc_counts.resellers }}" onclick="bcAud(this)">👑 Resellers ({{ bc_counts.resellers }})</button>
                </div>

                <label class="small mb-1">Message</label>
                <div class="bc-tools">
                    <button type="button" onclick="bcWrap('b')"><b>B</b></button>
                    <button type="button" onclick="bcWrap('i')"><i>I</i></button>
                    <button type="button" onclick="bcWrap('code')">&lt;/&gt;</button>
                    <button type="button" onclick="bcLink()">🔗 Link</button>
                    <button type="button" onclick="bcEmoji('🔥')">🔥</button>
                    <button type="button" onclick="bcEmoji('🎁')">🎁</button>
                    <button type="button" onclick="bcEmoji('⚡')">⚡</button>
                </div>
                <textarea id="bc_msg" class="form-control" rows="6" placeholder="Message... (HTML supported: bold, italic, code, links)" oninput="bcPreview()"></textarea>
                <div class="set-note"><span id="bc_count">0</span> characters · with media, text over 1024 characters is sent as a separate message.</div>

                <label class="small mb-1 mt-3">Attach media (optional)</label>
                <div class="media-grid">
                    <label class="media-tile" id="mt_image"><span class="mi">🖼️</span>Image<input type="file" id="bc_image" accept="image/*" hidden onchange="bcPick('image', this)"></label>
                    <label class="media-tile" id="mt_video"><span class="mi">🎬</span>Video<input type="file" id="bc_video" accept="video/*" hidden onchange="bcPick('video', this)"></label>
                    <label class="media-tile" id="mt_voice"><span class="mi">🎙️</span>Voice<input type="file" id="bc_voice" accept="audio/*,.ogg,.oga,.opus" hidden onchange="bcPick('voice', this)"></label>
                </div>
                <div class="media-file" id="bc_file"><span id="bc_file_name"></span><button type="button" class="btn-danger-soft" onclick="bcClearMedia()">✕ Remove</button></div>
                <div class="set-note mb-2">Max 50 MB. Voice: .ogg (Opus) plays as a voice note; mp3/m4a are sent as audio.</div>

                <div class="small mb-1">Preview</div>
                <div class="tg-preview"><div class="tg-bubble" id="bc_prev"><span style="color:#8d96b5;">Your message preview appears here...</span></div></div>

                <button class="btn-send" id="bc_send" onclick="sendBroadcast()">🚀 Send to All {{ bc_counts.all }} Users</button>
                <button class="btn-ghost" onclick="sendBroadcast(true)">📨 Send Test to Me First</button>
            </div>

            <div class="gcard blue">
                <h5 class="gt">📜 Broadcast History</h5>
                <div id="bc_hist"><div class="empty-note">No broadcasts yet.</div></div>
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
                <h5>💳 Deposit Requests (latest 30)</h5>
                <p class="text-muted small">Payments waiting for approval are approved or rejected from the Telegram message the bot sends you.</p>
                <div class="table-responsive mt-2">
                    <table class="table align-middle">
                        <thead><tr><th>Time</th><th>User ID</th><th>Amount</th><th>Status</th><th>Order</th></tr></thead>
                        <tbody>
                            {% for t in topups %}
                            <tr>
                                <td class="small">{{ t[0] }}</td>
                                <td><code>{{ t[1] }}</code></td>
                                <td>₹{{ '%.2f'|format(t[2] or 0) }}</td>
                                <td><span class="badge-st b-{{ t[3] }}">{{ t[3] }}</span></td>
                                <td class="small"><code>{{ t[4] }}</code></td>
                            </tr>
                            {% else %}
                            <tr><td colspan="5" class="text-center text-muted">No deposit requests yet.</td></tr>
                            {% endfor %}
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
            <div class="page-head">
                <div class="ph-ic">👤</div>
                <div><h3>My Profile</h3><p>Your owner account, password and sessions.</p></div>
            </div>

            <div class="owner-card">
                <div class="crown-big">👑</div>
                <div>
                    <h4>Owner Account</h4>
                    <span class="pill">🔐 Full Access</span>
                    <div class="set-note mt-2">{{ bot_info.name }} · {{ bot_info.username }}</div>
                    <div class="set-note">Admin Telegram ID: <code>{{ admin_id }}</code></div>
                </div>
            </div>

            <div class="gcard">
                <h5 class="gt">🔑 Change Panel Password</h5>
                <div class="mb-3"><label class="small mb-1">Current Password</label><input type="password" id="pf_cur" class="form-control" autocomplete="current-password"></div>
                <div class="mb-1"><label class="small mb-1">New Password</label><input type="password" id="pf_new" class="form-control" autocomplete="new-password" oninput="pwdMeter()"></div>
                <div class="meter"><i id="pf_bar"></i></div>
                <div class="set-note mb-3" id="pf_hint">Use at least 6 characters; longer with numbers and symbols is stronger.</div>
                <div class="mb-3"><label class="small mb-1">Confirm New Password</label><input type="password" id="pf_conf" class="form-control" autocomplete="new-password"></div>
                <button class="btn-grad" onclick="changePwd()">💾 Change Password</button>
                <div class="set-note mt-2">After a password change every other device is logged out.</div>
            </div>

            <div class="gcard">
                <h5 class="gt">🛡️ Security &amp; Sessions</h5>
                <div class="set-note mb-3">Signs out this panel on every phone and computer, including this one. You will need your password to sign in again.</div>
                <button class="btn-danger-soft" onclick="logoutAll()">🌐 Logout All Devices</button>
            </div>

            <div class="gcard blue">
                <h5 class="gt">🧰 Admin Tools</h5>
                <div class="d-flex gap-2 flex-wrap">
                    <a class="chip-btn text-decoration-none" href="/export/orders.csv">⬇️ Export all orders (CSV)</a>
                    <a class="chip-btn text-decoration-none" href="/export/users.csv">⬇️ Export all users (CSV)</a>
                </div>
            </div>
        </div>

        <!-- 1️⃣4️⃣ ⚙️ TAB: STORE SETTINGS -->
        <div class="tab-pane-content" id="tab-store" style="display:none;">
            <div class="page-head">
                <div class="ph-ic">⚙️</div>
                <div><h3>Store Settings</h3><p>Storefront branding and limits.</p></div>
            </div>

            <div class="gcard blue">
                <h5 class="gt">Branding</h5>
                <div class="mb-3"><label class="small mb-1">Shop Name</label><input type="text" id="st_name" class="form-control" value="{{ store.shop_name }}" placeholder="Your shop name"></div>
                <div class="mb-3"><label class="small mb-1">Tagline</label><input type="text" id="st_tag" class="form-control" value="{{ store.tagline }}" placeholder="Powered by ..."></div>
                <div class="mb-3"><label class="small mb-1">Support Username</label><input type="text" id="st_supp" class="form-control" value="{{ store.support_username }}" placeholder="@YourSupportHandle"></div>
                <div class="mb-3"><label class="small mb-1">Check All Update Link</label><input type="text" id="st_upd" class="form-control" value="{{ store.update_link }}" placeholder="https://t.me/yourchannel"><div class="set-note">Shows a "Check All Updates" button on the bot's main menu.</div></div>
                <div class="mb-3"><label class="small mb-1">How To Use Bot Link</label><input type="text" id="st_how" class="form-control" value="{{ store.how_to_use_link }}" placeholder="https://t.me/yourchannel/3"></div>
                <button class="btn-grad" onclick="saveBranding()">💾 Save Branding</button>
            </div>

            <div class="gcard blue">
                <h5 class="gt">💰 Deposits, Referral &amp; Spin</h5>
                <div class="mb-3"><label class="small mb-1">Min Deposit (₹)</label><input type="number" step="any" min="0" id="st_min" class="form-control" value="{{ store.min_deposit }}"></div>
                <div class="mb-3"><label class="small mb-1">Max Deposit (₹)</label><input type="number" step="any" min="0" id="st_max" class="form-control" value="{{ store.max_deposit }}"></div>
                <div class="mb-3"><label class="small mb-1">Deposit QR Expiry (minutes)</label><input type="number" step="1" min="1" id="st_qr" class="form-control" value="{{ store.qr_expiry_minutes }}"></div>
                <div class="mb-3"><label class="small mb-1">Referral Bonus (₹)</label><input type="number" step="any" min="0" id="st_ref" class="form-control" value="{{ store.referral_bonus }}"><div class="set-note">Credited to the referrer's wallet when a new user joins with their link. 0 = off.</div></div>
                <div class="mb-3"><label class="small mb-1">Daily Gift (Spin) — Min ₹</label><input type="number" step="any" min="0" id="st_smin" class="form-control" value="{{ store.spin_min }}"></div>
                <div class="mb-3"><label class="small mb-1">Daily Gift (Spin) — Max ₹</label><input type="number" step="any" min="0" id="st_smax" class="form-control" value="{{ store.spin_max }}"></div>
                <div class="mb-3"><label class="small mb-1">Spin Cooldown (hours)</label><input type="number" step="any" min="0" id="st_scool" class="form-control" value="{{ store.spin_cooldown_hours }}"></div>
                <div class="mb-3"><label class="small mb-1">Spin Cash Expiry (hours)<span class="set-new">NEW</span></label><input type="number" step="any" min="0" id="st_sexp" class="form-control" value="{{ store.spin_expiry_hours }}"><div class="set-note">The customer must buy a key within this time after winning, otherwise the spin cash is removed from the wallet and they get a notification. 0 = never expires.</div></div>
                <button class="btn-grad" onclick="saveLimits()">💾 Save</button>
            </div>
        </div>

        <!-- 1️⃣5️⃣ 🔑 TAB: MANAGE KEYS (add stock) -->
        <div class="tab-pane-content" id="tab-keystock" style="display:none;">
            <div class="page-head">
                <div class="ph-ic">🔑</div>
                <div><h3>Manage Keys</h3><p>Track live sales, key generation analytics, and product-wise revenue.</p></div>
            </div>

            <!-- 📊 Key Delivery Overview -->
            <div class="gcard">
                <h5 class="gt">🔑 Key Delivery Overview</h5>
                <div class="ov-grid">
                    <div class="ov"><div class="oi">🛒</div><div class="ov-v">{{ key_stats.total }}</div><div class="ov-l">Total generated / sold</div></div>
                    <div class="ov"><div class="oi">📈</div><div class="ov-v">{{ key_stats.today }}</div><div class="ov-l">Sold today</div></div>
                    <div class="ov"><div class="oi">💎</div><div class="ov-v">₹{{ key_stats.today_rev }}</div><div class="ov-l">Today's revenue</div></div>
                    <div class="ov"><div class="oi">📦</div><div class="ov-v">{{ key_stats.live_products }}</div><div class="ov-l">Live products</div></div>
                    <div class="ov"><div class="oi">⏳</div><div class="ov-v">{{ key_stats.pending }}</div><div class="ov-l">Pending queue</div></div>
                    <div class="ov"><div class="oi">🗝️</div><div class="ov-v">{{ stats.key_stock }}</div><div class="ov-l">Keys in stock</div></div>
                </div>
                <div class="d-flex gap-2 mt-3 flex-wrap">
                    <button class="chip-btn" onclick="document.getElementById('addKeysAnchor').scrollIntoView({behavior:'smooth'})">＋ Add Keys to Stock</button>
                    <button class="chip-btn" onclick="showTab('pending_keys')">⏳ Pending Keys Queue</button>
                </div>
            </div>

            <!-- 📡 Live Key Status -->
            <div class="gcard">
                <h5 class="gt">📡 Live Key Status <span class="live-dot">LIVE</span></h5>
                <div class="set-note mb-2">Search a Telegram ID, @username, product or the key itself. Use it to check a customer's complaint: you see exactly which key was delivered, when, for what price and how it was paid.</div>
                <div class="lk-filters">
                    <input type="text" id="lk_q" class="form-control" placeholder="🔍 Telegram ID, @user, product or key..." oninput="lkDebounce()">
                    <select id="lk_prod" class="form-select" onchange="lkReset()"><option value="">All products</option></select>
                    <select id="lk_status" class="form-select" onchange="lkReset()">
                        <option value="">All status</option><option value="delivered">Delivered</option><option value="pending">Pending</option>
                    </select>
                    <button class="chip-btn" id="lk_today" onclick="lkToggleToday()">📅 Today</button>
                </div>
                <div class="lk-user" id="lk_user"></div>
                <div class="lk-sum" id="lk_sum"></div>
                <div id="lk_rows"><div class="empty-note">Loading live key status...</div></div>
                <div class="lk-pager">
                    <span id="lk_info">—</span>
                    <span><button id="lk_prev" onclick="lkGo(-1)">« Prev</button> <span id="lk_pg" class="mx-2">Page 1 of 1</span> <button id="lk_next" onclick="lkGo(1)">Next »</button></span>
                </div>
                <div class="set-note mt-2" id="lk_upd">Auto-refreshes every 15 seconds while this page is open.</div>
            </div>

            <!-- 📈 Today's Key Sales by Product -->
            <div class="gcard">
                <h5 class="gt">📈 Today's Key Sales by Product <span class="pill">Total: {{ key_stats.today }} keys · ₹{{ key_stats.today_rev }}</span></h5>
                <div class="table-responsive">
                    <table class="table align-middle mb-0">
                        <thead><tr><th>Product</th><th>Keys sold today</th><th>Amount</th></tr></thead>
                        <tbody>
                            {% for r in today_by_product %}
                            <tr><td>{{ r[0] }}</td><td><b>{{ r[1] }}</b></td><td>₹{{ '%.2f'|format(r[2]) }}</td></tr>
                            {% else %}
                            <tr><td colspan="3" class="text-center text-muted">No sales yet today.</td></tr>
                            {% endfor %}
                        </tbody>
                    </table>
                </div>
            </div>

            <!-- 📦 Lifetime Orders By Product -->
            <div class="gcard">
                <h5 class="gt">📦 Lifetime Orders By Product</h5>
                <div class="table-responsive">
                    <table class="table align-middle mb-0">
                        <thead><tr><th>Product</th><th>Status</th><th>Total keys sold</th><th>Revenue</th><th>In stock</th><th>Action</th></tr></thead>
                        <tbody>
                            {% for p in products %}
                            <tr>
                                <td style="min-width:120px;font-weight:700;text-transform:uppercase;">{{ p.name }}</td>
                                <td>{% if p.maintenance %}<span class="pill a">Maintenance</span>{% elif p.stock_out %}<span class="pill r">Stock out</span>{% else %}<span class="pill g">Active</span>{% endif %}</td>
                                <td>{{ lifetime.get(p.name, [0, 0])[0] }}</td>
                                <td>₹{{ '%.2f'|format(lifetime.get(p.name, [0, 0])[1]) }}</td>
                                <td>{{ p.stock.values()|sum }}</td>
                                <td style="white-space:nowrap;">
                                    <button class="chip-btn" onclick="showTab('products'); openProductModal({{ loop.index0 }})">✏️ Configure Product</button>
                                    <button class="chip-btn" onclick="lkAddKeysFor({{ loop.index0 }})">＋ Keys</button>
                                </td>
                            </tr>
                            {% else %}
                            <tr><td colspan="6" class="text-center text-muted">No products yet.</td></tr>
                            {% endfor %}
                        </tbody>
                    </table>
                </div>
            </div>

            <!-- ＋ Add key stock (unchanged functionality) -->
            <div id="addKeysAnchor"></div>
            <div class="gcard blue">
                <h5 class="gt">＋ Add Key Stock</h5>
                <div class="row g-3">
                    <div class="col-md-6"><label class="small mb-1">Product</label><select id="ks_prod" class="form-select" onchange="fillKeyPlans()"></select></div>
                    <div class="col-md-6"><label class="small mb-1">Plan</label><select id="ks_plan" class="form-select"></select></div>
                    <div class="col-12"><label class="small mb-1">Keys (one per line)</label><textarea id="ks_keys" class="form-control" rows="5" placeholder="KEY1&#10;KEY2"></textarea></div>
                    <div class="col-12"><button class="btn-grad w-100" onclick="addKeys()">＋ Add Keys to Stock</button></div>
                </div>
            </div>
            <div class="gcard blue">
                <h5 class="gt">Stock available</h5>
                <div class="table-responsive">
                    <table class="table align-middle mb-0">
                        <thead><tr><th>Product</th><th>Plan</th><th>Keys left</th></tr></thead>
                        <tbody>
                            {% for p in products %}{% for pl in p.prices %}
                            <tr><td>{{ p.name }}</td><td>{{ pl[0]|replace('_', ' ') }}</td><td><strong>{{ p.stock.get(pl[0], 0) }}</strong></td></tr>
                            {% endfor %}{% else %}
                            <tr><td colspan="3" class="text-center text-muted">No products yet.</td></tr>
                            {% endfor %}
                        </tbody>
                    </table>
                </div>
            </div>
        </div>

        <!-- 1️⃣6️⃣ 🎨 TAB: BUTTON COLORS -->
        <div class="tab-pane-content" id="tab-btncolors" style="display:none;">
            <div class="page-head">
                <div class="ph-ic">🎨</div>
                <div><h3>Button Colors</h3><p>Pick a color for every bot button. Tap a group to open it.</p></div>
            </div>
            <div class="d-flex gap-2 mb-3 flex-wrap">
                <button class="btn-grad" onclick="saveButtonColors()">💾 Save Colors</button>
                <button class="pa" style="margin:0;" onclick="resetButtonColors()">↩️ Reset all to default</button>
            </div>
            {% for g in button_groups %}
            <details class="bc-group" {% if loop.first %}open{% endif %}>
                <summary><span>{{ g.title }}</span><span class="bc-count">{{ g['items']|length }} buttons ▾</span></summary>
                <div class="bc-all">
                    <span>Set whole group:</span>
                    <div class="sw-wrap">
                        <span class="sw s-none" onclick="setGroup(this, 'none')" title="Grey"></span>
                        <span class="sw s-primary" onclick="setGroup(this, 'primary')" title="Blue"></span>
                        <span class="sw s-success" onclick="setGroup(this, 'success')" title="Green"></span>
                        <span class="sw s-danger" onclick="setGroup(this, 'danger')" title="Red"></span>
                    </div>
                </div>
                {% for b in g['items'] %}
                <div class="bc-row" data-key="{{ b.key }}" data-style="{{ b.style }}" data-default="{{ b.default }}">
                    <span class="bc-prev s-{{ b.style }}">{{ b.label }}</span>
                    <div class="sw-wrap">
                        <span class="sw s-none {% if b.style == 'none' %}on{% endif %}" onclick="pickColor(this, 'none')" title="Grey"></span>
                        <span class="sw s-primary {% if b.style == 'primary' %}on{% endif %}" onclick="pickColor(this, 'primary')" title="Blue"></span>
                        <span class="sw s-success {% if b.style == 'success' %}on{% endif %}" onclick="pickColor(this, 'success')" title="Green"></span>
                        <span class="sw s-danger {% if b.style == 'danger' %}on{% endif %}" onclick="pickColor(this, 'danger')" title="Red"></span>
                    </div>
                </div>
                {% endfor %}
            </details>
            {% endfor %}
            <button class="btn-grad w-100 py-3 mt-2" onclick="saveButtonColors()">💾 Save Colors</button>
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
        const PRODUCTS = {% if session.get('admin_logged') %}{{ products|tojson }}{% else %}[]{% endif %};
        function toggleSidebar(force) {
            const sb = document.getElementById('sidebar');
            if (!sb) return;
            const open = (force === undefined) ? !sb.classList.contains('active') : force;
            sb.classList.toggle('active', open);
            document.getElementById('sbOverlay').classList.toggle('show', open);
            const ic = document.getElementById('menuIcon');
            if (ic) ic.className = open ? 'fas fa-xmark' : 'fas fa-bars';
        }
        function showTab(t, el) {
            document.querySelectorAll('.tab-pane-content').forEach(d => d.style.display = 'none');
            let target = document.getElementById('tab-' + t);
            if (target) target.style.display = 'block';
            document.querySelectorAll('.sb-link').forEach(l => l.classList.toggle('active', l.dataset.tab === t));
            if (window.innerWidth < 769) toggleSidebar(false);
            window.scrollTo(0, 0);
            if (t === 'keystock' && typeof lkLoad === 'function') lkLoad();
            if (t === 'broadcast' && typeof bcHistLoad === 'function') bcHistLoad();
        }
        function filterTable(inputId, tableId) {
            let filter = document.getElementById(inputId).value.toLowerCase();
            let rows = document.querySelectorAll('#' + tableId + ' tbody tr');
            rows.forEach(r => r.style.display = r.innerText.toLowerCase().includes(filter) ? '' : 'none');
        }
        let editingKey = null;
        function addPlanRow(plan) {
            const wrap = document.getElementById('pm_plans');
            const row = document.createElement('div');
            row.className = 'plan-row';
            row.dataset.old = plan ? plan.name : '';
            row.innerHTML =
                '<button type="button" class="plan-x" onclick="this.parentElement.remove()">✕</button>' +
                '<div class="row g-2">' +
                '<div class="col-6"><input type="text" class="form-control pl-name" maxlength="22" placeholder="e.g. 1 Day / 30 Days"></div>' +
                '<div class="col-6"><input type="number" step="0.01" min="0" class="form-control pl-price" placeholder="Price (₹)"></div>' +
                '<div class="col-12"><textarea class="form-control pl-keys" rows="2" placeholder="Add keys for this plan (one per line, optional)"></textarea></div>' +
                '<div class="col-12 small text-muted pl-stock"></div></div>';
            if (plan) {
                row.querySelector('.pl-name').value = plan.name;
                row.querySelector('.pl-price').value = plan.price;
                row.querySelector('.pl-stock').textContent = 'Keys in stock: ' + (plan.stock || 0);
            }
            wrap.appendChild(row);
        }
        function openProductModal(idx) {
            const isEdit = (idx !== undefined && idx !== null);
            const p = isEdit ? PRODUCTS[idx] : null;
            editingKey = isEdit ? p.prod_key : null;
            document.getElementById('pm_title').innerText = isEdit ? '✏️ Edit Product' : '＋ Add Product';
            document.getElementById('pm_submit').innerText = isEdit ? '💾 Save Changes' : '＋ Add Product';
            document.getElementById('pm_name').value = isEdit ? p.name : '';
            document.getElementById('pm_cat').value = isEdit ? (p.category || 'non_root') : 'non_root';
            document.getElementById('pm_icon').value = isEdit ? (p.icon || '') : '';
            document.getElementById('pm_link').value = isEdit ? (p.download_link || '') : '';
            document.getElementById('pm_plans').innerHTML = '';
            if (isEdit && p.prices.length) {
                p.prices.forEach(pl => addPlanRow({name: pl[0], price: pl[1], stock: (p.stock || {})[pl[0]] || 0}));
            } else {
                addPlanRow();
            }
            document.getElementById('prodModal').style.display = 'flex';
        }
        function closeProductModal() { document.getElementById('prodModal').style.display = 'none'; }
        function submitProduct() {
            const name = document.getElementById('pm_name').value.trim();
            if (!name) return alert('Enter the product name!');
            const plans = [];
            for (const r of document.querySelectorAll('#pm_plans .plan-row')) {
                const pn = r.querySelector('.pl-name').value.trim();
                const pr = parseFloat(r.querySelector('.pl-price').value);
                const keys = r.querySelector('.pl-keys').value;
                if (!pn && isNaN(pr) && !keys.trim()) continue;
                if (!pn || isNaN(pr) || pr < 0) return alert('Every plan needs a name and a price!');
                plans.push({old: r.dataset.old || '', name: pn, price: pr, keys: keys});
            }
            if (!plans.length) return alert('Add at least one plan!');
            const body = {
                prod_key: editingKey, name: name,
                category: document.getElementById('pm_cat').value,
                icon: document.getElementById('pm_icon').value.trim(),
                download_link: document.getElementById('pm_link').value.trim(),
                plans: plans
            };
            fetch(editingKey ? '/api/product/update' : '/api/product/save', {
                method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)
            }).then(r => r.json()).then(d => { alert(d.message); if (!d.error) location.reload(); });
        }
        function toggleProduct(k, field) {
            fetch('/api/product/toggle', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({prod_key: k, field: field}) })
            .then(r => r.json()).then(d => { alert(d.message); location.reload(); });
        }
        function deleteProduct(k) {
            if(!confirm('Delete this product?')) return;
            fetch('/api/product/delete', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({prod_key: k}) })
            .then(r => r.json()).then(d => { alert(d.message); location.reload(); });
        }
        function fillKeyPlans() {
            const sel = document.getElementById('ks_prod');
            const planSel = document.getElementById('ks_plan');
            if (!sel || !planSel) return;
            planSel.innerHTML = '';
            const p = PRODUCTS.find(x => x.prod_key === sel.value);
            (p ? p.prices : []).forEach(pl => {
                const o = document.createElement('option');
                o.value = pl[0]; o.textContent = pl[0].replace(/_/g, ' ');
                planSel.appendChild(o);
            });
        }
        (function initKeyStock() {
            const sel = document.getElementById('ks_prod');
            if (!sel) return;
            PRODUCTS.forEach(p => { const o = document.createElement('option'); o.value = p.prod_key; o.textContent = p.name; sel.appendChild(o); });
            fillKeyPlans();
        })();
        function addKeys() {
            const prod = document.getElementById('ks_prod').value;
            const plan = document.getElementById('ks_plan').value;
            const keys = document.getElementById('ks_keys').value;
            if (!prod || !plan || !keys.trim()) return alert('Choose a product, a plan and paste the keys!');
            fetch('/api/keys/add', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({prod_key: prod, plan: plan, keys: keys}) })
            .then(r => r.json()).then(d => { alert(d.message); if (!d.error) location.reload(); });
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
        function applyColor(row, style) {
            row.dataset.style = style;
            row.querySelector('.bc-prev').className = 'bc-prev s-' + style;
            row.querySelectorAll('.sw').forEach(s => s.classList.toggle('on', s.classList.contains('s-' + style)));
        }
        function pickColor(el, style) { applyColor(el.closest('.bc-row'), style); }
        function setGroup(el, style) {
            el.closest('.bc-group').querySelectorAll('.bc-row').forEach(r => applyColor(r, style));
        }
        function resetButtonColors() {
            if (!confirm('Reset every button to its default color? (not saved until you press Save)')) return;
            document.querySelectorAll('.bc-row').forEach(r => applyColor(r, r.dataset.default || 'none'));
        }
        function saveButtonColors() {
            let styles = {};
            document.querySelectorAll('.bc-row').forEach(r => { styles[r.dataset.key] = r.dataset.style; });
            fetch('/api/buttons/save', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({styles: styles}) })
            .then(r => r.json()).then(d => alert(d.message));
        }
        /* ===== Upgrade JS: toast, live key status, store settings, broadcast, profile ===== */
        function toast(msg, ok) {
            let box = document.getElementById('toastBox');
            if (!box) { box = document.createElement('div'); box.id = 'toastBox'; document.body.appendChild(box); }
            const t = document.createElement('div');
            t.className = 'toast-i' + (ok === false ? ' err' : '');
            t.textContent = msg;
            box.appendChild(t);
            setTimeout(() => t.remove(), 3500);
        }
        function esc(v) {
            return String(v === null || v === undefined ? '' : v).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
        }
        function postJSON(url, body) {
            return fetch(url, { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body || {}) })
                .then(r => r.json().then(d => { d._ok = r.ok; return d; }));
        }
        function copyText(txt) {
            const done = () => toast('Copied!');
            if (navigator.clipboard && navigator.clipboard.writeText) { navigator.clipboard.writeText(txt).then(done, done); return; }
            const ta = document.createElement('textarea'); ta.value = txt; document.body.appendChild(ta); ta.select();
            try { document.execCommand('copy'); } catch (e) {}
            ta.remove(); done();
        }

        /* ---- Live key status ---- */
        let lkPage = 1, lkPages = 1, lkToday = false, lkTimer = null, lkDeb = null, lkProdsFilled = false;
        function lkFillProducts() {
            if (lkProdsFilled) return;
            const sel = document.getElementById('lk_prod');
            if (!sel) return;
            PRODUCTS.forEach(p => { const o = document.createElement('option'); o.value = p.name; o.textContent = p.name; sel.appendChild(o); });
            lkProdsFilled = true;
        }
        function lkDebounce() { clearTimeout(lkDeb); lkDeb = setTimeout(lkReset, 350); }
        function lkReset() { lkPage = 1; lkLoad(); }
        function lkGo(d) { lkPage = Math.min(Math.max(1, lkPage + d), lkPages); lkLoad(); }
        function lkToggleToday() {
            lkToday = !lkToday;
            document.getElementById('lk_today').classList.toggle('on', lkToday);
            lkReset();
        }
        function lkAgo(iso) {
            if (!iso) return '';
            const s = Math.max(0, Math.floor((Date.now() - Date.parse(iso)) / 1000));
            if (s < 60) return s + 's ago';
            if (s < 3600) return Math.floor(s / 60) + ' min ago';
            if (s < 86400) return Math.floor(s / 3600) + ' h ago';
            return Math.floor(s / 86400) + ' d ago';
        }
        function lkAddKeysFor(idx) {
            const p = PRODUCTS[idx];
            const sel = document.getElementById('ks_prod');
            if (p && sel) { sel.value = p.prod_key; fillKeyPlans(); }
            document.getElementById('addKeysAnchor').scrollIntoView({behavior: 'smooth'});
        }
        function lkLoad() {
            lkFillProducts();
            const qs = new URLSearchParams({
                q: document.getElementById('lk_q').value.trim(),
                product: document.getElementById('lk_prod').value,
                status: document.getElementById('lk_status').value,
                today: lkToday ? '1' : '0',
                page: lkPage
            });
            fetch('/api/keys/live?' + qs.toString()).then(r => r.json()).then(d => {
                if (d.message) { document.getElementById('lk_rows').innerHTML = '<div class="empty-note">' + esc(d.message) + '</div>'; return; }
                lkPages = d.pages; lkPage = d.page;
                const u = document.getElementById('lk_user');
                if (d.user) {
                    u.style.display = 'block';
                    u.innerHTML = '<div class="uname">👤 ' + esc(d.user.name) + ' <span class="pill b">' + esc(d.user.role) + '</span></div>'
                        + '<div class="umeta">ID <code>' + esc(d.user.id) + '</code> · ' + esc(d.user.username) + ' · joined ' + esc(d.user.joined) + '</div>'
                        + '<div class="umeta">Wallet ₹' + esc(d.user.balance) + ' · lifetime spent ₹' + esc(d.user.spent) + ' · ' + esc(d.user.orders) + ' orders</div>';
                } else { u.style.display = 'none'; u.innerHTML = ''; }
                const s = d.summary;
                document.getElementById('lk_sum').innerHTML =
                    '<span class="pill g">✅ Delivered: ' + s.delivered + '</span>'
                    + '<span class="pill a">⏳ Pending: ' + s.pending + '</span>'
                    + '<span class="pill">💰 Total: ₹' + s.amount + '</span>'
                    + (s.first ? '<span class="pill b">First: ' + esc(s.first) + '</span>' : '')
                    + (s.last ? '<span class="pill b">Latest: ' + esc(s.last) + '</span>' : '');
                const box = document.getElementById('lk_rows');
                if (!d.rows.length) { box.innerHTML = '<div class="empty-note">No key delivery found.</div>'; }
                else {
                    box.innerHTML = d.rows.map(r => {
                        const keyHtml = r.pending
                            ? '<span class="pill a">⏳ Key pending — admin must dispatch</span>'
                            : '<span class="lk-key"><span>' + esc(r.key) + '</span><button class="lk-copy" onclick="copyText(this.dataset.k)" data-k="' + esc(r.key) + '">📋</button></span>';
                        return '<div class="lk-row">'
                            + '<div class="lk-top"><div><div class="lk-prod">' + esc(r.product) + ' <span class="pill">⏱ ' + esc(r.plan) + '</span></div>' + keyHtml + '</div>'
                            + '<div class="lk-price">₹' + esc(r.amount) + '</div></div>'
                            + '<div class="lk-meta"><span>🕒 <b>' + esc(r.time_txt) + '</b>' + (r.iso ? ' (' + lkAgo(r.iso) + ')' : '') + '</span>'
                            + '<span>👤 <b>' + esc(r.name) + '</b> · <code>' + esc(r.user_id) + '</code></span>'
                            + '<span>💳 ' + esc(r.paid_via) + '</span>'
                            + '<span>' + (r.pending ? '<span class="pill a">Pending</span>' : '<span class="pill g">Delivered</span>') + '</span></div>'
                            + '</div>';
                    }).join('');
                }
                const from = d.total ? (d.page - 1) * d.per_page + 1 : 0;
                const to = Math.min(d.total, d.page * d.per_page);
                document.getElementById('lk_info').textContent = 'Showing ' + from + ' - ' + to + ' of ' + d.total + ' records';
                document.getElementById('lk_pg').textContent = 'Page ' + d.page + ' of ' + d.pages;
                document.getElementById('lk_prev').disabled = d.page <= 1;
                document.getElementById('lk_next').disabled = d.page >= d.pages;
                document.getElementById('lk_upd').textContent = 'Updated ' + new Date().toLocaleTimeString() + ' · auto-refreshes every 15 seconds while this page is open.';
            }).catch(() => {});
        }
        function lkTabOpen() { const t = document.getElementById('tab-keystock'); return t && t.style.display === 'block'; }
        setInterval(() => { if (lkTabOpen() && !document.hidden && !document.getElementById('lk_q').matches(':focus')) lkLoad(); }, 15000);

        /* ---- Store settings ---- */
        function saveBranding() {
            postJSON('/api/store/save', {
                shop_name: document.getElementById('st_name').value,
                tagline: document.getElementById('st_tag').value,
                support_username: document.getElementById('st_supp').value,
                update_link: document.getElementById('st_upd').value,
                how_to_use_link: document.getElementById('st_how').value
            }).then(d => toast(d.message, d._ok));
        }
        function saveLimits() {
            const g = id => document.getElementById(id).value;
            postJSON('/api/store/limits', {
                min_deposit: g('st_min'), max_deposit: g('st_max'), qr_expiry_minutes: g('st_qr'),
                referral_bonus: g('st_ref'), spin_min: g('st_smin'), spin_max: g('st_smax'),
                spin_cooldown_hours: g('st_scool'), spin_expiry_hours: g('st_sexp')
            }).then(d => toast(d.message, d._ok));
        }

        /* ---- Broadcast ---- */
        let bcAudience = 'all', bcKind = '', bcFile = null;
        function bcAud(btn) {
            document.querySelectorAll('#bc_aud .chip-btn').forEach(b => b.classList.toggle('on', b === btn));
            bcAudience = btn.dataset.aud;
            const names = {all: 'All', customers: 'Buyers', non_buyers: 'Non-buyers', resellers: 'Resellers'};
            document.getElementById('bc_send').textContent = '🚀 Send to ' + names[bcAudience] + ' ' + btn.dataset.n + ' Users';
        }
        function bcWrap(tag) {
            const ta = document.getElementById('bc_msg');
            const a = ta.selectionStart, b = ta.selectionEnd;
            ta.value = ta.value.slice(0, a) + '<' + tag + '>' + ta.value.slice(a, b) + '</' + tag + '>' + ta.value.slice(b);
            ta.focus(); bcPreview();
        }
        function bcLink() {
            const url = prompt('Link URL (https://...)');
            if (!url) return;
            const ta = document.getElementById('bc_msg');
            const a = ta.selectionStart, b = ta.selectionEnd;
            const label = ta.value.slice(a, b) || 'Click here';
            ta.value = ta.value.slice(0, a) + '<a href="' + url + '">' + label + '</a>' + ta.value.slice(b);
            bcPreview();
        }
        function bcEmoji(e) {
            const ta = document.getElementById('bc_msg');
            const a = ta.selectionStart;
            ta.value = ta.value.slice(0, a) + e + ta.value.slice(a);
            ta.focus(); bcPreview();
        }
        function bcPreview() {
            const v = document.getElementById('bc_msg').value;
            document.getElementById('bc_count').textContent = v.length;
            let html = esc(v)
                .replace(/&lt;(b|i|u|s|code|pre)&gt;/g, '<$1>').replace(/&lt;[/](b|i|u|s|code|pre)&gt;/g, '</$1>');
            const names = {image: '🖼️ Image', video: '🎬 Video', voice: '🎙️ Voice'};
            const media = bcKind ? '<div class="tg-media">' + names[bcKind] + ' attached: ' + esc(bcFile ? bcFile.name : '') + '</div>' : '';
            document.getElementById('bc_prev').innerHTML = (media + html) || '<span style="color:#8d96b5;">Your message preview appears here...</span>';
        }
        function bcPick(kind, input) {
            if (!input.files || !input.files[0]) return;
            const f = input.files[0];
            if (f.size > 50 * 1024 * 1024) { input.value = ''; return toast('File is larger than 50 MB.', false); }
            ['image', 'video', 'voice'].forEach(k => { if (k !== kind) document.getElementById('bc_' + k).value = ''; });
            bcKind = kind; bcFile = f;
            ['image', 'video', 'voice'].forEach(k => document.getElementById('mt_' + k).classList.toggle('on', k === kind));
            document.getElementById('bc_file').style.display = 'flex';
            document.getElementById('bc_file_name').textContent = f.name + ' (' + (f.size / 1048576).toFixed(1) + ' MB)';
            bcPreview();
        }
        function bcClearMedia() {
            ['image', 'video', 'voice'].forEach(k => { document.getElementById('bc_' + k).value = ''; document.getElementById('mt_' + k).classList.remove('on'); });
            bcKind = ''; bcFile = null;
            document.getElementById('bc_file').style.display = 'none';
            bcPreview();
        }
        function sendBroadcast(testOnly) {
            const msg = document.getElementById('bc_msg').value;
            if (!msg.trim() && !bcFile) return toast('Write a message or attach media.', false);
            if (!testOnly && !confirm('Send this broadcast now?')) return;
            const fd = new FormData();
            fd.append('message', msg);
            fd.append('audience', bcAudience);
            fd.append('test', testOnly ? '1' : '0');
            if (bcFile) { fd.append('media_type', bcKind); fd.append('media', bcFile); }
            const btn = document.getElementById('bc_send');
            btn.disabled = true;
            fetch('/api/broadcast/send', { method: 'POST', body: fd })
                .then(r => r.json().then(d => { d._ok = r.ok; return d; }))
                .then(d => { toast(d.message, d._ok); btn.disabled = false; if (d._ok && !testOnly) { bcHistLoad(); } })
                .catch(() => { btn.disabled = false; toast('Upload failed. Check your connection.', false); });
        }
        let bcHistTimer = null;
        function bcHistLoad() {
            fetch('/api/broadcast/history').then(r => r.json()).then(d => {
                const box = document.getElementById('bc_hist');
                if (!d.rows || !d.rows.length) { box.innerHTML = '<div class="empty-note">No broadcasts yet.</div>'; return; }
                box.innerHTML = d.rows.map(r => {
                    const pct = r.total ? Math.round((r.sent + r.failed) * 100 / r.total) : 100;
                    const st = r.status === 'running' ? '<span class="pill a">Sending...</span>' : '<span class="pill g">Done</span>';
                    return '<div class="bc-hist"><div class="d-flex justify-content-between flex-wrap gap-1"><b>' + esc(r.time_txt) + '</b>' + st + '</div>'
                        + '<div class="set-note">' + esc(r.audience) + (r.media ? ' · ' + esc(r.media) : '') + '</div>'
                        + '<div style="margin-top:4px;">' + esc(r.preview) + '</div>'
                        + '<div class="set-note">✅ ' + r.sent + ' sent · ❌ ' + r.failed + ' failed · ' + r.total + ' total</div>'
                        + '<div class="bar"><i style="width:' + pct + '%"></i></div></div>';
                }).join('');
                clearTimeout(bcHistTimer);
                if (d.rows.some(r => r.status === 'running')) bcHistTimer = setTimeout(bcHistLoad, 3000);
            }).catch(() => {});
        }

        /* ---- My Profile ---- */
        function pwdMeter() {
            const p = document.getElementById('pf_new').value;
            let score = 0;
            if (p.length >= 6) score++;
            if (p.length >= 10) score++;
            if (/[0-9]/.test(p) && /[a-zA-Z]/.test(p)) score++;
            if (/[^a-zA-Z0-9]/.test(p)) score++;
            const bar = document.getElementById('pf_bar');
            bar.style.width = (p ? score * 25 : 0) + '%';
            bar.style.background = ['#ef4444', '#ef4444', '#f59e0b', '#84cc16', '#22c55e'][p ? score : 0];
            document.getElementById('pf_hint').textContent = p ? ['Too weak', 'Weak', 'Okay', 'Good', 'Strong'][score] : 'Use at least 6 characters; longer with numbers and symbols is stronger.';
        }
        function changePwd() {
            const cur = document.getElementById('pf_cur').value, nw = document.getElementById('pf_new').value, cf = document.getElementById('pf_conf').value;
            if (!cur || !nw || !cf) return toast('Fill all three password fields.', false);
            if (nw !== cf) return toast('New passwords do not match.', false);
            postJSON('/api/security/change_pwd', {current: cur, password: nw, confirm: cf}).then(d => {
                toast(d.message, d._ok);
                if (d._ok) ['pf_cur', 'pf_new', 'pf_conf'].forEach(i => document.getElementById(i).value = '');
                pwdMeter();
            });
        }
        function logoutAll() {
            if (!confirm('Log out of the panel on ALL devices?')) return;
            postJSON('/api/security/logout_all', {}).then(d => { toast(d.message, d._ok); setTimeout(() => location.href = '/', 900); });
        }
    </script>
</body>
</html>
"""


# ==========================================
# 🌐 BACKEND API ENDPOINTS
# ==========================================
def admin_required():
    """Logged in AND the session is from the current session generation ("Logout all devices" bumps it)."""
    if not session.get('admin_logged'):
        return False
    try:
        return int(session.get('sv', 1)) == db.get_session_version()
    except Exception:
        return True

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
        session['sv'] = db.get_session_version()
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
        session.clear()  # drops stale sessions (e.g. after "Logout all devices")
        return render_template_string(ADMIN_HTML, bot_info=BOT_INFO)

    r_u = q_one('SELECT COUNT(*), COALESCE(SUM(wallet_balance), 0) FROM users')
    tot_users, tot_wallet = r_u[0] or 0, float(r_u[1] or 0)
    r_o = q_one("SELECT COUNT(*), COALESCE(SUM(amount), 0) FROM order_history WHERE LEFT(COALESCE(key_delivered, ''), 7) <> 'PENDING'")
    tot_orders, tot_rev = r_o[0] or 0, float(r_o[1] or 0)
    tot_prods = q_one('SELECT COUNT(*) FROM products')[0]
    tot_keys = q_one('SELECT COUNT(*) FROM keys_inventory WHERE is_used = 0')[0]
    today_dep = float(q_one(
        """
        SELECT COALESCE(SUM(COALESCE(pay_amount, amount)), 0) FROM payment_records
        WHERE status = 'verified'
          AND (created_ts AT TIME ZONE 'Asia/Kolkata')::date = (now() AT TIME ZONE 'Asia/Kolkata')::date
        """
    )[0] or 0)
    awaiting = q_one("SELECT COUNT(*) FROM payment_records WHERE status = 'awaiting_approval'")[0] or 0

    stock = {}
    for pk, plan, cnt in q_all('SELECT prod_key, plan, COUNT(*) FROM keys_inventory WHERE is_used = 0 GROUP BY prod_key, plan'):
        stock.setdefault(pk, {})[plan] = cnt
    prods = [
        {"prod_key": p[0], "name": p[1], "category": p[2], "prices": parse_prices(p[3]),
         "icon": p[4] or "⚡", "download_link": p[5] or "", "maintenance": int(p[6] or 0),
         "stock_out": int(p[7] or 0), "stock": stock.get(p[0], {})}
        for p in q_all('SELECT prod_key, name, category, prices, icon, download_link, maintenance, stock_out FROM products ORDER BY name')
    ]
    all_orders = q_all('SELECT id, user_id, prod_name, plan, key_delivered, amount, utr, timestamp FROM order_history ORDER BY id DESC LIMIT 50')
    pending = q_all("SELECT id, user_id, prod_name, plan, key_delivered, amount, timestamp FROM order_history WHERE LEFT(key_delivered, 7) = 'PENDING' ORDER BY id DESC")
    users_list = q_all('SELECT user_id, full_name, username, joined_date, orders_count, wallet_balance, total_spent, total_referrals, referral_earnings, account_type FROM users ORDER BY user_id DESC LIMIT 50')
    topups = q_all('SELECT created_at, user_id, COALESCE(pay_amount, amount), status, order_id FROM payment_records ORDER BY id DESC LIMIT 30')
    st_r = q_one('SELECT support_username, how_to_use_link FROM store_settings WHERE id = 1') or ("@Athulsudin", "")
    pay = db.get_payment_settings()
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
    ist = IST
    alerts = [
        {
            "time_txt": r["received_at"].astimezone(ist).strftime("%d %b, %I:%M %p") if r["received_at"] else "",
            "amount": r["amount"], "utr": r["utr"], "status": r["status"], "order_id": r["order_id"],
            "snippet": (r["raw_text"] or "")[:90],
        }
        for r in db.recent_bank_credits(20)
    ]

    saved_styles = db.get_button_styles()
    button_groups = [
        {"title": title, "items": [
            {"key": k, "label": label, "default": default or "none",
             "style": (saved_styles.get(k) if saved_styles.get(k) is not None else default) or "none"}
            for k, label, default in items
        ]}
        for title, items in BUTTON_GROUPS
    ]

    # ---- Manage Keys analytics ----
    today_prefix = datetime.now(IST).strftime("%d %b %Y") + "%"
    delivered_sql = "LEFT(COALESCE(o.key_delivered, ''), 7) <> 'PENDING'"
    r_k = q_one(f"SELECT COUNT(*), COALESCE(SUM(o.amount), 0) FROM order_history o WHERE {delivered_sql}")
    r_t = q_one(f"SELECT COUNT(*), COALESCE(SUM(o.amount), 0) FROM order_history o WHERE {delivered_sql} AND {TODAY_SQL}", (today_prefix,))
    pending_n = q_one("SELECT COUNT(*) FROM order_history WHERE LEFT(COALESCE(key_delivered, ''), 7) = 'PENDING'")[0] or 0
    key_stats = {
        "total": r_k[0] or 0, "today": r_t[0] or 0, "today_rev": f"{float(r_t[1] or 0):,.2f}",
        "live_products": sum(1 for p in prods if not p["maintenance"] and not p["stock_out"]),
        "pending": pending_n,
    }
    today_by_product = [
        (r[0], r[1], float(r[2] or 0)) for r in q_all(
            f"SELECT o.prod_name, COUNT(*), SUM(o.amount) FROM order_history o WHERE {delivered_sql} AND {TODAY_SQL} "
            "GROUP BY o.prod_name ORDER BY SUM(o.amount) DESC", (today_prefix,))
    ]
    lifetime = {
        r[0]: [r[1], float(r[2] or 0)] for r in q_all(
            f"SELECT o.prod_name, COUNT(*), SUM(o.amount) FROM order_history o WHERE {delivered_sql} GROUP BY o.prod_name")
    }
    bc_row = q_one(
        "SELECT COUNT(*) AS c_all, COUNT(*) FILTER (WHERE orders_count > 0) AS c_cust, COUNT(*) FILTER (WHERE orders_count = 0) AS c_non, "
        "COUNT(*) FILTER (WHERE account_type = 'Reseller') AS c_res FROM users"
    )
    bc_counts = {"all": bc_row[0] or 0, "customers": bc_row[1] or 0, "non_buyers": bc_row[2] or 0, "resellers": bc_row[3] or 0}
    st = db.get_settings()

    return render_template_string(
        ADMIN_HTML, bot_info=BOT_INFO, button_groups=button_groups, topups=topups,
        key_stats=key_stats, today_by_product=today_by_product, lifetime=lifetime,
        bc_counts=bc_counts, admin_id=ADMIN_ID,
        stats={"users": tot_users, "total_wallet": f"{tot_wallet:,.2f}", "products_count": tot_prods,
               "orders": tot_orders, "revenue": f"{tot_rev:,.2f}", "key_stock": tot_keys,
               "today_deposit": f"{today_dep:,.2f}", "awaiting": awaiting},
        products=prods, all_orders=all_orders, pending_orders=pending, users_list=users_list,
        pay_cfg=pay_cfg, gmail=gmail_cfg, alerts=alerts, store=st
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

PLAN_RE = re.compile(r'^[A-Za-z0-9 _.\-+]{1,22}$')   # short enough for Telegram's 64-byte callback data
CATEGORIES = ("non_root", "root", "ios", "pc", "likes")

def _clean_plans(raw):
    """Validate the plans sent by the admin form. Returns (plans, error)."""
    plans, seen = [], set()
    for item in raw or []:
        name = (item.get('name') or '').strip()
        try:
            price = round(float(item.get('price')), 2)
        except (TypeError, ValueError):
            return None, f"Invalid price for plan '{name}'!"
        if not PLAN_RE.match(name):
            return None, f"Plan name '{name}' is invalid (max 22 letters/numbers, no special symbols)."
        if price < 0:
            return None, "Price cannot be negative!"
        if name in seen:
            return None, f"Plan '{name}' is added twice!"
        seen.add(name)
        keys = [x.strip() for x in (item.get('keys') or '').split('\n') if x.strip()]
        plans.append({"old": (item.get('old') or '').strip(), "name": name, "price": price, "keys": keys})
    if not plans:
        return None, "Add at least one plan!"
    return plans, None

def _save_product(d, is_update):
    name = (d.get('name') or '').strip()
    category = d.get('category') if d.get('category') in CATEGORIES else 'non_root'
    icon = (d.get('icon') or '').strip()[:8] or '⚡'
    link = (d.get('download_link') or '').strip()
    plans, err = _clean_plans(d.get('plans'))
    if not name:
        return jsonify({"message": "Enter the product name!", "error": True}), 400
    if err:
        return jsonify({"message": err, "error": True}), 400
    prices = [[p["name"], p["price"]] for p in plans]

    if is_update:
        k = (d.get('prod_key') or '').strip()
    else:
        k = re.sub(r'[^a-zA-Z0-9]', '_', name).strip('_').lower()[:32]
    if not k:
        return jsonify({"message": "Invalid product name!", "error": True}), 400

    with db.connection() as conn:
        with conn.cursor() as cur:
            cur.execute('SELECT 1 FROM products WHERE prod_key = %s', (k,))
            exists = cur.fetchone() is not None
            if is_update and not exists:
                return jsonify({"message": "Product not found!", "error": True}), 404
            if not is_update and exists:
                return jsonify({"message": "A product with this name already exists. Edit it instead.", "error": True}), 400
            if is_update:
                cur.execute(
                    'UPDATE products SET name=%s, category=%s, prices=%s, download_link=%s, icon=%s WHERE prod_key=%s',
                    (name, category, json.dumps(prices), link, icon, k),
                )
                for p in plans:   # a renamed plan keeps its unused keys
                    if p["old"] and p["old"] != p["name"]:
                        cur.execute('UPDATE keys_inventory SET plan=%s WHERE prod_key=%s AND plan=%s AND is_used=0',
                                    (p["name"], k, p["old"]))
            else:
                cur.execute(
                    """
                    INSERT INTO products (prod_key, name, category, prices, download_link, icon, maintenance, stock_out)
                    VALUES (%s, %s, %s, %s, %s, %s, 0, 0)
                    """,
                    (k, name, category, json.dumps(prices), link, icon),
                )
            rows = [(k, p["name"], item) for p in plans for item in p["keys"]]
            if rows:
                cur.executemany('INSERT INTO keys_inventory (prod_key, plan, item_key, is_used) VALUES (%s, %s, %s, 0)', rows)
    return jsonify({"message": "Product updated!" if is_update else "Product added!"})

@app.route('/api/product/save', methods=['POST'])
def api_save_p():
    if not admin_required(): return unauthorized()
    return _save_product(request.json or {}, is_update=False)

@app.route('/api/product/update', methods=['POST'])
def api_update_p():
    if not admin_required(): return unauthorized()
    return _save_product(request.json or {}, is_update=True)

@app.route('/api/product/toggle', methods=['POST'])
def api_toggle_p():
    if not admin_required(): return unauthorized()
    d = request.json or {}
    field = d.get('field')
    if field not in ('maintenance', 'stock_out'):
        return jsonify({"message": "Invalid option!"}), 400
    row = db.fetchone(
        f"UPDATE products SET {field} = CASE WHEN {field} = 0 THEN 1 ELSE 0 END WHERE prod_key = %s RETURNING maintenance, stock_out",
        (d.get('prod_key'),),
    )
    if not row:
        return jsonify({"message": "Product not found!"}), 404
    if row["maintenance"]:
        msg = "Product is now under MAINTENANCE (hidden from the shop)."
    elif row["stock_out"]:
        msg = "Product is now DISABLED (hidden from the shop)."
    else:
        msg = "Product is ACTIVE again."
    return jsonify({"message": msg})

@app.route('/api/product/delete', methods=['POST'])
def api_del_p():
    if not admin_required(): return unauthorized()
    db.execute('DELETE FROM products WHERE prod_key = %s', ((request.json or {}).get('prod_key'),))
    return jsonify({"message": "Product deleted!"})

@app.route('/api/keys/add', methods=['POST'])
def api_keys_add():
    if not admin_required(): return unauthorized()
    d = request.json or {}
    prod = db.get_product_by_key(d.get('prod_key') or '')
    plan = (d.get('plan') or '').strip()
    if not prod or not any(n == plan for n, _ in prod["prices"]):
        return jsonify({"message": "Choose a valid product and plan!", "error": True}), 400
    keys = [x.strip() for x in (d.get('keys') or '').split('\n') if x.strip()]
    if not keys:
        return jsonify({"message": "Paste at least one key!", "error": True}), 400
    with db.connection() as conn:
        with conn.cursor() as cur:
            cur.executemany('INSERT INTO keys_inventory (prod_key, plan, item_key, is_used) VALUES (%s, %s, %s, 0)',
                            [(d['prod_key'], plan, x) for x in keys])
    return jsonify({"message": f"{len(keys)} key(s) added to {prod['name']} — {plan}."})

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



@app.route('/api/buttons/save', methods=['POST'])
def api_buttons_save():
    if not admin_required(): return unauthorized()
    styles = (request.json or {}).get('styles') or {}
    if not isinstance(styles, dict):
        return jsonify({"message": "Invalid data!"}), 400
    allowed = set(BUTTON_COLORS) | {"none"}
    for key, style in styles.items():
        if key in MENU_BUTTONS and style in allowed:
            db.set_button_style(key, style)
    return jsonify({"message": "Button colors saved! The bot shows them within a few seconds."})


# ==========================================
# 🔑 LIVE KEY STATUS (Manage Keys)
# ==========================================
def _fmt_ist(ts):
    return ts.astimezone(IST).strftime("%d %b %Y, %I:%M:%S %p") if ts else ""

@app.route('/api/keys/live')
def api_keys_live():
    if not admin_required(): return unauthorized()
    q = (request.args.get('q') or '').strip()
    product = (request.args.get('product') or '').strip()
    status = (request.args.get('status') or '').strip()
    today_only = request.args.get('today') == '1'
    try:
        page = max(1, int(request.args.get('page', 1)))
    except ValueError:
        page = 1
    per = 10

    where, params = [], []
    if q:
        like = f"%{q.lstrip('@')}%"
        where.append(
            "(o.prod_name ILIKE %s OR o.key_delivered ILIKE %s OR COALESCE(u.username, '') ILIKE %s "
            "OR COALESCE(u.full_name, '') ILIKE %s OR COALESCE(o.utr, '') ILIKE %s OR CAST(o.user_id AS TEXT) LIKE %s)"
        )
        params += [like] * 6
    if product:
        where.append("o.prod_name = %s"); params.append(product)
    if status == 'pending':
        where.append("LEFT(COALESCE(o.key_delivered, ''), 7) = 'PENDING'")
    elif status == 'delivered':
        where.append("LEFT(COALESCE(o.key_delivered, ''), 7) <> 'PENDING'")
    if today_only:
        where.append(TODAY_SQL); params.append(datetime.now(IST).strftime("%d %b %Y") + "%")
    wsql = ("WHERE " + " AND ".join(where)) if where else ""
    base = f"FROM order_history o LEFT JOIN users u ON u.user_id = o.user_id {wsql}"

    summ = db.fetchone(
        "SELECT COUNT(*) AS n, "
        "COUNT(*) FILTER (WHERE LEFT(COALESCE(o.key_delivered, ''), 7) <> 'PENDING') AS delivered, "
        "COUNT(*) FILTER (WHERE LEFT(COALESCE(o.key_delivered, ''), 7) = 'PENDING') AS pending, "
        "COALESCE(SUM(o.amount), 0) AS amount, MIN(o.created_ts) AS first_ts, MAX(o.created_ts) AS last_ts "
        + base, params)
    total = int(summ["n"] or 0)
    pages = max(1, (total + per - 1) // per)
    page = min(page, pages)

    rows = db.fetchall(
        "SELECT o.id, o.user_id, o.prod_name, o.plan, o.key_delivered, o.amount, o.utr, o.timestamp, o.created_ts, "
        "u.full_name, u.username " + base + " ORDER BY o.id DESC LIMIT %s OFFSET %s",
        params + [per, (page - 1) * per])
    out = []
    for r in rows:
        key = r["key_delivered"] or ""
        utr = (r["utr"] or "").strip()
        if utr == "WALLET_PAY":
            paid_via = "Wallet balance"
        elif utr:
            paid_via = f"UPI · UTR {utr}"
        else:
            paid_via = "—"
        out.append({
            "id": r["id"], "user_id": r["user_id"],
            "name": r["full_name"] or r["username"] or "Unknown",
            "product": r["prod_name"] or "", "plan": (r["plan"] or "").replace("_", " "),
            "key": key, "pending": key[:7] == "PENDING",
            "amount": f"{float(r['amount'] or 0):,.2f}", "paid_via": paid_via,
            "time_txt": _fmt_ist(r["created_ts"]) or (r["timestamp"] or ""),
            "iso": r["created_ts"].isoformat() if r["created_ts"] else "",
        })

    # Customer card: when the result belongs to exactly one customer (or a Telegram ID with no orders yet)
    user = None
    owners = db.fetchall("SELECT DISTINCT o.user_id " + base + " LIMIT 2", params)
    uid = owners[0]["user_id"] if len(owners) == 1 else (int(q) if q.isdigit() and len(q) <= 18 and not owners else None)
    if uid is not None:
        u = db.get_user(uid)
        if u:
            user = {
                "id": u["user_id"], "name": u["full_name"] or "Unknown", "username": u["username"] or "N/A",
                "role": u["account_type"] or "Regular", "joined": u["joined_date"] or "",
                "balance": f"{float(u['wallet_balance'] or 0):,.2f}", "spent": f"{float(u['total_spent'] or 0):,.2f}",
                "orders": u["orders_count"] or 0,
            }
    return jsonify({
        "rows": out, "total": total, "page": page, "pages": pages, "per_page": per, "user": user,
        "summary": {
            "delivered": int(summ["delivered"] or 0), "pending": int(summ["pending"] or 0),
            "amount": f"{float(summ['amount'] or 0):,.2f}",
            "first": _fmt_ist(summ["first_ts"]), "last": _fmt_ist(summ["last_ts"]),
        },
    })

# ==========================================
# ⚙️ STORE SETTINGS
# ==========================================
@app.route('/api/store/save', methods=['POST'])
def api_save_st():
    if not admin_required(): return unauthorized()
    d = request.json or {}
    def clean(key, limit=200):
        return str(d.get(key, '') or '').strip()[:limit]
    shop_name = clean('shop_name', 60)
    if not shop_name:
        return jsonify({"message": "Shop name cannot be empty!"}), 400
    for key in ('update_link', 'how_to_use_link'):
        link = clean(key)
        if link and not link.lower().startswith(('http://', 'https://', 'tg://')):
            return jsonify({"message": "Links must start with https:// (or tg://)."}), 400
    db.save_branding(shop_name, clean('tagline', 120), clean('support_username', 64),
                     clean('update_link'), clean('how_to_use_link'))
    return jsonify({"message": "Branding saved! The bot uses it right away."})

@app.route('/api/store/limits', methods=['POST'])
def api_save_limits():
    if not admin_required(): return unauthorized()
    d = request.json or {}
    try:
        vals = {
            "min_deposit": float(d['min_deposit']), "max_deposit": float(d['max_deposit']),
            "qr_expiry_minutes": int(float(d['qr_expiry_minutes'])), "referral_bonus": float(d['referral_bonus']),
            "spin_min": float(d['spin_min']), "spin_max": float(d['spin_max']),
            "spin_cooldown_hours": float(d['spin_cooldown_hours']), "spin_expiry_hours": float(d['spin_expiry_hours']),
        }
    except (KeyError, TypeError, ValueError):
        return jsonify({"message": "Please fill every field with a valid number!"}), 400
    if any(v < 0 for v in vals.values()):
        return jsonify({"message": "Values cannot be negative!"}), 400
    if vals["min_deposit"] < 1 or vals["max_deposit"] < vals["min_deposit"]:
        return jsonify({"message": "Max deposit must be at least the min deposit (min ≥ ₹1)."}), 400
    if vals["qr_expiry_minutes"] < 1:
        return jsonify({"message": "QR expiry must be at least 1 minute."}), 400
    if vals["spin_max"] < vals["spin_min"]:
        return jsonify({"message": "Spin max must be at least the spin min."}), 400
    db.save_limits(**vals)
    return jsonify({"message": "Settings saved! The bot applies them immediately."})

# ==========================================
# 📢 BROADCAST (text + image / video / voice)
# ==========================================
BC_AUDIENCES = {
    "all": ("All users", "SELECT user_id FROM users"),
    "customers": ("Buyers", "SELECT user_id FROM users WHERE orders_count > 0"),
    "non_buyers": ("Not bought yet", "SELECT user_id FROM users WHERE orders_count = 0"),
    "resellers": ("Resellers", "SELECT user_id FROM users WHERE account_type = 'Reseller'"),
}
BC_METHODS = {"image": ("sendPhoto", "photo"), "video": ("sendVideo", "video"),
              "voice": ("sendVoice", "voice"), "audio": ("sendAudio", "audio")}

def tg_call(method, payload=None, multipart=None):
    """Call the Telegram Bot API. Returns (ok, result_or_description)."""
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    if multipart:
        boundary = "----bc" + hashlib.md5(os.urandom(8)).hexdigest()
        body = b""
        for name, value in multipart["fields"].items():
            body += f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode()
        fname = multipart["filename"].replace('"', '')
        body += (f'--{boundary}\r\nContent-Disposition: form-data; name="{multipart["field"]}"; filename="{fname}"\r\n'
                 f'Content-Type: {multipart["ctype"]}\r\n\r\n').encode() + multipart["data"] + f'\r\n--{boundary}--\r\n'.encode()
        req = urllib.request.Request(url, data=body, headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
        timeout = 180
    else:
        req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
        timeout = 15
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode())
            return bool(data.get("ok")), data.get("result")
    except urllib.error.HTTPError as e:
        try:
            return False, json.loads(e.read().decode()).get("description", str(e))
        except Exception:
            return False, str(e)
    except Exception as e:
        return False, str(e)

def _extract_file_id(kind, result):
    if not isinstance(result, dict):
        return None
    if kind == "image":
        photos = result.get("photo") or []
        return photos[-1]["file_id"] if photos else None
    return (result.get(BC_METHODS[kind][1]) or {}).get("file_id")

def _send_text(chat_id, text):
    ok, res = tg_call("sendMessage", {"chat_id": chat_id, "text": text, "parse_mode": "HTML"})
    if not ok and "parse" in str(res).lower():  # broken HTML tags: send as plain text instead
        ok, res = tg_call("sendMessage", {"chat_id": chat_id, "text": text})
    return ok, res

def _send_one(chat_id, text, kind, media):
    """Send one broadcast message. `media` is a dict holding either file bytes or a Telegram file_id."""
    if not kind:
        ok, _ = _send_text(chat_id, text)
        return ok, None
    method, field = BC_METHODS[kind]
    caption_ok = bool(text) and len(text) <= 1024
    if media.get("file_id"):
        payload = {"chat_id": chat_id, field: media["file_id"]}
        if caption_ok:
            payload.update(caption=text, parse_mode="HTML")
        ok, res = tg_call(method, payload)
        if not ok and caption_ok and "parse" in str(res).lower():
            payload.pop("parse_mode", None)
            ok, res = tg_call(method, payload)
        file_id = media["file_id"]
    else:
        fields = {"chat_id": str(chat_id)}
        if caption_ok:
            fields.update(caption=text, parse_mode="HTML")
        ok, res = tg_call(method, multipart={"fields": fields, "field": field, "filename": media["filename"],
                                             "ctype": media["ctype"], "data": media["data"]})
        file_id = _extract_file_id(kind, res) if ok else None
    if ok and text and not caption_ok:
        _send_text(chat_id, text)
    return ok, file_id

@app.route('/api/broadcast/send', methods=['POST'])
def api_bc():
    if not admin_required(): return unauthorized()
    msg = (request.form.get('message') or '').strip()
    audience = request.form.get('audience') or 'all'
    test_only = request.form.get('test') == '1'
    kind = (request.form.get('media_type') or '').strip()
    upload = request.files.get('media')
    if audience not in BC_AUDIENCES:
        return jsonify({"message": "Unknown audience!"}), 400

    media = None
    if upload and kind in ("image", "video", "voice"):
        data = upload.read()
        if not data:
            return jsonify({"message": "The attached file is empty!"}), 400
        if len(data) > 50 * 1024 * 1024:
            return jsonify({"message": "File is larger than 50 MB!"}), 400
        name = upload.filename or kind
        ctype = upload.mimetype or "application/octet-stream"
        if kind == "voice" and not name.lower().endswith((".ogg", ".oga", ".opus")):
            kind = "audio"  # mp3 / m4a / wav cannot be a voice note: send as an audio file
        media = {"data": data, "filename": name, "ctype": ctype}
    else:
        kind = ""
    if not msg and not media:
        return jsonify({"message": "Enter a message or attach media!"}), 400

    if test_only:
        ok, res = _send_one(ADMIN_ID, msg, kind, media or {})
        if ok:
            return jsonify({"message": "Test sent to your Telegram!"})
        return jsonify({"message": f"Test failed: {res or 'Open the bot and press /start first.'}"}), 400

    label, sql = BC_AUDIENCES[audience]
    uids = [r[0] for r in q_all(sql)]
    if not uids:
        return jsonify({"message": "No users in this audience!"}), 400
    hist = db.fetchone(
        "INSERT INTO broadcast_history (audience, media_type, preview, total) VALUES (%s, %s, %s, %s) RETURNING id",
        (label, kind or None, (msg or '[media only]')[:140], len(uids)))
    hist_id = hist["id"]

    def _run():
        sent = failed = 0
        shared = dict(media or {})
        for u in uids:
            try:
                ok, file_id = _send_one(u, msg, kind, shared)
                if ok and kind and file_id and not shared.get("file_id"):
                    shared = {"file_id": file_id}  # upload once, reuse the Telegram file for everyone else
                if ok: sent += 1
                else: failed += 1
            except Exception:
                failed += 1
            if (sent + failed) % 10 == 0:
                try: db.execute("UPDATE broadcast_history SET sent = %s, failed = %s WHERE id = %s", (sent, failed, hist_id))
                except Exception: pass
            time.sleep(0.05)  # stay under Telegram's broadcast rate limit
        try: db.execute("UPDATE broadcast_history SET sent = %s, failed = %s, status = 'done' WHERE id = %s", (sent, failed, hist_id))
        except Exception: pass
    Thread(target=_run, daemon=True).start()
    return jsonify({"message": f"Broadcast started to {len(uids)} users!"})

@app.route('/api/broadcast/history')
def api_bc_history():
    if not admin_required(): return unauthorized()
    rows = db.fetchall("SELECT * FROM broadcast_history ORDER BY id DESC LIMIT 15")
    names = {"image": "🖼️ Image", "video": "🎬 Video", "voice": "🎙️ Voice", "audio": "🎵 Audio"}
    return jsonify({"rows": [{
        "time_txt": _fmt_ist(r["created_ts"]), "audience": r["audience"], "media": names.get(r["media_type"] or "", ""),
        "preview": r["preview"] or "", "total": r["total"], "sent": r["sent"], "failed": r["failed"], "status": r["status"],
    } for r in rows]})

# ==========================================
# 👤 MY PROFILE (password, sessions, exports)
# ==========================================
@app.route('/api/security/change_pwd', methods=['POST'])
def api_pwd():
    if not admin_required(): return unauthorized()
    if os.environ.get('ADMIN_PASSWORD', '').strip():
        return jsonify({"message": "Password is managed by ADMIN_PASSWORD in Render Environment. Change it there and redeploy."}), 400
    d = request.json or {}
    current = (d.get('current') or '').strip()
    new_pwd = (d.get('password') or '').strip()
    if not db.check_admin_password(current):
        time.sleep(1)
        return jsonify({"message": "Current password is wrong!"}), 400
    if new_pwd != (d.get('confirm') or '').strip():
        return jsonify({"message": "New passwords do not match!"}), 400
    if len(new_pwd) < 6:
        return jsonify({"message": "Password must be at least 6 characters!"}), 400
    db.set_admin_password(new_pwd)          # also logs out every other device
    session['sv'] = db.get_session_version()  # keep THIS device signed in
    return jsonify({"message": "Password updated! Other devices were logged out."})

@app.route('/api/security/logout_all', methods=['POST'])
def api_logout_all():
    if not admin_required(): return unauthorized()
    db.bump_session_version()
    session.clear()
    return jsonify({"message": "Logged out on all devices."})

def _csv_response(name, header, rows):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(header)
    for r in rows:
        w.writerow(["" if v is None else v for v in r])
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename={name}"})

@app.route('/export/orders.csv')
def export_orders():
    if not admin_required(): return unauthorized()
    rows = db.fetchall("SELECT id, user_id, prod_name, plan, key_delivered, amount, utr, timestamp FROM order_history ORDER BY id DESC")
    return _csv_response("orders.csv", ["id", "user_id", "product", "plan", "key", "amount", "utr", "time"],
                         [tuple(r.values()) for r in rows])

@app.route('/export/users.csv')
def export_users():
    if not admin_required(): return unauthorized()
    rows = db.fetchall("SELECT user_id, full_name, username, joined_date, orders_count, wallet_balance, total_spent, account_type FROM users ORDER BY user_id")
    return _csv_response("users.csv", ["user_id", "name", "username", "joined", "orders", "wallet", "spent", "role"],
                         [tuple(r.values()) for r in rows])


def run_web():
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)

if __name__ == "__main__":
    init_db()
    run_web()


