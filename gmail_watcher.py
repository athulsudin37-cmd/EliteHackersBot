"""
gmail_watcher.py
Reads your bank's "money received" EMAIL alerts from Gmail (IMAP) and settles the matching
payment automatically. No phone app needed.

Render Environment Variables:
  GMAIL_USER          your Gmail address that receives the bank alerts
  GMAIL_APP_PASSWORD  16-character Google "App password" (NOT your normal Gmail password)
  BANK_EMAIL_FROM     part of the bank's sender address, e.g. hdfcbank  (comma separated for more)

Safety: anyone can e-mail your Gmail address, so an e-mail is only trusted when
  1) the sender address contains BANK_EMAIL_FROM, and
  2) Gmail itself verified the sender's DKIM signature for that same bank domain.
Fake or spoofed e-mails therefore can never credit a wallet.
"""

import os
import re
import time
import email
import imaplib
import hashlib
import logging
import html as htmllib
from datetime import datetime, timedelta, timezone
from email.header import decode_header, make_header
from email.utils import parseaddr, parsedate_to_datetime
from threading import Thread

from database import db
from fulfillment import process_bank_message, tg_call, ADMIN_ID

logger = logging.getLogger(__name__)

POLL_SECONDS = 15
MAX_EMAIL_AGE = timedelta(hours=3)   # never act on old e-mails
MAX_PER_POLL = 30                    # newest e-mails looked at per poll
BODY_CHARS = 800                     # the transaction is at the top; ignore long disclaimers


def _header_text(value):
    try:
        return str(make_header(decode_header(value or "")))
    except Exception:
        return value or ""


def _body_text(msg):
    plain, html_parts = [], []
    for part in msg.walk():
        ctype = part.get_content_type()
        if ctype not in ("text/plain", "text/html") or part.get_filename():
            continue
        payload = part.get_payload(decode=True)
        if payload is None:
            continue
        text = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
        (plain if ctype == "text/plain" else html_parts).append(text)

    if plain:
        text = " ".join(plain)
    else:
        text = " ".join(html_parts)
        text = re.sub(r"(?is)<(script|style).*?</\1>", " ", text)
        text = re.sub(r"<[^>]+>", " ", text)
        text = htmllib.unescape(text)
    return " ".join(text.split())


def sender_is_trusted(msg, allowed):
    """True only if the sender matches AND Gmail verified DKIM for that bank domain."""
    from_addr = parseaddr(msg.get("From", ""))[1].lower()
    if not from_addr or not any(a in from_addr for a in allowed):
        return False, f"sender {from_addr or '?'} is not in BANK_EMAIL_FROM"

    # The top-most Authentication-Results header is the one Gmail added itself.
    results = msg.get_all("Authentication-Results") or []
    if not results:
        return False, "no Authentication-Results header"
    header = " ".join(str(results[0]).split())
    if header.split(";")[0].strip().lower() != "mx.google.com":
        return False, "top Authentication-Results is not from Gmail"
    for clause in re.findall(r"dkim=pass[^;]*", header, re.I):
        if any(a in clause.lower() for a in allowed):
            return True, "ok"
    return False, "DKIM not verified for the bank domain"


def _mark_processed(key):
    """Remember an e-mail forever (survives restarts). False if it was already handled."""
    row = db.fetchone(
        "INSERT INTO processed_mails (message_key) VALUES (%s) ON CONFLICT DO NOTHING RETURNING message_key",
        (key,),
    )
    return row is not None


def _unmark(key):
    db.execute("DELETE FROM processed_mails WHERE message_key = %s", (key,))


def handle_email(raw, allowed):
    """Process one raw e-mail. Returns a short status string (useful for logs/tests)."""
    msg = email.message_from_bytes(raw)

    try:
        sent = parsedate_to_datetime(msg.get("Date", ""))
        if sent.tzinfo is None:
            sent = sent.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) - sent > MAX_EMAIL_AGE:
            return "too old"
    except Exception:
        return "bad date"

    ok, reason = sender_is_trusted(msg, allowed)
    if not ok:
        return "rejected: " + reason

    message_id = (msg.get("Message-ID") or "").strip() or hashlib.sha1(raw).hexdigest()
    key = hashlib.sha256(message_id.encode()).hexdigest()
    if not _mark_processed(key):
        return "already processed"

    text = _header_text(msg.get("Subject")) + " " + _body_text(msg)[:BODY_CHARS]
    try:
        settle = bool(db.get_payment_settings().get("auto_verify"))
        result = process_bank_message(text, settle=settle)
    except Exception:
        _unmark(key)   # try again on the next poll
        raise
    return "processed: " + result["status"]


def _poll_once(imap, allowed, last_uid):
    imap.select("INBOX")
    since = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%d-%b-%Y")
    typ, data = imap.uid("search", None, "SINCE", since)
    if typ != "OK" or not data or not data[0]:
        return last_uid

    uids = sorted(int(x) for x in data[0].split())
    for uid in [u for u in uids if u > last_uid][-MAX_PER_POLL:]:
        typ, parts = imap.uid("fetch", str(uid), "(BODY.PEEK[])")
        if typ != "OK" or not parts or not isinstance(parts[0], tuple):
            continue
        status = handle_email(parts[0][1], allowed)
        logger.info("Gmail alert uid=%s -> %s", uid, status)
    return max(uids[-1], last_uid)


def _close(imap):
    try:
        if imap is not None:
            imap.logout()
    except Exception:
        pass


def _run(user, password, allowed):
    db.execute(
        "CREATE TABLE IF NOT EXISTS processed_mails ("
        "message_key TEXT PRIMARY KEY, created_ts TIMESTAMPTZ NOT NULL DEFAULT now())"
    )
    imap, last_uid, alerted = None, 0, False
    logger.info("Gmail payment reader started for %s (trusted senders: %s)", user, ", ".join(allowed))
    while True:
        try:
            if imap is None:
                imap = imaplib.IMAP4_SSL("imap.gmail.com", 993, timeout=30)
                imap.login(user, password)
                alerted = False
                logger.info("Gmail login OK")
            last_uid = _poll_once(imap, allowed, last_uid)
            time.sleep(POLL_SECONDS)
        except imaplib.IMAP4.error as e:
            logger.error("Gmail login/IMAP error: %s", e)
            if not alerted:
                alerted = True
                tg_call("sendMessage", {
                    "chat_id": ADMIN_ID,
                    "text": "⚠️ Gmail payment reader could not log in. Check GMAIL_USER and GMAIL_APP_PASSWORD "
                            "in Render (it must be a Google App Password, 2-Step Verification must be on).",
                })
            _close(imap)
            imap = None
            time.sleep(120)
        except Exception:
            logger.exception("Gmail reader error")
            _close(imap)
            imap = None
            time.sleep(30)


def start():
    """Start the Gmail reader in the background if it is configured. Safe to call always."""
    user = os.getenv("GMAIL_USER", "").strip()
    password = "".join(os.getenv("GMAIL_APP_PASSWORD", "").split())
    allowed = [a.strip().lower() for a in os.getenv("BANK_EMAIL_FROM", "").split(",") if a.strip()]

    if not user or not password:
        logger.info("Gmail payment reader is off (GMAIL_USER / GMAIL_APP_PASSWORD not set).")
        return None
    if not allowed:
        logger.error("Gmail payment reader NOT started: set BANK_EMAIL_FROM (e.g. hdfcbank) so fake e-mails are ignored.")
        return None

    t = Thread(target=_run, args=(user, password, allowed), daemon=True)
    t.start()
    return t
