"""Cart logic. The agent never places orders; it only fills carts.

Mode (stored in the settings table, set from the Streamlit sidebar):
  multi  -> each item goes to whichever store is cheapest for it
  single -> every item goes to one chosen store
"""
import os
import re
import requests

from price_sources import STORE_LABELS, TOP_N, direct_link, get_offers_with_fallback, resolve_link, usable_url
from setup_db import get_conn

AMAZON_ADD_URL = "https://www.amazon.in/gp/aws/cart/add.html"
ASIN_RE = re.compile(r"/(?:dp|gp/product|gp/aw/d|d)/([A-Z0-9]{10})(?:[/?#]|$)", re.I)


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
    """The offers to show, following the dashboard toggle."""
    offers = sorted(data["offers"].values(), key=lambda o: o["price"])
    if get_setting("cart_mode") == "single":
        store = get_setting("single_store")
        return [o for o in offers if o["store"] == store]
    return offers[:TOP_N]


def _upsert_cart_item(product_id: int, store: str, title: str, price: float, url: str, qty: int) -> None:
    conn = get_conn()
    conn.execute(
        """
        INSERT INTO cart_items (product_id, store, title, price, url, qty)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(product_id) DO UPDATE SET
            store = excluded.store,
            title = excluded.title,
            price = excluded.price,
            url   = excluded.url,
            qty   = cart_items.qty + excluded.qty
        """,
        (product_id, store, title, price, url, max(1, qty)),
    )
    conn.commit()
    conn.close()


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
    _upsert_cart_item(data["product_id"], chosen["store"], chosen["title"], chosen["price"], url, qty)

    return {
        "ok": True, "mode": mode, "product": data["name"],
        "store": STORE_LABELS.get(chosen["store"], chosen["store"]),
        "price": chosen["price"], "qty": max(1, qty), "is_mock": data["is_mock"],
    }


def add_offer(product_id: int, store: str, qty: int = 1) -> bool:
    """Add one specific store's offer to the cart (used by the 'Add to cart' card button)."""
    conn = get_conn()
    o = conn.execute("SELECT * FROM offers WHERE product_id = ? AND store = ?", (product_id, store)).fetchone()
    conn.close()
    if not o:
        return False
    url = o["url"] if o["is_mock"] else direct_link(store, o["title"], o["url"])
    _upsert_cart_item(product_id, store, o["title"], o["price"], url, qty)
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


def _find_amazon_asin_url(title: str) -> str | None:
    """Fallback lookup using SerpApi Amazon engine to grab an exact /dp/<ASIN> link."""
    api_key = os.environ.get("SERPAPI_KEY")
    if not api_key or not title:
        return None
    try:
        resp = requests.get(
            "https://serpapi.com/search.json",
            params={"engine": "amazon", "k": " ".join(title.split()[:8]), "amazon_domain": "amazon.in", "api_key": api_key},
            timeout=20,
        )
        for item in resp.json().get("organic_results", []):
            asin = item.get("asin")
            if asin and len(asin) == 10:
                return f"https://www.amazon.in/dp/{asin}"
    except Exception:
        pass
    return None


def get_cart() -> dict:
    """Cart grouped by store, with totals and (where possible) a one-click link."""
    conn = get_conn()
    rows = conn.execute(
        "SELECT c.*, p.name FROM cart_items c JOIN products p ON p.id = c.product_id ORDER BY c.store"
    ).fetchall()

    stores: dict[str, dict] = {}
    for r in rows:
        url = usable_url(r["store"], r["title"], r["url"])
        if r["store"] == "amazon" and not ASIN_RE.search(url):
            resolved, _ = resolve_link("amazon", r["title"])
            if not (resolved and ASIN_RE.search(resolved)):
                resolved = _find_amazon_asin_url(r["title"])
            if resolved and ASIN_RE.search(resolved):
                url = resolved
                conn.execute("UPDATE cart_items SET url = ? WHERE product_id = ?", (url, r["product_id"]))
                conn.commit()

        grp = stores.setdefault(r["store"], {"label": STORE_LABELS.get(r["store"], r["store"]),
                                             "items": [], "total": 0.0})
        grp["items"].append({"name": r["name"], "title": r["title"], "price": r["price"],
                             "qty": r["qty"], "url": url})
        grp["total"] += r["price"] * r["qty"]

    conn.close()

    amazon = stores.get("amazon")
    if amazon:
        parts = ["AssociateTag=beautyshop-21"]
        count = 0
        for item in amazon["items"]:
            m = ASIN_RE.search(item["url"] or "")
            if m:
                count += 1
                parts.append(f"ASIN.{count}={m.group(1).upper()}&Quantity.{count}={item['qty']}")
        if count > 0:
            amazon["bulk_link"] = f"{AMAZON_ADD_URL}?{'&'.join(parts)}"

    return {"stores": stores, "grand_total": sum(g["total"] for g in stores.values())}