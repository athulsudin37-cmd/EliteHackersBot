"""Client for the PanelStore reseller REST API (the *other* shop we buy keys from).

Configuration (Render -> Environment):
    SUPPLIER_API_KEY   your private key (usr_...)      REQUIRED
    SUPPLIER_API_URL   optional, default below
"""
import os
import logging
import requests

logger = logging.getLogger(__name__)

DEFAULT_URL = "https://panelstore.shop/api/v1/index.php"


class SupplierError(Exception):
    """The supplier refused the call or could not be reached. Nothing was bought."""


class SupplierUncertain(SupplierError):
    """The call may have reached the supplier (timeout / broken answer): an order MAY have been charged."""


def get_config():
    """(url, key). The admin panel (Supplier API tab) wins; Render variables are the fallback."""
    url, key = "", ""
    try:
        from database import db
        row = db.get_supplier_config() or {}
        url, key = (row.get("supplier_url") or "").strip(), "".join((row.get("supplier_key") or "").split())
    except Exception:
        pass
    url = url or os.getenv("SUPPLIER_API_URL", "").strip() or DEFAULT_URL
    key = key or "".join(os.getenv("SUPPLIER_API_KEY", "").split())
    return url, key


def _key():
    return get_config()[1]


def configured():
    return bool(_key())


def _post(payload, timeout=15):
    url, key = get_config()
    if not key:
        raise SupplierError("Supplier API key is not set (Admin panel -> Supplier API)")
    body = {"key": key, **payload}
    try:
        resp = requests.post(url, json=body, timeout=timeout)
    except requests.exceptions.ConnectTimeout as e:      # never reached the server -> safe
        raise SupplierError(f"Could not connect: {e}")
    except requests.exceptions.RequestException as e:    # may have reached it
        raise SupplierUncertain(f"Connection problem: {e}")
    try:
        data = resp.json()
    except ValueError:
        raise SupplierUncertain(f"Invalid answer from supplier (HTTP {resp.status_code})")
    if not isinstance(data, dict):
        data = {"_list": data}
    return data


def _ok(data):
    return str(data.get("status", "")).lower() == "success"


def get_balance():
    """-> (balance: float, currency: str)"""
    data = _post({"action": "balance"})
    if not _ok(data):
        raise SupplierError(str(data.get("message") or data))
    try:
        return float(data.get("balance")), str(data.get("currency") or "INR")
    except (TypeError, ValueError):
        raise SupplierError(f"Unexpected balance answer: {data}")


def _first(d, names, default=""):
    for n in names:
        if n in d and d[n] not in (None, ""):
            return d[n]
    return default


def get_services():
    """-> list of {"pid", "name", "price", "stock", "duration", "raw"} (field names are matched loosely)."""
    data = _post({"action": "services"})
    if not _ok(data) and "_list" not in data:
        raise SupplierError(str(data.get("message") or data))
    items = data.get("_list")
    if items is None:
        for k in ("services", "products", "data", "catalog", "items"):
            if isinstance(data.get(k), list):
                items = data[k]
                break
    if items is None:
        raise SupplierError(f"Unexpected services answer: {str(data)[:300]}")
    out = []
    for it in items:
        if not isinstance(it, dict):
            continue
        out.append({
            "pid": _first(it, ("pid", "id", "product_id")),
            "name": " — ".join(x for x in (str(_first(it, ("panel_name",))), str(_first(it, ("name", "title", "product_name")))) if x),
            "device": _first(it, ("device_type",)),
            "category": _first(it, ("category",)),
            "status": _first(it, ("status",)),
            "price": _first(it, ("price", "wholesale_rate", "wholesale_price", "rate")),
            "stock": _first(it, ("stock", "stock_count", "available", "live_stock")),
            "duration": _first(it, ("duration", "plan")),
            "raw": it,
        })
    return out


def order(pid, quantity=1, note=""):
    """Buy `quantity` key(s). Returns {"license_key", "order_code", "remaining_balance"}.
    Raises SupplierError (nothing charged) or SupplierUncertain (maybe charged). Never retries."""
    data = _post({"action": "order", "pid": int(pid), "quantity": int(quantity), "custom_notes": note or ""})
    if not _ok(data):
        raise SupplierError(str(data.get("message") or "Order rejected by supplier"))
    key = data.get("license_key")
    if isinstance(key, (list, tuple)):
        key = "\n".join(str(k) for k in key)
    key = str(key or "").strip()
    if not key:
        raise SupplierUncertain(f"Supplier said success but sent no key: {str(data)[:300]}")
    try:
        remaining = float(data.get("remaining_balance"))
    except (TypeError, ValueError):
        remaining = None
    return {"license_key": key, "order_code": str(data.get("order_code") or ""), "remaining_balance": remaining}
