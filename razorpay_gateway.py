"""
razorpay_gateway.py
Real payment gateway (Razorpay). Fully automatic: the customer pays on Razorpay's page with any
UPI app / card / net banking, the bot sees the payment by itself and delivers the key.

All settings (Key ID, Key Secret, Webhook Secret, on/off) are saved from the admin panel
(UPI Payment Setup tab). No redeploy is needed to change them.

How a payment is confirmed (two independent ways, both safe to run together):
  1. Polling: every few seconds the bot asks Razorpay "is this payment link paid?"
     (works with only Key ID + Key Secret, nothing else to configure).
  2. Webhook (optional, instant): Razorpay calls /api/razorpay/webhook, verified by signature.
A payment can only ever be settled once (atomic status change in the database).
"""

import json
import time
import hmac
import base64
import hashlib
import logging
import urllib.error
import urllib.request
from threading import Thread

from database import db
from fulfillment import settle_payment, notify_settlement, now_ist

logger = logging.getLogger(__name__)

API_BASE = "https://api.razorpay.com/v1"
LINK_VALID_MINUTES = 20        # Razorpay needs at least 15
EXPIRE_PENDING_AFTER_MIN = 25  # our own record is closed a little later than the link
POLL_SECONDS = 5
SETTLE_FROM = ["pending", "expired", "cancelled"]


class RazorpayError(Exception):
    pass


# ----------------------------------------------------------------------
# Settings
# ----------------------------------------------------------------------
def get_config():
    try:
        row = db.get_razorpay_config()
    except Exception:
        logger.exception("Could not load Razorpay settings")
        row = {}
    key_id = (row.get("rzp_key_id") or "").strip()
    secret = (row.get("rzp_key_secret") or "").strip()
    selected = (row.get("active_gateway") or "") == "razorpay"
    return {
        "selected": selected,
        "active": bool(selected and key_id and secret),
        "key_id": key_id,
        "key_secret": secret,
        "webhook_secret": (row.get("rzp_webhook_secret") or "").strip(),
        "mode": "test" if key_id.startswith("rzp_test_") else ("live" if key_id.startswith("rzp_live_") else ""),
    }


# ----------------------------------------------------------------------
# Razorpay API
# ----------------------------------------------------------------------
def _request(method, path, payload=None, cfg=None, timeout=15):
    cfg = cfg or get_config()
    if not cfg.get("key_id") or not cfg.get("key_secret"):
        raise RazorpayError("Razorpay Key ID / Key Secret are not saved yet.")
    token = base64.b64encode(f"{cfg['key_id']}:{cfg['key_secret']}".encode()).decode()
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        API_BASE + path,
        data=data,
        method=method,
        headers={"Authorization": "Basic " + token, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read().decode())
        except Exception:
            body = {}
        description = (body.get("error") or {}).get("description") or f"HTTP {e.code}"
        raise RazorpayError(description) from None
    except Exception as e:
        raise RazorpayError(f"Could not reach Razorpay: {e}") from None


def create_payment_link(order_id, amount, description, cfg=None):
    """Create a one-time payment link for exactly `amount` rupees. Returns {"id", "url"}."""
    paise = int(round(float(amount) * 100))
    if paise < 100:
        raise RazorpayError("Minimum payment is ₹1.")
    body = {
        "amount": paise,
        "currency": "INR",
        "accept_partial": False,
        "reference_id": order_id[:40],
        "description": (description or "Wallet top-up")[:200],
        "reminder_enable": False,
        "notify": {"sms": False, "email": False},
        "notes": {"order_id": order_id},
        "expire_by": int(time.time()) + LINK_VALID_MINUTES * 60,
    }
    link = _request("POST", "/payment_links", body, cfg)
    if not link.get("id") or not link.get("short_url"):
        raise RazorpayError("Razorpay did not return a payment link.")
    return {"id": link["id"], "url": link["short_url"]}


def fetch_payment_link(link_id, cfg=None):
    return _request("GET", f"/payment_links/{link_id}", None, cfg)


def cancel_payment_link(link_id, cfg=None):
    return _request("POST", f"/payment_links/{link_id}/cancel", {}, cfg)


def test_connection(cfg=None):
    """Check the saved keys. Returns a short human readable message or raises RazorpayError."""
    cfg = cfg or get_config()
    _request("GET", "/payment_links?count=1", None, cfg)
    mode = {"test": "TEST mode (no real money)", "live": "LIVE mode (real money)"}.get(cfg.get("mode"), "")
    return "Connected to Razorpay" + (f" — {mode}" if mode else "")


# ----------------------------------------------------------------------
# Settling
# ----------------------------------------------------------------------
def _first_payment_id(link):
    for p in link.get("payments") or []:
        if isinstance(p, dict) and (p.get("payment_id") or p.get("id")):
            return p.get("payment_id") or p.get("id")
    return None


def settle_from_link(link, payment_id=None):
    """Settle our payment record from a Razorpay payment-link object.

    Returns "settled" (credited now), "already" (was settled before) or "ignored".
    The amount paid must cover the amount we asked for, and the link must be the one we
    created for that order, so a forged or mismatching message can never credit anything.
    """
    if not isinstance(link, dict) or link.get("status") != "paid":
        return "ignored"
    order_id = link.get("reference_id") or (link.get("notes") or {}).get("order_id")
    pay = db.get_payment(order_id) if order_id else None
    if not pay or pay.get("gateway") != "razorpay" or pay.get("gateway_ref") != link.get("id"):
        return "ignored"

    expected = int(round(float(pay.get("pay_amount") or pay["amount"]) * 100))
    if int(link.get("amount_paid") or 0) < expected:
        logger.warning("Razorpay link %s paid less than expected", link.get("id"))
        return "ignored"

    utr = payment_id or _first_payment_id(link) or link.get("id")
    outcome = settle_payment(order_id, SETTLE_FROM, utr, via="razorpay")
    if not outcome:
        return "already"
    notify_settlement(outcome)
    return "settled"


def cancel_payment(order_id):
    """User pressed Cancel. Never cancels a link that was already paid (credits it instead).

    Returns "settled" | "cancelled" | "error" | "none". On "error" the payment stays pending
    (and keeps being checked), so money can never get lost.
    """
    pay = db.get_payment(order_id)
    if not pay or pay.get("gateway") != "razorpay" or not pay.get("gateway_ref"):
        return "none"
    try:
        cfg = get_config()
        link = fetch_payment_link(pay["gateway_ref"], cfg)
        if link.get("status") == "paid":
            return "settled" if settle_from_link(link) in ("settled", "already") else "error"
        if link.get("status") not in ("cancelled", "expired"):
            cancel_payment_link(pay["gateway_ref"], cfg)
    except RazorpayError as e:
        logger.warning("Could not cancel Razorpay link for %s: %s", order_id, e)
        return "error"
    db.claim_payment(order_id, "pending", "cancelled", None, now_ist())
    return "cancelled"


# ----------------------------------------------------------------------
# Webhook (optional, instant)
# ----------------------------------------------------------------------
def verify_webhook(raw_body, signature, secret):
    if not secret or not signature:
        return False
    expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature.strip())


def handle_webhook(data):
    if (data or {}).get("event") != "payment_link.paid":
        return "ignored"
    payload = data.get("payload") or {}
    link = (payload.get("payment_link") or {}).get("entity") or {}
    payment_id = ((payload.get("payment") or {}).get("entity") or {}).get("id")
    return settle_from_link(link, payment_id)


# ----------------------------------------------------------------------
# Background poller
# ----------------------------------------------------------------------
def poll_once():
    cfg = get_config()
    if not cfg["key_id"] or not cfg["key_secret"]:
        return 0
    settled = 0
    for pay in db.open_gateway_payments("razorpay"):
        try:
            link = fetch_payment_link(pay["gateway_ref"], cfg)
            if settle_from_link(link) == "settled":
                settled += 1
        except RazorpayError as e:
            logger.warning("Razorpay check failed for %s: %s", pay["order_id"], e)
    db.expire_old_gateway_payments("razorpay", EXPIRE_PENDING_AFTER_MIN)
    return settled


def _loop():
    logger.info("Razorpay payment checker started")
    while True:
        try:
            poll_once()
        except Exception:
            logger.exception("Razorpay poll failed")
        time.sleep(POLL_SECONDS)


def start_poller():
    t = Thread(target=_loop, daemon=True)
    t.start()
    return t
