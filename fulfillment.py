"""
fulfillment.py
Money / key logic shared by the Telegram bot (main.py) and the web admin (web_admin.py):

  * wallet purchases (price is always calculated on the server, balance deducted atomically)
  * reading "money received" alerts (bank SMS / UPI app notifications) forwarded to the webhook
  * matching an alert to exactly one open payment and settling it (credit wallet / deliver key)
"""

import os
import re
import json
import hashlib
import logging
import urllib.request
from datetime import datetime

import pytz

from database import db

logger = logging.getLogger(__name__)

IST = pytz.timezone("Asia/Kolkata")
BOT_TOKEN = "".join(os.getenv("BOT_TOKEN", "").split())
ADMIN_ID = int(os.getenv("ADMIN_ID", "7616127905"))

RESELLER_FACTOR = 0.6
# A bank credit may settle a payment in any of these states (never one that is already verified/rejected).
OPEN_STATUSES = ["pending", "awaiting_approval", "expired", "cancelled"]


def now_ist():
    return datetime.now(IST).strftime("%d %b %Y, %I:%M %p (IST)")


def valid_url(url):
    return isinstance(url, str) and url.strip().lower().startswith(("http://", "https://", "tg://"))


# ----------------------------------------------------------------------
# Pricing & wallet purchase
# ----------------------------------------------------------------------
def unit_price(reg_price, is_reseller):
    return round(float(reg_price) * RESELLER_FACTOR, 2) if is_reseller else float(reg_price)


def fmt_price(value):
    value = float(value)
    return f"₹{value:,.0f}" if value.is_integer() else f"₹{value:,.2f}"


def _product(prod_key):
    row = db.get_product_by_key(prod_key)
    if not row:
        return None
    return {
        "name": row["name"],
        "category": row["category"],
        "prices": row.get("prices") or [],
        "icon": row.get("icon") or "⚡",
        "download_link": row.get("download_link"),
        "maintenance": row.get("maintenance", 0),
        "stock_out": row.get("stock_out", 0),
    }


def purchase_with_wallet(user_id, prod_key, plan_name, allow_pending=False, utr="WALLET_PAY"):
    """Charge the wallet and deliver a key. Money is only taken when the balance is enough.

    Returns a dict with a "status": ok | pending | insufficient | out_of_stock |
    no_product | no_plan | unavailable.
    """
    prod = _product(prod_key)
    if not prod:
        return {"status": "no_product"}
    if prod.get("maintenance") or prod.get("stock_out"):
        return {"status": "unavailable", "prod": prod}

    user = db.get_user(user_id)
    is_reseller = bool(user and user["account_type"] == "Reseller")
    reg_price = next((p for n, p in prod["prices"] if n == plan_name), None)
    if reg_price is None:
        return {"status": "no_plan", "prod": prod}

    price = unit_price(reg_price, is_reseller)
    new_balance = db.try_deduct(user_id, price)
    if new_balance is None:
        balance = float(user["wallet_balance"] or 0) if user else 0.0
        return {"status": "insufficient", "prod": prod, "price": price, "balance": balance}

    try:
        key = db.pop_auto_key(prod_key, plan_name)
        status = "ok"
        if not key:
            if not allow_pending:
                db.update_balance(user_id, price)  # nothing delivered -> give the money back
                return {"status": "out_of_stock", "prod": prod, "price": price}
            # Customer already paid by UPI: queue the order, admin dispatches it from the web panel.
            key, status = "PENDING", "pending"
        db.add_order(user_id, prod["name"], plan_name, key, price, utr, now_ist())
    except Exception:
        db.update_balance(user_id, price)
        raise

    return {"status": status, "prod": prod, "price": price, "key": key, "balance": new_balance}


# ----------------------------------------------------------------------
# Message texts (HTML) shared by the bot and the webhook
# ----------------------------------------------------------------------
def _esc(value):
    return (str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def build_key_card(prod_name, plan_name, key, balance):
    return (
        f"✅ <b>Payment verified — here's your key!</b>\n"
        f"⏩ ~~~~~~~~~~~~~~~~~~~~~~~~~~\n\n"
        f"🛒 <b>{_esc(prod_name)} — {_esc(plan_name.replace('_', ' '))}</b>\n\n"
        f"🗝️ Your Key:\n"
        f"<code>{_esc(key)}</code>\n\n"
        f"💰 Remaining balance: <b>₹{balance:,.2f}</b>"
    )


def build_pending_notice(prod_name, plan_name):
    return (
        "✅ <b>Payment Verified &amp; Received!</b>\n\n"
        f"⚠️ NOTICE: Auto-stock for <b>{_esc(prod_name)}</b> ({_esc(plan_name.replace('_', ' '))}) is currently restocking!\n\n"
        "🛡️ 100% SECURE: Admin is preparing your fresh key right now. It will be delivered directly to this chat shortly!"
    )


# ----------------------------------------------------------------------
# Telegram (plain HTTP so the web admin thread can use it too)
# ----------------------------------------------------------------------
def tg_call(method, payload):
    if not BOT_TOKEN:
        return None
    try:
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{BOT_TOKEN}/{method}",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=8) as resp:
            return json.loads(resp.read().decode())
    except Exception:
        logger.exception("Telegram API call failed: %s", method)
        return None


# ----------------------------------------------------------------------
# Reading bank alerts
# ----------------------------------------------------------------------
_CREDIT_RE = re.compile(r"\b(?:credited|received|deposited|paid you|sent you|credit alert)\b|\bcr\b", re.I)
_DEBIT_RE = re.compile(r"\b(?:debited|withdrawn|sent to|paid to)\b|\bdebit\b(?!\s*card)", re.I)
_BALANCE_RE = re.compile(
    r"\b(?:avl\.?\s*)?(?:clear\s*)?bal(?:ance)?\b[^0-9₹]{0,12}(?:rs\.?|inr|₹)?\s*[0-9][0-9,]*(?:\.[0-9]{1,2})?",
    re.I,
)
_AMOUNT_RE = re.compile(r"(?:rs\.?|inr|₹)\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)", re.I)
_UTR_RES = [
    re.compile(r"(?:utr|rrn|ref(?:erence)?(?:\s*(?:no|number|id))?\.?)[\s:#.\-]*([0-9]{9,18})", re.I),
    re.compile(r"upi/(?:cr|credit)/([0-9]{9,18})", re.I),
    re.compile(r"\b([0-9]{12})\b"),
]


def parse_sms(text):
    """Return {"is_credit": bool, "amount": float | None, "utr": str | None} for an alert text."""
    text = (text or "").strip()
    result = {"is_credit": False, "amount": None, "utr": None}
    if not text:
        return result

    result["is_credit"] = bool(_CREDIT_RE.search(text)) and not _DEBIT_RE.search(text)

    without_balance = _BALANCE_RE.sub(" ", text)
    m = _AMOUNT_RE.search(without_balance)
    if m:
        try:
            result["amount"] = round(float(m.group(1).replace(",", "")), 2)
        except ValueError:
            pass

    for rx in _UTR_RES:
        m = rx.search(text)
        if m:
            result["utr"] = m.group(1)
            break
    return result


def _fingerprint(text):
    bucket = int(datetime.now().timestamp() // 3600)
    return hashlib.sha256((text or "").strip().encode()).hexdigest() + f":{bucket}"


# ----------------------------------------------------------------------
# Settling a payment
# ----------------------------------------------------------------------
def settle_payment(order_id, from_statuses, utr, via="auto"):
    """Atomically mark a payment verified, credit the wallet and (for a product purchase) deliver the key.

    Returns None when the payment was not in an allowed state (already settled), so it can never be
    credited twice, otherwise a dict describing what happened.
    """
    row = db.claim_payment(order_id, from_statuses, "verified", utr, now_ist())
    if not row:
        return None

    user_id = row["user_id"]
    credit = float(row.get("pay_amount") or row["amount"])
    db.update_balance(user_id, credit)

    outcome = {"row": row, "amount": credit, "via": via, "purchase": None}
    if row.get("is_deficit") and row.get("prod_key") and row.get("plan"):
        try:
            outcome["purchase"] = purchase_with_wallet(
                user_id, row["prod_key"], row["plan"], allow_pending=True, utr="UPI_DEFICIT"
            )
        except Exception:
            logger.exception("Purchase after verified payment failed (wallet stays credited)")
    return outcome


def notify_settlement(outcome):
    """Tell the customer (and the admin) about an automatically settled payment."""
    row = outcome["row"]
    user_id = row["user_id"]
    amount = outcome["amount"]
    purchase = outcome.get("purchase")
    admin_note = ""

    if row.get("chat_id") and row.get("qr_msg_id"):
        tg_call("deleteMessage", {"chat_id": row["chat_id"], "message_id": row["qr_msg_id"]})

    buttons = [[{"text": "➡️ Back to Menu", "callback_data": "main_menu"}]]
    if purchase and purchase["status"] == "ok":
        text = build_key_card(purchase["prod"]["name"], row["plan"], purchase["key"], purchase["balance"])
        if valid_url(purchase["prod"].get("download_link")):
            buttons.insert(0, [{"text": "📥 Update File ↗️", "url": purchase["prod"]["download_link"]}])
    elif purchase and purchase["status"] == "pending":
        text = build_pending_notice(purchase["prod"]["name"], row["plan"])
        admin_note = "\n⚠️ Out of stock: dispatch the key from <b>Pending Keys Queue</b> in the admin panel."
    else:
        text = f"✅ <b>Payment Verified Successfully!</b>\n₹{amount:,.2f} has been added to your wallet."
        if row.get("is_deficit"):
            text += "\n\nYour balance is ready — open the shop to complete your purchase."

    tg_call("sendMessage", {
        "chat_id": user_id, "text": text, "parse_mode": "HTML",
        "reply_markup": {"inline_keyboard": buttons},
    })
    tg_call("sendMessage", {
        "chat_id": ADMIN_ID, "parse_mode": "HTML",
        "text": (
            f"✅ <b>Payment auto-verified</b>\n"
            f"👤 <code>{user_id}</code> · ₹{amount:,.2f}\n"
            f"🆔 <code>{row['order_id']}</code>{admin_note}"
        ),
    })


def process_bank_message(text, settle=True):
    """Handle one forwarded alert. Returns {"status", "order_id", "parsed"}.

    status: matched | unmatched | duplicate | ignored | logged
    """
    parsed = parse_sms(text)
    out = {"status": "ignored", "order_id": None, "parsed": parsed}

    if not parsed["is_credit"] or parsed["amount"] is None:
        db.record_bank_credit(_fingerprint(text), parsed["amount"], parsed["utr"], text, "ignored")
        return out

    fingerprint = ("utr:" + parsed["utr"]) if parsed["utr"] else _fingerprint(text)
    credit_id = db.record_bank_credit(
        fingerprint, parsed["amount"], parsed["utr"], text, "new" if settle else "logged"
    )
    if credit_id is None:
        out["status"] = "duplicate"
        return out
    if not settle:
        out["status"] = "logged"
        return out

    pay = db.find_open_payment_by_amount(parsed["amount"])
    outcome = None
    if pay:
        outcome = settle_payment(pay["order_id"], OPEN_STATUSES, parsed["utr"] or "AUTO", via="auto")

    if not outcome:
        db.set_bank_credit_status(credit_id, "unmatched")
        out["status"] = "unmatched"
        tg_call("sendMessage", {
            "chat_id": ADMIN_ID, "parse_mode": "HTML",
            "text": (
                f"⚠️ <b>Money received but no matching order</b>\n"
                f"💰 ₹{parsed['amount']:,.2f}" + (f" · UTR <code>{parsed['utr']}</code>" if parsed["utr"] else "") +
                "\nThe customer may have changed the amount or paid after 60 minutes. Check the admin panel → UPI Payment Setup."
            ),
        })
        return out

    db.set_bank_credit_status(credit_id, "matched", pay["order_id"])
    out["status"] = "matched"
    out["order_id"] = pay["order_id"]
    notify_settlement(outcome)
    return out
