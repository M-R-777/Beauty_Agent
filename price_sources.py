"""Fast, Google-accurate live price fetcher across Nykaa, Amazon, Tira, Purplle, Blinkit, Myntra & Ajio."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import os
import re
import time
from urllib.parse import parse_qs, quote_plus, urlparse

import requests

from setup_db import get_conn

TOP_N = 3
CACHE_SECONDS = 12 * 3600
ERROR_CACHE_SECONDS = 30
SERP_URL = "https://serpapi.com/search.json"
HINDI_RE = re.compile(r"[\u0900-\u097F]")

TRUSTED_ALIASES = {
    "amazon": "amazon",
    "tira": "tira",
    "nykaa": "nykaa",
    "purplle": "purplle",
    "blinkit": "blinkit",
    "myntra": "myntra",
    "ajio": "ajio",
    "big basket": "bigbasket",
    "bigbasket": "bigbasket",
}

STORE_LABELS = {
    "amazon": "Amazon",
    "tira": "Tira",
    "nykaa": "Nykaa",
    "purplle": "Purplle",
    "blinkit": "Blinkit",
    "myntra": "Myntra",
    "ajio": "Ajio",
    "bigbasket": "BigBasket",
}

STORE_DOMAINS = {
    "amazon": "amazon.in",
    "tira": "tirabeauty.com",
    "nykaa": "nykaa.com",
    "purplle": "purplle.com",
    "blinkit": "blinkit.com",
    "myntra": "myntra.com",
    "ajio": "ajio.com",
    "bigbasket": "bigbasket.com",
}

STORE_SEARCH = {
    "amazon": "https://www.amazon.in/s?k={q}",
    "tira": "https://www.tirabeauty.com/products/?q={q}",
    "nykaa": "https://www.nykaa.com/search/result/?q={q}",
    "purplle": "https://www.purplle.com/search?q={q}",
    "blinkit": "https://blinkit.com/s/?q={q}",
    "myntra": "https://www.myntra.com/{q}",
    "ajio": "https://www.ajio.com/search/?text={q}",
    "bigbasket": "https://www.bigbasket.com/ps/?q={q}",
}

PRODUCT_PATTERNS = {
    "amazon": "/dp/",
    "tira": "/product/",
    "nykaa": "/p/",
    "purplle": "/product/",
    "blinkit": "/prn/",
    "myntra": "/buy",
    "ajio": "/p/",
    "bigbasket": "/pd/",
}

BRAND_OFFICIAL_DOMAINS = {
    "minimalist": "beminimalist.co",
    "dot & key": "dotandkey.com",
    "dot and key": "dotandkey.com",
    "plum": "plumgoodness.com",
    "mamaearth": "mamaearth.in",
    "derma co": "thedermaco.com",
    "aqualogica": "aqualogica.in",
    "foxtale": "foxtale.in",
    "pilgrim": "discoverpilgrim.com",
    "sugar": "sugarcosmetics.com",
    "kay beauty": "kaybeauty.in",
    "lakme": "lakmeindia.com",
    "maybelline": "maybelline.co.in",
    "loreal": "lorealparis.co.in",
    "cetaphil": "cetaphil.in",
    "cerave": "cerave.com",
    "cosrx": "cosrx.com",
    "laneige": "laneige.com",
    "innisfree": "innisfree.in",
    "mcaffeine": "mcaffeine.com",
    "forest essentials": "forestessentialsindia.com",
    "kama ayurveda": "kamaayurveda.in",
    "staze": "stazebeauty.com",
    "renee": "reneecosmetics.in",
    "swiss beauty": "swissbeauty.in",
    "mars": "marscosmetics.in",
}

BLOCKED_DOMAINS = (
    "flipkart.", "meesho.", "snapdeal.", "shopclues.", "paytm", "indiamart.",
    "tradeindia.", "buyhatke.", "mysmartprice.", "pricehistory.", "smartprix.",
    "desidime.", "coupondunia.", "grabon.", "cashkaro.", "ebay.", "jiomart.",
    "gosupps.", "desertcart.", "ubuy.", "nailinfinity.", "sweetcare.", "lojaglamourosa.",
)

EXCLUSIVE_FORMS = {
    "shampoo": {"conditioner", "mask", "masque", "serum", "oil"},
    "conditioner": {"shampoo", "mask", "masque", "serum", "oil"},
    "mask": {"shampoo", "conditioner", "cleanser", "toner"},
    "serum": {"cleanser", "shampoo", "conditioner", "sunscreen"},
    "cleanser": {"serum", "moisturizer", "moisturiser", "sunscreen", "toner"},
    "moisturizer": {"cleanser", "shampoo", "sunscreen"},
    "sunscreen": {"cleanser", "shampoo", "conditioner"},
    "toner": {"cleanser", "moisturizer", "moisturiser", "sunscreen", "shampoo"},
}

EXCLUSIVE_ACTIVES = {
    "niacinamide", "vitamin", "salicylic", "hyaluronic", "retinol",
    "glycolic", "kojic", "alpha", "azelaic", "peptide", "ceramide", "squalane",
}

STOP_WORDS = {"the", "a", "an", "for", "and", "with", "from", "of", "in", "by", "buy", "online", "india", "hair", "skin"}
BAD_PAGE_TITLES = ("please enable javascript", "access denied", "just a moment", "robot check", "captcha")

TRACE: dict[str, list[str]] = {}
_CURRENT: list[str] = []
LAST_ERROR: str | None = None
LAST_LINK_ERROR: str | None = None
LENS_HINTS: list[dict] = []


def seed_lens_hints(links: list[dict]) -> None:
    """Keep Google Lens retailer matches so the next price lookup can merge them in."""
    LENS_HINTS.clear()
    for m in links:
        store = normalize_store(m.get("source") or m.get("link"))
        price = _num(m.get("price"))
        title = re.split(r"\s+\|\s+", re.sub(r"^Buy\s+", "", m.get("title") or "", flags=re.I))[0].strip()
        if store and price and m.get("link") and title:
            LENS_HINTS.append({"store": store, "title": title, "price": price, "url": m["link"],
                               "delivery": None, "thumbnail": _extract_thumbnail(m)})


def _t(msg: str) -> None:
    _CURRENT.append(msg)


def _is_hindi(text: str | None) -> bool:
    return bool(text and HINDI_RE.search(text))


def _norm(text: str) -> str:
    t = text.lower().replace("é", "e").replace("è", "e").replace("’", "").replace("'", "")
    t = t.replace("moisturiser", "moisturizer").replace("face wash", "cleanser").replace("facewash", "cleanser")
    t = t.replace("lip gloss", "lipgloss").replace("toning solution", "toner").replace("toning", "toner")
    return re.sub(r"\s+", " ", t).strip()


def _expand_abbreviations(query: str) -> str:
    q = _norm(query)
    q = re.sub(r"\bvit\.?\s*([a-z0-9]+)\b", r"vitamin \1", q, flags=re.I)
    q = re.sub(r"\bboj\b", "beauty of joseon", q, flags=re.I)
    q = re.sub(r"\blrp\b", "la roche posay", q, flags=re.I)
    q = re.sub(r"\bsdj\b", "sol de janeiro", q, flags=re.I)
    return re.sub(r"\s+", " ", q).strip()


def _clean_query(query: str) -> str:
    q = re.sub(r"^the\s+", "", _expand_abbreviations(query))
    q = re.sub(r"\b\d+(?:\.\d+)?\s*(?:ml|g|gm|gms|oz|l)\b", "", q, flags=re.I)
    seen, out = set(), []
    for w in q.split():
        if w not in seen:
            seen.add(w)
            out.append(w)
    return " ".join(out) or q


def _is_official_brand_source(source: str, query: str) -> bool:
    raw = source.strip()
    host = urlparse(raw if "://" in raw else f"https://{raw}").netloc if ("." in raw or "/" in raw) else raw
    s_clean = re.sub(r"[^a-z0-9]", "", _norm(host))
    q_low = _norm(query)
    if any(bad.rstrip(".") in s_clean for bad in BLOCKED_DOMAINS):
        return False
    for brand, dom in BRAND_OFFICIAL_DOMAINS.items():
        if brand in q_low and (dom.split(".")[0] in s_clean or re.sub(r"[^a-z0-9]", "", brand) in s_clean):
            return True
    return False


def normalize_store(source: str | None, query: str = "") -> str | None:
    raw = (source or "").strip()
    if not raw:
        return None
    s = _norm(raw)
    if any(bad.rstrip(".") in s for bad in BLOCKED_DOMAINS):
        return None
    for alias, key in TRUSTED_ALIASES.items():
        if alias in s:
            return key
    if query and _is_official_brand_source(raw, query):
        clean = re.sub(r"^www\.|\.(com|in|co\.in|co|org|net|shop|store)$", "", s)
        slug = re.sub(r"[^a-z0-9]+", "", clean)
        if slug:
            STORE_LABELS.setdefault(slug, f"{clean.title()} (Official)")
            return slug
    return None


def _num(value) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    m = re.search(r"[\d,]+(?:\.\d+)?", str(value))
    return float(m.group().replace(",", "")) if m else None


def _image_url(value) -> str | None:
    """Return the first usable image URL from SerpApi's image field formats."""
    if isinstance(value, str):
        url = value.strip()
        return url if url.startswith(("https://", "http://")) else None
    if isinstance(value, dict):
        for key in ("url", "link", "thumbnail"):
            url = _image_url(value.get(key))
            if url:
                return url
    if isinstance(value, (list, tuple)):
        for item in value:
            url = _image_url(item)
            if url:
                return url
    return None


def _extract_thumbnail(item: dict) -> str | None:
    """Read all documented SerpApi thumbnail fields, preferring its hosted image."""
    for key in ("serpapi_thumbnail", "thumbnail", "serpapi_thumbnails", "thumbnails"):
        url = _image_url(item.get(key))
        if url:
            return url
    return None


def _image_title_matches_product(title: str, query: str) -> bool:
    """Reject image-search results whose title does not describe this product."""
    title_words = set(re.findall(r"[a-z0-9]+", _norm(title or "")))
    query_words = [
        w for w in re.findall(r"[a-z0-9]+", _clean_query(query))
        if len(w) >= 2 and w not in STOP_WORDS
    ]
    if not query_words:
        return False
    # Keep the brand/product identity check, but allow the brand anywhere in
    # image titles (Google Images often places it after the product name).
    if query_words[0] not in title_words:
        return False
    descriptors = query_words[1:]
    if not descriptors:
        return True
    hits = sum(1 for word in descriptors if word in title_words)
    return hits / len(descriptors) >= 0.6


def _fetch_product_image(query: str) -> str | None:
    """Fallback to a title-matched Google Images product photo when Shopping has none."""
    try:
        body = _serp_get({
            "engine": "google_images",
            "q": f'"{query}" product packaging',
            "location": "India",
            "gl": "in",
            "hl": "en",
            "google_domain": "google.co.in",
            "num": 20,
        })
    except Exception:
        return None

    candidates = []
    for result in body.get("images_results", []):
        title = result.get("title") or ""
        if not _image_title_matches_product(title, query):
            continue
        # Prefer results Google explicitly identifies as products, then use
        # SerpApi's proxied thumbnail (more reliable in browser cards).
        is_product = bool(result.get("is_product"))
        image_url = _image_url(result.get("thumbnail")) or _image_url(result.get("original"))
        if image_url:
            candidates.append((not is_product, image_url))

    return min(candidates, key=lambda item: item[0])[1] if candidates else None


def _serp_get(params: dict, timeout: int = 12) -> dict:
    resp = requests.get(
        SERP_URL,
        params={**params, "api_key": os.environ["SERPAPI_KEY"]},
        timeout=timeout,
    )
    resp.raise_for_status()
    return resp.json()


def _describe_error(e: Exception) -> str:
    if isinstance(e, requests.HTTPError) and e.response is not None:
        detail = ""
        try:
            detail = e.response.json().get("error", "")
        except Exception:
            pass
        return f"SerpApi HTTP {e.response.status_code}: {detail}".strip()
    if isinstance(e, KeyError):
        return "SERPAPI_KEY was not found in .env."
    return f"{type(e).__name__}: {str(e)[:120]}"


def _real_url(res: dict) -> str:
    link = res.get("link") or ""
    if "google.com/goto" in link or "google.com/url" in link:
        qs = parse_qs(urlparse(link).query)
        target = (qs.get("url") or qs.get("q") or [""])[0]
        if target.startswith("http"):
            return target
    return link


def _matches_product(title: str, query: str) -> bool:
    t_norm = _norm(title)
    if not t_norm or any(bad in t_norm for bad in BAD_PAGE_TITLES):
        return False
    if re.search(r"\b(combo|pack\s*of\s*\d|set\s*of\b|kit|duo|trio|shades|collection|range|swatches|variants)\b", t_norm):
        return False

    q_words = [w for w in re.findall(r"[a-z0-9]+", _clean_query(query)) if len(w) >= 2 and w not in STOP_WORDS]
    if not q_words:
        return True

    t_list = re.findall(r"[a-z0-9]+", t_norm)
    t_words = set(t_list)
    q_set = set(q_words)

    # 1. Never mix incompatible product forms (e.g., shampoo vs serum)
    banned = set()
    for form, others in EXCLUSIVE_FORMS.items():
        if form in q_set:
            banned.update(others)
    banned -= q_set
    if banned and (t_words & banned) and not (t_words & (q_set & set(EXCLUSIVE_FORMS.keys()))):
        return False

    # 2. Never mix distinct active ingredients (e.g., Niacinamide vs Vitamin C)
    q_actives = q_set & EXCLUSIVE_ACTIVES
    if q_actives and not (q_actives <= t_words):
        return False

    # 3. Brand position rule: first meaningful word of query must appear in the first 4 words of the title
    if q_words[0] not in t_list[:4]:
        return False

    # 4. General descriptor overlap (at least 60% of remaining non-numeric query words)
    descriptors = [w for w in q_words[1:] if not w.isdigit()]
    if not descriptors:
        return True
    hits = sum(1 for w in descriptors if w in t_words or w in t_norm)
    return (hits / len(descriptors)) >= 0.6

def _strict_match(title: str, query: str) -> bool:
    """True only if every meaningful query word is in the title (used to vet thumbnails)."""
    t = _norm(title or "")
    return all(w in t for w in re.findall(r"[a-z0-9]+", _clean_query(query)) if len(w) >= 2 and w not in STOP_WORDS)


def _extract_web_price(res: dict) -> float | None:
    rich = res.get("rich_snippet") or {}
    top_ext = (rich.get("top") or {}).get("detected_extensions") or {}
    bot_ext = (rich.get("bottom") or {}).get("detected_extensions") or {}
    price = (
        _num(top_ext.get("price"))
        or _num(top_ext.get("price_from"))
        or _num(bot_ext.get("price"))
        or _num(bot_ext.get("price_from"))
        or _num(res.get("price"))
    )
    # Removed the regex fallback that grabbed random numbers from text snippets
    return price if (price and 25 <= price <= 50000) else None


# ---------------------------------------------------------------------------
# Fast Parallel Google Shopping + Google Search + Immersive Store Expansion
# ---------------------------------------------------------------------------
def _fetch_google_shopping(query: str) -> tuple[list[dict], list[str], str | None, str | None]:
    try:
        body = _serp_get({
            "engine": "google_shopping",
            "q": query,
            "location": "India",
            "gl": "in",
            "hl": "en",
            "google_domain": "google.co.in",
            "num": 40,
        })
    except Exception as e:
        return [], [], None, _describe_error(e)

    err = body.get("error") or ""
    raw = body.get("shopping_results", []) + body.get("inline_shopping_results", [])
    if err and not raw:
        if "hasn't returned any results" in err.lower() or "no results" in err.lower():
            return [], [], None, None
        return [], [], None, err

    listings, tokens, fallback_thumb = [], [], None
    for item in raw:
        title = item.get("title") or ""
        if not _matches_product(title, query):
            continue
        item_thumb = _extract_thumbnail(item)
        if not fallback_thumb and item_thumb:
            fallback_thumb = item_thumb
        tok = item.get("immersive_product_page_token")
        if tok and tok not in tokens:
            tokens.append(tok)

        store = normalize_store(item.get("source"), query)
        price = _num(item.get("extracted_price")) or _num(item.get("price"))
        if store and price and price > 0:
            listings.append({
                "store": store,
                "title": title,
                "price": price,
                "url": item.get("product_link") or item.get("link"),
                "delivery": item.get("delivery"),
                "thumbnail": item_thumb,
            })
    return listings, tokens[:3], fallback_thumb, None

def _fetch_immersive_stores(token: str, query: str) -> list[dict]:
    try:
        body = _serp_get({
            "engine": "google_immersive_product",
            "page_token": token,
            "more_stores": "true",
            "hl": "en",
            "gl": "in",
        }, timeout=12)
    except Exception:
        return []

    pr = body.get("product_results") or {}
    canonical = pr.get("title") or ""
    if canonical and not _matches_product(canonical, query):
        return []

    thumb = _extract_thumbnail(pr)
    out = []
    for st in (pr.get("stores") or []) + (pr.get("online_sellers") or []):
        store = normalize_store(st.get("name") or st.get("source"), query)
        price = _num(st.get("extracted_price")) or _num(st.get("price"))
        if store and price and price > 0:
            out.append({
                "store": store,
                "title": st.get("title") or canonical or query.title(),
                "price": price,
                "url": st.get("link"),
                "delivery": st.get("shipping"),
                "thumbnail": _extract_thumbnail(st) or thumb,
            })
    return out


def _fetch_google_search(query: str, target_store: str | None = None) -> list[dict]:
    site_filter = (
        f"site:{STORE_DOMAINS[target_store]}"
        if (target_store and target_store in STORE_DOMAINS)
        else "(site:nykaa.com OR site:amazon.in OR site:tirabeauty.com OR site:purplle.com OR site:blinkit.com OR site:myntra.com)"
    )
    try:
        body = _serp_get({
            "engine": "google",
            "q": f"{query} {site_filter}",
            "location": "India",
            "gl": "in",
            "hl": "en",
            "google_domain": "google.co.in",
            "num": 20,
        })
    except Exception:
        return []

    listings = []
    for item in body.get("shopping_results", []) + body.get("inline_shopping_results", []):
        title = item.get("title") or ""
        store = normalize_store(item.get("source"), query)
        price = _num(item.get("extracted_price")) or _num(item.get("price"))
        if store and price and price > 0 and _matches_product(title, query):
            listings.append({
                "store": store,
                "title": title,
                "price": price,
                "url": item.get("link") or item.get("product_link"),
                "delivery": item.get("delivery"),
                "thumbnail": _extract_thumbnail(item),
            })

    for res in body.get("organic_results", []):
        link = _real_url(res)
        if "/reviews/" in link:
            continue
        hint = f"{urlparse(link).netloc} {res.get('displayed_link', '')} {res.get('source', '')}"
        store = normalize_store(hint, query)
        if not store:
            continue
        clean_title = re.sub(
            r"\s*[-|:]\s*(Buy Online.*|Nykaa|Tira|Amazon\.in|Myntra|Purplle|Ajio|Blinkit).*$",
            "", res.get("title") or "", flags=re.I,
        ).strip()
        if not _matches_product(clean_title, query):
            continue
        price = _extract_web_price(res)
        if price:
            listings.append({
                "store": store,
                "title": clean_title,
                "price": price,
                "url": link,
                "delivery": None,
                "thumbnail": _extract_thumbnail(res),
            })
    return listings


def _fetch_demo(query: str) -> list[dict]:
    base = 200 + int(hashlib.md5(query.encode()).hexdigest(), 16) % 800
    return [
        {
            "store": store,
            "title": f"{query.title()} (demo listing)",
            "price": round(base * (0.9 + (int(hashlib.md5(f"{query}{store}".encode()).hexdigest(), 16) % 25) / 100), 0),
            "url": store_search_url(store, query),
            "delivery": None,
            "thumbnail": None,
        }
        for store in STORE_LABELS
    ]


def _best_per_store(listings: list[dict]) -> dict[str, dict]:
    best: dict[str, dict] = {}
    for item in listings:
        st, price = item.get("store"), item.get("price")
        if not st or not price:
            continue
        cur = best.get(st)
        if cur is None or price < cur["price"]:
            best[st] = item
        elif price == cur["price"] and is_google_url(cur.get("url")) and not is_google_url(item.get("url")):
            best[st] = item
    return best


# ---------------------------------------------------------------------------
# Instant Store URLs (Zero Extra Latency)
# ---------------------------------------------------------------------------
def is_google_url(url: str | None) -> bool:
    netloc = urlparse(url).netloc if url else ""
    return not netloc or "google." in netloc


def store_search_url(store: str, title: str | None) -> str:
    noise = {"buy", "online", "shop", "price", "india"}
    words = [w for w in re.sub(r"[…]|\.\.\.|[()]", " ", title or "").split() if w.lower() not in noise][:8]
    if store == "myntra":
        return f"https://www.myntra.com/{'-'.join(w.lower() for w in words)}"
    if store in STORE_SEARCH:
        return STORE_SEARCH[store].format(q=quote_plus(" ".join(words)))
    label = STORE_LABELS.get(store, store).replace(" (Official)", "")
    return f"https://www.google.com/search?q={quote_plus(f'{label} ' + ' '.join(words))}"


def usable_url(store: str, title: str | None, url: str | None) -> str:
    parsed = urlparse(url or "")
    if parsed.scheme in ("http", "https") and parsed.netloc and not is_google_url(url):
        return url
    return store_search_url(store, title)


def resolve_link(store: str, title: str | None) -> tuple[str | None, str]:
    return store_search_url(store, title), "ok"


def direct_link(store: str, title: str | None, url: str | None) -> str | None:
    return usable_url(store, title, url)


# ---------------------------------------------------------------------------
# Main Lookup + Cache + Target-Store Support
# ---------------------------------------------------------------------------
def get_offers(query: str, force_refresh: bool = False, product_type: str = "", target_store: str = "") -> dict:
    global LAST_ERROR
    name = _clean_query(query)
    target = normalize_store(target_store, name) if target_store else None

    conn = get_conn()
    conn.execute("INSERT OR IGNORE INTO products (name) VALUES (?)", (name,))
    product_id = conn.execute("SELECT id FROM products WHERE name = ?", (name,)).fetchone()["id"]

    rows = conn.execute("SELECT * FROM offers WHERE product_id = ?", (product_id,)).fetchall()
    stale_demo = bool(rows) and rows[0]["is_mock"] and bool(os.environ.get("SERPAPI_KEY"))
    ttl = ERROR_CACHE_SECONDS if rows and rows[0]["store"] in ("_error", "_none") else (600 if len(rows) < 2 else CACHE_SECONDS)
    # Retry image-less cached offers sooner so old placeholder-only cards can acquire real images.
    offer_rows = [r for r in rows if r["store"] not in ("_none", "_error")]
    missing_thumbnails = any(not r["thumbnail"] for r in offer_rows)
    cache_ttl = min(ttl, 600) if missing_thumbnails else ttl
    fresh = bool(rows) and not stale_demo and not LENS_HINTS and (time.time() - min(r["fetched_at"] for r in rows) < cache_ttl)

    # Existing price records may predate image extraction. Backfill only their
    # missing thumbnails without re-running the price search or changing prices.
    if fresh and missing_thumbnails and os.environ.get("SERPAPI_KEY"):
        cached_image = _fetch_product_image(name)
        if cached_image:
            conn.execute(
                "UPDATE offers SET thumbnail = ? WHERE product_id = ? "
                "AND store NOT IN ('_none', '_error') AND (thumbnail IS NULL OR thumbnail = '')",
                (cached_image, product_id),
            )
            conn.commit()
            rows = conn.execute("SELECT * FROM offers WHERE product_id = ?", (product_id,)).fetchall()

    # If user turns on 'One app only' for a store not in cache, fetch just that store in ~1s
    cached_stores = {r["store"] for r in rows}
    if fresh and target and target not in cached_stores and target in STORE_DOMAINS:
        extra = _fetch_google_search(name, target_store=target)
        if extra:
            top = sorted(extra, key=lambda x: x["price"])[0]
            top["url"] = usable_url(target, top["title"], top.get("url"))
            conn.execute("DELETE FROM offers WHERE product_id = ? AND store IN ('_none', ?)", (product_id, target))
            conn.execute(
                "INSERT INTO offers (product_id, store, title, price, url, delivery, thumbnail, is_mock, fetched_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?)",
                (product_id, target, top["title"], top["price"], top["url"],
                 None, top.get("thumbnail"), int(time.time())),
            )
            conn.commit()
            rows = conn.execute("SELECT * FROM offers WHERE product_id = ?", (product_id,)).fetchall()

    if not fresh or force_refresh:
        is_mock, error, listings, shop_thumb = 0, None, [], None
        if not os.environ.get("SERPAPI_KEY"):
            LAST_ERROR = "SERPAPI_KEY was not found. Check your .env file."
            listings, is_mock = _fetch_demo(name), 1
        else:
            with ThreadPoolExecutor(max_workers=2) as pool:
                f_shop = pool.submit(_fetch_google_shopping, name)
                f_web = pool.submit(_fetch_google_search, name, target)
                shop_items, tokens, shop_thumb, shop_err = f_shop.result()
                web_items = f_web.result()

            listings = shop_items + web_items
            listings += [h for h in LENS_HINTS if _matches_product(h["title"], name)]
            LENS_HINTS.clear()
            if tokens:
                with ThreadPoolExecutor(max_workers=len(tokens)) as pool:
                    for extra in pool.map(lambda tok: _fetch_immersive_stores(tok, name), tokens):
                        listings.extend(extra)

            # Quick fallback if target_store wasn't in the general results
            if target and target in STORE_DOMAINS and not any(l.get("store") == target for l in listings):
                listings.extend(_fetch_google_search(name, target_store=target))

            if shop_err and not listings:
                error = LAST_ERROR = shop_err
            else:
                LAST_ERROR = None

            _t(f'Google search for "{name}": {len(listings)} matches across '
               f'{len({l["store"] for l in listings})} stores')

        if error and not listings:
            conn.execute("DELETE FROM offers WHERE product_id = ?", (product_id,))
            conn.execute(
                "INSERT INTO offers (product_id, store, title, price, url, is_mock, fetched_at) "
                "VALUES (?, '_error', ?, 0, NULL, 0, ?)", (product_id, error, int(time.time())),
            )
            conn.commit()
            rows = conn.execute("SELECT * FROM offers WHERE product_id = ?", (product_id,)).fetchall()
            conn.close()
            return _result(product_id, name, rows, None, True)

        best = _best_per_store(listings)
        en_titles = [v["title"] for v in best.values() if v.get("title") and not _is_hindi(v["title"])]
        match_title = en_titles[0] if en_titles else (next(iter(best.values()))["title"] if best else None)

        if best and not is_mock:
            any_thumb = next((l["thumbnail"] for l in listings if l.get("thumbnail")), None) or shop_thumb
            # Shopping results sometimes contain prices but omit all image URLs.
            # In that case, fetch one exact-product image and reuse it for the
            # same product across the retailer cards.
            if not any_thumb and os.environ.get("SERPAPI_KEY"):
                any_thumb = _fetch_product_image(name)
            store_thumbs = {}
            for l in listings:
                if l.get("thumbnail"):
                    store_thumbs.setdefault(l["store"], l["thumbnail"])
            for store, item in best.items():
                if _is_hindi(item.get("title")):
                    item["title"] = match_title or name.title()
                item["thumbnail"] = item.get("thumbnail") or store_thumbs.get(store) or any_thumb
                item["url"] = usable_url(store, item["title"], item.get("url"))

            _t("Trusted offers: " + ", ".join(
                f"{STORE_LABELS.get(k, k)} ₹{v['price']:,.0f}"
                for k, v in sorted(best.items(), key=lambda kv: kv[1]["price"])
            ))

        now = int(time.time())
        conn.execute("DELETE FROM offers WHERE product_id = ?", (product_id,))
        if not best:
            conn.execute(
                "INSERT INTO offers (product_id, store, title, price, url, is_mock, fetched_at) "
                "VALUES (?, '_none', NULL, 0, NULL, ?, ?)", (product_id, is_mock, now),
            )
        for store, item in best.items():
            conn.execute(
                "INSERT INTO offers (product_id, store, title, price, url, delivery, thumbnail, is_mock, fetched_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (product_id, store, item["title"], item["price"], item["url"],
                 item.get("delivery"), item.get("thumbnail"), is_mock, now),
            )
        conn.execute("UPDATE products SET match_title = ?, exact = 1 WHERE id = ?", (match_title, product_id))
        conn.commit()
        rows = conn.execute("SELECT * FROM offers WHERE product_id = ?", (product_id,)).fetchall()

    prod = conn.execute("SELECT match_title, exact FROM products WHERE id = ?", (product_id,)).fetchone()
    conn.close()
    return _result(product_id, name, rows, prod["match_title"], bool(prod["exact"]))


def _result(product_id: int, name: str, rows, match_title: str | None, exact: bool) -> dict:
    error_row = next((r for r in rows if r["store"] == "_error"), None)
    offers = {}
    for r in rows:
        if r["store"] not in ("_none", "_error"):
            STORE_LABELS.setdefault(r["store"], f"{r['store'].title()} (Official)")
            offer = dict(r)
            image_url = _image_url(offer.get("thumbnail"))
            # Keep the DB field unchanged and expose aliases for UI consumers
            # that use image_url/image rather than thumbnail.
            offer["thumbnail"] = image_url
            offer["image_url"] = image_url
            offer["image"] = image_url
            offers[r["store"]] = offer
    return {
        "product_id": product_id,
        "name": name,
        "match_title": match_title,
        "exact": exact,
        "error": error_row["title"] if error_row else None,
        "is_mock": bool(rows and rows[0]["is_mock"]),
        "offers": offers,
    }


def get_offers_with_fallback(query: str, product_type: str = "", target_store: str = "") -> dict:
    _CURRENT.clear()
    data = get_offers(query, product_type=product_type, target_store=target_store)
    if not data["offers"] and not data.get("error"):
        words = data["name"].split()
        if len(words) > 3:
            shorter = " ".join(words[:3])
            _t(f'Retrying shorter query: "{shorter}"')
            data = get_offers(shorter, product_type=product_type, target_store=target_store)
    if _CURRENT:
        TRACE[data["name"]] = list(_CURRENT)
    return data