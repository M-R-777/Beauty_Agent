"""Cart logic. The agent never places orders; it only fills carts.

Mode (stored in the settings table, set from the Streamlit sidebar):
  multi  -> each item goes to whichever store is cheapest for it
  single -> every item goes to one chosen store
"""
import re

from price_sources import STORE_LABELS, TOP_N, direct_link, get_offers_with_fallback, usable_url
from setup_db import get_conn

AMAZON_ADD_URL = "https://www.amazon.in/gp/aws/cart/add.html"
ASIN_RE = re.compile(r"/(?:dp|gp/product)/([A-Z0-9]{10})")


def get_setting(key: str) -> str:
    conn = get_conn()
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    conn.close()
    return row["value"]


def set_setting(key: str, value: str) -> None:
    conn = get_conn()
    conn.execute("INSERT OR REPLACE INTO settings VALUES (?, ?)", (key, value))
    conn.commit()
    conn.close()


def visible_offers(data: dict) -> list[dict]:
    """The offers to show, following the dashboard toggle.

    Cheapest-across-apps: the TOP_N cheapest stores. One-app-only: just that app (or nothing).
    """
    offers = sorted(data["offers"].values(), key=lambda o: o["price"])
    if get_setting("cart_mode") == "single":
        store = get_setting("single_store")
        return [o for o in offers if o["store"] == store]
    return offers[:TOP_N]


def add_item(query: str, qty: int = 1) -> dict:
    data = get_offers_with_fallback(query)
    offers = data["offers"]
    if data.get("error"):
        return {"ok": False, "error": f"Couldn't fetch prices right now: {data['error']}"}
    if not offers:
        return {"ok": False, "error": f"No prices found for '{query}'."}

    mode = get_setting("cart_mode")
    if mode == "single":
        store = get_setting("single_store")
        if store not in offers:
            return {
                "ok": False,
                "error": f"'{query}' was not found on {STORE_LABELS.get(store, store)}. "
                         f"Available on: {', '.join(STORE_LABELS.get(s, s) for s in offers)}.",
            }
        chosen = offers[store]
    else:
        chosen = min(offers.values(), key=lambda o: o["price"])

    url = chosen["url"] if data["is_mock"] else direct_link(chosen["store"], chosen["title"], chosen["url"])

    conn = get_conn()
    conn.execute(
        "INSERT OR REPLACE INTO cart_items (product_id, store, title, price, url, qty) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (data["product_id"], chosen["store"], chosen["title"], chosen["price"], url, max(1, qty)),
    )
    conn.commit()
    conn.close()
    return {
        "ok": True, "mode": mode, "product": data["name"],
        "store": STORE_LABELS.get(chosen["store"], chosen["store"]),
        "price": chosen["price"], "qty": max(1, qty), "is_mock": data["is_mock"],
    }


def add_offer(product_id: int, store: str, qty: int = 1) -> bool:
    """Add one specific store's offer to the cart (used by the 'Add to cart' card button)."""
    conn = get_conn()
    o = conn.execute("SELECT * FROM offers WHERE product_id = ? AND store = ?", (product_id, store)).fetchone()
    if not o:
        conn.close()
        return False
    conn.execute(
        "INSERT OR REPLACE INTO cart_items (product_id, store, title, price, url, qty) VALUES (?, ?, ?, ?, ?, ?)",
        (product_id, store, o["title"], o["price"], o["url"], max(1, qty)),
    )
    conn.commit()
    conn.close()
    return True


def remove_item(query: str) -> bool:
    name = re.sub(r"\s+", " ", query.strip().lower())
    conn = get_conn()
    cur = conn.execute(
        "DELETE FROM cart_items WHERE product_id IN (SELECT id FROM products WHERE name LIKE ?)",
        (f"%{name}%",),
    )
    conn.commit()
    conn.close()
    return cur.rowcount > 0


def clear_cart() -> None:
    conn = get_conn()
    conn.execute("DELETE FROM cart_items")
    conn.commit()
    conn.close()


def get_cart() -> dict:
    """Cart grouped by store, with totals and (where possible) a one-click link."""
    conn = get_conn()
    rows = conn.execute(
        "SELECT c.*, p.name FROM cart_items c JOIN products p ON p.id = c.product_id ORDER BY c.store"
    ).fetchall()
    conn.close()

    stores: dict[str, dict] = {}
    for r in rows:
        grp = stores.setdefault(r["store"], {"label": STORE_LABELS.get(r["store"], r["store"]),
                                             "items": [], "total": 0.0})
        grp["items"].append({"name": r["name"], "title": r["title"], "price": r["price"],
                             "qty": r["qty"], "url": usable_url(r["store"], r["title"], r["url"])})
        grp["total"] += r["price"] * r["qty"]

    # Amazon supports a real add-to-cart link, but only if we have every ASIN
    amazon = stores.get("amazon")
    if amazon:
        parts = []
        for i, item in enumerate(amazon["items"], start=1):
            m = ASIN_RE.search(item["url"] or "")
            if not m:
                parts = []
                break
            parts.append(f"ASIN.{i}={m.group(1)}&Quantity.{i}={item['qty']}")
        if parts:
            amazon["bulk_link"] = f"{AMAZON_ADD_URL}?{'&'.join(parts)}"

    return {"stores": stores, "grand_total": sum(g["total"] for g in stores.values())}