import os
import json
import re
import time
import hashlib
import csv
import io
import urllib.request
import urllib.error
import mimetypes
import html as _html
from datetime import datetime, timedelta, timezone
from threading import Thread
from flask import Flask, request, jsonify, redirect, session, Response

from database import db, init_db, parse_prices, MENU_BUTTONS, BUTTON_GROUPS, BUTTON_COLORS
import supplier_api
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

# Filled in by main.py when both run in one process (otherwise these stay harmless no-ops)
send_test_alert = lambda: False
translate_hook = None

def tg_send(chat_id, text):
    try:
        if translate_hook and int(chat_id) != ADMIN_ID:
            text = translate_hook(text, db.get_user_lang(int(chat_id)))
    except Exception:
        pass
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
        .bc-emo { display: flex; align-items: center; gap: 8px; margin-top: 10px; flex-wrap: wrap; }
        .bc-ico img, .emo-cell img { width: 22px; height: 22px; object-fit: contain; vertical-align: middle; margin-right: 6px; }
        .emo-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(52px, 1fr)); gap: 8px; max-height: 52vh; overflow-y: auto; padding: 4px; }
        .emo-cell { position: relative; height: 52px; border-radius: 12px; background: #0d0622; border: 1px solid var(--neon-border); color: #fff; font-size: 24px; padding: 0; }
        .emo-cell img { position: absolute; inset: 8px; width: 36px; height: 36px; margin: 0; background: #0d0622; }
        .emo-cell:hover { border-color: #a855f7; }
        .hint-box { background: rgba(251,191,36,.08); border: 1px solid rgba(251,191,36,.35); border-radius: 12px; padding: 10px 12px; font-size: .82rem; color: #e5dcc0; margin-bottom: 12px; }
        .pv-box { background: #0d0622; border: 1px solid var(--neon-border); border-radius: 14px; padding: 12px; font-size: .9rem; white-space: pre-wrap; word-break: break-word; min-height: 48px; }
        .bc-prev { display: block; text-align: center; padding: 11px 12px; border-radius: 10px; font-weight: 700; color: #fff; margin-bottom: 10px; }
        .s-none { background: #3a4052; } .s-primary { background: #2f7de1; } .s-success { background: #2e9b44; } .s-danger { background: #c9424a; }
        .sw-wrap { display: flex; gap: 10px; align-items: center; }
        .sw { width: 38px; height: 38px; border-radius: 50%; border: 3px solid transparent; cursor: pointer; box-shadow: 0 0 0 1px rgba(255,255,255,.2); }
        .sw.on { border-color: #fff; box-shadow: 0 0 0 3px rgba(167,139,250,.8); }
        .sw.s-none { background: #5b6378; }
        .badge-st { padding: 4px 10px; border-radius: 10px; font-size: .75rem; font-weight: 700; }
        .b-verified { background: rgba(16,185,129,.2); color: #6ee7b7; } .b-pending, .b-awaiting_approval { background: rgba(250,204,21,.2); color: #fde68a; }
        .b-rejected, .b-cancelled, .b-expired { background: rgba(248,113,113,.2); color: #fca5a5; }

        /* ===================== PANEL COMPONENTS (cards, sections, chips, stats) ===================== */
        .gcard { --ac: #8b5cf6; position: relative; background: var(--card-glass); border: 1px solid var(--neon-border); border-top: 4px solid var(--ac); border-radius: 22px; padding: 18px 16px; margin-bottom: 18px; box-shadow: 0 10px 30px rgba(0,0,0,.35); }
        .gcard.blue { --ac: #38bdf8; } .gcard.green { --ac: #34d399; } .gcard.amber { --ac: #fbbf24; } .gcard.pink { --ac: #f472b6; } .gcard.red { --ac: #fb7185; }
        .gt { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin: 0 0 14px; font-weight: 800; font-size: 1.05rem; color: #fff; }
        .sec-head { display: flex; align-items: center; gap: 12px; margin-bottom: 16px; padding-bottom: 14px; border-bottom: 1px dashed rgba(167,139,250,.35); }
        .sec-no { width: 36px; height: 36px; border-radius: 12px; background: var(--ac); color: #0b0420; font-weight: 900; font-size: 1.05rem; display: flex; align-items: center; justify-content: center; flex-shrink: 0; box-shadow: 0 4px 14px rgba(0,0,0,.35); }
        .sec-head h5 { margin: 0; font-weight: 800; font-size: 1.08rem; color: #fff; }
        .sec-head p { margin: 2px 0 0; color: var(--txt-soft); font-size: .82rem; line-height: 1.4; }
        .sec-head .pill { margin-left: auto; }
        .sec-nav { display: flex; gap: 8px; overflow-x: auto; padding: 2px 2px 14px; margin-bottom: 4px; -webkit-overflow-scrolling: touch; }
        .sec-nav::-webkit-scrollbar { display: none; }

        .chip-btn { display: inline-flex; align-items: center; gap: 6px; padding: 9px 14px; border-radius: 999px; background: rgba(40,20,90,.55); border: 1px solid rgba(139,92,246,.5); color: #e9e5ff; font-weight: 700; font-size: .85rem; white-space: nowrap; cursor: pointer; }
        .chip-btn:hover { color: #fff; border-color: #c4b5fd; }
        .chip-btn.on { background: linear-gradient(135deg, #8b5cf6, #6366f1); border-color: #c4b5fd; color: #fff; box-shadow: 0 4px 14px rgba(99,102,241,.45); }

        .pill { display: inline-flex; align-items: center; gap: 4px; padding: 4px 11px; border-radius: 999px; font-size: .76rem; font-weight: 800; background: rgba(139,92,246,.22); color: #ddd6fe; border: 1px solid rgba(139,92,246,.4); white-space: nowrap; }
        .pill.g { background: rgba(16,185,129,.18); color: #6ee7b7; border-color: rgba(16,185,129,.45); }
        .pill.a { background: rgba(250,204,21,.16); color: #fde68a; border-color: rgba(250,204,21,.45); }
        .pill.r { background: rgba(248,113,113,.18); color: #fca5a5; border-color: rgba(248,113,113,.45); }
        .pill.b { background: rgba(56,189,248,.16); color: #7dd3fc; border-color: rgba(56,189,248,.45); }

        .set-note { font-size: .8rem; color: var(--txt-soft); line-height: 1.5; margin-top: 6px; }
        .set-new { margin-left: 8px; padding: 2px 8px; border-radius: 8px; background: #22c55e; color: #052e16; font-size: .65rem; font-weight: 900; }
        .empty-note { text-align: center; padding: 22px 10px; color: var(--txt-soft); border: 1px dashed rgba(167,139,250,.35); border-radius: 14px; }
        .live-dot { display: inline-flex; align-items: center; gap: 6px; padding: 3px 10px; border-radius: 999px; background: rgba(16,185,129,.18); color: #6ee7b7; font-size: .68rem; font-weight: 900; letter-spacing: .08em; }
        .live-dot::before { content: ''; width: 8px; height: 8px; border-radius: 50%; background: #34d399; animation: pulseDot 1.4s infinite; }
        @keyframes pulseDot { 0% { box-shadow: 0 0 0 0 rgba(52,211,153,.7); } 100% { box-shadow: 0 0 0 9px rgba(52,211,153,0); } }

        .ov-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 10px; }
        .ov { background: rgba(10,4,30,.55); border: 1px solid rgba(139,92,246,.35); border-radius: 16px; padding: 14px 12px; text-align: center; }
        .oi { font-size: 1.5rem; margin-bottom: 4px; }
        .ov-v { font-size: 1.45rem; font-weight: 900; color: #fff; line-height: 1.1; }
        .ov-l { font-size: .74rem; color: var(--txt-soft); margin-top: 4px; letter-spacing: .03em; }

        .btn-danger-soft { background: rgba(225,29,72,.18); border: 1px solid #e11d48; color: #fda4af; border-radius: 12px; padding: 9px 16px; font-weight: 700; }
        .btn-ghost { width: 100%; margin-top: 10px; padding: 12px; border-radius: 14px; background: transparent; border: 1px dashed rgba(167,139,250,.6); color: #ddd6fe; font-weight: 700; }
        .btn-send { width: 100%; margin-top: 8px; padding: 15px; border: none; border-radius: 16px; background: linear-gradient(135deg, #10b981, #059669); color: #fff; font-weight: 900; font-size: 1.02rem; box-shadow: 0 8px 24px rgba(16,185,129,.4); }
        .btn-send:disabled { opacity: .55; }

        /* Live key status */
        .lk-filters { display: grid; grid-template-columns: 1fr; gap: 8px; margin-bottom: 12px; }
        @media (min-width: 700px) { .lk-filters { grid-template-columns: 2fr 1fr 1fr auto; } }
        .lk-user { display: none; background: rgba(56,189,248,.1); border: 1px solid rgba(56,189,248,.4); border-radius: 14px; padding: 12px 14px; margin-bottom: 12px; }
        .uname { font-weight: 800; color: #fff; } .umeta { font-size: .82rem; color: var(--txt-soft); margin-top: 3px; }
        .lk-sum { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 12px; }
        .lk-row { background: rgba(10,4,30,.55); border: 1px solid rgba(139,92,246,.3); border-left: 4px solid #8b5cf6; border-radius: 14px; padding: 12px 14px; margin-bottom: 10px; }
        .lk-top { display: flex; justify-content: space-between; gap: 10px; align-items: flex-start; }
        .lk-prod { font-weight: 800; color: #fff; margin-bottom: 6px; }
        .lk-price { font-weight: 900; color: #6ee7b7; font-size: 1.1rem; white-space: nowrap; }
        .lk-key { display: inline-flex; align-items: center; gap: 8px; max-width: 100%; background: #0d0622; border: 1px solid rgba(139,92,246,.45); border-radius: 10px; padding: 6px 8px 6px 10px; font-family: monospace; font-size: .85rem; color: #e9e5ff; word-break: break-all; }
        .lk-copy { border: none; background: rgba(139,92,246,.35); color: #fff; border-radius: 8px; padding: 3px 8px; }
        .lk-meta { display: flex; flex-wrap: wrap; gap: 6px 16px; margin-top: 10px; padding-top: 8px; border-top: 1px dashed rgba(167,139,250,.25); font-size: .78rem; color: var(--txt-soft); }
        .lk-pager { display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 8px; margin-top: 12px; font-size: .82rem; color: var(--txt-soft); }
        .lk-pager button { background: rgba(40,20,90,.6); border: 1px solid rgba(139,92,246,.5); color: #fff; border-radius: 10px; padding: 6px 12px; font-weight: 700; }
        .lk-pager button:disabled { opacity: .4; }

        /* Broadcast */
        .bc-premium { position: absolute; top: 14px; right: 14px; padding: 3px 10px; border-radius: 999px; background: linear-gradient(135deg, #f59e0b, #ec4899); color: #fff; font-size: .68rem; font-weight: 900; letter-spacing: .06em; }
        .bc-aud { display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 4px; }
        .bc-tools { display: flex; flex-wrap: wrap; gap: 6px; margin: 4px 0 8px; }
        .bc-tools button { min-width: 40px; padding: 7px 11px; border-radius: 10px; background: rgba(40,20,90,.6); border: 1px solid rgba(139,92,246,.5); color: #fff; font-weight: 700; }
        .media-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 10px; }
        .media-tile { display: flex; flex-direction: column; align-items: center; gap: 4px; padding: 16px 6px; border-radius: 16px; background: rgba(10,4,30,.55); border: 2px dashed rgba(139,92,246,.5); color: #e9e5ff; font-weight: 700; font-size: .88rem; cursor: pointer; text-align: center; }
        .media-tile.on { border-style: solid; border-color: #34d399; background: rgba(16,185,129,.14); color: #6ee7b7; }
        .mi { font-size: 1.8rem; }
        .media-file { display: none; align-items: center; justify-content: space-between; gap: 10px; margin-top: 10px; padding: 10px 12px; border-radius: 12px; background: rgba(16,185,129,.12); border: 1px solid rgba(16,185,129,.4); color: #d1fae5; font-size: .85rem; word-break: break-all; }
        .tg-preview { background: #0e1621; border-radius: 16px; padding: 14px; margin-bottom: 12px; }
        .tg-bubble { background: #182533; color: #fff; border-radius: 14px 14px 14px 4px; padding: 10px 13px; max-width: 100%; white-space: pre-wrap; word-break: break-word; font-size: .92rem; }
        .tg-media { background: rgba(255,255,255,.08); border-radius: 10px; padding: 8px 10px; margin-bottom: 8px; font-size: .82rem; color: #93c5fd; }
        .bc-hist { background: rgba(10,4,30,.55); border: 1px solid rgba(139,92,246,.3); border-radius: 14px; padding: 12px 14px; margin-bottom: 10px; }
        .bar { height: 9px; border-radius: 99px; background: rgba(255,255,255,.1); overflow: hidden; margin-top: 8px; }
        .bar > i { display: block; height: 100%; width: 0; border-radius: 99px; background: linear-gradient(90deg, #8b5cf6, #38bdf8, #34d399); transition: width .3s ease; }

        /* Upload / send progress (Broadcast) */
        .up-card { display: none; margin-top: 16px; padding: 14px; border-radius: 16px; background: rgba(10,4,30,.7); border: 1px solid rgba(56,189,248,.5); }
        .up-card.done { border-color: rgba(52,211,153,.7); background: rgba(16,185,129,.1); }
        .up-card.err { border-color: rgba(248,113,113,.7); background: rgba(225,29,72,.1); }
        .up-title { display: flex; justify-content: space-between; align-items: center; gap: 8px; font-weight: 800; color: #fff; }
        .up-pct { font-size: 1.5rem; font-weight: 900; color: #7dd3fc; }
        .up-card.done .up-pct { color: #6ee7b7; }
        .up-bar { height: 14px; border-radius: 99px; background: rgba(255,255,255,.12); overflow: hidden; margin: 10px 0; }
        .up-bar > i { display: block; height: 100%; width: 0; border-radius: 99px; background: linear-gradient(90deg, #8b5cf6, #38bdf8); transition: width .25s ease; }
        .up-card.done .up-bar > i { background: linear-gradient(90deg, #10b981, #34d399); }
        .up-grid { display: grid; grid-template-columns: repeat(2, 1fr); gap: 6px 12px; font-size: .82rem; color: var(--txt-soft); }
        .up-grid b { color: #fff; }
        .up-steps { display: flex; gap: 6px; margin-bottom: 8px; flex-wrap: wrap; }
        .up-step { padding: 3px 10px; border-radius: 999px; font-size: .72rem; font-weight: 800; background: rgba(255,255,255,.08); color: var(--txt-soft); }
        .up-step.on { background: rgba(56,189,248,.25); color: #7dd3fc; } .up-step.ok { background: rgba(16,185,129,.22); color: #6ee7b7; }

        /* Profile */
        .owner-card { display: flex; align-items: center; gap: 16px; background: linear-gradient(135deg, #2a1466, #120832); border: 1px solid var(--neon-border); border-radius: 22px; padding: 18px; margin-bottom: 18px; }
        .owner-card h4 { margin: 0 0 6px; font-weight: 800; color: #fff; }
        .crown-big { width: 64px; height: 64px; border-radius: 50%; background: linear-gradient(135deg, #f59e0b, #ec4899, #8b5cf6); display: flex; align-items: center; justify-content: center; font-size: 2rem; flex-shrink: 0; }
        .meter { height: 7px; border-radius: 99px; background: rgba(255,255,255,.12); overflow: hidden; margin: 8px 0 4px; }
        .meter > i { display: block; height: 100%; width: 0; background: #ef4444; transition: width .25s, background .25s; }

        /* Toasts */
        #toastBox { position: fixed; left: 50%; bottom: 22px; transform: translateX(-50%); z-index: 5000; display: flex; flex-direction: column; gap: 8px; align-items: center; width: calc(100% - 28px); max-width: 420px; pointer-events: none; }
        .toast-i { background: #065f46; color: #fff; padding: 12px 18px; border-radius: 14px; font-weight: 700; box-shadow: 0 8px 28px rgba(0,0,0,.5); border: 1px solid #34d399; width: 100%; text-align: center; }
        .toast-i.err { background: #7f1d1d; border-color: #f87171; }

        /* ===== LIVE WALLPAPER (video + song) ===== */
        html { background: #05020c; }
        #bgVideo { position: fixed; top: 0; left: 0; width: 100%; height: 100%; object-fit: cover; z-index: -2; display: none; background: #05020c; }
        #bgDim { position: fixed; top: 0; left: 0; width: 100%; height: 100%; z-index: -1; display: none; pointer-events: none; background: rgba(5,2,12,.45); }
        body.bg-live { background: transparent !important; --card-glass: rgba(24,14,52,.66); }
        body.bg-live #bgVideo, body.bg-live #bgDim { display: block; }
        body.bg-live::before { opacity: .35; }
        body.bg-live .card, body.bg-live .gcard, body.bg-live .stat-card, body.bg-live .gold-box, body.bg-live .cyan-box, body.bg-live .mc, body.bg-live .hero-card { backdrop-filter: blur(9px); -webkit-backdrop-filter: blur(9px); }
        .music-fab { position: fixed; right: 16px; bottom: 18px; z-index: 1500; display: none; align-items: center; gap: 8px; padding: 10px 14px; border-radius: 999px; background: rgba(20,8,46,.88); border: 1px solid #a855f7; color: #fff; font-weight: 800; font-size: .82rem; box-shadow: 0 8px 26px rgba(0,0,0,.5); cursor: pointer; backdrop-filter: blur(8px); }
        .music-fab #musicIc { font-size: 1.15rem; }
        .music-fab.need-tap { animation: fabPulse 1.4s infinite; border-color: #f472b6; }
        .music-fab:not(.need-tap) #musicHint { display: none; }
        @keyframes fabPulse { 0% { box-shadow: 0 0 0 0 rgba(244,114,182,.7); } 70% { box-shadow: 0 0 0 14px rgba(244,114,182,0); } 100% { box-shadow: 0 0 0 0 rgba(244,114,182,0); } }
        .wp-btn { display: inline-flex; align-items: center; gap: 8px; padding: 9px 15px; border: 0; border-radius: 14px; color: #fff; font-weight: 800; font-size: .85rem; background: linear-gradient(135deg,#ec4899,#8b5cf6,#38bdf8); background-size: 220% 220%; animation: wpShift 6s ease infinite; box-shadow: 0 6px 22px rgba(168,85,247,.5); }
        @keyframes wpShift { 0% { background-position: 0% 50%; } 50% { background-position: 100% 50%; } 100% { background-position: 0% 50%; } }
        .topbar { flex-wrap: wrap; gap: 10px; }
        .topbar-tools { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin-left: auto; order: 2; }
        .topbar .crown-ring { order: 3; }
        @media (max-width: 768px) { .topbar-tools { order: 3; flex-basis: 100%; margin-left: 0; } .topbar .crown-ring { order: 2; margin-left: auto; width: 46px; height: 46px; } .topbar .title-grad { font-size: 1.2rem !important; } }
        .live-chip { display: inline-flex; align-items: center; gap: 7px; padding: 5px 11px; border-radius: 999px; font-size: .72rem; font-weight: 700; color: #6ee7b7; background: rgba(16,185,129,.12); border: 1px solid rgba(16,185,129,.35); white-space: nowrap; }
        .live-chip i { width: 8px; height: 8px; border-radius: 50%; background: #34d399; animation: livePulse 1.6s infinite; }
        .live-chip b { color: #fff; min-width: 1.3em; display: inline-block; text-align: right; }
        @keyframes livePulse { 0% { box-shadow: 0 0 0 0 rgba(52,211,153,.7); } 70% { box-shadow: 0 0 0 7px rgba(52,211,153,0); } 100% { box-shadow: 0 0 0 0 rgba(52,211,153,0); } }
        .wp-prog { height: 8px; border-radius: 99px; background: rgba(255,255,255,.12); overflow: hidden; margin-top: 8px; display: none; }
        .wp-prog > i { display: block; height: 100%; width: 0; background: linear-gradient(90deg,#ec4899,#38bdf8); transition: width .2s; }
        .wp-now { font-size: .8rem; color: var(--txt-soft); margin-bottom: 8px; word-break: break-all; }
        .wp-now b { color: #fff; }

        /* ===== MEMBERS & INSPECTOR ===== */
        .mem-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(260px, 1fr)); gap: 14px; }
        .mc { position: relative; overflow: hidden; background: var(--card-glass); border: 1px solid var(--neon-border); border-radius: 20px; padding: 16px 16px 16px 20px; }
        .mc::before { content: ''; position: absolute; left: 0; top: 0; bottom: 0; width: 5px; background: #64748b; }
        .mc.reseller::before { background: linear-gradient(180deg,#f59e0b,#ec4899); }
        .mc .av, .ins-av { width: 46px; height: 46px; border-radius: 15px; display: flex; align-items: center; justify-content: center; font-weight: 900; font-size: 1.2rem; color: #fff; background: linear-gradient(135deg,#8b5cf6,#38bdf8); flex-shrink: 0; }
        .mc-name { font-weight: 800; color: #fff; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 150px; }
        .mc-sub { font-size: .76rem; color: var(--txt-soft); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 170px; }
        .mc-stats { display: grid; grid-template-columns: repeat(3, 1fr); gap: 8px; margin-top: 12px; }
        .mc-stats div { background: rgba(10,4,30,.55); border: 1px solid rgba(139,92,246,.3); border-radius: 12px; padding: 8px 6px; text-align: center; }
        .mc-stats b { display: block; color: #fff; font-size: .95rem; }
        .mc-stats span { font-size: .66rem; color: var(--txt-soft); text-transform: uppercase; letter-spacing: .04em; }
        .ins-head { display: flex; align-items: center; gap: 14px; margin-bottom: 14px; }
        .ins-av { width: 58px; height: 58px; font-size: 1.5rem; border-radius: 18px; }
        .ins-tiles { display: grid; grid-template-columns: repeat(2, 1fr); gap: 10px; margin-bottom: 14px; }
        .ins-tile { background: rgba(10,4,30,.6); border: 1px solid rgba(139,92,246,.35); border-radius: 16px; padding: 12px; }
        .ins-tile small { display: block; color: var(--txt-soft); font-size: .72rem; text-transform: uppercase; letter-spacing: .04em; }
        .ins-tile b { color: #fff; font-size: 1.2rem; }
        .ins-tile.big b { color: #fbbf24; font-size: 1.5rem; }
        .ins-box { background: rgba(10,4,30,.5); border: 1px solid rgba(139,92,246,.3); border-radius: 18px; padding: 14px; margin-bottom: 14px; }
        .ins-box h6 { font-weight: 800; margin-bottom: 10px; color: #c4b5fd; }
        .ins-row { display: flex; justify-content: space-between; gap: 10px; padding: 7px 0; border-bottom: 1px dashed rgba(167,139,250,.25); font-size: .82rem; }
        .ins-row:last-child { border-bottom: 0; }
        .ins-row span:first-child { color: #fff; font-weight: 600; word-break: break-word; }
        .ins-row span:last-child { color: var(--txt-soft); text-align: right; white-space: nowrap; }
        .step-list { counter-reset: st; list-style: none; padding: 0; margin: 0 0 14px; }
        .step-list li { counter-increment: st; position: relative; padding: 8px 8px 8px 44px; margin-bottom: 6px; background: rgba(10,4,30,.5); border-radius: 12px; font-size: .84rem; color: var(--txt-soft); }
        .step-list li::before { content: counter(st); position: absolute; left: 10px; top: 7px; width: 26px; height: 26px; border-radius: 9px; background: #fbbf24; color: #0b0420; font-weight: 900; display: flex; align-items: center; justify-content: center; }
        .field-note { font-size: .78rem; color: var(--txt-soft); margin-top: 6px; line-height: 1.45; }
        .qr-box { text-align: center; background: rgba(10,4,30,.5); border: 1px dashed rgba(56,189,248,.45); border-radius: 18px; padding: 14px; }
</style>
</head>
<body class="{% if bg.video and bg.enabled %}bg-live{% endif %}">
    <video id="bgVideo" muted loop playsinline autoplay preload="auto"></video>
    <div id="bgDim"></div>
    <audio id="bgAudio" loop preload="auto"></audio>
    <div id="musicFab" class="music-fab" onclick="musicToggle()" title="Music on / off"><span id="musicIc">🎵</span><span id="musicHint">Tap to play music</span></div>
    <script>const BG = {{ bg|tojson }};</script>

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
        <a class="sb-link" data-tab="supplier" onclick="showTab('supplier', this)"><span class="sb-ic g5">🔌</span> Supplier API</a>
        <a class="sb-link" data-tab="broadcast" onclick="showTab('broadcast', this)"><span class="sb-ic g8">📢</span> Broadcast</a>
        <a class="sb-link" data-tab="coupons" onclick="showTab('coupons', this)"><span class="sb-ic g4">🎟️</span> Coupon Manager</a>
        <a class="sb-link" data-tab="btncolors" onclick="showTab('btncolors', this)"><span class="sb-ic g1">🎨</span> Button Colors</a>
        <a class="sb-link" data-tab="botscreens" onclick="showTab('botscreens', this)"><span class="sb-ic g6">👋</span> Welcome &amp; Alerts</a>
        <a class="sb-link" data-tab="store" onclick="showTab('store', this)"><span class="sb-ic g7">⚙️</span> Store Settings</a>
        <a class="sb-link" data-tab="security" onclick="showTab('security', this)"><span class="sb-ic g7">👤</span> My Profile</a>
        <a href="/logout" class="sb-link" style="color:#fca5a5;margin-top:10px;"><span class="sb-ic g8">🔒</span> Logout</a>
    </div>

    <!-- 🌟 1. CUSTOMER PROFILE INSPECTOR MODAL -->
    <div class="modal-bg" id="userModal">
        <div class="modal-card">
            <div class="d-flex justify-content-between align-items-start mb-2">
                <h5 class="fw-bold m-0 text-info"><i class="fas fa-user-shield me-2"></i>Customer Profile Inspector</h5>
                <button class="btn-close btn-close-white" onclick="closeInspector()"></button>
            </div>

            <div class="ins-head">
                <div class="ins-av" id="m_av">?</div>
                <div style="min-width:0;flex:1;">
                    <div class="fw-bold fs-5 text-white" id="m_name" style="word-break:break-word;"></div>
                    <div class="small text-muted"><span id="m_user"></span> &middot; ID <code id="m_id"></code>
                        <a href="#" class="ms-1" onclick="copyUid();return false;" title="Copy Telegram ID"><i class="far fa-copy"></i></a></div>
                </div>
                <span id="m_role" class="pill a"></span>
            </div>

            <div class="ins-tiles">
                <div class="ins-tile big"><small>💰 Wallet balance</small><b>&#8377;<span id="m_bal">0.00</span></b></div>
                <div class="ins-tile"><small>🧾 Lifetime spent</small><b>&#8377;<span id="m_spent">0.00</span></b></div>
                <div class="ins-tile"><small>🛒 Orders</small><b id="m_orders">0</b></div>
                <div class="ins-tile"><small>👥 Referrals</small><b><span id="m_refs">0</span> <span class="small text-muted">(&#8377;<span id="m_refearn">0</span>)</span></b></div>
            </div>

            <div class="ins-box">
                <h6>📅 Account</h6>
                <div class="ins-row"><span>Joined</span><span id="m_joined"></span></div>
                <div class="d-flex gap-2 mt-2">
                    <button class="btn btn-sm btn-outline-warning flex-fill" id="m_role_btn" onclick="inspectToggleRole()">👑 Make Reseller</button>
                </div>
            </div>

            <div class="ins-box">
                <h6>💰 Adjust wallet</h6>
                <div class="d-flex gap-2 flex-wrap mb-2">
                    <button class="btn btn-sm btn-outline-success" onclick="quickAddBal(50)">+&#8377;50</button>
                    <button class="btn btn-sm btn-outline-success" onclick="quickAddBal(100)">+&#8377;100</button>
                    <button class="btn btn-sm btn-outline-success" onclick="quickAddBal(500)">+&#8377;500</button>
                    <button class="btn btn-sm btn-outline-danger" onclick="quickAddBal(-50)">-&#8377;50</button>
                    <button class="btn btn-sm btn-outline-danger" onclick="quickAddBal(-100)">-&#8377;100</button>
                </div>
                <div class="d-flex gap-2">
                    <input type="number" id="m_custom_bal" class="form-control form-control-sm" placeholder="Custom amount (+150 or -50)">
                    <button class="btn btn-sm btn-custom" onclick="applyCustomBal()">Apply</button>
                </div>
            </div>

            <div class="ins-box">
                <h6>🛒 Recent orders</h6>
                <div id="m_recent" class="small text-muted">Loading...</div>
            </div>

            <div class="ins-box">
                <h6>💬 Contact</h6>
                <div class="d-flex gap-2 flex-wrap mb-2">
                    <a id="m_dm_link" href="#" rel="noopener" class="btn btn-primary btn-sm flex-fill"><i class="fab fa-telegram me-1"></i> Send DM on Telegram</a>
                </div>
                <div class="field-note" id="m_dm_note"></div>
                <textarea id="m_msg" class="form-control mt-2" rows="2" maxlength="3500" placeholder="Or type a message and send it from your bot..."></textarea>
                <button class="btn btn-sm btn-custom w-100 mt-2" onclick="sendBotMsg()"><i class="fas fa-paper-plane me-1"></i> Message via bot</button>
            </div>

            <div class="text-end">
                <button class="btn btn-secondary btn-sm" onclick="closeInspector()">Close</button>
            </div>
        </div>
    </div>

    <!-- 🎬 LIVE WALLPAPER MODAL -->
    <div class="modal-bg" id="wpModal">
        <div class="modal-card">
            <div class="d-flex justify-content-between align-items-center mb-3">
                <h5 class="fw-bold m-0"><i class="fas fa-film me-2"></i>Live Wallpaper &amp; Music</h5>
                <button class="btn-close btn-close-white" onclick="closeWallpaper()"></button>
            </div>
            <p class="small text-muted">Add a video that plays behind the whole admin panel and a song that plays with it. Both repeat forever, so a song of any length keeps looping.</p>

            <div class="gcard pink" style="margin-bottom:14px;">
                <div class="sec-head"><span class="sec-no">🎬</span><div><h5>Background video</h5><p>Muted loop. Short 720p MP4 works best.</p></div></div>
                <div class="wp-now" id="wp_vnow"></div>
                <label class="small mb-1">Video link (direct .mp4 / .webm address)</label>
                <input type="url" id="wp_vurl" class="form-control mb-2" placeholder="https://.../video.mp4">
                <label class="small mb-1">...or upload a video file (max 50 MB)</label>
                <input type="file" id="wp_vfile" class="form-control" accept="video/*">
                <div class="wp-prog" id="wp_vprog"><i></i></div>
                <button class="btn btn-sm btn-outline-danger mt-2" onclick="wpClear('video')"><i class="fas fa-trash me-1"></i> Remove video</button>
            </div>

            <div class="gcard blue" style="margin-bottom:14px;">
                <div class="sec-head"><span class="sec-no">🎵</span><div><h5>Song</h5><p>Plays together with the video and repeats automatically.</p></div></div>
                <div class="wp-now" id="wp_anow"></div>
                <label class="small mb-1">Song link (direct .mp3 / .m4a address)</label>
                <input type="url" id="wp_aurl" class="form-control mb-2" placeholder="https://.../song.mp3">
                <label class="small mb-1">...or upload a song file (max 20 MB)</label>
                <input type="file" id="wp_afile" class="form-control" accept="audio/*">
                <div class="wp-prog" id="wp_aprog"><i></i></div>
                <button class="btn btn-sm btn-outline-danger mt-2" onclick="wpClear('audio')"><i class="fas fa-trash me-1"></i> Remove song</button>
            </div>

            <div class="gcard green" style="margin-bottom:14px;">
                <div class="sec-head"><span class="sec-no">🎚️</span><div><h5>Look &amp; sound</h5><p>Changes are applied when you press Save.</p></div></div>
                <label class="small mb-1">Music volume: <b id="wp_vol_t">60</b>%</label>
                <input type="range" min="0" max="100" value="60" id="wp_vol" class="form-range mb-3" oninput="wpPreview()">
                <label class="small mb-1">Dark overlay (makes text easier to read): <b id="wp_dim_t">45</b>%</label>
                <input type="range" min="0" max="85" value="45" id="wp_dim" class="form-range mb-3" oninput="wpPreview()">
                <div class="form-check form-switch">
                    <input class="form-check-input" type="checkbox" id="wp_on" checked>
                    <label class="form-check-label fw-bold" for="wp_on">Live wallpaper ON</label>
                </div>
            </div>

            <div class="field-note mb-3">&#9888;&#65039; Browsers do not allow sound before you touch the page. If the music does not start by itself, tap anywhere once (or the music button at the bottom) and it will play and keep looping. YouTube links do not work &mdash; use a direct file link or upload the file.</div>
            <div class="d-flex gap-2">
                <button class="btn-grad flex-fill" id="wp_save" onclick="wpSave()">💾 Save &amp; Apply</button>
                <button class="btn btn-secondary" onclick="closeWallpaper()">Close</button>
            </div>
        </div>
    </div>

    <div class="main-content">
        <div class="topbar">
            <div class="d-flex align-items-center gap-3">
                <button class="menu-btn d-md-none" id="menuBtn" onclick="toggleSidebar()"><i id="menuIcon" class="fas fa-bars"></i></button>
                <div class="title-grad" style="font-size:1.5rem;">⚡ Bot Control Center</div>
            </div>
            <div class="topbar-tools">
                <span class="live-chip" id="liveChip" title="This page updates itself in the background"><i></i> Live &middot; auto refresh in <b id="liveSec">15</b>s</span>
                <button class="wp-btn" onclick="openWallpaper()"><i class="fas fa-film"></i><span>Live Wallpaper</span></button>
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
            <div class="stat-grid" data-live>
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
            <div data-live>
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
                            <option value="carrom">Carrom Pool (game)</option><option value="pool8">8 Ball Pool (game)</option>
                        </select>
                    </div>
                    <div class="col-4"><label class="small mb-1">Icon</label><input type="text" id="pm_icon" class="form-control" placeholder="⚡"></div>
                </div>
                <label class="small mb-1">Channel / Download Link (optional)</label>
                <input type="text" id="pm_link" class="form-control mb-3" placeholder="e.g. https://t.me/yourchannel">
                <div class="small text-muted mb-3">This link becomes the <b>Preview Video</b> button at the bottom of the product page.</div>
                <hr style="border-color:rgba(147,51,234,.4);">
                <div class="fw-bold mb-2" style="color:#c4b5fd;">&#128444;&#65039; Product page (what the customer sees)</div>
                <label class="small mb-1">Banner image (optional)</label>
                <input type="file" id="pm_img" accept="image/*" class="form-control mb-1" onchange="pmPickImg(this)">
                <div id="pm_img_note" class="small text-muted mb-1"></div>
                <img id="pm_img_prev" class="mb-2" style="max-width:100%;max-height:150px;border-radius:10px;display:none">
                <button type="button" class="btn btn-sm btn-outline-danger mb-2" id="pm_img_rm" style="display:none" onclick="pmRemoveImg()">&#128465; Remove image</button>
                <label class="small mb-1">Title under the image (empty = product name)</label>
                <input type="text" id="pm_ptitle" class="form-control mb-2" placeholder="e.g. HG CHEATS APK MOD">
                <label class="small mb-1">Features - one per line (shown in a quote box)</label>
                <textarea id="pm_feat" rows="4" class="form-control mb-3" placeholder="Silent Aim&#10;ESP Location&#10;Non Root Working"></textarea>
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
                        <tbody data-live>
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
            <style>
                @keyframes dmIn{from{opacity:0;transform:translateY(18px) scale(.97)}to{opacity:1;transform:none}}
                @keyframes dmGrow{from{width:0}to{width:var(--w)}}
                @keyframes dmFire{0%,100%{transform:scale(1) rotate(-4deg)}50%{transform:scale(1.25) rotate(5deg)}}
                @keyframes dmFloat{0%,100%{transform:translateY(0)}50%{transform:translateY(-10px)}}
                @keyframes dmPulse{0%{box-shadow:0 0 0 0 rgba(239,108,74,.6)}100%{box-shadow:0 0 0 14px rgba(239,108,74,0)}}
                @keyframes dmShine{from{background-position:-200% 0}to{background-position:200% 0}}
                .dm-fire{display:inline-block;animation:dmFire 1.2s ease-in-out infinite}
                .dm-chips{display:flex;gap:10px;flex-wrap:wrap;margin:6px 0 12px}
                .dm-chip{flex:1;min-width:110px;border-radius:14px;padding:10px 12px;background:rgba(255,255,255,.06);border:1px solid rgba(255,255,255,.1);animation:dmIn .5s both}
                .dm-chip b{display:block;font-size:22px}
                .dm-chip small{opacity:.8}
                .dm-item{display:flex;align-items:center;gap:12px;flex-wrap:wrap;border-radius:16px;padding:12px;margin-top:10px;background:rgba(255,255,255,.05);border:1px solid rgba(255,255,255,.1);animation:dmIn .55s both}
                .dm-item.top{border-color:#ef6c4a;background:linear-gradient(120deg,rgba(239,108,74,.18),rgba(250,204,21,.12));animation:dmIn .55s both,dmPulse 1.8s ease-out infinite}
                .dm-rank{font-size:30px;width:44px;text-align:center;flex:none}
                .dm-main{flex:1;min-width:170px}
                .dm-name{font-weight:700;font-size:15px}
                .dm-meta{font-size:12px;opacity:.85;margin:2px 0 6px}
                .dm-track{height:12px;border-radius:7px;background:rgba(255,255,255,.08);overflow:hidden}
                .dm-bar{height:100%;border-radius:7px;width:var(--w);animation:dmGrow 1.1s cubic-bezier(.2,.8,.2,1) both;background:linear-gradient(90deg,#f59e0b,#ef6c4a,#f59e0b);background-size:200% 100%}
                .dm-item.top .dm-bar{animation:dmGrow 1.1s cubic-bezier(.2,.8,.2,1) both,dmShine 2.2s linear infinite}
                .dm-num{text-align:center;flex:none;min-width:64px}
                .dm-num .dm-n{font-size:26px;font-weight:800;display:block;line-height:1}
                .dm-num small{font-size:11px;opacity:.8}
                .dm-tag{display:inline-block;font-size:11px;font-weight:700;padding:2px 8px;border-radius:12px;margin-left:6px;background:#d63a2f;color:#fff}
                .dm-tag.warm{background:#d98a14}.dm-tag.new{background:#4b5563}
                .dm-empty{text-align:center;padding:26px 10px}
                .dm-float{font-size:46px;animation:dmFloat 2.2s ease-in-out infinite}
            </style>
            <div class="card p-4 mb-3">
                <div class="d-flex align-items-center justify-content-between flex-wrap gap-2">
                    <h5 class="mb-0"><span class="dm-fire">&#128293;</span> Out-of-Stock Demand &mdash; what customers wanted</h5>
                    <div class="d-flex gap-2">
                        <button class="btn btn-sm btn-outline-light" onclick="loadDemand(true)">&#128260; Refresh</button>
                        <button class="btn btn-sm btn-outline-danger" onclick="clearDemand()">&#128465; Clear all</button>
                    </div>
                </div>
                <p class="text-muted small mb-1">Every time a customer tries to buy something with no key in stock, it appears here (product, plan and price). Their money stays safe in their bot wallet. <b>Add keys for the top ones first</b>, then press &#9989; Stock added.</p>
                <div id="dm_chips" class="dm-chips"></div>
                <div id="dm_list"><div class="dm-empty text-muted small">Loading...</div></div>
            </div>
            <div class="card p-4">
                <h5>⏳ Pending Keys Queue (Out-of-Stock Manual Dispatch)</h5>
                <p class="text-muted small">Orders waiting for stock. Enter fresh key and click send to dispatch directly to customer Telegram!</p>
                <div class="table-responsive">
                    <table class="table table-dark">
                        <thead><tr><th>User ID</th><th>Product</th><th>Plan</th><th>Paid</th><th>Action</th></tr></thead>
                        <tbody data-live>
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
            <div class="page-head">
                <div class="ph-ic">👥</div>
                <div><h3>Members &amp; Wallets</h3><p>Everyone who started your bot. Tap <b>Inspect Profile</b> to see orders, change the wallet or contact the member.</p></div>
            </div>

            <div class="gcard blue">
                <div class="sec-head"><span class="sec-no">1</span><div><h5>Overview</h5><p>Your community at a glance.</p></div></div>
                <div class="ov-grid" data-live>
                    <div class="ov"><div class="oi">👥</div><div class="ov-v">{{ bc_counts.all }}</div><div class="ov-l">Total members</div></div>
                    <div class="ov"><div class="oi">🛒</div><div class="ov-v">{{ bc_counts.customers }}</div><div class="ov-l">Buyers</div></div>
                    <div class="ov"><div class="oi">👑</div><div class="ov-v">{{ bc_counts.resellers }}</div><div class="ov-l">Resellers</div></div>
                    <div class="ov"><div class="oi">💰</div><div class="ov-v">&#8377;{{ stats.total_wallet }}</div><div class="ov-l">Total in wallets</div></div>
                </div>
            </div>

            <div class="gcard green">
                <div class="sec-head"><span class="sec-no">2</span><div><h5>Find a member</h5><p>Search by name, @username or Telegram ID, or pick a filter.</p></div></div>
                <input type="text" id="memSearch" class="form-control mb-3" placeholder="🔍 Search by name, @username, or Telegram ID..." oninput="memFilter()">
                <div class="d-flex gap-2 flex-wrap" id="memChips">
                    <button type="button" class="chip-btn on" data-f="all" onclick="memChip(this)">All</button>
                    <button type="button" class="chip-btn" data-f="reseller" onclick="memChip(this)">👑 Resellers</button>
                    <button type="button" class="chip-btn" data-f="regular" onclick="memChip(this)">Regular</button>
                    <button type="button" class="chip-btn" data-f="buyers" onclick="memChip(this)">🛒 Buyers</button>
                    <button type="button" class="chip-btn" data-f="balance" onclick="memChip(this)">💰 Has balance</button>
                </div>
            </div>

            <div class="gcard">
                <div class="sec-head"><span class="sec-no">3</span><div><h5>Member list</h5><p>Newest members first (latest 500).</p></div><span class="pill b" id="memCount">0</span></div>
                <div class="mem-grid" id="memGrid" data-live>
                    {% for u in users_list %}
                    <div class="mc {{ 'reseller' if u[9] == 'Reseller' else '' }}"
                         data-uid="{{ u[0] }}" data-name="{{ u[1] or '' }}" data-user="{{ u[2] or '' }}" data-joined="{{ u[3] or '' }}"
                         data-bal="{{ u[5] or 0 }}" data-spent="{{ u[6] or 0 }}" data-orders="{{ u[4] or 0 }}"
                         data-refs="{{ u[7] or 0 }}" data-refearn="{{ u[8] or 0 }}" data-role="{{ u[9] }}"
                         data-search="{{ ((u[0]|string) ~ ' ' ~ (u[1] or '') ~ ' ' ~ (u[2] or ''))|lower }}">
                        <div class="d-flex gap-3 align-items-center">
                            <div class="av">{{ (u[1] or '?')[:1]|upper }}</div>
                            <div style="min-width:0;">
                                <div class="mc-name">{{ u[1] or 'Unknown' }}</div>
                                <div class="mc-sub">{{ u[2] or 'N/A' }} &middot; {{ u[0] }}</div>
                            </div>
                            <span class="pill {{ 'a' if u[9] == 'Reseller' else '' }} ms-auto">{{ '👑 Reseller' if u[9] == 'Reseller' else 'Regular' }}</span>
                        </div>
                        <div class="mc-stats">
                            <div><b>&#8377;{{ '%.2f'|format(u[5] or 0) }}</b><span>Balance</span></div>
                            <div><b>{{ u[4] or 0 }}</b><span>Orders</span></div>
                            <div><b>&#8377;{{ '%.0f'|format(u[6] or 0) }}</b><span>Spent</span></div>
                        </div>
                        <button class="btn-grad w-100 mt-3" onclick="openInspector(this.closest('.mc'))"><i class="fas fa-eye me-1"></i> Inspect Profile</button>
                    </div>
                    {% endfor %}
                </div>
                <div id="memEmpty" class="text-center text-muted py-4" style="display:none;">No member matches your search.</div>
            </div>
        </div>

        <!-- 7️⃣ 🤝 TAB: RESELLERS -->
        <div class="tab-pane-content" id="tab-resellers" style="display:none;">
            <div class="gcard amber">
                <div class="sec-head"><span class="sec-no">👑</span><div><h5>Reseller plan screen</h5><p>The bot shows exactly what you write here. Placeholders: {min_balance} {fee} {rest} {balance}.</p></div></div>
                <div class="form-check form-switch mb-3">
                    <input class="form-check-input" type="checkbox" id="rs_on" {% if ui.r_enabled not in ('0','false','False','') %}checked{% endif %}>
                    <label class="form-check-label" for="rs_on">Reseller plan is on</label>
                </div>
                <div class="row g-2 mb-2">
                    <div class="col-4"><label class="small mb-1">Min balance ₹</label><input type="number" min="0" id="rs_min" class="form-control" value="{{ ui.r_min }}" oninput="rsPrev()"></div>
                    <div class="col-4"><label class="small mb-1">Fee ₹</label><input type="number" min="0" id="rs_fee" class="form-control" value="{{ ui.r_fee }}" oninput="rsPrev()"></div>
                    <div class="col-4"><label class="small mb-1">Price factor</label><input type="number" min="0" step="0.01" id="rs_factor" class="form-control" value="{{ ui.r_factor }}"></div>
                </div>
                <label class="small mb-1">Plan screen text</label>
                <textarea id="rs_text" class="form-control mb-2" rows="6" oninput="rsPrev()">{{ ui.r_text }}</textarea>
                <div class="pv-box mb-3" id="rs_prev"></div>
                <label class="small mb-1">Buy button text</label>
                <input type="text" id="rs_buy" class="form-control mb-2" maxlength="60" value="{{ ui.r_buy }}">
                <label class="small mb-1">Success message</label>
                <textarea id="rs_ok" class="form-control mb-2" rows="3">{{ ui.r_success }}</textarea>
                <label class="small mb-1">Not-enough-balance message</label>
                <textarea id="rs_low" class="form-control mb-3" rows="3">{{ ui.r_low }}</textarea>
                <button class="btn-grad" onclick="saveReseller()">💾 Save reseller settings</button>
            </div>
            <div class="card p-4">
                <h5>🤝 Resellers Management</h5>
                <div class="table-responsive mt-3">
                    <table class="table table-dark align-middle">
                        <thead><tr><th>Telegram ID</th><th>Name</th><th>Wallet</th><th>Promote / Demote</th></tr></thead>
                        <tbody data-live>
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
                <div><h3>Broadcast</h3><p>Send a message, photo, video or voice note to your users.</p></div>
            </div>

            <!-- STEP 1: AUDIENCE -->
            <div class="gcard">
                <div class="sec-head">
                    <span class="sec-no">1</span>
                    <div><h5>Choose Audience</h5><p>Who should receive this broadcast?</p></div>
                </div>
                <div class="bc-aud" id="bc_aud">
                    <button type="button" class="chip-btn on" data-aud="all" data-n="{{ bc_counts.all }}" onclick="bcAud(this)">👥 All ({{ bc_counts.all }})</button>
                    <button type="button" class="chip-btn" data-aud="customers" data-n="{{ bc_counts.customers }}" onclick="bcAud(this)">🛒 Buyers ({{ bc_counts.customers }})</button>
                    <button type="button" class="chip-btn" data-aud="non_buyers" data-n="{{ bc_counts.non_buyers }}" onclick="bcAud(this)">🆕 Not bought yet ({{ bc_counts.non_buyers }})</button>
                    <button type="button" class="chip-btn" data-aud="resellers" data-n="{{ bc_counts.resellers }}" onclick="bcAud(this)">👑 Resellers ({{ bc_counts.resellers }})</button>
                </div>
            </div>

            <!-- STEP 2: MESSAGE -->
            <div class="gcard blue">
                <div class="sec-head">
                    <span class="sec-no">2</span>
                    <div><h5>Write Message</h5><p>Use the buttons for bold, italic, code, links and emojis.</p></div>
                </div>
                <div class="bc-tools">
                    <button type="button" onclick="bcWrap('b')"><b>B</b></button>
                    <button type="button" onclick="bcWrap('i')"><i>I</i></button>
                    <button type="button" onclick="bcWrap('code')">&lt;/&gt;</button>
                    <button type="button" onclick="bcLink()">🔗 Link</button>
                    <button type="button" onclick="bcEmoji('🔥')">🔥</button>
                    <button type="button" onclick="bcEmoji('🎁')">🎁</button>
                    <button type="button" onclick="bcEmoji('⚡')">⚡</button>
                </div>
                <textarea id="bc_msg" class="form-control" rows="6" placeholder="Type your message here..." oninput="bcPreview()"></textarea>
                <div class="set-note"><span id="bc_count">0</span> characters · with media, text over 1024 characters is sent as a separate message.</div>
            </div>

            <!-- STEP 3: MEDIA -->
            <div class="gcard amber">
                <div class="sec-head">
                    <span class="sec-no">3</span>
                    <div><h5>Attach Media <span class="pill">optional</span></h5><p>One file only. Maximum 50 MB.</p></div>
                </div>
                <div class="media-grid">
                    <label class="media-tile" id="mt_image"><span class="mi">🖼️</span>Image<input type="file" id="bc_image" accept="image/*" hidden onchange="bcPick('image', this)"></label>
                    <label class="media-tile" id="mt_video"><span class="mi">🎬</span>Video<input type="file" id="bc_video" accept="video/*" hidden onchange="bcPick('video', this)"></label>
                    <label class="media-tile" id="mt_voice"><span class="mi">🎙️</span>Voice<input type="file" id="bc_voice" accept="audio/*,.ogg,.oga,.opus" hidden onchange="bcPick('voice', this)"></label>
                </div>
                <div class="media-file" id="bc_file"><span id="bc_file_name"></span><button type="button" class="btn-danger-soft" onclick="bcClearMedia()">✕ Remove</button></div>
                <div class="set-note">Voice: a .ogg (Opus) file plays as a voice note; mp3 / m4a are sent as audio.</div>
            </div>

            <!-- STEP 4: PREVIEW & SEND -->
            <div class="gcard green">
                <span class="bc-premium">✨ PREMIUM</span>
                <div class="sec-head">
                    <span class="sec-no">4</span>
                    <div><h5>Preview &amp; Send</h5><p>Check how it looks, send yourself a test, then send to everyone.</p></div>
                </div>
                <div class="tg-preview"><div class="tg-bubble" id="bc_prev"><span style="color:#8d96b5;">Your message preview appears here...</span></div></div>
                <button class="btn-send" id="bc_send" onclick="sendBroadcast()">🚀 Send to All {{ bc_counts.all }} Users</button>
                <button class="btn-ghost" id="bc_test" onclick="sendBroadcast(true)">📨 Send Test to Me First</button>

                <!-- Live upload / sending progress -->
                <div class="up-card" id="bc_prog">
                    <div class="up-steps">
                        <span class="up-step" id="ps_up">1 · Upload</span>
                        <span class="up-step" id="ps_send">2 · Sending</span>
                        <span class="up-step" id="ps_done">3 · Done</span>
                    </div>
                    <div class="up-title"><span id="up_title">Uploading...</span><span class="up-pct" id="up_pct">0%</span></div>
                    <div class="up-bar"><i id="up_bar"></i></div>
                    <div class="up-grid" id="up_grid"></div>
                </div>
            </div>

            <!-- HISTORY -->
            <div class="gcard pink">
                <div class="sec-head">
                    <span class="sec-no">5</span>
                    <div><h5>Broadcast History</h5><p>Your last 15 broadcasts with delivery results.</p></div>
                </div>
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
            <div class="page-head">
                <div class="ph-ic">💳</div>
                <div><h3>UPI Payment Setup</h3><p>Choose where customers pay, connect Gmail for automatic confirmation and watch every payment in one place.</p></div>
            </div>

            <!-- 1. OVERVIEW -->
            <div class="gcard blue">
                <div class="sec-head"><span class="sec-no">1</span><div><h5>Payment overview</h5><p>Live numbers for today.</p></div></div>
                <div class="ov-grid" data-live>
                    <div class="ov"><div class="oi">📅</div><div class="ov-v">&#8377;{{ stats.today_deposit }}</div><div class="ov-l">Today's deposit</div></div>
                    <div class="ov"><div class="oi">✅</div><div class="ov-v">{{ pay_stats.today_count }}</div><div class="ov-l">Verified today</div></div>
                    <div class="ov"><div class="oi">⏳</div><div class="ov-v">{{ stats.awaiting }}</div><div class="ov-l">Awaiting review</div></div>
                    <div class="ov"><div class="oi">❓</div><div class="ov-v">{{ pay_stats.unmatched }}</div><div class="ov-l">Unmatched credits</div></div>
                    <div class="ov"><div class="oi">🤖</div><div class="ov-v">{{ 'ON' if pay_cfg.auto_verify else 'OFF' }}</div><div class="ov-l">Auto confirm</div></div>
                    <div class="ov"><div class="oi">📧</div><div class="ov-v">{{ 'Linked' if (gmail.user and gmail.has_password) else 'Not set' }}</div><div class="ov-l">Gmail</div></div>
                </div>
            </div>

            <!-- 2. UPI DETAILS -->
            <div class="gcard green">
                <div class="sec-head"><span class="sec-no">2</span><div><h5>UPI details</h5><p>Customers scan a QR made from this UPI ID (FamPay, GPay, PhonePe...). Changes reach the bot <b>instantly</b>.</p></div></div>
                <div class="row g-3">
                    <div class="col-md-7">
                        <label class="small mb-1">UPI ID (where you receive the money)</label>
                        <input type="text" id="upi_id" class="form-control mb-3" value="{{ pay_cfg.upi_id }}" placeholder="yourname@fam" oninput="updateUpiPreview()">
                        <label class="small mb-1">Payee / Shop name</label>
                        <input type="text" id="upi_name" class="form-control mb-3" value="{{ pay_cfg.payee_name }}" placeholder="Elite Hackers Store" oninput="updateUpiPreview()">
                        <div class="ins-box mb-3">
                            <div class="form-check form-switch">
                                <input class="form-check-input" type="checkbox" id="auto_verify" {% if pay_cfg.auto_verify %}checked{% endif %}>
                                <label class="form-check-label fw-bold" for="auto_verify">🤖 Automatic payment confirmation</label>
                            </div>
                            <div class="field-note"><b>ON:</b> every customer pays an exact amount (for example &#8377;100.07). The bot reads your FamPay &ldquo;You received...&rdquo; e-mail in Gmail every 5 seconds, checks amount, UTR and time, and confirms the payment by itself.<br><b>OFF:</b> you approve each payment in Telegram.</div>
                        </div>
                        <button class="btn-grad w-100" onclick="saveUpiSettings()"><i class="fas fa-save me-1"></i> Save UPI settings</button>
                    </div>
                    <div class="col-md-5">
                        <div class="qr-box">
                            <div class="small text-muted mb-2">Live QR preview (&#8377;1 test amount)</div>
                            <img id="upi_qr_preview" alt="QR preview" style="width:210px;max-width:100%;background:#fff;padding:8px;border-radius:12px;display:none;">
                            <div id="upi_qr_empty" class="small text-warning">Enter your UPI ID to see the QR.</div>
                        </div>
                    </div>
                </div>
            </div>

            <!-- 3. GMAIL -->
            <div class="gcard amber">
                <div class="sec-head"><span class="sec-no">3</span><div><h5>FamPay e-mail connection (Gmail)</h5><p>This is what makes automatic confirmation work.</p></div></div>
                <ol class="step-list">
                    <li>Use the Gmail that receives your FamPay &ldquo;You received &#8377;...&rdquo; e-mails.</li>
                    <li>Google Account &rarr; Security &rarr; turn on <b>2-Step Verification</b> &rarr; <b>App passwords</b> &rarr; create one (16 letters).</li>
                    <li>Fill the Gmail address and the App Password below and press <b>Save</b>.</li>
                    <li>Press <b>Connect &amp; detect sender</b>. It finds FamPay's sender domain by itself.</li>
                    <li>Switch on <b>Automatic payment confirmation</b> above, save, and test with &#8377;10.</li>
                </ol>
                <div class="row g-3">
                    <div class="col-md-6">
                        <label class="small mb-1">Gmail address</label>
                        <input type="email" id="gm_user" class="form-control" value="{{ gmail.user }}" placeholder="you@gmail.com" autocomplete="off">
                    </div>
                    <div class="col-md-6">
                        <label class="small mb-1">App Password {% if gmail.has_password %}<span class="pill g">saved</span>{% endif %}</label>
                        <input type="password" id="gm_pass" class="form-control" placeholder="{% if gmail.has_password %}saved &mdash; leave empty to keep it{% else %}16-letter App Password{% endif %}" autocomplete="new-password">
                    </div>
                    <div class="col-12">
                        <label class="small mb-1">FamPay sender domain (filled by the detect button)</label>
                        <input type="text" id="gm_domain" class="form-control" value="{{ gmail.domain }}" placeholder="for example famapp.in" autocomplete="off">
                    </div>
                    <div class="col-12 d-flex gap-2 flex-wrap">
                        <button class="btn-grad flex-fill" onclick="saveGmail()"><i class="fas fa-save me-1"></i> Save</button>
                        <button class="btn btn-outline-warning flex-fill" onclick="detectGmail()"><i class="fas fa-magnifying-glass me-1"></i> Connect &amp; detect sender</button>
                    </div>
                    <div class="col-12"><div class="ins-box mb-0 small" id="gm_status"><b>Status:</b> {{ gmail.status }}</div></div>
                </div>
            </div>

            <!-- 4. TEST -->
            <div class="gcard pink">
                <div class="sec-head"><span class="sec-no">4</span><div><h5>Test an e-mail</h5><p>Paste the text of a payment e-mail. Nothing is credited.</p></div></div>
                <textarea id="test_sms" class="form-control mb-2" rows="3" placeholder="Paste the text of a FamPay e-mail, for example: You received 10.01 in your FamX account ... UTR 632512726024"></textarea>
                <button class="btn btn-custom w-100" onclick="testParse()"><i class="fas fa-vial me-1"></i> Test this message</button>
                <div id="test_result" class="small mt-2 text-info"></div>
            </div>

            <!-- 5. E-MAILS READ -->
            <div class="gcard">
                <div class="sec-head"><span class="sec-no">5</span><div><h5>Recent payment e-mails</h5><p>The last 20 e-mails the bot read from Gmail.</p></div></div>
                <div class="table-responsive">
                    <table class="table table-dark align-middle mb-0">
                        <thead><tr><th>Time (IST)</th><th>Amount</th><th>UTR</th><th>Result</th><th>Message</th></tr></thead>
                        <tbody data-live>
                            {% for a in alerts %}
                            <tr>
                                <td class="small">{{ a.time_txt }}</td>
                                <td>{% if a.amount is not none %}&#8377;{{ '%.2f'|format(a.amount) }}{% else %}-{% endif %}</td>
                                <td><code>{{ a.utr or '-' }}</code></td>
                                <td><span class="pill {{ {'matched': 'g', 'unmatched': 'r', 'logged': 'b'}.get(a.status, '') }}">{{ a.status }}</span>{% if a.order_id %}<br><small><code>{{ a.order_id }}</code></small>{% endif %}</td>
                                <td class="small text-muted">{{ a.snippet }}</td>
                            </tr>
                            {% else %}
                            <tr><td colspan="5" class="text-center text-muted">No payment e-mails read yet.</td></tr>
                            {% endfor %}
                        </tbody>
                    </table>
                </div>
                <div class="field-note"><span class="pill g">matched</span> credited automatically &nbsp; <span class="pill r">unmatched</span> money arrived but no order has that exact amount (check manually) &nbsp; <span class="pill">ignored</span> not a &ldquo;money received&rdquo; e-mail &nbsp; <span class="pill b">logged</span> read while automatic mode was OFF</div>
            </div>

            <!-- 6. DEPOSIT HISTORY -->
            <div class="gcard blue">
                <div class="sec-head"><span class="sec-no">6</span><div><h5>Deposit history</h5><p>The latest 15 wallet top-up requests (full list in the Top-ups page).</p></div></div>
                <div class="table-responsive">
                    <table class="table align-middle mb-0">
                        <thead><tr><th>Time</th><th>User ID</th><th>Amount</th><th>Status</th><th>Order</th></tr></thead>
                        <tbody data-live>
                            {% for t in topups[:15] %}
                            <tr>
                                <td class="small">{{ t[0] }}</td>
                                <td><code>{{ t[1] }}</code></td>
                                <td>&#8377;{{ '%.2f'|format(t[2] or 0) }}</td>
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

        <!-- 1️⃣1️⃣ 💳 TAB: TOP-UPS QUEUE -->
        <div class="tab-pane-content" id="tab-supplier" style="display:none;">
            <div class="page-head">
                <div class="ph-ic">🔌</div>
                <div><h3>Supplier API Setup</h3><p>Connect the other panel (panelstore.shop). When your own key stock is empty, the bot buys the key from them instantly.</p></div>
            </div>

            <style>
                .sup-flow{display:flex;align-items:center;justify-content:center;gap:6px;flex-wrap:wrap;text-align:center;margin:6px 0 10px}
                .sup-node{border-radius:14px;padding:10px 12px;min-width:92px;background:rgba(255,255,255,.07);border:1px solid rgba(255,255,255,.12)}
                .sup-node .big{font-size:26px;line-height:1.1}
                .sup-node b{display:block;font-size:15px}
                .sup-node small{opacity:.8}
                .sup-arr{font-size:20px;opacity:.8;text-align:center;line-height:1.1}
                .sup-arr small{display:block;font-size:11px}
                .sup-win{border-color:#3fb67a;background:rgba(63,182,122,.15)}
                .sup-lose{border-color:#ef6c4a;background:rgba(239,108,74,.15)}
                .sup-row{border-radius:12px;padding:10px;margin-top:10px;background:rgba(255,255,255,.04);border:1px solid rgba(255,255,255,.1)}
                .sup-money{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin:8px 0}
                .sup-box{flex:1;min-width:120px;border-radius:10px;padding:8px;text-align:center}
                .sup-box.theirs{background:rgba(239,108,74,.15);border:1px solid #ef6c4a}
                .sup-box.mine{background:rgba(63,182,122,.15);border:1px solid #3fb67a}
                .sup-box .lbl{display:block;font-size:12px;opacity:.9}
                .sup-box .amt{font-size:20px;font-weight:700}
                .sup-box input{max-width:110px;margin:2px auto 0;text-align:center;font-weight:700}
                .sup-sub{font-size:12px;font-weight:700;margin-top:8px}
                .sup-bl{display:flex;align-items:center;gap:6px;font-size:12px;margin-top:3px}
                .sup-bl .t{width:112px;flex:none}
                .sup-bl .track{flex:1;height:12px;border-radius:6px;background:rgba(255,255,255,.08);overflow:hidden}
                .sup-bl .bar{height:100%;border-radius:6px}
                .sup-bl .bar.cost{background:#ef6c4a}
                .sup-bl .bar.mine{background:#3fb67a}
                .sup-bl .v{width:62px;flex:none;text-align:right;font-weight:700}
                @keyframes supLive{0%{box-shadow:0 0 0 0 rgba(63,182,122,.7)}100%{box-shadow:0 0 0 9px rgba(63,182,122,0)}}
                .sup-live{display:inline-block;width:9px;height:9px;border-radius:50%;background:#3fb67a;margin-right:4px;animation:supLive 1.4s ease-out infinite}
                .sup-bal{font-size:20px;font-weight:800}
                .sup-badge{display:inline-block;margin-top:5px;padding:3px 10px;border-radius:20px;font-weight:700;font-size:13px}
                .sup-badge.good{background:#1f8f5a;color:#fff}
                .sup-badge.bad{background:#d63a2f;color:#fff}
            </style>

            <div class="ins-box mb-3">
                <b>&#128214; Look at this picture (example)</b>
                <div class="sup-flow">
                    <div class="sup-node sup-lose"><div class="big">&#127981;</div><b>THEIR shop</b><small>sells to you for</small><b>&#8377;50</b></div>
                    <div class="sup-arr">&#10145;&#65039;<small>you buy</small></div>
                    <div class="sup-node"><div class="big">&#129489;&#8205;&#128188;</div><b>YOU</b><small>sell to customer for</small><b>&#8377;100</b></div>
                    <div class="sup-arr">&#10145;&#65039;<small>customer buys</small></div>
                    <div class="sup-node sup-win"><div class="big">&#128176;</div><b>Your profit</b><small>100 - 50 =</small><b>&#8377;50</b></div>
                </div>
                <div class="small"><span class="text-danger"><b>Orange = money you pay them.</b></span> &nbsp; <span class="text-success"><b>Green = money customer pays you.</b></span> &nbsp; Green is longer than orange = &#9989; profit.</div>
            </div>

            <div class="ins-box small mb-3">
                <b>How it works (very simple)</b><br>
                1&#65039;&#8419; <b>Connect</b> - paste their API key once.<br>
                2&#65039;&#8419; <b>Add their plan to your shop</b> - you see <b>THEIR price</b> (what you pay), you type <b>YOUR price</b> (what customer pays). <span class="text-success"><b>Green = profit</b></span>, <span class="text-danger"><b>Red = loss</b></span>.<br>
                3&#65039;&#8419; <b>Done</b> - when a customer buys and your own stock is empty, the bot buys the key from them automatically. You keep the difference.<br>
                <span class="text-secondary">Your old products and prices are not changed.</span>
            </div>

            <div class="gcard blue">
                <div class="sec-head"><span class="sec-no">1</span><div><h5>&#128273; Connect (do this once)</h5><p>Paste the API key from their "Reseller API Hub" page.</p></div></div>
                <div class="row g-3">
                    <div class="col-12">
                        <label class="small mb-1">API URL</label>
                        <input type="text" id="sup_url" class="form-control" placeholder="https://panelstore.shop/api/v1/index.php" autocomplete="off">
                    </div>
                    <div class="col-12">
                        <label class="small mb-1">API Key <span class="pill g" id="sup_saved" style="display:none;">saved</span></label>
                        <input type="password" id="sup_key" class="form-control" placeholder="usr_..." autocomplete="new-password">
                    </div>
                    <div class="col-12 d-flex gap-2 flex-wrap">
                        <button class="btn-grad flex-fill" onclick="supSave()"><i class="fas fa-save me-1"></i> Save API Settings</button>
                        <button class="btn btn-outline-warning flex-fill" onclick="supTest()"><i class="fas fa-vial me-1"></i> Test Connection</button>
                    </div>
                    <div class="col-12"><div class="ins-box mb-0 small" id="sup_status"><b>Status:</b> not tested yet</div></div>
                </div>
            </div>

            <div class="gcard blue">
                <div class="sec-head"><span class="sec-no">2</span><div><h5>&#10133; Add their product to my shop</h5><p>Press <b>Load</b>, open a product, tick the plans you want, check <b>YOUR price</b> (change it if you like) and press <b>Add to my shop</b>.</p></div></div>
                <div class="d-flex gap-2 flex-wrap mb-2">
                    <button class="btn btn-outline-warning flex-fill" onclick="supLoadServices(true)"><i class="fas fa-rotate me-1"></i> Load / refresh their products</button>
                </div>
                <div class="row g-2 mb-2">
                    <div class="col-7"><input type="text" id="sup_q" class="form-control" placeholder="Search (drip, silent, ios...)" oninput="supRenderImport()"></div>
                    <div class="col-5"><div class="input-group"><span class="input-group-text">Price x</span><input type="number" id="sup_mult" class="form-control" value="2" step="0.1" min="1"><button class="btn btn-outline-warning" onclick="supApplyMult()">Apply</button></div></div>
                </div>
                <div class="ins-box small mb-2"><b>Auto price:</b> YOUR price = THEIR price x the number above.<br>Example: their price &#8377;50, x2 &rarr; your price &#8377;100, profit &#8377;50.<br><b>Reseller customers pay only 60% of your price</b>, so keep the number at <b>1.7 or more</b> to stay in profit. You can also type any price by hand in each row.</div>
                <div class="small mb-2" id="sup_imp_info"></div>
                <div id="sup_import"></div>
            </div>

            <div class="gcard blue">
                <div class="sec-head"><span class="sec-no">3</span><div><h5>&#128717; My shop products (see / remove)</h5><p>All products in your shop. Tap a product to open it. Press &#128465; to remove a plan. (Optional: pick THEIR product and press Save to link a plan.)</p></div></div>
                <div class="d-flex gap-2 flex-wrap mb-2">
                    <button class="btn btn-outline-warning flex-fill" onclick="supLoadServices(true)"><i class="fas fa-rotate me-1"></i> Load / refresh their products</button>
                </div>
                <div class="small mb-2" id="sup_svc_info">Their products are not loaded yet.</div>
                <datalist id="sup_dl"></datalist>
                <div id="sup_maps"></div>
            </div>

            <div class="gcard blue">
                <div class="sec-head"><span class="sec-no">4</span><div><h5>&#129514; Test order (spends real money)</h5><p>Buys ONE real key from their panel. Use their cheapest product (testwithapi, 1 Hour, &#8377;0.50).</p></div></div>
                <div class="row g-3">
                    <div class="col-6"><label class="small mb-1">Their PID</label><input type="number" id="sup_t_pid" class="form-control" placeholder="107"></div>
                    <div class="col-6 d-flex align-items-end"><button class="btn-grad w-100" onclick="supTestOrder()"><i class="fas fa-rocket me-1"></i> Buy 1 test key</button></div>
                    <div class="col-12"><pre class="ins-box mb-0 small" id="sup_t_out" style="white-space:pre-wrap;word-break:break-all;">Result appears here.</pre></div>
                </div>
            </div>
        </div>

        <div class="tab-pane-content" id="tab-topups" style="display:none;">
            <div class="card p-4">
                <h5>💳 Deposit Requests (latest 30)</h5>
                <p class="text-muted small">Payments waiting for approval are approved or rejected from the Telegram message the bot sends you.</p>
                <div class="table-responsive mt-2">
                    <table class="table align-middle">
                        <thead><tr><th>Time</th><th>User ID</th><th>Amount</th><th>Status</th><th>Order</th></tr></thead>
                        <tbody data-live>
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
                        <tbody data-live>
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

        <!-- 1️⃣5️⃣ 🔑 TAB: MANAGE KEYS -->
        <div class="tab-pane-content" id="tab-keystock" style="display:none;">
            <div class="page-head">
                <div class="ph-ic">🔑</div>
                <div><h3>Manage Keys</h3><p>Add stock, watch sales and check every key that was delivered.</p></div>
            </div>

            <!-- Quick jump bar -->
            <div class="sec-nav">
                <button class="chip-btn" onclick="ksGo('ks_overview')">📊 Overview</button>
                <button class="chip-btn" onclick="ksGo('addKeysAnchor')">➕ Add Stock</button>
                <button class="chip-btn" onclick="ksGo('ks_stock')">📦 Stock Left</button>
                <button class="chip-btn" onclick="ksGo('ks_today')">📈 Today</button>
                <button class="chip-btn" onclick="ksGo('ks_products')">🏷️ Products</button>
                <button class="chip-btn" onclick="ksGo('ks_live')">📡 Live Status</button>
                <button class="chip-btn" onclick="showTab('pending_keys')">⏳ Pending ({{ key_stats.pending }})</button>
            </div>

            <!-- 1. OVERVIEW -->
            <div class="gcard" id="ks_overview">
                <div class="sec-head">
                    <span class="sec-no">1</span>
                    <div><h5>Overview</h5><p>Your key business at a glance.</p></div>
                </div>
                <div class="ov-grid" data-live>
                    <div class="ov"><div class="oi">🛒</div><div class="ov-v">{{ key_stats.total }}</div><div class="ov-l">Total keys sold</div></div>
                    <div class="ov"><div class="oi">📈</div><div class="ov-v">{{ key_stats.today }}</div><div class="ov-l">Sold today</div></div>
                    <div class="ov"><div class="oi">💎</div><div class="ov-v">₹{{ key_stats.today_rev }}</div><div class="ov-l">Today's revenue</div></div>
                    <div class="ov"><div class="oi">📦</div><div class="ov-v">{{ key_stats.live_products }}</div><div class="ov-l">Live products</div></div>
                    <div class="ov"><div class="oi">🗝️</div><div class="ov-v">{{ stats.key_stock }}</div><div class="ov-l">Keys in stock</div></div>
                    <div class="ov"><div class="oi">⏳</div><div class="ov-v">{{ key_stats.pending }}</div><div class="ov-l">Pending queue</div></div>
                </div>
            </div>

            <!-- 2. ADD KEY STOCK -->
            <div class="gcard green" id="addKeysAnchor">
                <div class="sec-head">
                    <span class="sec-no">2</span>
                    <div><h5>Add Key Stock</h5><p>Choose product and plan, paste the keys (one per line) and save.</p></div>
                </div>
                <div class="row g-3">
                    <div class="col-md-6"><label class="small mb-1">Product</label><select id="ks_prod" class="form-select" onchange="fillKeyPlans()"></select></div>
                    <div class="col-md-6"><label class="small mb-1">Plan</label><select id="ks_plan" class="form-select"></select></div>
                    <div class="col-12"><label class="small mb-1">Keys (one per line)</label><textarea id="ks_keys" class="form-control" rows="5" placeholder="KEY1&#10;KEY2"></textarea></div>
                    <div class="col-12"><button class="btn-grad w-100" onclick="addKeys()">＋ Add Keys to Stock</button></div>
                </div>
            </div>

            <!-- 3. STOCK AVAILABLE -->
            <div class="gcard blue" id="ks_stock">
                <div class="sec-head">
                    <span class="sec-no">3</span>
                    <div><h5>Stock Available</h5><p>Keys left for every product and plan.</p></div>
                </div>
                <div class="table-responsive">
                    <table class="table align-middle mb-0">
                        <thead><tr><th>Product</th><th>Plan</th><th>Keys left</th></tr></thead>
                        <tbody data-live>
                            {% for p in products %}{% for pl in p.prices %}
                            <tr><td>{{ p.name }}</td><td>{{ pl[0]|replace('_', ' ') }}</td><td>{% set left = p.stock.get(pl[0], 0) %}<span class="pill {{ 'g' if left > 5 else ('a' if left > 0 else 'r') }}">{{ left }}</span></td></tr>
                            {% endfor %}{% else %}
                            <tr><td colspan="3" class="text-center text-muted">No products yet.</td></tr>
                            {% endfor %}
                        </tbody>
                    </table>
                </div>
            </div>

            <!-- 4. TODAY'S SALES -->
            <div class="gcard amber" id="ks_today">
                <div class="sec-head">
                    <span class="sec-no">4</span>
                    <div><h5>Today's Sales</h5><p>Keys sold today, product by product.</p></div>
                    <span class="pill a">{{ key_stats.today }} keys · ₹{{ key_stats.today_rev }}</span>
                </div>
                <div class="table-responsive">
                    <table class="table align-middle mb-0">
                        <thead><tr><th>Product</th><th>Keys sold</th><th>Amount</th></tr></thead>
                        <tbody data-live>
                            {% for r in today_by_product %}
                            <tr><td>{{ r[0] }}</td><td><b>{{ r[1] }}</b></td><td>₹{{ '%.2f'|format(r[2]) }}</td></tr>
                            {% else %}
                            <tr><td colspan="3" class="text-center text-muted">No sales yet today.</td></tr>
                            {% endfor %}
                        </tbody>
                    </table>
                </div>
            </div>

            <!-- 5. PRODUCTS (LIFETIME) -->
            <div class="gcard pink" id="ks_products">
                <div class="sec-head">
                    <span class="sec-no">5</span>
                    <div><h5>Products &amp; Lifetime Sales</h5><p>Total keys sold and revenue for each product. Tap a button to edit or add keys.</p></div>
                </div>
                <div class="table-responsive">
                    <table class="table align-middle mb-0">
                        <thead><tr><th>Product</th><th>Status</th><th>Sold</th><th>Revenue</th><th>In stock</th><th>Actions</th></tr></thead>
                        <tbody data-live>
                            {% for p in products %}
                            <tr>
                                <td style="min-width:120px;font-weight:700;text-transform:uppercase;">{{ p.name }}</td>
                                <td>{% if p.maintenance %}<span class="pill a">Maintenance</span>{% elif p.stock_out %}<span class="pill r">Stock out</span>{% else %}<span class="pill g">Active</span>{% endif %}</td>
                                <td>{{ lifetime.get(p.name, [0, 0])[0] }}</td>
                                <td>₹{{ '%.2f'|format(lifetime.get(p.name, [0, 0])[1]) }}</td>
                                <td>{{ p.stock.values()|sum }}</td>
                                <td style="white-space:nowrap;">
                                    <button class="chip-btn" onclick="showTab('products'); openProductModal({{ loop.index0 }})">✏️ Edit</button>
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

            <!-- 6. LIVE KEY STATUS -->
            <div class="gcard blue" id="ks_live">
                <div class="sec-head">
                    <span class="sec-no">6</span>
                    <div><h5>Live Key Status</h5><p>Search a customer complaint: see which key was delivered, when and how it was paid.</p></div>
                    <span class="live-dot">LIVE</span>
                </div>
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
                <div class="set-note" id="lk_upd">Auto-refreshes every 15 seconds while this page is open.</div>
            </div>
        </div>

        <!-- 1️⃣6️⃣ 🎨 TAB: BUTTON COLORS -->
        <div class="tab-pane-content" id="tab-btncolors" style="display:none;">
            <div class="page-head">
                <div class="ph-ic">🎨</div>
                <div><h3>Button Colors</h3><p>Pick a color for every bot button. Tap a group to open it.</p></div>
            </div>
            <div class="hint-box">⭐ <b>Premium icons:</b> the bot's owner account needs <b>Telegram Premium</b> for button icons to show. If Telegram refuses an icon the bot simply sends the button without it (nothing breaks).
                Paste your emoji pack link here, press <b>Load pack</b>, then tap <b>😀 Icon</b> on any button.</div>
            <div class="d-flex gap-2 mb-3 flex-wrap">
                <input type="text" id="emoPackName" class="form-control" style="max-width:340px" placeholder="t.me/addemoji/YourPack" value="{{ ui.emoji_pack_name }}">
                <button class="pa" style="margin:0;" onclick="emoLoadPack()">⬇️ Load pack</button>
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
                <div class="bc-row" data-key="{{ b.key }}" data-style="{{ b.style }}" data-ostyle="{{ b.style }}" data-emoji="{{ b.emoji }}" data-oemoji="{{ b.emoji }}" data-default="{{ b.default }}">
                    <span class="bc-prev s-{{ b.style }}"><span class="bc-ico"></span>{{ b.label }}</span>
                    <div class="sw-wrap">
                        <span class="sw s-none {% if b.style == 'none' %}on{% endif %}" onclick="pickColor(this, 'none')" title="Grey"></span>
                        <span class="sw s-primary {% if b.style == 'primary' %}on{% endif %}" onclick="pickColor(this, 'primary')" title="Blue"></span>
                        <span class="sw s-success {% if b.style == 'success' %}on{% endif %}" onclick="pickColor(this, 'success')" title="Green"></span>
                        <span class="sw s-danger {% if b.style == 'danger' %}on{% endif %}" onclick="pickColor(this, 'danger')" title="Red"></span>
                    </div>
                    <div class="bc-emo">
                        <button type="button" class="chip-btn" onclick="emoOpen(this)">😀 Icon</button>
                        <button type="button" class="chip-btn bc-emoclear" style="display:none" onclick="emoSet(this.closest('.bc-row'), '')">✖ Remove icon</button>
                    </div>
                </div>
                {% endfor %}
            </details>
            {% endfor %}
            <button class="btn-grad w-100 py-3 mt-2" onclick="saveButtonColors()">💾 Save Colors</button>
        </div>

        <!-- 😀 emoji picker -->
        <div class="modal-bg" id="emoModal">
            <div class="modal-card">
                <div class="d-flex justify-content-between align-items-center mb-3">
                    <h5 class="m-0">😀 Choose a premium icon</h5>
                    <button class="btn-close btn-close-white" onclick="document.getElementById('emoModal').style.display='none'"></button>
                </div>
                <div class="emo-grid" id="emoGrid"></div>
                <label class="small mt-3 mb-1">Or type an emoji ID</label>
                <div class="d-flex gap-2">
                    <input type="text" id="emoManual" class="form-control" inputmode="numeric" placeholder="e.g. 5368324170671202286">
                    <button class="btn-grad" onclick="emoManualUse()">Use</button>
                </div>
            </div>
        </div>

        <!-- 👋 TAB: WELCOME / ALERTS / LANGUAGE -->
        <div class="tab-pane-content" id="tab-botscreens" style="display:none;">
            <div class="page-head">
                <div class="ph-ic">👋</div>
                <div><h3>Welcome &amp; Alerts</h3><p>What new users see, error alerts to your Telegram, and the language button.</p></div>
            </div>

            <div class="gcard green">
                <div class="sec-head"><span class="sec-no">1</span><div><h5>Welcome message (after /start)</h5><p>Leave the text empty to keep the bot's normal welcome. Use {name} {user_id} {balance} {shop_name}.</p></div></div>
                <textarea id="wc_text" class="form-control mb-2" rows="5" oninput="wcPrev()" placeholder="Welcome {name}! ...">{{ ui.welcome_text }}</textarea>
                <div class="pv-box mb-3" id="wc_prev"></div>
                <div class="form-check form-switch mb-2">
                    <input class="form-check-input" type="checkbox" id="wc_img_on" {% if ui.welcome_image_on == '1' %}checked{% endif %}>
                    <label class="form-check-label" for="wc_img_on">Send a picture with the welcome message</label>
                </div>
                <div id="wc_img_box" class="mb-2"></div>
                <div class="d-flex gap-2 flex-wrap mb-2">
                    <input type="file" id="wc_file" accept="image/*" style="display:none" onchange="wcUpload(this)">
                    <button class="pa" style="margin:0;" onclick="document.getElementById('wc_file').click()">🖼️ Upload picture</button>
                    <button class="pa" style="margin:0;" onclick="wcRemoveImg()">🗑️ Remove picture</button>
                </div>
                <input type="text" id="wc_url" class="form-control mb-3" placeholder="…or paste a picture link (https://…)" oninput="wcUrl(this.value)">
                <label class="small mb-1">Bot description <span class="text-muted">(shown before pressing Start, max 512)</span></label>
                <textarea id="wc_desc" class="form-control mb-2" rows="3" maxlength="512">{{ ui.bot_description }}</textarea>
                <label class="small mb-1">Short description <span class="text-muted">(bot profile, max 120)</span></label>
                <input type="text" id="wc_short" class="form-control mb-2" maxlength="120" value="{{ ui.bot_short_description }}">
                <div class="hint-box">ℹ️ The <b>picture</b> shown on the pre-Start screen cannot be set from code — Telegram allows it only in BotFather: <code>/setdescriptionpic</code>. The text above is set automatically.</div>
                <button class="btn-grad" onclick="saveWelcome()">💾 Save welcome</button>
            </div>

            <div class="gcard red">
                <div class="sec-head"><span class="sec-no">2</span><div><h5>Error alerts</h5><p>Errors (payments etc.) are sent to your Telegram with the reason.</p></div></div>
                <div class="form-check form-switch mb-2">
                    <input class="form-check-input" type="checkbox" id="al_on" {% if ui.alert_on != '0' %}checked{% endif %}>
                    <label class="form-check-label" for="al_on">Send error alerts</label>
                </div>
                <label class="small mb-1">Send to Telegram ID</label>
                <input type="text" id="al_chat" class="form-control mb-3" inputmode="numeric" value="{{ ui.alert_chat }}">
                <div class="d-flex gap-2 flex-wrap">
                    <button class="btn-grad" onclick="saveAlerts()">💾 Save</button>
                    <button class="pa" style="margin:0;" onclick="testAlert()">🔔 Send test alert</button>
                </div>
                <div class="hint-box mt-3 mb-0">If the whole bot is down it cannot message you. Add a free uptime monitor (e.g. UptimeRobot) on <code>/health</code> for that.</div>
            </div>

            <div class="gcard blue">
                <div class="sec-head"><span class="sec-no">3</span><div><h5>Language button</h5><p>Shows 🌐 Language on the start menu (English, हिन्दी, తెలుగు, தமிழ், മലയാളം).</p></div></div>
                <div class="form-check form-switch mb-3">
                    <input class="form-check-input" type="checkbox" id="lg_on" {% if ui.lang_on != '0' %}checked{% endif %} onchange="saveLang()">
                    <label class="form-check-label" for="lg_on">Show the Language button</label>
                </div>
            </div>
        </div>
    </div>
    {% endif %}

    <script>
        let currentInspectUid = null;
        let curU = null;
        function escH(v) { return String(v == null ? '' : v).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;'); }
        function dmTarget(user, uid) {
            const u = String(user || '').replace('@', '').trim();
            if (/^[A-Za-z][A-Za-z0-9_]{4,31}$/.test(u)) return {href: 'https://t.me/' + u, web: true};
            return {href: 'tg://user?id=' + uid, web: false};
        }
        function openInspector(card) {
            const d = card.dataset;
            curU = {uid: d.uid, name: d.name, user: d.user, role: d.role, bal: parseFloat(d.bal) || 0};
            currentInspectUid = d.uid;
            document.getElementById('m_av').innerText = ((d.name || '?').trim().charAt(0) || '?').toUpperCase();
            document.getElementById('m_id').innerText = d.uid;
            document.getElementById('m_name').innerText = d.name || 'Unknown';
            document.getElementById('m_user').innerText = d.user || 'N/A';
            document.getElementById('m_joined').innerText = d.joined || '-';
            document.getElementById('m_bal').innerText = (parseFloat(d.bal) || 0).toFixed(2);
            document.getElementById('m_spent').innerText = (parseFloat(d.spent) || 0).toFixed(2);
            document.getElementById('m_orders').innerText = d.orders || 0;
            document.getElementById('m_refs').innerText = d.refs || 0;
            document.getElementById('m_refearn').innerText = (parseFloat(d.refearn) || 0).toFixed(0);
            inspectShowRole();
            const t = dmTarget(d.user, d.uid);
            const a = document.getElementById('m_dm_link');
            a.href = t.href;
            if (t.web) { a.target = '_blank'; } else { a.removeAttribute('target'); }
            document.getElementById('m_dm_note').innerText = t.web
                ? "Opens this member's chat in Telegram."
                : 'This member has no public @username, so Telegram cannot always open the chat from a browser. If it does not open, use "Message via bot" below.';
            document.getElementById('m_msg').value = '';
            document.getElementById('m_custom_bal').value = '';
            document.getElementById('m_recent').innerText = 'Loading...';
            document.getElementById('userModal').style.display = 'flex';
            fetch('/api/user/detail?user_id=' + encodeURIComponent(d.uid), {credentials: 'same-origin'})
                .then(r => r.json()).then(x => {
                    const box = document.getElementById('m_recent');
                    if (!curU || String(curU.uid) !== String(d.uid)) return;
                    if (!x.orders || !x.orders.length) { box.innerText = 'No orders yet.'; return; }
                    box.innerHTML = x.orders.map(o =>
                        '<div class="ins-row"><span>' + escH(o.prod_name) + ' - ' + escH(String(o.plan || '').replace(/_/g, ' ')) +
                        (o.pending ? ' <span class="pill a">pending</span>' : '') + '</span><span>&#8377;' + Number(o.amount || 0).toFixed(2) +
                        '<br><small>' + escH(o.timestamp) + '</small></span></div>').join('');
                }).catch(() => { document.getElementById('m_recent').innerText = 'Could not load orders.'; });
        }
        function closeInspector() { document.getElementById('userModal').style.display = 'none'; curU = null; }
        function inspectShowRole() {
            const isRes = curU && curU.role === 'Reseller';
            const r = document.getElementById('m_role');
            r.innerText = isRes ? '👑 Reseller' : 'Regular';
            r.className = 'pill ' + (isRes ? 'a' : '');
            document.getElementById('m_role_btn').innerText = isRes ? 'Revoke Reseller' : '👑 Make Reseller';
        }
        function inspectToggleRole() {
            if (!curU) return;
            const role = curU.role === 'Reseller' ? 'Regular' : 'Reseller';
            fetch('/api/user/set_role', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({user_id: curU.uid, role: role}) })
            .then(r => r.json()).then(d => { toast(d.message); curU.role = role; inspectShowRole(); liveRefresh(true); });
        }
        function copyUid() {
            if (!curU) return;
            try { navigator.clipboard.writeText(String(curU.uid)); toast('Telegram ID copied'); } catch (e) { toast('Copy failed', false); }
        }
        function sendBotMsg() {
            if (!curU) return;
            const text = document.getElementById('m_msg').value.trim();
            if (!text) return toast('Type a message first', false);
            fetch('/api/user/message', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({user_id: curU.uid, text: text}) })
            .then(r => r.json()).then(d => { toast(d.message, !d.error); if (!d.error) document.getElementById('m_msg').value = ''; });
        }
        function quickAddBal(delta) {
            if (!curU) return;
            fetch('/api/user/adjust_balance', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({user_id: curU.uid, delta: delta}) })
            .then(r => r.json()).then(d => {
                toast(d.message);
                curU.bal = Math.max(0, curU.bal + delta);
                document.getElementById('m_bal').innerText = curU.bal.toFixed(2);
                liveRefresh(true);
            });
        }
        function applyCustomBal() {
            let val = parseFloat(document.getElementById('m_custom_bal').value);
            if (isNaN(val)) return toast('Enter a valid number', false);
            quickAddBal(val);
            document.getElementById('m_custom_bal').value = '';
        }
        /* ---- Members list: search + filter chips ---- */
        let memFilterKey = 'all';
        function memChip(btn) {
            memFilterKey = btn.dataset.f;
            document.querySelectorAll('#memChips .chip-btn').forEach(b => b.classList.toggle('on', b === btn));
            memFilter();
        }
        function memFilter() {
            const qEl = document.getElementById('memSearch');
            if (!qEl) return;
            const q = qEl.value.toLowerCase().trim();
            let shown = 0;
            document.querySelectorAll('#memGrid .mc').forEach(c => {
                const d = c.dataset;
                let ok = !q || (d.search || '').includes(q);
                if (ok && memFilterKey === 'reseller') ok = d.role === 'Reseller';
                if (ok && memFilterKey === 'regular') ok = d.role !== 'Reseller';
                if (ok && memFilterKey === 'buyers') ok = (parseInt(d.orders) || 0) > 0;
                if (ok && memFilterKey === 'balance') ok = (parseFloat(d.bal) || 0) > 0;
                c.style.display = ok ? '' : 'none';
                if (ok) shown++;
            });
            const cnt = document.getElementById('memCount');
            if (cnt) cnt.innerText = shown + ' shown';
            const em = document.getElementById('memEmpty');
            if (em) em.style.display = shown ? 'none' : 'block';
        }
        let PRODUCTS = {% if session.get('admin_logged') %}{{ products|tojson }}{% else %}[]{% endif %};
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
            if (t === 'supplier' && typeof supLoad === 'function') supLoad();
            if (t === 'btncolors' && !EMO_LOADED) emoLoad();
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
        let PM_IMG = '', PM_IMG_RM = false;
        function pmShowImg(src, note, has) {
            const im = document.getElementById('pm_img_prev');
            im.src = src || ''; im.style.display = src ? 'block' : 'none';
            document.getElementById('pm_img_note').innerText = note || '';
            document.getElementById('pm_img_rm').style.display = (src || has) ? 'inline-block' : 'none';
        }
        function pmPickImg(inp) {
            const f = inp.files[0]; if (!f) return;
            const rd = new FileReader();
            rd.onload = () => {
                const im = new Image();
                im.onload = () => {
                    const k = Math.min(1, 1280 / Math.max(im.width, im.height));
                    const c = document.createElement('canvas');
                    c.width = Math.round(im.width * k); c.height = Math.round(im.height * k);
                    const cx = c.getContext('2d'); cx.fillStyle = '#fff'; cx.fillRect(0, 0, c.width, c.height);
                    cx.drawImage(im, 0, 0, c.width, c.height);
                    PM_IMG = c.toDataURL('image/jpeg', 0.88); PM_IMG_RM = false;
                    pmShowImg(PM_IMG, 'New image ready - press Save.');
                };
                im.src = rd.result;
            };
            rd.readAsDataURL(f);
        }
        function pmRemoveImg() {
            PM_IMG = ''; PM_IMG_RM = true; document.getElementById('pm_img').value = '';
            pmShowImg('', 'Image will be removed when you press Save.');
            document.getElementById('pm_img_rm').style.display = 'none';
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
            PM_IMG = ''; PM_IMG_RM = false; pmShowImg('', '');
            document.getElementById('pm_img').value = '';
            document.getElementById('pm_ptitle').value = '';
            document.getElementById('pm_feat').value = '';
            if (isEdit) {
                fetch('/api/product/details?k=' + encodeURIComponent(p.prod_key)).then(r => r.json()).then(d => {
                    document.getElementById('pm_ptitle').value = d.title || '';
                    document.getElementById('pm_feat').value = d.features || '';
                    if (d.has_image) pmShowImg('', 'This product already has a banner image. Choose a file to replace it.', true);
                }).catch(() => {});
            }
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
            if (!name) return toast('Enter the product name!', false);
            const plans = [];
            for (const r of document.querySelectorAll('#pm_plans .plan-row')) {
                const pn = r.querySelector('.pl-name').value.trim();
                const pr = parseFloat(r.querySelector('.pl-price').value);
                const keys = r.querySelector('.pl-keys').value;
                if (!pn && isNaN(pr) && !keys.trim()) continue;
                if (!pn || isNaN(pr) || pr < 0) return toast('Every plan needs a name and a price!', false);
                plans.push({old: r.dataset.old || '', name: pn, price: pr, keys: keys});
            }
            if (!plans.length) return toast('Add at least one plan!', false);
            const body = {
                prod_key: editingKey, name: name,
                category: document.getElementById('pm_cat').value,
                icon: document.getElementById('pm_icon').value.trim(),
                download_link: document.getElementById('pm_link').value.trim(),
                pd_title: document.getElementById('pm_ptitle').value.trim(),
                pd_features: document.getElementById('pm_feat').value,
                pd_image: PM_IMG, pd_remove_image: PM_IMG_RM,
                plans: plans
            };
            act(editingKey ? '/api/product/update' : '/api/product/save', body, () => { closeProductModal(); });
        }
        function toggleProduct(k, field) {
            act('/api/product/toggle', {prod_key: k, field: field});
        }
        function deleteProduct(k) {
            if(!confirm('Delete this product?')) return;
            act('/api/product/delete', {prod_key: k});
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
            if (!prod || !plan || !keys.trim()) return toast('Choose a product, a plan and paste the keys!', false);
            act('/api/keys/add', {prod_key: prod, plan: plan, keys: keys}, () => { document.getElementById('ks_keys').value = ''; });
        }
        /* ===== Out-of-stock demand board ===== */
        let DM_LAST = '';
        function dmCount(el) {
            const to = parseInt(el.dataset.to, 10) || 0, t0 = performance.now();
            (function tick(t) {
                const k = Math.min(1, (t - t0) / 900);
                el.textContent = Math.round(to * (1 - Math.pow(1 - k, 3)));
                if (k < 1) requestAnimationFrame(tick);
            })(t0);
        }
        function loadDemand(manual) {
            const box = document.getElementById('dm_list');
            if (!box) return;
            fetch('/api/demand').then(r => r.json()).then(d => {
                const sig = JSON.stringify(d);
                if (sig === DM_LAST && !manual) return;
                DM_LAST = sig;
                const chips = document.getElementById('dm_chips');
                if (!d.items || !d.items.length) {
                    chips.innerHTML = '';
                    box.innerHTML = '<div class="dm-empty"><div class="dm-float">&#127881;</div><b>No demand right now</b><div class="small text-muted">Every product customers wanted was in stock.</div></div>';
                    return;
                }
                chips.innerHTML =
                    '<div class="dm-chip"><b class="dm-n" data-to="' + d.total_requests + '">0</b><small>&#128722; total requests</small></div>' +
                    '<div class="dm-chip" style="animation-delay:.1s"><b class="dm-n" data-to="' + d.items.length + '">0</b><small>&#128230; products wanted</small></div>' +
                    '<div class="dm-chip" style="animation-delay:.2s"><b>&#8377;' + Math.round(d.total_value) + '</b><small>&#128176; waiting in wallets</small></div>';
                const max = Math.max.apply(null, d.items.map(i => i.attempts));
                const medals = ['&#129351;', '&#129352;', '&#129353;'];
                box.innerHTML = d.items.map((i, n) => {
                    const pct = Math.max(6, Math.round(i.attempts / max * 100));
                    const heat = i.attempts >= 5 ? '<span class="dm-tag">&#128293;&#128293;&#128293; HOT</span>'
                               : i.attempts >= 2 ? '<span class="dm-tag warm">&#128293; WARM</span>' : '<span class="dm-tag new">&#128064; NEW</span>';
                    return '<div class="dm-item' + (n === 0 ? ' top' : '') + '" style="animation-delay:' + (n * 0.08) + 's">'
                        + '<div class="dm-rank">' + (medals[n] || '#' + (n + 1)) + '</div>'
                        + '<div class="dm-main"><div class="dm-name">' + esc(i.prod_name) + ' &middot; ' + esc(i.plan.replace(/_/g, ' ')) + (n === 0 ? ' <span class="dm-tag">MOST DEMAND</span>' : heat) + '</div>'
                        + '<div class="dm-meta">&#128176; Price &#8377;' + i.price + ' &nbsp;&middot;&nbsp; &#128101; ' + i.buyers + ' customer(s) &nbsp;&middot;&nbsp; &#128338; last ' + esc(i.last_at) + '</div>'
                        + '<div class="dm-track"><div class="dm-bar" style="--w:' + pct + '%"></div></div></div>'
                        + '<div class="dm-num"><span class="dm-n" data-to="' + i.attempts + '">0</span><small>requests</small></div>'
                        + '<button class="btn btn-sm btn-success" data-prod="' + esc(i.prod_name) + '" data-plan="' + esc(i.plan) + '" onclick="clearDemand(this)">&#9989; Stock added</button>'
                        + '</div>';
                }).join('');
                document.querySelectorAll('#dm_chips .dm-n, #dm_list .dm-n').forEach(dmCount);
            }).catch(() => {});
        }
        function clearDemand(btn) {
            const body = btn ? {prod_name: btn.dataset.prod, plan: btn.dataset.plan} : {all: true};
            if (!confirm(btn ? 'Mark this as restocked and remove it from the list?' : 'Clear the whole demand list?')) return;
            fetch('/api/demand/clear', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)})
                .then(r => r.json()).then(d => { toast(d.message, true); loadDemand(true); });
        }
        document.querySelectorAll('[data-tab="pending_keys"]').forEach(a => a.addEventListener('click', () => setTimeout(() => loadDemand(true), 60)));
        setInterval(() => { const b = document.getElementById('tab-pending_keys'); if (b && b.offsetParent !== null) loadDemand(false); }, 10000);
        setTimeout(() => loadDemand(true), 400);

        function dispatchKey(oid, uid) {
            let key = document.getElementById('key_disp_' + oid).value;
            if(!key) return toast('Enter key!', false);
            act('/api/pending/dispatch', {order_id: oid, user_id: uid, key: key}, () => { const e = document.getElementById('key_disp_' + oid); if (e) { const row = e.closest('tr'); if (row) row.remove(); } });
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
            .then(r => r.json()).then(d => toast(d.message));
        }
        function saveGmail() {
            let body = {gmail_user: document.getElementById('gm_user').value.trim(),
                        app_password: document.getElementById('gm_pass').value,
                        bank_domain: document.getElementById('gm_domain').value.trim()};
            return fetch('/api/gmail/save', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body) })
            .then(r => r.json()).then(d => { toast(d.message); document.getElementById('gm_pass').value = ''; return d; });
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
        /* ===== Supplier API tab ===== */
        let SUP_SVC = {};
        let SUP_LINKED = new Set();
        let SUP_EDIT = {};
        function supJson(url, body) {
            return fetch(url, body === undefined ? {} : { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body) })
                .then(r => r.json()).catch(() => ({ok: false, message: 'Network error'}));
        }
        function supLoad() {
            supJson('/api/supplier/config').then(d => {
                document.getElementById('sup_url').value = d.url || '';
                document.getElementById('sup_saved').style.display = d.has_key ? 'inline-block' : 'none';
                document.getElementById('sup_key').placeholder = d.has_key ? ('saved (' + d.key_hint + ') - leave empty to keep it') : 'usr_...';
            });
            supLoadMaps();
            supLiveTick(true);
        }
        let SUP_DIRTY = false, SUP_TICK = 0;
        document.addEventListener('input', e => { if (e.target.classList && e.target.classList.contains('sup-pid')) SUP_DIRTY = true; });
        function supLiveTick(first) {
            const tab = document.getElementById('tab-supplier');
            if (!tab || tab.offsetParent === null || document.hidden) return;
            SUP_TICK++;
            // 1) their wallet balance, live
            supJson('/api/supplier/test', {}).then(d => {
                const st = document.getElementById('sup_status');
                if (!st) return;
                st.innerHTML = '<span class="sup-live"></span><b>LIVE</b> &middot; ' + esc(d.message) + ' <span class="small text-secondary">&middot; updated ' + new Date().toLocaleTimeString() + '</span>';
            }).catch(() => {});
            // 2) their products: stock + prices (fresh from the supplier every 30 s), updated in place so nothing you typed is lost
            supJson('/api/supplier/services' + ((first || SUP_TICK % 2 === 0) ? '?refresh=1' : '')).then(d => {
                if (!d.ok || !d.services) return;
                const fresh = {};
                d.services.forEach(x => { fresh[x.pid] = x; });
                SUP_LINKED = new Set((d.linked || []).map(Number));
                if (Object.keys(SUP_SVC).length) {
                    SUP_SVC = fresh;
                    document.querySelectorAll('#sup_import .sup-row').forEach(row => {
                        const sv = fresh[row.dataset.pid];
                        if (!sv) return;
                        const stk = row.querySelector('.sup-stk');
                        if (stk) stk.innerText = 'stock ' + sv.stock;
                        const cost = parseFloat(sv.price);
                        if (!isNaN(cost) && cost !== parseFloat(row.dataset.cost)) {
                            row.dataset.cost = cost;
                            const amt = row.querySelector('.sup-box.theirs .amt');
                            if (amt) amt.innerHTML = supMoney(cost);
                            const inp = row.querySelector('.sup-price');
                            if (inp) supRowCalc(inp);
                        }
                    });
                    if (!SUP_DIRTY) document.querySelectorAll('.sup-pid').forEach(supCheck);
                }
            }).catch(() => {});
            // 3) my shop list (skipped while you are typing a link)
            const ae = document.activeElement;
            if (!first && !SUP_DIRTY && !(ae && ae.classList && ae.classList.contains('sup-pid'))) supLoadMaps();
        }
        setInterval(() => supLiveTick(false), 15000);
        function supSave() {
            supJson('/api/supplier/save', {url: document.getElementById('sup_url').value.trim(), key: document.getElementById('sup_key').value.trim()})
                .then(d => { toast(d.message, d.ok !== false); document.getElementById('sup_key').value = ''; supLoad(); });
        }
        function supTest() {
            const st = document.getElementById('sup_status');
            st.innerText = 'Testing...';
            supSave();
            setTimeout(() => supJson('/api/supplier/test', {}).then(d => { st.innerText = d.message; toast(d.message, d.ok); }), 700);
        }
        function supLabel(s) {
            return s.pid + ' | ' + s.name + ' | ' + (s.device || '') + ' | Rs ' + s.price + ' | stock ' + s.stock;
        }
        function supLoadServices(force) {
            const info = document.getElementById('sup_svc_info');
            info.innerText = 'Loading their products...';
            return supJson('/api/supplier/services' + (force ? '?refresh=1' : '')).then(d => {
                if (!d.ok) { info.innerText = d.message; return; }
                SUP_SVC = {};
                const dl = document.getElementById('sup_dl');
                dl.innerHTML = '';
                d.services.forEach(s => { SUP_SVC[s.pid] = s; const o = document.createElement('option'); o.value = supLabel(s); dl.appendChild(o); });
                SUP_LINKED = new Set((d.linked || []).map(Number));
                info.innerText = d.services.length + ' products loaded.';
                document.querySelectorAll('.sup-pid').forEach(supCheck);
                supRenderImport();
            });
        }
        function supPlanName(dur) {
            const n = String(dur || '').replace(/[^A-Za-z0-9 .+-]/g, '').trim().replace(/ +/g, '_').slice(0, 22);
            return n || 'Key';
        }
        function supGuessCat(dev, panel) {
            const pn = String(panel || '').toLowerCase();
            if (pn.includes('carrom')) return 'carrom';
            if (pn.includes('8 ball') || pn.includes('8ball') || pn.includes('pool')) return 'pool8';
            const d = String(dev || '').toUpperCase();
            if (d.includes('IOS') || d.includes('IPHONE')) return 'ios';
            if ((' ' + d + ' ').includes(' PC ') || d.includes('WINDOWS')) return 'pc';
            if (d.includes('NON')) return 'non_root';
            if (d.includes('ROOT')) return 'root';
            return 'non_root';
        }
        function supMult() { const m = parseFloat(document.getElementById('sup_mult').value); return m >= 1 ? m : 2; }
        function supApplyMult() { SUP_EDIT = {}; supRenderImport(); }
        function supMoney(v) { return '&#8377;' + (Math.round(v * 100) / 100); }
        function supBarLine(label, amt, max, cls) {
            const w = Math.max(3, Math.min(100, Math.round(amt / max * 100)));
            return '<div class="sup-bl"><span class="t">' + label + '</span><div class="track"><div class="bar ' + cls + '" style="width:' + w + '%"></div></div><span class="v">' + supMoney(amt) + '</span></div>';
        }
        function supBadge(v) {
            return v < 0 ? '<span class="sup-badge bad">&#10060; LOSS ' + supMoney(Math.abs(v)) + '</span>'
                         : '<span class="sup-badge good">&#9989; PROFIT ' + supMoney(v) + '</span>';
        }
        function supCalcText(cost, my) {
            const res = Math.round(my * 0.6 * 100) / 100;
            const max = Math.max(cost, my, 1);
            return '<div class="sup-sub">&#128100; Normal customer</div>'
                 + supBarLine('I pay them', cost, max, 'cost') + supBarLine('Customer pays me', my, max, 'mine') + supBadge(my - cost)
                 + '<div class="sup-sub">&#128081; Reseller customer (pays 60% of my price)</div>'
                 + supBarLine('I pay them', cost, max, 'cost') + supBarLine('Reseller pays me', res, max, 'mine') + supBadge(res - cost);
        }
        function supRowCalc(inp) {
            const row = inp.closest('.sup-row');
            const cost = parseFloat(row.dataset.cost), my = parseFloat(inp.value);
            SUP_EDIT[row.dataset.pid] = inp.value;
            row.querySelector('.sup-calc').innerHTML = isNaN(my) ? '' : supCalcText(cost, my);
        }
        function supRenderImport() {
            const box = document.getElementById('sup_import');
            const all = Object.values(SUP_SVC);
            if (!all.length) { box.innerHTML = ''; return; }
            const q = document.getElementById('sup_q').value.trim().toLowerCase();
            const mult = supMult();
            const groups = {};
            all.forEach(sv => {
                const parts = String(sv.name).split(' ' + String.fromCharCode(8212) + ' ');
                const panel = parts.length > 1 ? parts[0] : String(sv.name);
                const dur = parts.length > 1 ? parts.slice(1).join(' ') : String(sv.duration || 'Key');
                const gk = panel + '|' + (sv.device || '');
                (groups[gk] = groups[gk] || {panel: panel, device: sv.device || '', cat: sv.category || '', items: []}).items.push({sv: sv, dur: dur});
            });
            const panelDevices = {};
            Object.values(groups).forEach(g => { (panelDevices[g.panel] = panelDevices[g.panel] || []).push(g.device); });
            let h = '', shown = 0;
            Object.values(groups).sort((a, b) => a.panel.localeCompare(b.panel)).forEach(g => {
                const hay = (g.panel + ' ' + g.device + ' ' + g.cat).toLowerCase();
                if (q && !hay.includes(q)) return;
                shown++;
                const dispName = panelDevices[g.panel].length > 1 ? (g.panel + ' ' + g.device) : g.panel;
                const used = {};
                g.items.sort((a, b) => parseFloat(a.sv.price) - parseFloat(b.sv.price));
                let rows = '', free = 0;
                g.items.forEach(it => {
                    const sv = it.sv, pid = Number(sv.pid), cost = parseFloat(sv.price);
                    let plan = supPlanName(it.dur);
                    if (used[plan]) plan = (plan.slice(0, 16) + '_' + pid).slice(0, 22);
                    used[plan] = 1;
                    if (SUP_LINKED.has(pid)) {
                        rows += '<div class="small mt-2 pt-2 d-flex align-items-center gap-2" style="border-top:1px solid rgba(255,255,255,.08)"><span class="flex-grow-1">&#9989; ' + esc(it.dur) + ' - already in your shop</span>'
                          + '<button class="btn btn-sm btn-outline-danger" data-pid="' + pid + '" onclick="supRemovePid(this)">&#128465; Remove</button></div>';
                        return;
                    }
                    free++;
                    const my = SUP_EDIT[pid] !== undefined ? parseFloat(SUP_EDIT[pid]) : Math.ceil(cost * mult);
                    rows += '<div class="sup-row" data-pid="' + pid + '" data-plan="' + esc(plan) + '" data-cost="' + cost + '">'
                          + '<div class="d-flex align-items-center gap-2"><input type="checkbox" class="sup-chk" checked>'
                          + '<b class="flex-grow-1">&#128338; ' + esc(it.dur) + '</b><span class="small sup-stk">stock ' + esc(sv.stock) + '</span></div>'
                          + '<div class="sup-money">'
                          + '<div class="sup-box theirs"><span class="lbl">&#127981; I pay THEM</span><span class="amt">' + supMoney(cost) + '</span></div>'
                          + '<div class="sup-arr">&#10145;&#65039;</div>'
                          + '<div class="sup-box mine"><span class="lbl">&#128100; Customer pays ME</span><input type="number" class="form-control sup-price" value="' + my + '" min="0" step="1" oninput="supRowCalc(this)"></div>'
                          + '</div>'
                          + '<div class="sup-calc">' + supCalcText(cost, my) + '</div></div>';
                });
                const cat = supGuessCat(g.device, g.panel);
                const opts = [['non_root', 'Non-Root Mobile'], ['root', 'Root Mobile'], ['ios', 'iOS'], ['pc', 'PC'], ['likes', '8 Level ID'], ['carrom', 'Carrom Pool'], ['pool8', '8 Ball Pool']]
                    .map(o => '<option value="' + o[0] + '"' + (o[0] === cat ? ' selected' : '') + '>' + o[1] + '</option>').join('');
                h += '<details class="ins-box sup-grp" data-name="' + esc(dispName) + '"><summary><b>' + esc(dispName) + '</b> <span class="small">&middot; ' + esc(g.device) + ' &middot; ' + g.items.length + ' plan(s)</span></summary>'
                   + rows
                   + (free ? '<div class="d-flex gap-2 mt-3"><select class="form-control sup-cat">' + opts + '</select><button class="btn-grad" style="white-space:nowrap" onclick="supImportGroup(this)">Add to my shop</button></div>' : '')
                   + '</details>';
            });
            box.innerHTML = h || '<div class="small">No product matches your search.</div>';
            document.getElementById('sup_imp_info').innerText = shown + ' product(s) shown. Tap one to open it.';
        }
        function supImportGroup(btn) {
            const grp = btn.closest('.sup-grp');
            const plans = [];
            grp.querySelectorAll('.sup-row').forEach(r => {
                if (!r.querySelector('.sup-chk').checked) return;
                plans.push({plan: r.dataset.plan, pid: parseInt(r.dataset.pid, 10), price: r.querySelector('.sup-price').value});
            });
            if (!plans.length) { toast('Tick at least one plan.', false); return; }
            let loss = 0;
            grp.querySelectorAll('.sup-row').forEach(r => {
                if (!r.querySelector('.sup-chk').checked) return;
                const c = parseFloat(r.dataset.cost), m = parseFloat(r.querySelector('.sup-price').value);
                if (isNaN(m) || m < c || m * 0.6 < c) loss++;
            });
            if (loss && !confirm(loss + ' plan(s) will give you a LOSS (their price is higher than what you earn). Add anyway?')) return;
            btn.disabled = true;
            supJson('/api/supplier/import', {name: grp.dataset.name, category: grp.querySelector('.sup-cat').value, plans: plans}).then(d => {
                btn.disabled = false;
                toast(d.message, d.ok);
                if (d.ok) { plans.forEach(p => { SUP_LINKED.add(p.pid); delete SUP_EDIT[p.pid]; }); supRenderImport(); supLoadMaps(); }
            });
        }
        function supPidOf(v) { const m = String(v || '').trim().match(/^([0-9]+)/); return m ? parseInt(m[1], 10) : null; }
        function supCheck(inp) {
            const note = inp.parentElement.parentElement.querySelector('.sup-note');
            const pid = supPidOf(inp.value);
            if (!pid) { note.innerHTML = '<span class="text-secondary">Not linked - uses only your own keys.</span>'; return; }
            const s = SUP_SVC[pid];
            if (!s) { note.innerHTML = '<span class="text-secondary">Linked to their PID ' + pid + '. Press "Load / refresh their products" above to see the picture.</span>'; return; }
            const cost = parseFloat(s.price), my = parseFloat(inp.dataset.price);
            note.innerHTML = '<div class="small"><b>' + esc(s.name) + '</b></div>' + supCalcText(cost, my);
        }
        function supLoadMaps() {
            supJson('/api/supplier/maps').then(d => {
                const box = document.getElementById('sup_maps');
                if (!d.products || !d.products.length) { box.innerHTML = '<div class="small">No products in your shop yet.</div>'; return; }
                let h = '';
                d.products.forEach(p => {
                    h += '<details class="ins-box"><summary><b>' + esc(p.name) + '</b> <span class="small">&middot; ' + p.plans.length + ' plan(s)</span></summary>'
                       + '<div class="mt-2"><button class="btn btn-sm btn-outline-danger" data-prod="' + esc(p.prod_key) + '" data-name="' + esc(p.name) + '" onclick="supRemoveProduct(this)">&#128465; Remove whole product</button></div>';
                    p.plans.forEach(pl => {
                        const id = 'sp_' + btoa(unescape(encodeURIComponent(p.prod_key + '|' + pl.plan))).replace(/[^A-Za-z0-9]/g, '');
                        h += '<div class="mt-2 pt-2" style="border-top:1px solid rgba(255,255,255,.08)">'
                          + '<div class="small">' + esc(pl.plan.replace(/_/g, ' ')) + ' &middot; your price &#8377;' + pl.price + ' (reseller &#8377;' + pl.reseller_price + ')</div>'
                          + '<div class="d-flex gap-2 mt-1"><input class="form-control sup-pid" id="' + id + '" list="sup_dl" placeholder="their product (type to search)"'
                          + ' value="' + (pl.pid ? esc(SUP_SVC[pl.pid] ? supLabel(SUP_SVC[pl.pid]) : String(pl.pid)) : '') + '"'
                          + ' data-prod="' + esc(p.prod_key) + '" data-plan="' + esc(pl.plan) + '" data-price="' + pl.price + '" data-reseller="' + pl.reseller_price + '"'
                          + ' oninput="supCheck(this)">'
                          + '<button class="btn btn-outline-warning" onclick="supSaveMap(this.previousElementSibling)">Save</button>'
                          + '<button class="btn btn-outline-danger" data-prod="' + esc(p.prod_key) + '" data-plan="' + esc(pl.plan) + '" onclick="supRemovePlan(this)">&#128465;</button></div>'
                          + '<div class="small mt-1 sup-note"></div></div>';
                    });
                    h += '</details>';
                });
                box.innerHTML = h;
                SUP_DIRTY = false;
                document.querySelectorAll('.sup-pid').forEach(supCheck);
            });
        }
        function supAfterRemove(d) {
            toast(d.message, d.ok !== false);
            if (d.ok !== false) { supLoadServices(false).then(supLoadMaps); }
        }
        function supRemovePid(btn) {
            if (!confirm('Remove this plan from your shop?')) return;
            supJson('/api/supplier/remove', {pid: parseInt(btn.dataset.pid, 10)}).then(supAfterRemove);
        }
        function supRemovePlan(btn) {
            if (!confirm('Remove plan "' + btn.dataset.plan.replace(/_/g, ' ') + '" from your shop?')) return;
            supJson('/api/supplier/remove', {prod_key: btn.dataset.prod, plan: btn.dataset.plan}).then(supAfterRemove);
        }
        function supRemoveProduct(btn) {
            if (!confirm('Remove the WHOLE product "' + btn.dataset.name + '" with all its plans from your shop?')) return;
            supJson('/api/supplier/remove', {prod_key: btn.dataset.prod}).then(supAfterRemove);
        }
        function supSaveMap(inp) {
            SUP_DIRTY = false;
            const raw = inp.value.trim();
            const pid = raw === '' ? null : supPidOf(raw);
            if (raw !== '' && !pid) { toast('Pick a product from the list (it starts with the PID number).', false); return; }
            supJson('/api/supplier/map', {prod_key: inp.dataset.prod, plan: inp.dataset.plan, pid: pid}).then(d => { toast(d.message, d.ok !== false); supCheck(inp); });
        }
        function supTestOrder() {
            const pid = parseInt(document.getElementById('sup_t_pid').value, 10);
            if (!pid) { toast('Enter their PID first.', false); return; }
            if (!confirm('This buys ONE real key from the supplier and spends money from your supplier wallet. Continue?')) return;
            const out = document.getElementById('sup_t_out');
            out.innerText = 'Ordering...';
            supJson('/api/supplier/test_order', {pid: pid, confirm: true}).then(d => { out.innerText = d.message; toast(d.ok ? 'Test order done' : 'Test order failed', d.ok); });
        }
        updateUpiPreview();
        function saveIdStock() {
            let cat = document.getElementById('id_cat').value;
            let pr = document.getElementById('id_price').value;
            let cred = document.getElementById('id_cred').value;
            if(!pr || !cred) return toast('Fill fields!', false);
            act('/api/id_stock/add', {category: cat, price: parseFloat(pr), credentials: cred}, () => { document.getElementById('id_cred').value = ''; document.getElementById('id_price').value = ''; });
        }
        function saveCoupon() {
            let c = document.getElementById('cp_code').value;
            let v = document.getElementById('cp_val').value;
            let l = document.getElementById('cp_limit').value;
            if(!c || !v) return toast('Fill fields!', false);
            act('/api/coupon/save', {code: c, val: parseFloat(v), limit: parseInt(l||100)}, () => { document.getElementById('cp_code').value = ''; document.getElementById('cp_val').value = ''; });
        }
        function toggleResellerRole(uid, role) {
            act('/api/user/set_role', {user_id: uid, role: role});
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
            const styles = {}, emojis = {}; let n = 0;
            document.querySelectorAll('.bc-row').forEach(r => {
                if (r.dataset.style !== r.dataset.ostyle || (r.dataset.emoji || '') !== (r.dataset.oemoji || '')) {
                    styles[r.dataset.key] = r.dataset.style; emojis[r.dataset.key] = r.dataset.emoji || ''; n++;
                }
            });
            if (!n) { toast('Nothing changed'); return; }
            act('/api/buttons/save', {styles: styles, emojis: emojis}, () => {
                document.querySelectorAll('.bc-row').forEach(r => { r.dataset.ostyle = r.dataset.style; r.dataset.oemoji = r.dataset.emoji || ''; });
            }, false);
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
        let bcDur = 0, bcBusy = false, bcPollT = null, bcTickT = null;
        function fmtDur(s) { s = Math.max(0, Math.round(s)); const m = Math.floor(s / 60); const r = s % 60; return m ? m + 'm ' + r + 's' : r + 's'; }
        function fmtClock(s) { s = Math.max(0, Math.round(s)); const m = Math.floor(s / 60); const r = s % 60; return m + ':' + (r < 10 ? '0' : '') + r; }
        function fmtMB(b) { return (b / 1048576).toFixed(1) + ' MB'; }
        function ksGo(id) { const el = document.getElementById(id); if (el) el.scrollIntoView({behavior: 'smooth', block: 'start'}); }
        function bcFileLabel() {
            if (!bcFile) return '';
            return bcFile.name + ' (' + fmtMB(bcFile.size) + (bcDur ? ' · ⏱ ' + fmtClock(bcDur) + ' long' : '') + ')';
        }
        function bcPick(kind, input) {
            if (!input.files || !input.files[0]) return;
            const f = input.files[0];
            if (f.size > 50 * 1024 * 1024) { input.value = ''; return toast('File is larger than 50 MB.', false); }
            ['image', 'video', 'voice'].forEach(k => { if (k !== kind) document.getElementById('bc_' + k).value = ''; });
            bcKind = kind; bcFile = f; bcDur = 0;
            ['image', 'video', 'voice'].forEach(k => document.getElementById('mt_' + k).classList.toggle('on', k === kind));
            document.getElementById('bc_file').style.display = 'flex';
            document.getElementById('bc_file_name').textContent = bcFileLabel();
            if (kind === 'video' || kind === 'voice') {
                const el = document.createElement(kind === 'video' ? 'video' : 'audio');
                el.preload = 'metadata';
                el.onloadedmetadata = () => {
                    if (isFinite(el.duration)) { bcDur = el.duration; if (bcFile === f) document.getElementById('bc_file_name').textContent = bcFileLabel(); }
                    URL.revokeObjectURL(el.src);
                };
                el.onerror = () => URL.revokeObjectURL(el.src);
                el.src = URL.createObjectURL(f);
            }
            bcPreview();
        }
        function bcClearMedia() {
            ['image', 'video', 'voice'].forEach(k => { document.getElementById('bc_' + k).value = ''; document.getElementById('mt_' + k).classList.remove('on'); });
            bcKind = ''; bcFile = null; bcDur = 0;
            document.getElementById('bc_file').style.display = 'none';
            bcPreview();
        }
        function bcStep(active) {
            ['up', 'send', 'done'].forEach((k, i) => {
                document.getElementById('ps_' + k).className = 'up-step' + (i < active ? ' ok' : (i === active ? ' on' : ''));
            });
        }
        function bcUI(o) {
            const card = document.getElementById('bc_prog');
            card.style.display = 'block';
            card.className = 'up-card' + (o.state ? ' ' + o.state : '');
            document.getElementById('up_title').textContent = o.title;
            document.getElementById('up_pct').textContent = Math.round(o.pct) + '%';
            document.getElementById('up_bar').style.width = o.pct + '%';
            document.getElementById('up_grid').innerHTML = o.rows.map(r => '<div>' + esc(r[0]) + '</div><div><b>' + esc(r[1]) + '</b></div>').join('');
        }
        function bcEnd() {
            clearTimeout(bcPollT); clearInterval(bcTickT);
            bcBusy = false;
            document.getElementById('bc_send').disabled = false;
            document.getElementById('bc_test').disabled = false;
        }
        function sendBroadcast(testOnly) {
            if (bcBusy) return;
            const msg = document.getElementById('bc_msg').value;
            if (!msg.trim() && !bcFile) return toast('Write a message or attach media.', false);
            if (!testOnly && !confirm('Send this broadcast now?')) return;
            const fd = new FormData();
            fd.append('message', msg);
            fd.append('audience', bcAudience);
            fd.append('test', testOnly ? '1' : '0');
            if (bcFile) { fd.append('media_type', bcKind); fd.append('media', bcFile); }
            bcBusy = true;
            document.getElementById('bc_send').disabled = true;
            document.getElementById('bc_test').disabled = true;
            const t0 = Date.now();
            const total = bcFile ? bcFile.size : 0;
            const names = {image: 'image', video: 'video', voice: 'voice note'};
            const what = bcKind ? names[bcKind] : 'message';
            const fail = (m) => { bcEnd(); bcStep(-1); bcUI({state: 'err', title: '❌ ' + m, pct: 0, rows: [['Elapsed', fmtDur((Date.now() - t0) / 1000)]]}); toast(m, false); };
            const showUp = (loaded) => {
                const el = (Date.now() - t0) / 1000;
                const speed = loaded / Math.max(el, 0.2);
                const left = loaded >= total ? 'finishing...' : '~' + fmtDur((total - loaded) / Math.max(speed, 1));
                bcStep(0);
                bcUI({title: '⬆️ Uploading ' + what + '...', pct: total ? loaded * 100 / total : 0,
                      rows: [['Uploaded', fmtMB(loaded) + ' of ' + fmtMB(total)], ['Speed', fmtMB(speed) + '/s'], ['Time elapsed', fmtDur(el)], ['Time left', left]]});
            };
            if (total) showUp(0); else { bcStep(0); bcUI({title: '⏳ Sending ' + what + '...', pct: 5, rows: [['Time elapsed', '0s']]}); }
            const xhr = new XMLHttpRequest();
            xhr.open('POST', '/api/broadcast/send');
            xhr.upload.onprogress = (e) => { if (e.lengthComputable && total) showUp(e.loaded); };
            xhr.onerror = () => fail('Upload failed. Check your connection.');
            xhr.onabort = () => fail('Upload cancelled.');
            xhr.onload = () => {
                let d = {};
                try { d = JSON.parse(xhr.responseText); } catch (e) {}
                if (xhr.status < 200 || xhr.status >= 300) return fail(d.message || ('Failed (' + xhr.status + ')'));
                const upSec = (Date.now() - t0) / 1000;
                if (testOnly) {
                    bcEnd(); bcStep(3);
                    bcUI({state: 'done', title: '✅ Test sent to your Telegram', pct: 100, rows: [['Upload time', fmtDur(upSec)], ['Total time', fmtDur(upSec)]]});
                    return toast(d.message, true);
                }
                bcStep(1);
                let last = null;
                const render = () => {
                    const now = (Date.now() - t0) / 1000;
                    const sendSec = Math.max(0.1, now - upSec);
                    const done = last ? last.sent + last.failed : 0;
                    const tot = last ? last.total : (d.total || 0);
                    const rate = done / sendSec;
                    const left = rate > 0 && tot > done ? '~' + fmtDur((tot - done) / rate) : '—';
                    bcUI({title: '📤 Sending to users...', pct: tot ? done * 100 / tot : 0,
                          rows: [['Delivered', done + ' of ' + tot], ['Failed', last ? last.failed : 0], ['Upload took', fmtDur(upSec)], ['Time elapsed', fmtDur(now)], ['Time left', left]]});
                };
                const poll = () => {
                    fetch('/api/broadcast/history').then(r => r.json()).then(h => {
                        const row = (h.rows || []).find(x => x.id === d.hist_id);
                        if (row) { last = row; }
                        if (row && row.status !== 'running') {
                            const total2 = (Date.now() - t0) / 1000;
                            bcEnd(); bcStep(3);
                            bcUI({state: 'done', title: '✅ Broadcast completed', pct: 100,
                                  rows: [['Delivered', row.sent + ' of ' + row.total], ['Failed', row.failed], ['Upload time', fmtDur(upSec)], ['Sending time', fmtDur(Math.max(0, total2 - upSec))], ['Total time', fmtDur(total2)]]});
                            bcHistLoad(); toast('Broadcast completed in ' + fmtDur(total2), true);
                            return;
                        }
                        render();
                        bcPollT = setTimeout(poll, 1500);
                    }).catch(() => { bcPollT = setTimeout(poll, 3000); });
                };
                bcTickT = setInterval(render, 1000);
                render(); poll(); bcHistLoad();
            };
            xhr.send(fd);
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
                        + '<div class="set-note">' + esc(r.audience) + (r.media ? ' · ' + esc(r.media) : '') + (r.dur ? ' · ⏱ ' + fmtDur(r.dur) : '') + '</div>'
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

        /* ===== LIVE WALLPAPER: background video + looping song ===== */
        let bgCfg = BG;
        const bgV = document.getElementById('bgVideo');
        const bgA = document.getElementById('bgAudio');
        const musicFab = document.getElementById('musicFab');
        let musicMuted = false;
        try { musicMuted = localStorage.getItem('bg_music_muted') === '1'; } catch (e) {}
        function musicFabState(playing, needTap) {
            if (!musicFab) return;
            document.getElementById('musicIc').innerText = playing ? '🔊' : (musicMuted ? '🔇' : '🎵');
            musicFab.classList.toggle('need-tap', !!needTap);
        }
        function musicTry() {
            if (!bgA || !bgA.getAttribute('src') || musicMuted) { musicFabState(false, false); return; }
            const p = bgA.play();
            if (p && p.then) {
                p.then(() => musicFabState(true, false)).catch(() => {
                    musicFabState(false, true);
                    const once = () => { document.removeEventListener('pointerdown', once, true); document.removeEventListener('keydown', once, true); musicTry(); };
                    document.addEventListener('pointerdown', once, true);
                    document.addEventListener('keydown', once, true);
                });
            }
        }
        function musicToggle() {
            if (!bgA || !bgA.getAttribute('src')) return;
            if (!bgA.paused) { bgA.pause(); musicMuted = true; }
            else { musicMuted = false; }
            try { localStorage.setItem('bg_music_muted', musicMuted ? '1' : '0'); } catch (e) {}
            if (musicMuted) { musicFabState(false, false); } else { musicTry(); }
        }
        function bgApply(cfg) {
            bgCfg = cfg;
            const live = !!(cfg.enabled && cfg.video);
            document.body.classList.toggle('bg-live', live);
            const dim = document.getElementById('bgDim');
            if (dim) dim.style.background = 'rgba(5,2,12,' + cfg.dim + ')';
            if (live) {
                if (bgV.getAttribute('src') !== cfg.video) { bgV.src = cfg.video; }
                bgV.muted = true;
                const vp = bgV.play(); if (vp && vp.catch) vp.catch(() => {});
            } else {
                bgV.pause(); bgV.removeAttribute('src'); bgV.load();
            }
            const song = !!(cfg.enabled && cfg.audio);
            if (song) {
                if (bgA.getAttribute('src') !== cfg.audio) { bgA.src = cfg.audio; }
                bgA.loop = true;
                bgA.volume = Math.min(1, Math.max(0, cfg.volume));
                if (musicFab) musicFab.style.display = 'flex';
                musicTry();
            } else {
                bgA.pause(); bgA.removeAttribute('src'); bgA.load();
                if (musicFab) musicFab.style.display = 'none';
            }
        }
        bgApply(BG);
        function wpNow(kind) {
            const el = document.getElementById(kind === 'video' ? 'wp_vnow' : 'wp_anow');
            const name = bgCfg[kind + '_name'], src = bgCfg[kind + '_src'];
            el.innerHTML = name ? ('Now: <b>' + escH(name) + '</b> (' + (src === 'upload' ? 'uploaded file' : 'link') + ')') : 'Nothing added yet.';
        }
        function openWallpaper() {
            document.getElementById('wp_vurl').value = bgCfg.video_src === 'url' ? bgCfg.video : '';
            document.getElementById('wp_aurl').value = bgCfg.audio_src === 'url' ? bgCfg.audio : '';
            document.getElementById('wp_vfile').value = '';
            document.getElementById('wp_afile').value = '';
            document.getElementById('wp_vol').value = Math.round(bgCfg.volume * 100);
            document.getElementById('wp_dim').value = Math.round(bgCfg.dim * 100);
            document.getElementById('wp_on').checked = !!bgCfg.enabled;
            wpNow('video'); wpNow('audio'); wpPreview();
            document.getElementById('wpModal').style.display = 'flex';
        }
        function closeWallpaper() { document.getElementById('wpModal').style.display = 'none'; bgApply(bgCfg); }
        function wpPreview() {
            const v = parseInt(document.getElementById('wp_vol').value), d = parseInt(document.getElementById('wp_dim').value);
            document.getElementById('wp_vol_t').innerText = v;
            document.getElementById('wp_dim_t').innerText = d;
            if (bgA) bgA.volume = v / 100;
            const dim = document.getElementById('bgDim'); if (dim) dim.style.background = 'rgba(5,2,12,' + (d / 100) + ')';
        }
        function wpUpload(kind, file) {
            return new Promise((resolve, reject) => {
                const prog = document.getElementById(kind === 'video' ? 'wp_vprog' : 'wp_aprog');
                const bar = prog.firstElementChild;
                prog.style.display = 'block'; bar.style.width = '0';
                const fd = new FormData(); fd.append('kind', kind); fd.append('file', file);
                const x = new XMLHttpRequest();
                x.open('POST', '/api/bg/upload');
                x.upload.onprogress = e => { if (e.lengthComputable) bar.style.width = Math.round(e.loaded * 100 / e.total) + '%'; };
                x.onload = () => {
                    let d = {};
                    try { d = JSON.parse(x.responseText); } catch (e) {}
                    if (x.status >= 200 && x.status < 300) { bar.style.width = '100%'; resolve(d); }
                    else reject(new Error(d.message || (x.status === 413 ? 'File is too large' : 'Upload failed')));
                };
                x.onerror = () => reject(new Error('Network error while uploading'));
                x.send(fd);
            });
        }
        async function wpSave() {
            const btn = document.getElementById('wp_save');
            btn.disabled = true; btn.innerText = 'Saving...';
            try {
                const vf = document.getElementById('wp_vfile').files[0];
                const af = document.getElementById('wp_afile').files[0];
                if (vf) await wpUpload('video', vf);
                if (af) await wpUpload('audio', af);
                const body = {
                    volume: parseInt(document.getElementById('wp_vol').value) / 100,
                    dim: parseInt(document.getElementById('wp_dim').value) / 100,
                    enabled: document.getElementById('wp_on').checked
                };
                const vu = document.getElementById('wp_vurl').value.trim();
                const au = document.getElementById('wp_aurl').value.trim();
                if (!vf && vu !== (bgCfg.video_src === 'url' ? bgCfg.video : '')) body.video_url = vu;
                if (!af && au !== (bgCfg.audio_src === 'url' ? bgCfg.audio : '')) body.audio_url = au;
                const r = await fetch('/api/bg/save', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body) });
                const d = await r.json();
                if (!r.ok) throw new Error(d.message || 'Save failed');
                bgApply(d.bg);
                document.getElementById('wpModal').style.display = 'none';
                toast('Live wallpaper saved ✔');
            } catch (e) {
                toast(e.message || 'Save failed', false);
            }
            btn.disabled = false; btn.innerText = '💾 Save & Apply';
            document.querySelectorAll('.wp-prog').forEach(p => { p.style.display = 'none'; });
        }
        function wpClear(kind) {
            if (!confirm('Remove the ' + kind + '?')) return;
            fetch('/api/bg/clear', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({kind: kind}) })
            .then(r => r.json()).then(d => {
                bgApply(d.bg);
                document.getElementById(kind === 'video' ? 'wp_vurl' : 'wp_aurl').value = '';
                wpNow('video'); wpNow('audio');
                toast(d.message);
            });
        }


        /* ===== Instant save: fetch, green toast (red on failure), refresh only the changed tables - the page never reloads ===== */
        function act(url, body, after, refresh) {
            return postJSON(url, body).then(d => {
                const good = d._ok && !d.error;
                toast(d.message || (good ? 'Saved ✔' : 'Failed'), good);
                if (good) { if (after) after(d); if (refresh !== false) softRefresh(); }
                return d;
            }).catch(() => { toast('Network error - please try again', false); });
        }
        async function softRefresh() {
            for (let i = 0; i < 30 && liveBusy; i++) await new Promise(r => setTimeout(r, 100));
            await liveRefresh(true);
        }
        function val(id) { const e = document.getElementById(id); return e ? e.value : ''; }
        function chk(id) { const e = document.getElementById(id); return !!(e && e.checked); }
        function prevHtml(t) {
            return esc(t).replace(new RegExp('&lt;(/?)(b|i|u|s|code|pre|strong|em)&gt;', 'g'), '<$1$2>');
        }
        /* ---- Reseller ---- */
        function rsPrev() {
            const mn = parseFloat(val('rs_min')) || 0, fee = parseFloat(val('rs_fee')) || 0;
            const t = val('rs_text');
            const fmt = n => String(Math.round(n * 100) / 100);
            document.getElementById('rs_prev').innerHTML = t.trim()
                ? prevHtml(t).split('{min_balance}').join(fmt(mn)).split('{fee}').join(fmt(fee)).split('{rest}').join(fmt(Math.max(0, mn - fee)))
                : '<span class="text-muted">(empty: the bot shows only "👑 Reseller Plan")</span>';
        }
        function saveReseller() {
            act('/api/reseller/save', {enabled: chk('rs_on'), min_balance: val('rs_min'), fee: val('rs_fee'), factor: val('rs_factor'),
                text: val('rs_text'), buy_label: val('rs_buy'), success_text: val('rs_ok'), lowbal_text: val('rs_low')}, null, false);
        }
        /* ---- Welcome / alerts / language ---- */
        let wcImg = {{ ui.welcome_image|tojson }};
        function wcPrev() {
            const t = val('wc_text');
            document.getElementById('wc_prev').innerHTML = t.trim() ? prevHtml(t) : '<span class="text-muted">(empty: the normal welcome of the bot is used)</span>';
        }
        function wcShowImg() {
            const box = document.getElementById('wc_img_box');
            if (!box) return;
            if (!wcImg) { box.innerHTML = ''; return; }
            const src = /^https?:/.test(wcImg) ? wcImg : '/api/tgfile/' + encodeURIComponent(wcImg);
            box.innerHTML = '<img src="' + esc(src) + '" alt="" style="max-width:100%;max-height:200px;border-radius:12px;border:1px solid var(--neon-border)">';
        }
        function wcUrl(v) { wcImg = v.trim(); wcShowImg(); }
        function wcUpload(inp) {
            const f = inp.files && inp.files[0];
            if (!f) return;
            const rd = new FileReader();
            rd.onload = () => {
                const im = new Image();
                im.onload = () => {
                    const sc = Math.min(1, 1280 / Math.max(im.width, im.height));
                    const c = document.createElement('canvas');
                    c.width = Math.round(im.width * sc); c.height = Math.round(im.height * sc);
                    c.getContext('2d').drawImage(im, 0, 0, c.width, c.height);
                    toast('Uploading picture...');
                    act('/api/welcome/image', {data: c.toDataURL('image/jpeg', 0.85)}, d => {
                        wcImg = d.file_id; document.getElementById('wc_img_on').checked = true; wcShowImg();
                    }, false);
                };
                im.onerror = () => toast('Could not read that picture', false);
                im.src = rd.result;
            };
            rd.readAsDataURL(f);
            inp.value = '';
        }
        function wcRemoveImg() {
            act('/api/welcome/image', {clear: true}, () => { wcImg = ''; document.getElementById('wc_img_on').checked = false; document.getElementById('wc_url').value = ''; wcShowImg(); }, false);
        }
        function saveWelcome() {
            act('/api/welcome/save', {welcome_text: val('wc_text'), image_on: chk('wc_img_on'), welcome_image: wcImg,
                bot_description: val('wc_desc'), bot_short_description: val('wc_short')}, null, false);
        }
        function saveAlerts() { act('/api/alerts/save', {on: chk('al_on'), chat: val('al_chat')}, null, false); }
        function testAlert() { act('/api/alerts/test', {}, null, false); }
        function saveLang() { act('/api/lang/save', {on: chk('lg_on')}, null, false); }
        wcPrev(); rsPrev(); wcShowImg();
        /* ---- Premium emoji picker ---- */
        let EMO_PACK = [], EMO_BY_ID = {}, EMO_LOADED = false, emoRow = null;
        function emoThumb(e) { return e && e.thumb ? '/api/tgfile/' + encodeURIComponent(e.thumb) : ''; }
        function emoIndex(pack) { EMO_PACK = pack || []; EMO_BY_ID = {}; EMO_PACK.forEach(e => { EMO_BY_ID[e.id] = e; }); }
        function emoPaintRow(row) {
            const id = row.dataset.emoji || '', e = EMO_BY_ID[id], box = row.querySelector('.bc-ico');
            box.innerHTML = !id ? '' : (e && e.thumb ? '<img src="' + emoThumb(e) + '" alt="">' : '<span>' + esc(e ? e.emoji : '★') + '</span> ');
            row.querySelector('.bc-emoclear').style.display = id ? '' : 'none';
        }
        function emoPaintAll() { document.querySelectorAll('.bc-row').forEach(emoPaintRow); }
        function emoLoad() {
            EMO_LOADED = true;
            fetch('/api/emoji/pack').then(r => r.json()).then(d => { emoIndex(d.pack); emoPaintAll(); }).catch(() => { EMO_LOADED = false; });
        }
        function emoLoadPack() {
            act('/api/emoji/pack', {name: val('emoPackName')}, d => { emoIndex(d.pack); emoPaintAll(); }, false);
        }
        function emoSet(row, id) { row.dataset.emoji = id || ''; emoPaintRow(row); }
        function emoOpen(btn) {
            emoRow = btn.closest('.bc-row');
            document.getElementById('emoManual').value = emoRow.dataset.emoji || '';
            const g = document.getElementById('emoGrid');
            g.innerHTML = EMO_PACK.length ? EMO_PACK.map((e, i) =>
                '<button type="button" class="emo-cell" onclick="emoChoose(' + i + ')" title="' + esc(e.emoji) + '"><span>' + esc(e.emoji) + '</span>' +
                (e.thumb ? '<img loading="lazy" src="' + emoThumb(e) + '" alt="" onerror="this.remove()">' : '') + '</button>').join('')
                : '<div class="small text-muted p-2">No pack loaded yet. Close this, paste your emoji pack link and press "Load pack" (or type an emoji ID below).</div>';
            document.getElementById('emoModal').style.display = 'flex';
        }
        function emoChoose(i) { if (emoRow && EMO_PACK[i]) emoSet(emoRow, EMO_PACK[i].id); document.getElementById('emoModal').style.display = 'none'; }
        function emoManualUse() {
            const v = val('emoManual').trim();
            if (v && !/^[0-9]{5,25}$/.test(v)) { toast('An emoji ID is digits only', false); return; }
            if (emoRow) emoSet(emoRow, v);
            document.getElementById('emoModal').style.display = 'none';
        }

        /* ===== AUTO REFRESH every 15 s: silent, the page never reloads (so the video and song keep playing) ===== */
        let liveLeft = 15, liveBusy = false, liveReloaded = false;
        function anyModalOpen() {
            return Array.from(document.querySelectorAll('.modal-bg')).some(m => m.style.display === 'flex');
        }
        async function liveRefresh(force) {
            if (liveBusy) return;
            if (!force && (document.hidden || anyModalOpen())) return;
            liveBusy = true;
            try {
                const r = await fetch('/', { credentials: 'same-origin', cache: 'no-store' });
                const html = await r.text();
                const doc = new DOMParser().parseFromString(html, 'text/html');
                if (!doc.getElementById('tab-dashboard')) {
                    if (!liveReloaded) { liveReloaded = true; location.reload(); }
                    return;
                }
                document.querySelectorAll('.tab-pane-content').forEach(pane => {
                    const np = doc.getElementById(pane.id);
                    if (!np) return;
                    const cur = pane.querySelectorAll('[data-live]');
                    const nw = np.querySelectorAll('[data-live]');
                    if (cur.length !== nw.length) return;
                    cur.forEach((el, i) => {
                        if (el.innerHTML === nw[i].innerHTML) return;
                        // never overwrite something the admin is typing into
                        if (!force && el.contains(document.activeElement)) return;
                        if (!force && Array.from(el.querySelectorAll('input,textarea')).some(f => f.value)) return;
                        el.innerHTML = nw[i].innerHTML;
                    });
                });
                const m = html.match(/PRODUCTS = (.*);/);
                const pm = document.getElementById('prodModal');
                if (m && !(pm && pm.style.display === 'flex')) { try { PRODUCTS = JSON.parse(m[1]); } catch (e) {} }
                if (typeof lkProdsFilled !== 'undefined') lkProdsFilled = false;
                document.querySelectorAll('.tab-pane-content input[onkeyup]').forEach(i => { if (i.value && i.onkeyup) i.onkeyup(); });
                if (typeof memFilter === 'function') memFilter();
            } catch (e) { /* network hiccup: try again at the next tick */ }
            liveBusy = false;
        }
        (function () {
            const sec = document.getElementById('liveSec');
            if (!sec) return;
            memFilter();
            setInterval(() => {
                liveLeft -= 1;
                if (liveLeft <= 0) { liveLeft = 15; liveRefresh(false); }
                sec.textContent = liveLeft;
            }, 1000);
        })();
    </script>
</body>
</html>
"""


# ==========================================
# 🌐 BACKEND API ENDPOINTS
# ==========================================
from concurrent.futures import ThreadPoolExecutor as _TPE
_POOL = _TPE(max_workers=6)
_ADMIN_TPL = None

def _par(*fns):
    """Run independent database reads at the same time and return their results in order."""
    futs = [_POOL.submit(f) for f in fns]
    return [f.result() for f in futs]

def render_admin(**ctx):
    """Render the admin page. The big template is compiled ONCE (render_template_string re-compiled it on every request)."""
    global _ADMIN_TPL
    if _ADMIN_TPL is None:
        _ADMIN_TPL = app.jinja_env.from_string(ADMIN_HTML)
    app.update_template_context(ctx)
    return _ADMIN_TPL.render(ctx)

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
    return render_admin(bg=_bg_ctx(), bot_info=BOT_INFO, error="Invalid Admin Password!")

@app.route('/logout')
def logout():
    session.clear()
    return redirect('/')

@app.route('/')
def dashboard():
    if not admin_required():
        session.clear()  # drops stale sessions (e.g. after "Logout all devices")
        return render_admin(bg=_bg_ctx(), bot_info=BOT_INFO)

    today_prefix = datetime.now(IST).strftime("%d %b %Y") + "%"
    delivered_sql = "LEFT(COALESCE(o.key_delivered, ''), 7) <> 'PENDING'"

    def _stock():
        d = {}
        for pk, plan, cnt in q_all('SELECT prod_key, plan, COUNT(*) FROM keys_inventory WHERE is_used = 0 GROUP BY prod_key, plan'):
            d.setdefault(pk, {})[plan] = cnt
        return d

    def _alerts():
        return [
            {
                "time_txt": r["received_at"].astimezone(IST).strftime("%d %b, %I:%M %p") if r["received_at"] else "",
                "amount": r["amount"], "utr": r["utr"], "status": r["status"], "order_id": r["order_id"],
                "snippet": (r["raw_text"] or "")[:90],
            }
            for r in db.recent_bank_credits(20)
        ]

    # all independent reads run at the same time (several pooled connections) instead of one after another
    (r_u, r_o, tot_prods, tot_keys, today_dep, awaiting, stock, prod_rows, all_orders, pending, users_list, topups,
     st_r, pay, gm, alerts, pay_stats_rows, saved_looks, r_k, r_t, pending_n, today_by_product_rows, lifetime_rows,
     bc_row, st, ui) = _par(
        lambda: q_one('SELECT COUNT(*), COALESCE(SUM(wallet_balance), 0) FROM users'),
        lambda: q_one("SELECT COUNT(*), COALESCE(SUM(amount), 0) FROM order_history WHERE LEFT(COALESCE(key_delivered, ''), 7) <> 'PENDING'"),
        lambda: q_one('SELECT COUNT(*) FROM products')[0],
        lambda: q_one('SELECT COUNT(*) FROM keys_inventory WHERE is_used = 0')[0],
        lambda: float(q_one(
            """
            SELECT COALESCE(SUM(COALESCE(pay_amount, amount)), 0) FROM payment_records
            WHERE status = 'verified'
              AND (created_ts AT TIME ZONE 'Asia/Kolkata')::date = (now() AT TIME ZONE 'Asia/Kolkata')::date
            """
        )[0] or 0),
        lambda: q_one("SELECT COUNT(*) FROM payment_records WHERE status = 'awaiting_approval'")[0] or 0,
        _stock,
        lambda: q_all('SELECT prod_key, name, category, prices, icon, download_link, maintenance, stock_out FROM products ORDER BY name'),
        lambda: q_all('SELECT id, user_id, prod_name, plan, key_delivered, amount, utr, timestamp FROM order_history ORDER BY id DESC LIMIT 50'),
        lambda: q_all("SELECT id, user_id, prod_name, plan, key_delivered, amount, timestamp FROM order_history WHERE LEFT(key_delivered, 7) = 'PENDING' ORDER BY id DESC"),
        lambda: q_all('SELECT user_id, full_name, username, joined_date, orders_count, wallet_balance, total_spent, total_referrals, referral_earnings, account_type FROM users ORDER BY user_id DESC LIMIT 500'),
        lambda: q_all('SELECT created_at, user_id, COALESCE(pay_amount, amount), status, order_id FROM payment_records ORDER BY id DESC LIMIT 30'),
        lambda: q_one('SELECT support_username, how_to_use_link FROM store_settings WHERE id = 1') or ("@Athulsudin", ""),
        db.get_payment_settings,
        db.get_gmail_config,
        _alerts,
        lambda: (
            q_one("SELECT COUNT(*) FROM payment_records WHERE status = 'verified' "
                  "AND (created_ts AT TIME ZONE 'Asia/Kolkata')::date = (now() AT TIME ZONE 'Asia/Kolkata')::date")[0] or 0,
            q_one("SELECT COUNT(*) FROM bank_credits WHERE status = 'unmatched'")[0] or 0,
        ),
        db.get_button_looks,
        lambda: q_one(f"SELECT COUNT(*), COALESCE(SUM(o.amount), 0) FROM order_history o WHERE {delivered_sql}"),
        lambda: q_one(f"SELECT COUNT(*), COALESCE(SUM(o.amount), 0) FROM order_history o WHERE {delivered_sql} AND {TODAY_SQL}", (today_prefix,)),
        lambda: q_one("SELECT COUNT(*) FROM order_history WHERE LEFT(COALESCE(key_delivered, ''), 7) = 'PENDING'")[0] or 0,
        lambda: q_all(
            f"SELECT o.prod_name, COUNT(*), SUM(o.amount) FROM order_history o WHERE {delivered_sql} AND {TODAY_SQL} "
            "GROUP BY o.prod_name ORDER BY SUM(o.amount) DESC", (today_prefix,)),
        lambda: q_all(
            f"SELECT o.prod_name, COUNT(*), SUM(o.amount) FROM order_history o WHERE {delivered_sql} GROUP BY o.prod_name"),
        lambda: q_one(
            "SELECT COUNT(*) AS c_all, COUNT(*) FILTER (WHERE orders_count > 0) AS c_cust, COUNT(*) FILTER (WHERE orders_count = 0) AS c_non, "
            "COUNT(*) FILTER (WHERE account_type = 'Reseller') AS c_res FROM users"
        ),
        db.get_settings,
        db.get_ui_settings,
    )
    tot_users, tot_wallet = r_u[0] or 0, float(r_u[1] or 0)
    tot_orders, tot_rev = r_o[0] or 0, float(r_o[1] or 0)
    prods = [
        {"prod_key": p[0], "name": p[1], "category": p[2], "prices": parse_prices(p[3]),
         "icon": p[4] or "⚡", "download_link": p[5] or "", "maintenance": int(p[6] or 0),
         "stock_out": int(p[7] or 0), "stock": stock.get(p[0], {})}
        for p in prod_rows
    ]
    pay_cfg = {
        "upi_id": pay["upi_id"], "payee_name": pay["payee_name"], "auto_verify": bool(pay["auto_verify"]),
    }
    gm_state = (gmail_status() if gmail_status else None) or {}
    gmail_cfg = {
        "user": gm.get("gmail_user") or "", "has_password": bool(gm.get("gmail_app_password")),
        "domain": gm.get("bank_domain") or "",
        "status": (gm_state.get("detail") or "Not connected.")
                  + (f" Last check: {gm_state['last_check']}." if gm_state.get("last_check") else "")
                  + (f" Last e-mail: {gm_state['last_mail']}." if gm_state.get("last_mail") else ""),
    }
    pay_stats = {"today_count": pay_stats_rows[0], "unmatched": pay_stats_rows[1]}

    button_groups = []
    for title, items in BUTTON_GROUPS:
        rows = []
        for k, label, default in items:
            sv = saved_looks.get(k)
            style = (sv[0] if sv and sv[0] is not None else default) or "none"
            rows.append({"key": k, "label": label, "default": default or "none",
                         "style": style, "emoji": (sv[1] if sv else "") or ""})
        button_groups.append({"title": title, "items": rows})

    key_stats = {
        "total": r_k[0] or 0, "today": r_t[0] or 0, "today_rev": f"{float(r_t[1] or 0):,.2f}",
        "live_products": sum(1 for p in prods if not p["maintenance"] and not p["stock_out"]),
        "pending": pending_n,
    }
    today_by_product = [(r[0], r[1], float(r[2] or 0)) for r in today_by_product_rows]
    lifetime = {r[0]: [r[1], float(r[2] or 0)] for r in lifetime_rows}
    bc_counts = {"all": bc_row[0] or 0, "customers": bc_row[1] or 0, "non_buyers": bc_row[2] or 0, "resellers": bc_row[3] or 0}

    return render_admin(
        bg=_bg_ctx(), bot_info=BOT_INFO, button_groups=button_groups, topups=topups, ui=_ui_view(ui),
        key_stats=key_stats, today_by_product=today_by_product, lifetime=lifetime,
        bc_counts=bc_counts, admin_id=ADMIN_ID,
        stats={"users": tot_users, "total_wallet": f"{tot_wallet:,.2f}", "products_count": tot_prods,
               "orders": tot_orders, "revenue": f"{tot_rev:,.2f}", "key_stock": tot_keys,
               "today_deposit": f"{today_dep:,.2f}", "awaiting": awaiting},
        products=prods, all_orders=all_orders, pending_orders=pending, users_list=users_list,
        pay_cfg=pay_cfg, gmail=gmail_cfg, alerts=alerts, store=st, pay_stats=pay_stats
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

# ==========================================
# 🔌 SUPPLIER API (other panel we buy keys from)
# ==========================================
RESELLER_FACTOR = 0.6   # same value as main.py: reseller price = regular price x 0.6
_SUP_CACHE = {"t": 0.0, "data": None}

@app.route('/api/supplier/config')
def api_supplier_config():
    if not admin_required(): return unauthorized()
    url, key = supplier_api.get_config()
    return jsonify({"url": url, "has_key": bool(key), "key_hint": ("..." + key[-4:]) if key else ""})

@app.route('/api/supplier/save', methods=['POST'])
def api_supplier_save():
    if not admin_required(): return unauthorized()
    d = request.json or {}
    url = (d.get('url') or '').strip() or supplier_api.DEFAULT_URL
    key = "".join((d.get('key') or '').split())
    if not url.lower().startswith('https://'):
        return jsonify({"ok": False, "message": "The API URL must start with https://"}), 400
    db.set_supplier_config(url, key if key else None)
    _SUP_CACHE["data"] = None
    return jsonify({"ok": True, "message": "Supplier API settings saved."})

@app.route('/api/supplier/test', methods=['POST'])
def api_supplier_test():
    if not admin_required(): return unauthorized()
    try:
        bal, cur = supplier_api.get_balance()
        return jsonify({"ok": True, "message": f"✅ Connected! Supplier wallet balance: ₹{bal:,.2f} {cur}"})
    except supplier_api.SupplierError as e:
        return jsonify({"ok": False, "message": f"❌ {e}"})

@app.route('/api/supplier/services')
def api_supplier_services():
    if not admin_required(): return unauthorized()
    now = time.time()
    if request.args.get('refresh') or not _SUP_CACHE["data"] or now - _SUP_CACHE["t"] > 120:
        try:
            items = supplier_api.get_services()
        except supplier_api.SupplierError as e:
            return jsonify({"ok": False, "message": f"❌ {e}"})
        _SUP_CACHE["data"], _SUP_CACHE["t"] = [
            {k: v for k, v in it.items() if k != "raw"} for it in items if it.get("pid") != ""
        ], now
    linked = sorted({m["pid"] for m in db.list_supplier_map()})
    return jsonify({"ok": True, "services": _SUP_CACHE["data"], "linked": linked})

@app.route('/api/supplier/maps')
def api_supplier_maps():
    if not admin_required(): return unauthorized()
    mapped = {(m["prod_key"], m["plan"]): m["pid"] for m in db.list_supplier_map()}
    out = []
    for row in db.fetchall("SELECT prod_key, name, prices FROM products ORDER BY category, prod_key"):
        plans = [{"plan": n, "price": p, "reseller_price": round(p * RESELLER_FACTOR, 2), "pid": mapped.get((row["prod_key"], n))}
                 for n, p in parse_prices(row["prices"])]
        out.append({"prod_key": row["prod_key"], "name": row["name"] or row["prod_key"], "plans": plans})
    return jsonify({"products": out})

@app.route('/api/supplier/map', methods=['POST'])
def api_supplier_map():
    if not admin_required(): return unauthorized()
    d = request.json or {}
    prod_key, plan, pid = d.get('prod_key'), d.get('plan'), d.get('pid')
    prod = db.get_product_by_key(prod_key or '')
    if not prod or not any(n == plan for n, _ in parse_prices(prod["prices"])):
        return jsonify({"ok": False, "message": "Product or plan not found."}), 400
    if pid in (None, ''):
        db.delete_supplier_pid(prod_key, plan)
        return jsonify({"ok": True, "message": "Link removed. This plan now uses only your own stock."})
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return jsonify({"ok": False, "message": "Invalid PID."}), 400
    db.set_supplier_pid(prod_key, plan, pid)
    return jsonify({"ok": True, "message": f"Linked to supplier PID {pid}. Empty stock now buys from the supplier."})

@app.route('/api/supplier/test_order', methods=['POST'])
def api_supplier_test_order():
    if not admin_required(): return unauthorized()
    d = request.json or {}
    if d.get('confirm') is not True:
        return jsonify({"ok": False, "message": "Confirmation missing."}), 400
    try:
        pid = int(d.get('pid'))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "message": "Invalid PID."}), 400
    try:
        r = supplier_api.order(pid, 1, "admin_test")
    except supplier_api.SupplierUncertain as e:
        return jsonify({"ok": False, "message": f"⚠️ Unclear result - the order MAY have been charged. Check the supplier panel.\n{e}"})
    except supplier_api.SupplierError as e:
        return jsonify({"ok": False, "message": f"❌ {e}"})
    rem = f"₹{r['remaining_balance']:,.2f}" if r["remaining_balance"] is not None else "?"
    return jsonify({"ok": True, "message": f"✅ Key: {r['license_key']}\nOrder: #{r['order_code']}\nSupplier balance left: {rem}"})

@app.route('/api/supplier/remove', methods=['POST'])
def api_supplier_remove():
    """Remove a plan (by supplier PID, or by product+plan) or a whole product from the shop."""
    if not admin_required(): return unauthorized()
    d = request.json or {}
    targets = []
    if d.get('pid') not in (None, ''):
        try:
            pid = int(d.get('pid'))
        except (TypeError, ValueError):
            return jsonify({"ok": False, "message": "Invalid PID."}), 400
        targets = [(m["prod_key"], m["plan"]) for m in db.list_supplier_map() if int(m["pid"]) == pid]
    elif d.get('prod_key'):
        targets = [(d.get('prod_key'), d.get('plan') or None)]
    if not targets:
        return jsonify({"ok": False, "message": "Nothing found to remove."}), 400
    removed = 0
    for prod_key, plan in targets:
        prod = db.get_product_by_key(prod_key)
        if plan is None or not prod:
            db.execute('DELETE FROM products WHERE prod_key = %s', (prod_key,))
            db.execute('DELETE FROM supplier_map WHERE prod_key = %s', (prod_key,))
            removed += 1
            continue
        left = [[n, pr] for n, pr in parse_prices(prod["prices"]) if n != plan]
        db.delete_supplier_pid(prod_key, plan)
        if left:
            db.execute('UPDATE products SET prices = %s WHERE prod_key = %s', (json.dumps(left), prod_key))
        else:   # no plan left -> remove the empty product too
            db.execute('DELETE FROM products WHERE prod_key = %s', (prod_key,))
        removed += 1
    return jsonify({"ok": True, "message": f"Removed {removed} item(s) from your shop."})

@app.route('/api/supplier/import', methods=['POST'])
def api_supplier_import():
    """Create (or extend) a product in our shop from a supplier product and link every plan to its PID."""
    if not admin_required(): return unauthorized()
    d = request.json or {}
    name = re.sub(r'\s+', ' ', (d.get('name') or '').strip())[:60]
    category = d.get('category') if d.get('category') in CATEGORIES else 'non_root'
    if not name:
        return jsonify({"ok": False, "message": "Product name is missing."}), 400
    plans, seen = [], set()
    for it in d.get('plans') or []:
        pn = (it.get('plan') or '').strip()
        try:
            price, pid = round(float(it.get('price')), 2), int(it.get('pid'))
        except (TypeError, ValueError):
            return jsonify({"ok": False, "message": f"Invalid price or PID for '{pn}'."}), 400
        if not PLAN_RE.match(pn) or price < 0 or pn in seen:
            return jsonify({"ok": False, "message": f"Plan name/price not valid: '{pn}'."}), 400
        seen.add(pn)
        plans.append((pn, price, pid))
    if not plans:
        return jsonify({"ok": False, "message": "Tick at least one plan."}), 400
    k = re.sub(r'[^a-zA-Z0-9]', '_', name).strip('_').lower()[:32]
    if not k:
        return jsonify({"ok": False, "message": "Invalid product name."}), 400

    with db.connection() as conn:
        with conn.cursor() as cur:
            cur.execute('SELECT prices FROM products WHERE prod_key = %s', (k,))
            row = cur.fetchone()
            if row:
                existing = parse_prices(row["prices"])
                have = {n for n, _ in existing}
                plans = [p for p in plans if p[0] not in have]
                if not plans:
                    return jsonify({"ok": False, "message": "All these plans already exist in your product."}), 400
                cur.execute('UPDATE products SET prices = %s WHERE prod_key = %s',
                            (json.dumps(existing + [[n, pr] for n, pr, _ in plans]), k))
                created = False
            else:
                cur.execute(
                    "INSERT INTO products (prod_key, name, category, prices, download_link, icon, maintenance, stock_out) "
                    "VALUES (%s, %s, %s, %s, '', '⚡', 0, 0)",
                    (k, name, category, json.dumps([[n, pr] for n, pr, _ in plans])),
                )
                created = True
    for pn, _price, pid in plans:
        db.set_supplier_pid(k, pn, pid)
    msg = (f"✅ Product '{name}' created with {len(plans)} plan(s)." if created
           else f"✅ Added {len(plans)} new plan(s) to the existing product '{name}'.")
    return jsonify({"ok": True, "message": msg + " Customers can buy it now; keys come from the supplier automatically."})

PLAN_RE = re.compile(r'^[A-Za-z0-9 _.\-+]{1,22}$')   # short enough for Telegram's 64-byte callback data
CATEGORIES = ("non_root", "root", "ios", "pc", "likes", "carrom", "pool8")

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

def _details_table():
    db.execute("CREATE TABLE IF NOT EXISTS product_details (prod_key TEXT PRIMARY KEY, image_id TEXT, title TEXT, features TEXT)")

def _get_details(prod_key):
    try:
        _details_table()
        return db.fetchone("SELECT image_id, title, features FROM product_details WHERE prod_key = %s", (prod_key,))
    except Exception:
        return None

def _tg_photo_file_id(data_url):
    """Upload the banner once (to the admin chat), keep Telegram's file_id, delete the upload message."""
    import base64, uuid
    raw = base64.b64decode(data_url.split(',', 1)[1])
    admin_id = os.environ.get("ADMIN_ID", "7616127905")
    b = uuid.uuid4().hex
    body = b""
    for n, v in (("chat_id", admin_id), ("disable_notification", "true")):
        body += ('--%s\r\nContent-Disposition: form-data; name="%s"\r\n\r\n%s\r\n' % (b, n, v)).encode()
    body += ('--%s\r\nContent-Disposition: form-data; name="photo"; filename="banner.jpg"\r\nContent-Type: image/jpeg\r\n\r\n' % b).encode()
    body += raw + ('\r\n--%s--\r\n' % b).encode()
    req = urllib.request.Request("https://api.telegram.org/bot%s/sendPhoto" % BOT_TOKEN, data=body,
                                 headers={"Content-Type": "multipart/form-data; boundary=" + b})
    with urllib.request.urlopen(req, timeout=40) as r:
        res = json.loads(r.read().decode())["result"]
    try:
        dreq = urllib.request.Request("https://api.telegram.org/bot%s/deleteMessage" % BOT_TOKEN,
                                      data=json.dumps({"chat_id": admin_id, "message_id": res["message_id"]}).encode(),
                                      headers={"Content-Type": "application/json"})
        urllib.request.urlopen(dreq, timeout=10).read()
    except Exception:
        pass
    return res["photo"][-1]["file_id"]

def _store_details(d, k):
    _details_table()
    old = db.fetchone("SELECT image_id FROM product_details WHERE prod_key = %s", (k,)) or {}
    image_id = old.get("image_id")
    if d.get("pd_remove_image"):
        image_id = None
    if d.get("pd_image"):
        image_id = _tg_photo_file_id(d["pd_image"])
    db.execute("""INSERT INTO product_details (prod_key, image_id, title, features) VALUES (%s, %s, %s, %s)
                  ON CONFLICT (prod_key) DO UPDATE SET image_id = EXCLUDED.image_id, title = EXCLUDED.title, features = EXCLUDED.features""",
               (k, image_id, (d.get("pd_title") or "").strip()[:200], (d.get("pd_features") or "").strip()[:700]))

def _save_with_details(is_update):
    d = request.json or {}
    resp = _save_product(d, is_update=is_update)
    code = resp[1] if isinstance(resp, tuple) else getattr(resp, "status_code", 200)
    if code < 400:
        k = (d.get('prod_key') or '').strip() if is_update else re.sub(r'[^a-zA-Z0-9]', '_', (d.get('name') or '').strip()).strip('_').lower()[:32]
        try:
            _store_details(d, k)
        except Exception:
            import logging
            logging.getLogger(__name__).exception("Could not save product page details")
            return jsonify({"message": "Product saved, but the banner image could not be uploaded to Telegram. Try again."})
    return resp

@app.route('/api/product/details')
def api_product_details():
    if not admin_required(): return unauthorized()
    r = _get_details(request.args.get('k', '')) or {}
    return jsonify({"title": r.get("title") or "", "features": r.get("features") or "", "has_image": bool(r.get("image_id"))})

@app.route('/api/product/save', methods=['POST'])
def api_save_p():
    if not admin_required(): return unauthorized()
    return _save_with_details(False)

@app.route('/api/product/update', methods=['POST'])
def api_update_p():
    if not admin_required(): return unauthorized()
    return _save_with_details(True)

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
    try:
        _details_table()
        db.execute('DELETE FROM product_details WHERE prod_key = %s', ((request.json or {}).get('prod_key'),))
    except Exception:
        pass
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

def _main_mod():
    import sys
    me = sys.modules.get(__name__)
    for name in ("__main__", "main"):
        m = sys.modules.get(name)
        if m is not None and m is not me and hasattr(m, "build_plans_view"):
            return m
    return None

def _esc_html(t):
    return _html.escape(str(t or ""), quote=False)

def _wrap_build_view(fn):
    """Product page text = title + features from the admin panel (no fixed Aim/Headshot lines)."""
    import functools
    @functools.wraps(fn)
    def wrapper(user_id, prod_key, *a, **k):
        res = fn(user_id, prod_key, *a, **k)
        try:
            if not res:
                return res
            row = db.fetchone("SELECT name, icon FROM products WHERE prod_key = %s", (prod_key,)) or {}
            det = _get_details(prod_key) or {}
            title = (det.get("title") or "").strip() or ("%s %s" % (row.get("icon") or "", row.get("name") or "")).strip()
            lines = [x.strip() for x in (det.get("features") or "").splitlines() if x.strip()]
            text = "<b>%s</b>" % _esc_html(title)
            if lines:
                text += "\n\n<blockquote>%s</blockquote>" % "\n".join(_esc_html(x) for x in lines)
            text += "\n\nSelect duration:"
            return text, res[1]
        except Exception:
            return res
    wrapper._pp_hooked = True
    return wrapper

def _wrap_show_plans(orig):
    """Product page with the banner image on top (falls back to the normal text page)."""
    import asyncio
    async def show_product_plans_hooked(update, context):
        try:
            q = update.callback_query
            pk = (q.data or "").replace("selprod_", "", 1)
            det = await asyncio.to_thread(_get_details, pk)
            m = _main_mod()
            if det and det.get("image_id") and m is not None:
                prod = m.get_product_by_key(pk)
                if prod and not prod.get("maintenance"):
                    view = m.build_plans_view(update.effective_user.id, pk)
                    if view and len(view[0]) <= 1000:
                        await context.bot.send_photo(chat_id=q.message.chat_id, photo=det["image_id"], caption=view[0],
                                                     parse_mode="HTML", reply_markup=view[1])
                        try:
                            await q.message.delete()
                        except Exception:
                            pass
                        try:
                            await q.answer()
                        except Exception:
                            pass
                        return
        except Exception:
            import logging
            logging.getLogger(__name__).exception("Product page with image failed - using text page")
        return await orig(update, context)
    show_product_plans_hooked._pp_hooked = True
    return show_product_plans_hooked

def _install_product_page_hook():
    # (a) swap the handler of the product page before the bot registers it
    try:
        from telegram.ext import Application
        if not getattr(Application.add_handler, "_pp_hooked", False):
            _orig_add = Application.add_handler
            def add_handler(self, handler, group=0):
                cb = getattr(handler, "callback", None)
                if getattr(cb, "__name__", "") == "show_product_plans" and not getattr(cb, "_pp_hooked", False):
                    handler.callback = _wrap_show_plans(cb)
                return _orig_add(self, handler, group)
            add_handler._pp_hooked = True
            Application.add_handler = add_handler
    except Exception:
        pass
    # (b) swap the text builder once main.py has loaded
    import sys, time
    for _ in range(240):
        m = _main_mod()
        if m is not None:
            if not getattr(m.build_plans_view, "_pp_hooked", False):
                m.build_plans_view = _wrap_build_view(m.build_plans_view)
            return
        time.sleep(0.5)

try:
    from telegram.ext import Application as _PTBApp
    _pp_orig = _PTBApp.add_handler
    if not getattr(_pp_orig, "_pp_hooked", False):
        def _pp_add_handler(self, handler, group=0):
            cb = getattr(handler, "callback", None)
            if getattr(cb, "__name__", "") == "show_product_plans" and not getattr(cb, "_pp_hooked", False):
                handler.callback = _wrap_show_plans(cb)
            return _pp_orig(self, handler, group)
        _pp_add_handler._pp_hooked = True
        _PTBApp.add_handler = _pp_add_handler
except Exception:
    pass
import threading as _pp_threading
_pp_threading.Thread(target=_install_product_page_hook, daemon=True).start()

_DEMAND_READY = False
def _demand_table():
    global _DEMAND_READY
    if not _DEMAND_READY:
        db.execute("""CREATE TABLE IF NOT EXISTS out_of_stock_demand (
            id SERIAL PRIMARY KEY, user_id BIGINT, prod_name TEXT, plan TEXT,
            price NUMERIC DEFAULT 0, created_at TIMESTAMPTZ DEFAULT now())""")
        _DEMAND_READY = True

def record_demand(user_id, prod_name, plan, price):
    """Remember that a customer wanted this product/plan but there was no key in stock."""
    try:
        _demand_table()
        dup = db.fetchone("SELECT 1 AS x FROM out_of_stock_demand WHERE user_id = %s AND prod_name = %s AND plan = %s "
                          "AND created_at > now() - interval '30 seconds'", (user_id, prod_name, plan))
        if dup:
            return
        db.execute("INSERT INTO out_of_stock_demand (user_id, prod_name, plan, price) VALUES (%s, %s, %s, %s)",
                   (user_id, prod_name, plan, price or 0))
    except Exception:
        import logging
        logging.getLogger(__name__).exception("Could not record out-of-stock demand")

def _wrap_purchase(fn):
    import functools
    @functools.wraps(fn)
    def wrapper(*a, **k):
        res = fn(*a, **k)
        try:
            if isinstance(res, dict) and res.get("status") == "out_of_stock":
                uid = a[0] if a else k.get("user_id")
                plan = a[2] if len(a) > 2 else k.get("plan_name")
                name = (res.get("prod") or {}).get("name") or (a[1] if len(a) > 1 else k.get("prod_key"))
                record_demand(uid, name, plan, res.get("price"))
        except Exception:
            pass
        return res
    wrapper._demand_hooked = True
    return wrapper

def _install_demand_hook():
    """The bot (main.py) runs in this same process: wrap its purchase function so every
    out-of-stock attempt lands in the demand board. No change to main.py is needed."""
    import sys, time
    me = sys.modules.get(__name__)
    for _ in range(240):
        for name in ("__main__", "main"):
            m = sys.modules.get(name)
            fn = getattr(m, "purchase_with_wallet", None) if m is not None and m is not me else None
            if callable(fn):
                if not getattr(fn, "_demand_hooked", False):
                    m.purchase_with_wallet = _wrap_purchase(fn)
                return
        time.sleep(0.5)

import threading as _demand_threading
_demand_threading.Thread(target=_install_demand_hook, daemon=True).start()

@app.route('/api/demand')
def api_demand():
    if not admin_required(): return unauthorized()
    _demand_table()
    rows = db.fetchall("""
        SELECT prod_name, plan, COUNT(*) AS attempts, COUNT(DISTINCT user_id) AS buyers, MAX(price) AS price,
               to_char(MAX(created_at) AT TIME ZONE 'Asia/Kolkata', 'DD Mon, HH12:MI AM') AS last_at
        FROM out_of_stock_demand GROUP BY prod_name, plan
        ORDER BY attempts DESC, MAX(created_at) DESC LIMIT 100""")
    tot = db.fetchone("SELECT COALESCE(SUM(price), 0) AS v, COUNT(*) AS n FROM out_of_stock_demand") or {"v": 0, "n": 0}
    items = [{"prod_name": r["prod_name"] or "", "plan": r["plan"] or "", "attempts": int(r["attempts"]),
              "buyers": int(r["buyers"]), "price": float(r["price"] or 0), "last_at": r["last_at"] or ""} for r in rows]
    return jsonify({"items": items, "total_requests": int(tot["n"] or 0), "total_value": float(tot["v"] or 0)})

@app.route('/api/demand/clear', methods=['POST'])
def api_demand_clear():
    if not admin_required(): return unauthorized()
    d = request.json or {}
    _demand_table()
    if d.get('all'):
        db.execute("DELETE FROM out_of_stock_demand")
        return jsonify({"message": "Demand list cleared."})
    if not d.get('prod_name') or not d.get('plan'):
        return jsonify({"message": "Missing product/plan."}), 400
    db.execute("DELETE FROM out_of_stock_demand WHERE prod_name = %s AND plan = %s", (d['prod_name'], d['plan']))
    return jsonify({"message": "Marked as restocked."})

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
    body = request.json or {}
    styles, emojis = body.get('styles') or {}, body.get('emojis') or {}
    if not isinstance(styles, dict) or not isinstance(emojis, dict):
        return jsonify({"message": "Invalid data!", "error": True}), 400
    allowed = set(BUTTON_COLORS) | {"none"}
    n = 0
    for key in set(styles) | set(emojis):
        if key not in MENU_BUTTONS:
            continue
        st = styles.get(key)
        st = st if st in allowed else None
        em = emojis.get(key)
        em = str(em).strip() if em is not None else None
        if em and not re.fullmatch(r'\d{5,25}', em):
            return jsonify({"message": "An emoji ID must be digits only.", "error": True}), 400
        if st is None and em is None:
            continue
        if st is None:
            st = (db.get_button_looks().get(key) or (MENU_BUTTONS[key][1] or "none",))[0] or "none"
        db.set_button_look(key, style=st, emoji_id=em)
        n += 1
    return jsonify({"message": "Saved ✔ (%d button%s). The bot shows it within a few seconds." % (n, "" if n == 1 else "s")})


# ==========================================
# 😀 PREMIUM EMOJI PACK + TELEGRAM FILES (admin preview)
# ==========================================
def _pack_names(raw):
    out = []
    for part in re.split(r'[\s,]+', raw or ''):
        part = part.strip().rstrip('/')
        if part:
            out.append(part.split('/')[-1])
    return out[:5]

@app.route('/api/emoji/pack', methods=['GET', 'POST'])
def api_emoji_pack():
    if not admin_required(): return unauthorized()
    if request.method == 'GET':
        ui = db.get_ui_settings()
        try:
            pack = json.loads(ui.get("emoji_pack_json") or "[]")
        except Exception:
            pack = []
        return jsonify({"name": ui.get("emoji_pack_name", ""), "pack": pack})
    names = _pack_names((request.json or {}).get('name'))
    if not names:
        return jsonify({"message": "Paste the emoji pack link (like t.me/addemoji/YourPack).", "error": True}), 400
    pack = []
    for nm in names:
        ok, res = tg_call("getStickerSet", {"name": nm})
        if not ok or not isinstance(res, dict):
            return jsonify({"message": "Telegram could not find '%s': %s" % (nm, res), "error": True}), 400
        for st in res.get("stickers", []):
            if st.get("custom_emoji_id"):
                th = (st.get("thumbnail") or {}).get("file_id") or st.get("file_id")
                pack.append({"id": st["custom_emoji_id"], "emoji": st.get("emoji", ""), "thumb": th})
    if not pack:
        return jsonify({"message": "That pack has no custom (premium) emoji.", "error": True}), 400
    db.set_ui_settings({"emoji_pack_name": " ".join(names), "emoji_pack_json": json.dumps(pack, ensure_ascii=False)})
    return jsonify({"message": "Loaded %d emoji ✔" % len(pack), "name": " ".join(names), "pack": pack})

@app.route('/api/tgfile/<fid>')
def api_tgfile(fid):
    """Shows a Telegram file (emoji thumbnail / welcome picture) in the admin without ever exposing the bot token."""
    if not admin_required(): return unauthorized()
    if not re.fullmatch(r'[A-Za-z0-9_\-]{10,250}', fid):
        return "bad id", 400
    ok, res = tg_call("getFile", {"file_id": fid})
    if not ok or not isinstance(res, dict) or not res.get("file_path"):
        return "not found", 404
    try:
        with urllib.request.urlopen("https://api.telegram.org/file/bot%s/%s" % (BOT_TOKEN, res["file_path"]), timeout=15) as r:
            data = r.read()
    except Exception:
        return "not found", 404
    ctype = mimetypes.guess_type(res["file_path"])[0] or "image/webp"
    return Response(data, mimetype=ctype, headers={"Cache-Control": "private, max-age=86400"})


# ==========================================
# 👑 RESELLER / 👋 WELCOME / 🚨 ALERTS / 🌐 LANGUAGE settings (shared with the bot through ui_settings)
# ==========================================
def _ui_view(ui):
    """Admin-written bot settings with defaults, for the settings cards."""
    g = lambda k, d="": (ui.get(k) if ui.get(k) is not None else d)
    return {
        "welcome_text": g("welcome_text"), "welcome_image": g("welcome_image"),
        "welcome_image_on": str(g("welcome_image_on", "0")),
        "bot_description": g("bot_description"), "bot_short_description": g("bot_short_description"),
        "alert_on": str(g("alert_on", "1")), "alert_chat": g("alert_chat") or str(ADMIN_ID),
        "lang_on": str(g("lang_on", "1")),
        "emoji_pack_name": g("emoji_pack_name"),
        "r_enabled": str(g("reseller_enabled", "1")), "r_min": g("reseller_min_balance", "2500"),
        "r_fee": g("reseller_fee", "700"), "r_factor": g("reseller_factor", "0.6"),
        "r_text": g("reseller_text"), "r_buy": g("reseller_buy_label", "Buy Reseller Plan"),
        "r_success": g("reseller_success_text"), "r_low": g("reseller_lowbal_text"),
    }

def _pos_num(v, name, maxv=10_000_000):
    try:
        n = float(str(v).strip())
    except Exception:
        raise ValueError("%s must be a number." % name)
    if n < 0 or n > maxv:
        raise ValueError("%s must be between 0 and %d." % (name, maxv))
    return ("%g" % n)

@app.route('/api/reseller/save', methods=['POST'])
def api_reseller_save():
    if not admin_required(): return unauthorized()
    d = request.json or {}
    try:
        vals = {
            "reseller_enabled": "1" if d.get("enabled") else "0",
            "reseller_min_balance": _pos_num(d.get("min_balance"), "Minimum balance"),
            "reseller_fee": _pos_num(d.get("fee"), "Activation fee"),
            "reseller_factor": _pos_num(d.get("factor"), "Price factor", 10),
        }
    except ValueError as e:
        return jsonify({"message": str(e), "error": True}), 400
    for key, field, limit in (("reseller_text", "text", 3500), ("reseller_buy_label", "buy_label", 60),
                              ("reseller_success_text", "success_text", 3500), ("reseller_lowbal_text", "lowbal_text", 3500)):
        v = str(d.get(field) or "")
        if len(v) > limit:
            return jsonify({"message": "'%s' is too long (max %d characters)." % (field, limit), "error": True}), 400
        vals[key] = v
    db.set_ui_settings(vals)
    return jsonify({"message": "Reseller settings saved ✔ The bot shows them within a few seconds."})

def _bg_tg(method, payload):
    try:
        ok, res = tg_call(method, payload)
        if not ok:
            print("[admin] %s failed: %s" % (method, res))
    except Exception as e:
        print("[admin] %s error: %s" % (method, e))

@app.route('/api/welcome/save', methods=['POST'])
def api_welcome_save():
    if not admin_required(): return unauthorized()
    d = request.json or {}
    text = str(d.get("welcome_text") or "")
    desc = str(d.get("bot_description") or "")
    short = str(d.get("bot_short_description") or "")
    if len(text) > 3500 or len(desc) > 512 or len(short) > 120:
        return jsonify({"message": "Too long: welcome text max 3500, description max 512, short description max 120.", "error": True}), 400
    vals = {"welcome_text": text, "welcome_image_on": "1" if d.get("image_on") else "0",
            "bot_description": desc, "bot_short_description": short}
    if "welcome_image" in d:
        img = str(d.get("welcome_image") or "").strip()
        if img and not (img.startswith("http://") or img.startswith("https://") or re.fullmatch(r'[A-Za-z0-9_\-]{20,250}', img)):
            return jsonify({"message": "Picture must be an https:// link or an uploaded picture.", "error": True}), 400
        vals["welcome_image"] = img
    db.set_ui_settings(vals)
    # the pre-Start texts are sent to Telegram in the background so the Save button answers at once
    _POOL.submit(_bg_tg, "setMyDescription", {"description": desc})
    _POOL.submit(_bg_tg, "setMyShortDescription", {"short_description": short})
    return jsonify({"message": "Welcome settings saved ✔"})

@app.route('/api/welcome/image', methods=['POST'])
def api_welcome_image():
    if not admin_required(): return unauthorized()
    d = request.json or {}
    if d.get("clear"):
        db.set_ui_settings({"welcome_image": "", "welcome_image_on": "0"})
        return jsonify({"message": "Welcome picture removed ✔", "file_id": ""})
    data = str(d.get("data") or "")
    if not data.startswith("data:image/") or "," not in data or len(data) > 9_000_000:
        return jsonify({"message": "Choose a picture (JPG / PNG, under about 6 MB).", "error": True}), 400
    try:
        fid = _tg_photo_file_id(data)
    except Exception as e:
        return jsonify({"message": "Upload to Telegram failed: %s" % e, "error": True}), 400
    db.set_ui_settings({"welcome_image": fid, "welcome_image_on": "1"})
    return jsonify({"message": "Picture uploaded & saved ✔", "file_id": fid})

@app.route('/api/alerts/save', methods=['POST'])
def api_alerts_save():
    if not admin_required(): return unauthorized()
    d = request.json or {}
    chat = str(d.get("chat") or "").strip()
    if chat and not re.fullmatch(r'-?\d{5,20}', chat):
        return jsonify({"message": "Chat ID must be a number (your Telegram ID).", "error": True}), 400
    db.set_ui_settings({"alert_on": "1" if d.get("on") else "0", "alert_chat": chat})
    return jsonify({"message": "Alert settings saved ✔"})

@app.route('/api/alerts/test', methods=['POST'])
def api_alerts_test():
    if not admin_required(): return unauthorized()
    try:
        ok = bool(send_test_alert())
    except Exception:
        ok = False
    if ok:
        return jsonify({"message": "Test alert sent ✔ Check your Telegram."})
    return jsonify({"message": "Could not send. Press /start on the bot with the alert account and check the Chat ID.", "error": True}), 400

@app.route('/api/lang/save', methods=['POST'])
def api_lang_save():
    if not admin_required(): return unauthorized()
    db.set_ui_settings({"lang_on": "1" if (request.json or {}).get("on") else "0"})
    return jsonify({"message": "Language button saved ✔"})


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
_BC_TIMES = {}  # broadcast id -> {"start": ts, "end": ts or None}
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
    _BC_TIMES[hist_id] = {"start": time.time(), "end": None}

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
        _BC_TIMES.setdefault(hist_id, {})["end"] = time.time()
    Thread(target=_run, daemon=True).start()
    return jsonify({"message": f"Broadcast started to {len(uids)} users!", "hist_id": hist_id, "total": len(uids)})

@app.route('/api/broadcast/history')
def api_bc_history():
    if not admin_required(): return unauthorized()
    rows = db.fetchall("SELECT * FROM broadcast_history ORDER BY id DESC LIMIT 15")
    names = {"image": "🖼️ Image", "video": "🎬 Video", "voice": "🎙️ Voice", "audio": "🎵 Audio"}
    def _dur(i):
        t = _BC_TIMES.get(i) or {}
        return round(t["end"] - t["start"], 1) if t.get("end") and t.get("start") else None
    return jsonify({"rows": [{
        "id": r["id"], "dur": _dur(r["id"]),
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

# ==========================================
# 🎬 LIVE WALLPAPER (background video + looping song)
# ==========================================
BG_LIMITS = {"video": 50 * 1024 * 1024, "audio": 20 * 1024 * 1024}
BG_CHUNK = 4 * 1024 * 1024

def _bg_ctx():
    """Everything the page needs to show the wallpaper (safe defaults if the database has nothing)."""
    out = {"enabled": True, "volume": 0.6, "dim": 0.45,
           "video": "", "video_name": "", "video_src": "",
           "audio": "", "audio_name": "", "audio_src": ""}
    try:
        cfg = db.get_ui_settings()
        out["enabled"] = str(cfg.get("enabled", "1")) == "1"
        out["volume"] = min(1.0, max(0.0, float(cfg.get("volume", 0.6))))
        out["dim"] = min(0.9, max(0.0, float(cfg.get("dim", 0.45))))
        for kind in ("video", "audio"):
            meta = db.media_meta(kind)
            url = (cfg.get(kind + "_url") or "").strip()
            if meta:
                out[kind] = f"/media/{kind}?v={meta['ver']}"
                out[kind + "_name"] = meta["filename"] or kind
                out[kind + "_src"] = "upload"
            elif re.match(r"^https?://\S+$", url):
                out[kind], out[kind + "_name"], out[kind + "_src"] = url, url, "url"
    except Exception:
        pass
    return out

@app.route('/media/<kind>')
def media_file(kind):
    """Serves the uploaded wallpaper files with Range support, so videos can start and seek quickly."""
    if kind not in BG_LIMITS:
        return "", 404
    meta = db.media_meta(kind)
    if not meta:
        return "", 404
    size = int(meta["size"])
    mime = meta["mime"] or "application/octet-stream"
    headers = {"Accept-Ranges": "bytes", "Cache-Control": "public, max-age=86400"}
    rng = (request.headers.get("Range") or "").strip()
    m = re.match(r"^bytes=(\d*)-(\d*)$", rng)
    if m and (m.group(1) or m.group(2)):
        if m.group(1):
            start = int(m.group(1))
            end = int(m.group(2)) if m.group(2) else size - 1
        else:
            start = max(0, size - int(m.group(2)))
            end = size - 1
        if start >= size:
            return Response(status=416, headers={"Content-Range": f"bytes */{size}"})
        end = min(end, size - 1, start + BG_CHUNK - 1)
        data = db.media_chunk(kind, start, end - start + 1)
        headers.update({"Content-Range": f"bytes {start}-{end}/{size}", "Content-Length": str(len(data))})
        return Response(data, status=206, mimetype=mime, headers=headers)

    def _stream():
        pos = 0
        while pos < size:
            part = db.media_chunk(kind, pos, min(BG_CHUNK, size - pos))
            if not part:
                break
            pos += len(part)
            yield part
    headers["Content-Length"] = str(size)
    return Response(_stream(), mimetype=mime, headers=headers)

@app.route('/api/bg/save', methods=['POST'])
def api_bg_save():
    if not admin_required(): return unauthorized()
    d = request.json or {}
    vals = {}
    for kind in ("video", "audio"):
        key = kind + "_url"
        if key in d:
            url = str(d[key] or "").strip()
            if url and not re.match(r"^https?://\S+$", url):
                return jsonify({"message": f"The {kind} link must start with http:// or https://"}), 400
            vals[key] = url
            if url:
                db.delete_media(kind)   # a link replaces an uploaded file
    try:
        if "volume" in d: vals["volume"] = min(1.0, max(0.0, float(d["volume"])))
        if "dim" in d: vals["dim"] = min(0.9, max(0.0, float(d["dim"])))
    except (TypeError, ValueError):
        return jsonify({"message": "Invalid volume or dimming value"}), 400
    if "enabled" in d: vals["enabled"] = "1" if d["enabled"] else "0"
    if vals:
        db.set_ui_settings(vals)
    return jsonify({"message": "Live wallpaper saved!", "bg": _bg_ctx()})

@app.route('/api/bg/upload', methods=['POST'])
def api_bg_upload():
    if not admin_required(): return unauthorized()
    kind = request.form.get("kind", "")
    f = request.files.get("file")
    if kind not in BG_LIMITS or not f or not f.filename:
        return jsonify({"message": "Choose a video or song file first."}), 400
    mime = (f.mimetype or "").lower()
    if not mime.startswith(kind + "/"):
        guessed = (mimetypes.guess_type(f.filename)[0] or "").lower()
        if guessed.startswith(kind + "/"):
            mime = guessed
        else:
            return jsonify({"message": f"That file is not a {kind} file."}), 400
    data = f.read()
    if len(data) > BG_LIMITS[kind]:
        return jsonify({"message": f"The {kind} is too large (max {BG_LIMITS[kind] // (1024 * 1024)} MB)."}), 413
    if not data:
        return jsonify({"message": "The file is empty."}), 400
    db.save_media(kind, os.path.basename(f.filename)[:120], mime, data)
    db.set_ui_settings({kind + "_url": ""})
    return jsonify({"message": f"{kind.title()} uploaded!", "bg": _bg_ctx()})

@app.route('/api/bg/clear', methods=['POST'])
def api_bg_clear():
    if not admin_required(): return unauthorized()
    kind = (request.json or {}).get("kind", "")
    if kind not in BG_LIMITS:
        return jsonify({"message": "Invalid type"}), 400
    db.delete_media(kind)
    db.set_ui_settings({kind + "_url": ""})
    return jsonify({"message": f"{kind.title()} removed.", "bg": _bg_ctx()})

# ==========================================
# 👤 MEMBER INSPECTOR HELPERS
# ==========================================
@app.route('/api/user/detail')
def api_user_detail():
    if not admin_required(): return unauthorized()
    try:
        uid = int(request.args.get("user_id", ""))
    except ValueError:
        return jsonify({"message": "Invalid user"}), 400
    rows = db.fetchall(
        "SELECT prod_name, plan, amount, timestamp, key_delivered FROM order_history "
        "WHERE user_id = %s ORDER BY id DESC LIMIT 5", (uid,))
    orders = [{
        "prod_name": r["prod_name"] or "", "plan": r["plan"] or "", "amount": float(r["amount"] or 0),
        "timestamp": r["timestamp"] or "", "pending": str(r["key_delivered"] or "").startswith("PENDING"),
    } for r in rows]
    return jsonify({"orders": orders})

@app.route('/api/user/message', methods=['POST'])
def api_user_message():
    """Send a message to a member from the bot (works for anyone who has started the bot)."""
    if not admin_required(): return unauthorized()
    d = request.json or {}
    try:
        uid = int(d["user_id"])
    except (KeyError, TypeError, ValueError):
        return jsonify({"message": "Invalid user", "error": True}), 400
    text = str(d.get("text") or "").strip()[:3500]
    if not text:
        return jsonify({"message": "Type a message first", "error": True}), 400
    try:
        tg_send(uid, "📩 <b>Message from Admin</b>\n\n" + _html.escape(text))
    except Exception:
        return jsonify({"message": "Could not deliver (the user may have blocked the bot).", "error": True})
    return jsonify({"message": "Message sent!"})


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














