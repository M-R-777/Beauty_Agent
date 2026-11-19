"""Shopping cart database logic and Amazon one-click bulk cart link builder.

Store selection rules for add_item():
  1. If the user names a specific store (e.g., 'add to Nykaa', 'add to Tira', 'add to Amazon'),
     use that store.
  2. If sidebar is in 'One app only' mode and no store was explicitly named, use that selected app.
  3. Otherwise, default to Amazon (since almost everything is available there), falling back to the
     cheapest trusted store only if Amazon does not carry that item.
"""
import os
import re
import requests

from price_sources import (
    STORE_LABELS, TOP_N, direct_link, get_offers_with_fallback, normalize_store, resolve_link, usable_url,
)
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


def visible_offers(data: dict, requested_store: str = "") -> list[dict]:
    """Return top 3 cheapest trusted offers, or the single requested/selected store."""
    offers = sorted(data["offers"].values(), key=lambda o: o["price"])
    target = normalize_store(requested_store, data["name"]) if requested_store else None
    if target:
        return [o for o in offers if o["store"] == target]
    if get_setting("cart_mode") == "single":
        store = get_setting("single_store")
        return [o for o in offers if o["store"] == store]
    return offers[:TOP_N]


def _upsert_cart_item(product_id: int, store: str, title: str, price: float, url: str, qty: int, shade: str = "", size: str = "") -> None:
    conn = get_conn()
    conn.execute(
        """
        INSERT INTO cart_items (product_id, store, shade, size, title, price, url, qty)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(product_id, store, shade, size) DO UPDATE SET
            title = excluded.title,
            price = excluded.price,
            url   = excluded.url,
            qty   = cart_items.qty + excluded.qty
        """,
        (product_id, store, shade.strip(), size.strip(), title, price, url, max(1, qty)),
    )
    conn.commit()
    conn.close()


def add_item(query: str, qty: int = 1, store: str = "") -> dict:
    """Add a product to the requested store's cart (defaults to Amazon if unspecified)."""
    mode = get_setting("cart_mode")
    explicit_store = normalize_store(store, query) if store else None
    preferred_store = explicit_store or (get_setting("single_store") if mode == "single" else "amazon")

    data = get_offers_with_fallback(query, target_store=preferred_store)
    offers = data["offers"]
    if data.get("error"):
        return {"ok": False, "error": f"Couldn't fetch prices right now: {data['error']}"}
    if not offers:
        return {"ok": False, "error": f"No trusted store prices found for '{query}'."}

    if explicit_store or mode == "single":
        if preferred_store not in offers:
            avail = ", ".join(STORE_LABELS.get(s, s) for s in offers)
            return {
                "ok": False,
                "error": f"'{query}' was not found on {STORE_LABELS.get(preferred_store, preferred_store)}. "
                         f"Available on: {avail}.",
            }
        chosen = offers[preferred_store]
    else:
        # Default to Amazon unless unavailable, in which case fall back to cheapest trusted store
        chosen = offers["amazon"] if "amazon" in offers else min(offers.values(), key=lambda o: o["price"])

    url = chosen["url"] if data["is_mock"] else direct_link(chosen["store"], chosen["title"], chosen["url"])
    _upsert_cart_item(data["product_id"], chosen["store"], chosen["title"], chosen["price"], url, qty)
    return {
        "ok": True,
        "mode": mode,
        "product": data["name"],
        "store": STORE_LABELS.get(chosen["store"], chosen["store"]),
        "price": chosen["price"],
        "qty": max(1, qty),
        "is_mock": data["is_mock"],
    }


def add_offer(product_id: int, store: str, qty: int = 1, shade: str = "", size: str = "") -> bool:
    conn = get_conn()
    o = conn.execute("SELECT * FROM offers WHERE product_id = ? AND store = ?", (product_id, store)).fetchone()
    conn.close()
    if not o:
        return False
    url = o["url"] if o["is_mock"] else direct_link(store, o["title"], o["url"])
    
    # Append the variant details to the title for display
    final_title = o["title"]
    variant_str = " / ".join(filter(None, [size.strip(), shade.strip()]))
    if variant_str:
        final_title += f" ({variant_str})"

    _upsert_cart_item(product_id, store, final_title, o["price"], url, qty, shade, size)
    return True


def remove_item(query: str, store: str = "", shade: str = "", size: str = "") -> bool:
    name = re.sub(r"\s+", " ", query.strip().lower())
    conn = get_conn()
    
    if store:
        cur = conn.execute(
            "DELETE FROM cart_items WHERE store = ? AND shade = ? AND size = ? AND product_id IN (SELECT id FROM products WHERE name LIKE ?)",
            (store, shade.strip(), size.strip(), f"%{name}%"),
        )
    else:
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
    api_key = os.environ.get("SERPAPI_KEY")
    if not api_key or not title:
        return None
        
    # Extract important keywords to verify we got the right product
    stop_words = {"the", "a", "an", "for", "and", "with", "from", "of", "in", "by", "buy", "online", "india"}
    keywords = [w for w in re.findall(r"[a-z0-9]+", title.lower()) if w not in stop_words and len(w) > 2]
    if not keywords:
        return None

    try:
        resp = requests.get(
            "https://serpapi.com/search.json",
            params={"engine": "amazon", "k": " ".join(title.split()[:8]), "amazon_domain": "amazon.in", "api_key": api_key},
            timeout=20,
        )
        for item in resp.json().get("organic_results", []):
            asin = item.get("asin")
            amazon_title = item.get("title", "").lower()
            
            # Verify the result actually contains a significant portion of our search keywords
            if asin and len(asin) == 10 and amazon_title:
                match_count = sum(1 for kw in keywords if kw in amazon_title)
                # Require at least 50% of the keywords to match to accept the ASIN
                if match_count / len(keywords) >= 0.5: 
                    return f"https://www.amazon.in/dp/{asin}"
    except Exception:
        pass
    return None


def get_cart() -> dict:
    """Return cart grouped by store, totals, and Amazon's 1-click bulk add link."""
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
                # Update ONLY this specific variant (size/shade) so we don't override the whole cart
                conn.execute(
                    "UPDATE cart_items SET url = ? WHERE product_id = ? AND store = ? AND shade = ? AND size = ?", 
                    (url, r["product_id"], r["store"], r["shade"] or "", r["size"] or "")
                )
                conn.commit()

        grp = stores.setdefault(r["store"], {"label": STORE_LABELS.get(r["store"], r["store"]), "items": [], "total": 0.0})
        grp["items"].append({"name": r["name"], "title": r["title"], "price": r["price"], "qty": r["qty"], "url": url, "shade": r["shade"], "size": r["size"]})
        grp["total"] += r["price"] * r["qty"]

    conn.close()

    amazon = stores.get("amazon")
    if amazon:
        parts = ["AssociateTag=skipia-21"]
        count = 0
        for item in amazon["items"]:
            m = ASIN_RE.search(item["url"] or "")
            if m:
                count += 1
                parts.append(f"ASIN.{count}={m.group(1).upper()}&Quantity.{count}={item['qty']}")
        if count > 0:
            amazon["bulk_link"] = f"{AMAZON_ADD_URL}?{'&'.join(parts)}"
        else:
            amazon["bulk_link"] = None  # Explicitly set to None so the UI knows to show the caption"

    return {"stores": stores, "grand_total": sum(g["total"] for g in stores.values())}