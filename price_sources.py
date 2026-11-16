"""Fetch and match live product prices across trusted Indian beauty retailers & official brand websites.

Trusted retailers:
  Amazon, Tira, Nykaa, Ajio, Purplle, Instagram, Blinkit, Myntra, BigBasket
  + the product's own official brand website (e.g., beminimalist.co, dotandkey.com).
"""
import hashlib
import json
import os
import re
import time
from urllib.parse import quote_plus, urlparse

import requests

import llm_client
from setup_db import get_conn

TOP_N = 3
CACHE_SECONDS = 12 * 3600
ERROR_CACHE_SECONDS = 60
SERP_URL = "https://serpapi.com/search.json"
HINDI_RE = re.compile(r"[\u0900-\u097F]")

# Strictly trusted platforms (no Flipkart, Meesho, or third-party price aggregators)
TRUSTED_ALIASES = {
    "amazon": "amazon",
    "tira": "tira",
    "nykaa": "nykaa",
    "ajio": "ajio",
    "purplle": "purplle",
    "instagram": "instagram",
    "blinkit": "blinkit",
    "myntra": "myntra",
    "big basket": "bigbasket",
    "bigbasket": "bigbasket",
}
STORE_LABELS = {
    "amazon": "Amazon",
    "tira": "Tira",
    "nykaa": "Nykaa",
    "ajio": "Ajio",
    "purplle": "Purplle",
    "instagram": "Instagram",
    "blinkit": "Blinkit",
    "myntra": "Myntra",
    "bigbasket": "BigBasket",
}
STORE_DOMAINS = {
    "amazon": "amazon.in",
    "tira": "tirabeauty.com",
    "nykaa": "nykaa.com",
    "ajio": "ajio.com",
    "purplle": "purplle.com",
    "instagram": "instagram.com",
    "blinkit": "blinkit.com",
    "myntra": "myntra.com",
    "bigbasket": "bigbasket.com",
}
STORE_SEARCH = {
    "amazon": "https://www.amazon.in/s?k={q}",
    "tira": "https://www.tirabeauty.com/products/?q={q}",
    "nykaa": "https://www.nykaa.com/search/result/?q={q}",
    "ajio": "https://www.ajio.com/search/?text={q}",
    "purplle": "https://www.purplle.com/search?q={q}",
    "instagram": "https://www.instagram.com/explore/tags/{q}/",
    "blinkit": "https://blinkit.com/s/?q={q}",
    "myntra": "https://www.myntra.com/{q}",
    "bigbasket": "https://www.bigbasket.com/ps/?q={q}",
}
PRODUCT_PATTERNS = {
    "amazon": "/dp/",
    "tira": "/product/",
    "nykaa": "/p/",
    "ajio": "/p/",
    "purplle": "/product/",
    "blinkit": "/prn/",
    "myntra": "/buy",
    "bigbasket": "/pd/",
}

# Known Indian/global beauty brand official domains
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
)

TRACE: dict[str, list[str]] = {}
_CURRENT: list[str] = []
LAST_ERROR: str | None = None
LAST_LINK_ERROR: str | None = None


def _t(msg: str) -> None:
    _CURRENT.append(msg)


def _is_hindi(text: str | None) -> bool:
    return bool(text and HINDI_RE.search(text))


def _expand_abbreviations(query: str) -> str:
    q = query.strip()
    q = re.sub(r"\bvit\.?\s*([a-z0-9]+)\b", r"vitamin \1", q, flags=re.I)
    q = re.sub(r"\bboj\b", "beauty of joseon", q, flags=re.I)
    q = re.sub(r"\blrp\b", "la roche posay", q, flags=re.I)
    q = re.sub(r"\bsdj\b", "sol de janeiro", q, flags=re.I)
    return re.sub(r"\s+", " ", q).strip()


def _is_official_brand_source(source: str, query: str) -> bool:
    """Check if the seller/domain is the official website of the brand being searched."""
    s_clean = re.sub(r"[^a-z0-9]", "", source.lower())
    q_low = query.lower()
    if any(bad.rstrip(".") in s_clean for bad in BLOCKED_DOMAINS):
        return False

    for brand, dom in BRAND_OFFICIAL_DOMAINS.items():
        if brand in q_low and (dom.split(".")[0] in s_clean or re.sub(r"[^a-z0-9]", "", brand) in s_clean):
            return True

    # Also match if any significant brand word from the query is directly in the domain/seller name
    stop = {"the", "for", "and", "with", "serum", "cream", "cleanser", "wash", "face", "gel", "oil", "lip", "tint"}
    brand_tokens = [w for w in re.findall(r"[a-z]{4,}", q_low) if w not in stop][:2]
    return any(tok in s_clean for tok in brand_tokens)


def normalize_store(source: str | None, query: str = "") -> str | None:
    """Return store key ONLY if it is one of the 9 trusted apps or the official brand website."""
    raw = (source or "").strip()
    if not raw:
        return None
    s = raw.lower()
    if any(bad.rstrip(".") in s for bad in BLOCKED_DOMAINS):
        return None

    for alias, key in TRUSTED_ALIASES.items():
        if alias in s:
            return key

    if query and _is_official_brand_source(raw, query):
        clean = re.sub(r"^www\.", "", s)
        clean = re.sub(r"\.(com|in|co\.in|co|org|net|shop|store)$", "", clean)
        slug = re.sub(r"[^a-z0-9]+", "", clean)
        if slug:
            label = re.sub(r"\.(com|in|co\.in|co|org|net)$", "", raw, flags=re.I).strip()
            if label.islower():
                label = label.title()
            STORE_LABELS.setdefault(slug, f"{label} (Official)")
            return slug

    return None


def _num(value) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    m = re.search(r"[\d,]+(?:\.\d+)?", str(value))
    return float(m.group().replace(",", "")) if m else None


def _serp_get(params: dict, timeout: int = 30) -> dict:
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
        return f"SerpApi returned HTTP {e.response.status_code}. {detail}".strip()
    if isinstance(e, KeyError):
        return "SERPAPI_KEY was not found. Check your .env file and restart Streamlit."
    if isinstance(e, requests.ConnectionError):
        return "Could not reach serpapi.com. Check your internet connection."
    if isinstance(e, requests.Timeout):
        return "SerpApi took too long to answer. Try again."
    key = os.environ.get("SERPAPI_KEY", "\0")
    return f"{type(e).__name__}: {str(e).replace(key, '***')[:120]}"


# ---------------------------------------------------------------------------
# Search fetchers (Google Shopping + Targeted Trusted Store Search)
# ---------------------------------------------------------------------------
def _fetch_live(query: str) -> list[dict]:
    body = _serp_get({
        "engine": "google_shopping", "q": query,
        "gl": "in", "hl": "en", "google_domain": "google.co.in",
    })
    err = body.get("error") or ""
    if err and not body.get("shopping_results"):
        if "hasn't returned any results" in err or "no results" in err.lower():
            return []
        raise RuntimeError(err)

    listings = []
    for item in body.get("shopping_results", []):
        if item.get("title"):
            src = item.get("source")
            listings.append({
                "store": normalize_store(src, query),
                "source": src,
                "title": item.get("title"),
                "price": _num(item.get("extracted_price")) or _num(item.get("price")),
                "url": item.get("link") or item.get("product_link"),
                "delivery": item.get("delivery"),
                "thumbnail": item.get("thumbnail"),
                "token": item.get("immersive_product_page_token"),
            })
    return listings


def _extract_web_price(res: dict) -> float | None:
    rich = res.get("rich_snippet") or {}
    price = (
        _num(((rich.get("top") or {}).get("detected_extensions") or {}).get("price"))
        or _num(((rich.get("bottom") or {}).get("detected_extensions") or {}).get("price"))
    )
    if not price:
        text = f"{res.get('title', '')} {res.get('snippet', '')}"
        m = re.search(r"(?:₹|Rs\.?|INR)\s*([\d,]+(?:\.\d+)?)", text, re.I)
        price = _num(m.group(1)) if m else None
    return price if (price and price > 0) else None


def _fetch_store_specific(query: str, store: str) -> dict | None:
    """Run a targeted site: search on a specific trusted store (e.g., Tira or Nykaa)."""
    domain = STORE_DOMAINS.get(store)
    if not domain:
        return None
    try:
        body = _serp_get({
            "engine": "google",
            "q": f"{query} site:{domain}",
            "gl": "in", "hl": "en", "google_domain": "google.co.in",
        })
    except Exception:
        return None

    pattern = PRODUCT_PATTERNS.get(store, "")
    fallback_item = None
    for res in body.get("organic_results", []):
        link = res.get("link") or ""
        if domain not in link:
            continue
        price = _extract_web_price(res)
        if not price:
            continue
        clean_title = re.sub(
            r"\s*[-|:]\s*(Buy Online.*|Nykaa|Tira|Amazon\.in|Myntra|Purplle|Ajio).*$",
            "", res.get("title") or query.title(), flags=re.I,
        ).strip()
        item = {
            "store": store, "source": STORE_LABELS.get(store, store.title()),
            "title": clean_title, "price": price, "url": link,
            "delivery": None, "thumbnail": res.get("thumbnail"), "token": None,
        }
        if pattern and pattern in urlparse(link).path:
            return item
        if fallback_item is None:
            fallback_item = item
    return fallback_item


def _fetch_trusted_web_listings(query: str) -> list[dict]:
    """Supplement with Google Web Search across trusted retailers + official brand site."""
    body = _serp_get({
        "engine": "google",
        "q": f"{query} price nykaa OR tira OR amazon OR myntra OR purplle OR ajio",
        "gl": "in", "hl": "en", "google_domain": "google.co.in",
    })
    listings = []
    for res in body.get("organic_results", []):
        link = res.get("link") or ""
        netloc = urlparse(link).netloc.lower()
        store = normalize_store(netloc, query)
        if not store:
            continue
        price = _extract_web_price(res)
        if price:
            clean_title = re.sub(
                r"\s*[-|:]\s*(Buy Online.*|Nykaa|Tira|Amazon\.in|Myntra|Purplle|Ajio).*$",
                "", res.get("title") or query.title(), flags=re.I,
            ).strip()
            listings.append({
                "store": store, "source": STORE_LABELS.get(store, store.title()),
                "title": clean_title, "price": price, "url": link,
                "delivery": None, "thumbnail": res.get("thumbnail"), "token": None,
            })
    return listings


def _fetch_demo(query: str) -> list[dict]:
    base = 200 + int(hashlib.md5(query.encode()).hexdigest(), 16) % 800
    return [
        {
            "store": store,
            "title": f"{query.title()} (demo listing)",
            "price": round(base * (0.9 + (int(hashlib.md5(f"{query}{store}".encode()).hexdigest(), 16) % 25) / 100), 0),
            "url": f"https://www.google.com/search?q={query.replace(' ', '+')}+{store}",
            "delivery": None,
            "thumbnail": None,
        }
        for store in STORE_LABELS
    ]


# ---------------------------------------------------------------------------
# Product matching (LLM Judge + simple keyword fallback)
# ---------------------------------------------------------------------------
JUDGE_PROMPT = """You match online shopping listings to a beauty / personal-care product search.

SEARCH: "<<QUERY>>"
<<TYPE>>The search may contain typos, abbreviations (e.g. 'vit c' = 'Vitamin C'), or omit the brand/full line name.

LISTINGS (index: title):
<<LINES>>

Rules:
1. BRAND: If the search names a brand, the listing must match that brand.
2. PRODUCT & DESCRIPTORS: Match the product line and type. Descriptive formula words (like 'tubing', 'matte', 'hydrating', 'vitamin c') do not need to appear verbatim if the brand and product match (e.g. 'Staze 9to9 All Eyes On You Mascara' matches 'staze tubing mascara').
3. LANGUAGE: Always pick an English listing as "best" whenever available, and include both English and Hindi listings of the exact same product in "same".
4. FORM & BUNDLES: Do not match a shampoo to a serum, or a combo/kit to a single product unless requested.
5. SHADE/SIZE: Prefer the requested shade/size; if only another shade/size is available, pick it and set "exact": false.
6. "same" = all listing indexes that are the exact same product across stores/websites. If nothing fits, set "best": null.

Return ONLY JSON: {"best": <index or null>, "same": [<indexes>], "exact": <true or false>}"""


def _judge_listings(query: str, listings: list[dict], product_type: str = ""):
    items = listings[:30]
    if not items:
        return None
    lines = "\n".join(f"{i}: {(l.get('title') or '')[:110]}" for i, l in enumerate(items))
    type_line = f'Product type: "{product_type}".\n' if product_type else ""
    prompt = JUDGE_PROMPT.replace("<<QUERY>>", query).replace("<<TYPE>>", type_line).replace("<<LINES>>", lines)
    try:
        out = llm_client.chat(prompt)
        data = json.loads(re.search(r"\{.*\}", out, flags=re.S).group())
        best = data.get("best")
        if best is None:
            return None, [], True
        best = int(best)
        if not 0 <= best < len(items):
            return None
        same = {int(i) for i in (data.get("same") or []) if isinstance(i, (int, float)) and 0 <= int(i) < len(items)}
        same.add(best)
        return items[best], [items[i] for i in sorted(same)], bool(data.get("exact"))
    except Exception:
        return None


def _simple_keyword_fallback(listings: list[dict], query: str):
    words = [w for w in re.findall(r"[a-z0-9]+", query.lower()) if len(w) > 2]
    if not listings or not words:
        return (listings[0], listings[:1], True) if listings else (None, [], True)

    scored = []
    for item in listings:
        title_low = (item.get("title") or "").lower()
        hits = sum(1 for w in words if w in title_low)
        ratio = hits / len(words)
        if ratio >= 0.4 and not re.search(r"\b(combo|pack of \d|set of \d|kit)\b", title_low):
            scored.append((not _is_hindi(item.get("title")), ratio, item))

    if not scored:
        return None, [], True
    scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
    anchor = scored[0][2]
    same = [x[2] for x in scored if x[1] >= 0.5]
    return anchor, same, scored[0][1] >= 0.7


def _best_per_store(listings: list[dict]) -> dict[str, dict]:
    best: dict[str, dict] = {}
    for item in listings:
        if not item.get("store") or not item.get("price"):
            continue
        cur = best.get(item["store"])
        if cur is None or item["price"] < cur["price"]:
            best[item["store"]] = item
        elif item["price"] == cur["price"] and _is_hindi(cur.get("title")) and not _is_hindi(item.get("title")):
            best[item["store"]] = item
    return best


# ---------------------------------------------------------------------------
# Direct store URLs
# ---------------------------------------------------------------------------
def is_google_url(url: str | None) -> bool:
    netloc = urlparse(url).netloc if url else ""
    return not netloc or "google." in netloc


def usable_url(store: str, title: str | None, url: str | None) -> str:
    parsed = urlparse(url or "")
    if parsed.scheme in ("http", "https") and parsed.netloc and not is_google_url(url):
        return url
    return store_search_url(store, title)


def store_search_url(store: str, title: str | None) -> str:
    noise = {"buy", "online", "shop", "price", "india"}
    words = [w for w in re.sub(r"[…]|\.\.\.|[()]", " ", title or "").split() if w.lower() not in noise][:8]
    if store == "myntra":
        return f"https://www.myntra.com/{'-'.join(w.lower() for w in words)}"
    if store in STORE_SEARCH:
        return STORE_SEARCH[store].format(q=quote_plus(" ".join(words)))
    label = STORE_LABELS.get(store, store).replace(" (Official)", "")
    return f"https://www.google.com/search?q={quote_plus(f'{label} ' + ' '.join(words))}"


def resolve_link(store: str, title: str | None) -> tuple[str | None, str]:
    domain = STORE_DOMAINS.get(store)
    pattern = PRODUCT_PATTERNS.get(store, "")
    if not (domain and title):
        return None, "no store domain or title"
    if not os.environ.get("SERPAPI_KEY"):
        return None, "no SERPAPI_KEY"

    words = re.sub(r"[…]|\.\.\.|[()|,]", " ", title).split()
    queries = [" ".join(words[:14])] + ([" ".join(words[:6])] if len(words) > 6 else [])
    for q in queries:
        try:
            body = _serp_get({"engine": "google", "q": f"{q} site:{domain}", "gl": "in", "hl": "en"})
        except Exception as e:
            return None, _describe_error(e)
        for res in body.get("organic_results", []):
            link = res.get("link", "")
            if domain in link and (not pattern or pattern in urlparse(link).path):
                return link, "ok"
    return None, f"no {domain} product page in search results"


def direct_link(store: str, title: str | None, url: str | None) -> str | None:
    global LAST_LINK_ERROR
    if not is_google_url(url):
        return url
    if os.environ.get("RESOLVE_PRODUCT_PAGES", "1") == "0" or store not in STORE_DOMAINS:
        return store_search_url(store, title)
    found, reason = resolve_link(store, title)
    if found:
        return found
    LAST_LINK_ERROR = f"{STORE_LABELS.get(store, store)}: {reason}"
    return store_search_url(store, title)


def _fetch_exact_stores(token: str, query: str) -> tuple[list[dict], str]:
    try:
        body = _serp_get({
            "engine": "google_immersive_product", "page_token": token,
            "more_stores": "true", "hl": "en", "gl": "in",
        }, timeout=45)
    except Exception as e:
        return [], _describe_error(e)

    pr = body.get("product_results") or {}
    thumbs = pr.get("thumbnails") or []
    out = []
    for st in pr.get("stores") or []:
        store = normalize_store(st.get("name") or st.get("source"), query)
        price = _num(st.get("extracted_price")) or _num(st.get("price"))
        link = st.get("link")
        if store and price and link:
            out.append({
                "store": store, "title": st.get("title") or pr.get("title"),
                "price": price, "url": link, "delivery": None,
                "thumbnail": thumbs[0] if thumbs else None,
            })
    return out, ("ok" if out else "no trusted stores in immersive response")


# ---------------------------------------------------------------------------
# Main lookup + caching + targeted store search
# ---------------------------------------------------------------------------
def get_offers(query: str, force_refresh: bool = False, product_type: str = "", target_store: str = "") -> dict:
    """Fetch, match, cache, and return {product_id, name, match_title, exact, is_mock, offers}."""
    global LAST_ERROR, LAST_LINK_ERROR
    name = re.sub(r"\s+", " ", _expand_abbreviations(query).lower())
    target = normalize_store(target_store, name) if target_store else None

    conn = get_conn()
    conn.execute("INSERT OR IGNORE INTO products (name) VALUES (?)", (name,))
    product_id = conn.execute("SELECT id FROM products WHERE name = ?", (name,)).fetchone()["id"]

    rows = conn.execute("SELECT * FROM offers WHERE product_id = ?", (product_id,)).fetchall()
    stale_demo = bool(rows) and rows[0]["is_mock"] and bool(os.environ.get("SERPAPI_KEY"))
    ttl = ERROR_CACHE_SECONDS if rows and rows[0]["store"] in ("_error", "_none") else CACHE_SECONDS
    fresh = bool(rows) and not stale_demo and (time.time() - min(r["fetched_at"] for r in rows) < ttl)

    # If the user asks for a specific store (e.g., Tira or Nykaa) that isn't cached yet, fetch it directly
    cached_stores = {r["store"] for r in rows}
    if fresh and target and target not in cached_stores and target in STORE_DOMAINS:
        found_item = _fetch_store_specific(name, target)
        if found_item:
            _t(f"Targeted {STORE_LABELS.get(target, target)} search found ₹{found_item['price']:,.0f}")
            conn.execute("DELETE FROM offers WHERE product_id = ? AND store IN ('_none', ?)", (product_id, target))
            conn.execute(
                "INSERT INTO offers (product_id, store, title, price, url, delivery, thumbnail, is_mock, fetched_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?)",
                (product_id, target, found_item["title"], found_item["price"], found_item["url"],
                 None, found_item.get("thumbnail"), int(time.time())),
            )
            conn.commit()
            rows = conn.execute("SELECT * FROM offers WHERE product_id = ?", (product_id,)).fetchall()

    if not fresh or force_refresh:
        is_mock, error, listings = 0, None, []
        if not os.environ.get("SERPAPI_KEY"):
            LAST_ERROR = "SERPAPI_KEY was not found. Check your .env file and restart Streamlit."
            listings, is_mock = _fetch_demo(name), 1
        else:
            try:
                listings = _fetch_live(name)
                LAST_ERROR = None
            except Exception as e:
                error = LAST_ERROR = _describe_error(e)

        if error:
            conn.execute("DELETE FROM offers WHERE product_id = ?", (product_id,))
            conn.execute(
                "INSERT INTO offers (product_id, store, title, price, url, is_mock, fetched_at) "
                "VALUES (?, '_error', ?, 0, NULL, 0, ?)", (product_id, error, int(time.time())),
            )
            conn.commit()
            rows = conn.execute("SELECT * FROM offers WHERE product_id = ?", (product_id,)).fetchall()
            conn.close()
            return _result(product_id, name, rows, None, True)

        if not is_mock:
            _t(f'Google Shopping for "{name}": {len(listings)} results across '
               f'{len({l["store"] for l in listings if l["store"]})} trusted stores')

        verdict = None if (is_mock or not listings) else _judge_listings(name, listings, product_type)
        if verdict is not None:
            anchor, same, exact_ok = verdict
        else:
            anchor, same, exact_ok = _simple_keyword_fallback(listings, name)

        best = _best_per_store(same)
        match_title = anchor["title"] if anchor else None
        exact_flag = 1 if (anchor is None or exact_ok) else 0

        if anchor is not None and not is_mock:
            seed = anchor if anchor.get("token") else next((l for l in same if l.get("token")), None)
            if seed:
                exact, note = _fetch_exact_stores(seed["token"], name)
                if exact:
                    for it in exact:
                        cur = best.get(it["store"])
                        if cur is None or it["price"] <= cur["price"]:
                            best[it["store"]] = {**it, "thumbnail": it.get("thumbnail") or seed.get("thumbnail")}
                else:
                    LAST_LINK_ERROR = f"exact-product lookup: {note}"

        # Always check trusted retailers via Google Web Search if fewer than 3 trusted stores matched
        if len(best) < TOP_N and not is_mock:
            try:
                web_listings = _fetch_trusted_web_listings(name)
                if web_listings:
                    _t(f"Trusted web search found {len(web_listings)} additional store listings")
                    for item in web_listings:
                        st = item["store"]
                        if st not in best or item["price"] < best[st]["price"]:
                            best[st] = item
                    if not match_title:
                        en_web = next((w["title"] for w in web_listings if not _is_hindi(w.get("title"))), None)
                        match_title = en_web or web_listings[0]["title"]
                        exact_flag = 1
            except Exception as e:
                _t(f"Trusted web search failed: {_describe_error(e)}")

        # If a specific store was requested (or single-app mode is active) and still missing, search that site directly
        stores_to_check = set()
        if target and target not in best:
            stores_to_check.add(target)
        for must_store in ("nykaa", "tira", "amazon"):
            if len(best) < TOP_N and must_store not in best:
                stores_to_check.add(must_store)

        if not is_mock:
            for st in list(stores_to_check)[:2]:
                found_item = _fetch_store_specific(name, st)
                if found_item:
                    _t(f"Direct {STORE_LABELS.get(st, st)} lookup found ₹{found_item['price']:,.0f}")
                    best[st] = found_item
                    if not match_title:
                        match_title = found_item["title"]
                        exact_flag = 1

        if best and not is_mock:
            en_titles = [v["title"] for v in best.values() if v.get("title") and not _is_hindi(v["title"])]
            english_title = (
                match_title if (match_title and not _is_hindi(match_title))
                else (en_titles[0] if en_titles else name.title())
            )
            if _is_hindi(match_title):
                match_title = english_title

            default_thumb = (
                (anchor.get("thumbnail") if anchor else None)
                or next((v["thumbnail"] for v in best.values() if v.get("thumbnail")), None)
            )
            for item in best.values():
                if _is_hindi(item.get("title")):
                    item["title"] = english_title
                if not item.get("thumbnail") and default_thumb:
                    item["thumbnail"] = default_thumb

            for store, item in best.items():
                item["url"] = direct_link(store, item["title"], item["url"])
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
                 item["delivery"], item.get("thumbnail"), is_mock, now),
            )
        conn.execute("UPDATE products SET match_title = ?, exact = ? WHERE id = ?", (match_title, exact_flag, product_id))
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
            offers[r["store"]] = dict(r)
    return {
        "product_id": product_id,
        "name": name,
        "match_title": match_title,
        "exact": exact,
        "error": error_row["title"] if error_row else None,
        "is_mock": bool(rows and rows[0]["is_mock"]),
        "offers": offers,
    }


ALT_PROMPT = (
    "A shopper searched Google India for a beauty product. Expand abbreviations or colloquial names "
    "to the official product name (e.g. 'staze tubing mascara' -> 'Staze 9to9 All Eyes On You Mascara').\n"
    "SEARCH: \"<<Q>>\"\n<<HINT>>"
    "Return ONLY a JSON array of up to 2 better English Google search queries."
)


def _alternatives(query: str, closest: str | None = None) -> list[str]:
    hint = f'Closest listing so far: "{closest}".\n' if closest else ""
    try:
        out = llm_client.chat(ALT_PROMPT.replace("<<Q>>", query).replace("<<HINT>>", hint))
        alts = [re.sub(r"\s+", " ", str(q)).strip() for q in json.loads(re.search(r"\[.*\]", out, flags=re.S).group())]
    except Exception:
        expanded = _expand_abbreviations(query)
        no_pct = re.sub(r"\s+", " ", re.sub(r"\b\d+\s*%\s*", "", expanded)).strip()
        alts = [q for q in (expanded, no_pct) if q.lower() != query.strip().lower()]

    seen, result = {query.strip().lower()}, []
    for q in alts:
        if q and q.lower() not in seen:
            seen.add(q.lower())
            result.append(q)
    return result[:2]


def get_offers_with_fallback(query: str, product_type: str = "", target_store: str = "") -> dict:
    """Look up prices across trusted stores, retrying with expanded names if needed."""
    _CURRENT.clear()
    data = get_offers(query, product_type=product_type, target_store=target_store)
    best = data
    if not (data.get("error") or data["is_mock"] or (data["offers"] and data["exact"])):
        for alt in _alternatives(query, data.get("match_title")):
            _t(f'Retrying with: "{alt}"')
            retry = get_offers(alt, product_type=product_type, target_store=target_store)
            if retry.get("error"):
                break
            if retry["offers"] and (retry["exact"] or not best["offers"]):
                best = retry
                if retry["exact"]:
                    break
    if _CURRENT:
        TRACE[best["name"]] = list(_CURRENT)
    return best