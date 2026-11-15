"""Fetch the best price per store for a product.

Live mode: Google Shopping (India) through SerpApi. Set SERPAPI_KEY in .env.
Demo mode: if the key is missing or the call fails, deterministic fake prices are
used and flagged is_mock=True. The reason is kept in LAST_ERROR so the UI can show it.

Direct links: Google Shopping often returns a google.com link. For the TOP_N cheapest
stores we do one extra Google search ("<title> site:<store domain>") and use the first
result, so "View on Nykaa" opens the store itself. Results are cached for 12 hours.
"""
import difflib
import hashlib
import json
import os
import re
import time
from urllib.parse import quote_plus, urlparse

import requests

import llm_client
import matcher
from setup_db import get_conn

TOP_N = 3  # only the 3 cheapest stores are shown

STORE_ALIASES = {
    "nykaa": "nykaa",
    "tira": "tira",
    "amazon": "amazon",
    "blinkit": "blinkit",
    "big basket": "bigbasket",
    "bigbasket": "bigbasket",
    "ajio": "ajio",
}
STORE_LABELS = {
    "nykaa": "Nykaa", "tira": "Tira", "amazon": "Amazon",
    "blinkit": "Blinkit", "bigbasket": "BigBasket", "ajio": "Ajio",
}
# Used to find each store's own page. Check these if a store's links look wrong.
STORE_DOMAINS = {
    "nykaa": "nykaa.com", "tira": "tirabeauty.com", "amazon": "amazon.in",
    "blinkit": "blinkit.com", "bigbasket": "bigbasket.com", "ajio": "ajio.com",
}
CACHE_SECONDS = 12 * 3600
ERROR_CACHE_SECONDS = 300  # a failed lookup is remembered for 5 minutes only
SERP_URL = "https://serpapi.com/search.json"

TRACE: dict[str, list[str]] = {}   # product name -> how it was searched (shown in "Search details")
_CURRENT: list[str] = []           # lines for the lookup in progress


def _t(message: str) -> None:
    _CURRENT.append(message)


LAST_ERROR: str | None = None  # why the last lookup fell back to demo data


def normalize_store(source: str | None) -> str | None:
    s = (source or "").lower()
    for alias, key in STORE_ALIASES.items():
        if alias in s:
            return key
    return None


def _describe_error(e: Exception) -> str:
    """Readable reason without leaking the API key (requests errors contain the URL)."""
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


def _fetch_live(query: str) -> list[dict]:
    resp = requests.get(
        SERP_URL,
        params={"engine": "google_shopping", "q": query, "gl": "in", "hl": "en",
                "google_domain": "google.co.in", "api_key": os.environ["SERPAPI_KEY"]},
        timeout=30,
    )
    resp.raise_for_status()
    body = resp.json()
    err = (body.get("error") or "")
    if err and not body.get("shopping_results"):
        if "hasn't returned any results" in err or "no results" in err.lower():
            return []  # a valid answer: Google has nothing for this query
        raise RuntimeError(err)
    listings = []
    for item in body.get("shopping_results", []):
        if not item.get("title"):
            continue
        price = item.get("extracted_price")
        listings.append({
            "store": normalize_store(item.get("source")),  # None for stores we do not support
            "source": item.get("source"),
            "title": item.get("title"),
            "price": float(price) if price else None,
            "url": item.get("link") or item.get("product_link"),
            "delivery": item.get("delivery"),
            "thumbnail": item.get("thumbnail"),
            "token": item.get("immersive_product_page_token"),
        })
    return listings


def _fetch_demo(query: str) -> list[dict]:
    base = 200 + int(hashlib.md5(query.encode()).hexdigest(), 16) % 800
    listings = []
    for store in STORE_LABELS:
        digest = int(hashlib.md5(f"{query}{store}".encode()).hexdigest(), 16)
        listings.append({
            "store": store,
            "title": f"{query.title()} (demo listing)",
            "price": round(base * (0.9 + (digest % 25) / 100), 0),
            "url": f"https://www.google.com/search?q={query.replace(' ', '+')}+{store}",
            "delivery": None,
            "thumbnail": None,
        })
    return listings


STOPWORDS = {"the", "for", "and", "with", "best", "buy", "compare", "price", "of", "in", "online", "india"}
MIN_SCORE = 0.34    # below this nothing in the results resembles the query
EXACT_SCORE = 0.75  # at or above this we call it an exact match (typos are forgiven)


def _words(text: str | None) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9]+", (text or "").lower()) if len(w) > 2 and w not in STOPWORDS]


def _word_hit(word: str, tokens: list[str], text: str) -> bool:
    """True if the word is in the text, allowing prefixes ('mar' in 'mars') and typos ('glowil' ~ 'glow')."""
    return word in text or any(difflib.SequenceMatcher(None, word, t).ratio() >= 0.8 for t in tokens)


def _score(words: list[str], title: str | None) -> float:
    """Share of the words that appear (fuzzily) in the title, from 0 to 1."""
    if not words:
        return 0.0
    text = (title or "").lower()
    tokens = re.findall(r"[a-z0-9]+", text)
    return sum(_word_hit(w, tokens, text) for w in words) / len(words)


def _similarity(title_a: str | None, title_b: str | None) -> float:
    """How alike two listing titles are: the shorter title's words found in the longer one."""
    wa, wb = _words(title_a), _words(title_b)
    short, long_title = (wa, title_b) if len(wa) <= len(wb) else (wb, title_a)
    return _score(short, long_title)


# Words that describe a TYPE of product. They never decide whether two products are the same.
# Everything else in the query (brand, product line, shade, number) is "distinctive".
GENERIC = {
    "lipstick", "lip", "lips", "gloss", "balm", "liner", "pencil", "crayon", "tint", "stain",
    "foundation", "concealer", "primer", "powder", "compact", "blush", "highlighter", "bronzer",
    "contour", "eyeshadow", "shadow", "palette", "mascara", "eyeliner", "kajal", "brow", "eye",
    "nail", "polish", "remover", "cleanser", "face", "wash", "serum", "moisturizer", "moisturiser",
    "cream", "lotion", "sunscreen", "spf", "toner", "mist", "mask", "gel", "oil", "shampoo",
    "conditioner", "hair", "scalp", "body", "butter", "scrub", "set", "kit", "combo", "pack",
    "spray", "stick", "liquid", "matte", "glossy", "long", "wear", "stay", "women", "men",
    "shade", "new", "natural", "beauty", "makeup", "skin", "care", "skincare", "haircare",
}


# Product families, used by the fallback matcher when the language model is unavailable
TYPE_FAMILY = {
    "serum": "oil", "oil": "oil", "elixir": "oil", "shampoo": "shampoo", "conditioner": "conditioner",
    "mask": "mask", "cleanser": "cleanser", "facewash": "cleanser", "moisturizer": "cream",
    "moisturiser": "cream", "cream": "cream", "lotion": "cream", "sunscreen": "sun", "spf": "sun",
    "toner": "toner", "lipstick": "lipstick", "gloss": "gloss", "balm": "balm", "foundation": "foundation",
    "concealer": "concealer", "primer": "primer", "compact": "powder", "powder": "powder",
    "blush": "blush", "highlighter": "highlighter", "bronzer": "bronzer", "mascara": "mascara",
    "eyeliner": "liner", "kajal": "liner", "eyeshadow": "shadow", "palette": "shadow",
    "trimmer": "trimmer", "razor": "razor", "epilator": "epilator", "perfume": "perfume",
}
HARD_FAMILIES = {"shampoo", "conditioner", "mask", "cleanser"}  # an extra one of these = different product
BUNDLE_RE = re.compile(r"\b(combo|pack of \d|set of \d|\d\s?-?\s?pack|kit)\b", re.I)


def _families(words: list[str]) -> set[str]:
    return {TYPE_FAMILY[w] for w in words if w in TYPE_FAMILY}


def _core(words: list[str]) -> list[str]:
    """The distinctive words: brand, product line, shade. Spelling slips are forgiven elsewhere."""
    return [w for w in words if w not in GENERIC]


def _misses(core: list[str], title: str | None) -> int:
    text = (title or "").lower()
    tokens = re.findall(r"[a-z0-9]+", text)
    return sum(not _word_hit(w, tokens, text) for w in core)


def _qualifies(item: dict, words: list[str], core: list[str]) -> bool:
    """Could this listing be what the user asked for?

    With no distinctive words (e.g. 'hair mask') most of the words must match. Otherwise every
    distinctive word must match, except one miss is allowed when there are two or more (covers a
    badly spelled word or a brand left out of the listing title).
    """
    text = (item["title"] or "").lower()
    title_words = re.findall(r"[a-z0-9]+", text)
    q_fam, t_fam = _families(words), _families(title_words)
    if q_fam and t_fam and (q_fam.isdisjoint(t_fam) or (t_fam - q_fam) & HARD_FAMILIES):
        return False  # a different type of product (e.g. conditioner when a serum was asked for)
    if BUNDLE_RE.search(text) and not BUNDLE_RE.search(" ".join(words)):
        return False  # a combo / pack when a single product was asked for
    for n in (w for w in words if w.isdigit()):  # shade numbers must match exactly (128 is not 220)
        if not re.search(rf"(?<!\d){n}(?!\d)", text):
            return False
    if not core:
        return _score(words, item["title"]) >= 0.5
    return _misses(core, item["title"]) <= (1 if len(core) >= 2 else 0)


def _pick_anchor(listings: list[dict], query: str) -> tuple[dict | None, float]:
    """The qualifying listing closest to what the user typed (typos and missing brands are fine)."""
    words = _words(query)
    if not listings:
        return None, 0.0
    if not words:
        return listings[0], 1.0
    core = _core(words)
    best, best_key = None, (-1.0, False, 0)
    for i, item in enumerate(listings):
        if not _qualifies(item, words, core):
            continue
        key = (_score(words, item["title"]), bool(item.get("token")), -i)  # ties: has token, Google's order
        if key > best_key:
            best, best_key = item, key
    return (best, best_key[0]) if best is not None and best_key[0] >= MIN_SCORE else (None, max(best_key[0], 0.0))


def _group(listings: list[dict], query: str) -> tuple[dict | None, float, list[dict], bool]:
    """(anchor, score, listings that are the same product as the anchor, is_exact_match)."""
    anchor, score = _pick_anchor(listings, query)
    if anchor is None:
        return None, score, [], True
    words = _words(query)
    core = _core(words)
    same = [l for l in listings
            if _qualifies(l, words, core) and _similarity(l["title"], anchor["title"]) >= 0.6]
    exact = _misses(core, anchor["title"]) == 0 and score >= EXACT_SCORE
    return anchor, score, same, exact


def _best_per_store(listings: list[dict]) -> dict[str, dict]:
    best: dict[str, dict] = {}
    for item in listings:
        if not item.get("store") or not item.get("price"):
            continue
        cur = best.get(item["store"])
        if cur is None or item["price"] < cur["price"]:
            best[item["store"]] = item
    return best


def is_google_url(url: str | None) -> bool:
    """True for missing, relative (no site name) or google.* links: none of these open a store."""
    netloc = urlparse(url).netloc if url else ""
    return not netloc or "google." in netloc


def usable_url(store: str, title: str | None, url: str | None) -> str:
    """A link a browser can open from our page. A relative link like '/shopping/...' would
    just reload this app, so anything that is not a full http(s) address becomes the
    store's search page instead."""
    parsed = urlparse(url or "")
    if parsed.scheme in ("http", "https") and parsed.netloc and not is_google_url(url):
        return url
    return store_search_url(store, title)


def _num(value) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    m = re.search(r"[\d,]+(?:\.\d+)?", str(value))
    return float(m.group().replace(",", "")) if m else None


def _fetch_exact_stores(token: str) -> tuple[list[dict], str]:
    """Every store selling ONE exact Google product, with direct links: (listings, note)."""
    try:
        resp = requests.get(
            SERP_URL,
            params={"engine": "google_immersive_product", "page_token": token,
                    "more_stores": "true", "api_key": os.environ["SERPAPI_KEY"]},
            timeout=45,
        )
        resp.raise_for_status()
        body = resp.json()
    except Exception as e:
        return [], _describe_error(e)

    pr = body.get("product_results") or {}
    thumbs = pr.get("thumbnails") or []
    out = []
    for st in pr.get("stores") or []:
        store = normalize_store(st.get("name") or st.get("source"))
        price = (_num(st.get("extracted_price")) or _num(st.get("extracted_total"))
                 or _num(st.get("price")) or _num(st.get("total")))
        link = st.get("link")
        if store and price and link:
            out.append({"store": store, "title": st.get("title") or pr.get("title"), "price": price,
                        "url": link, "delivery": None, "thumbnail": thumbs[0] if thumbs else None})
    note = "ok" if out else f"no usable stores in the response (it had: {', '.join(list(pr)[:8]) or body.get('error', 'nothing')})"
    return out, note


STORE_SEARCH = {
    "amazon": "https://www.amazon.in/s?k={q}",
    "nykaa": "https://www.nykaa.com/search/result/?q={q}",
    "tira": "https://www.tirabeauty.com/products/?q={q}",
    "blinkit": "https://blinkit.com/s/?q={q}",
    "bigbasket": "https://www.bigbasket.com/ps/?q={q}",
    "ajio": "https://www.ajio.com/search/?text={q}",
}
LAST_LINK_ERROR: str | None = None  # why the last direct-link lookup failed


def store_search_url(store: str, title: str | None) -> str:
    """The store's own search page for this product (used when no direct page is found)."""
    noise = {"buy", "online", "shop", "price", "india"}
    words = [w for w in re.sub(r"[…]|\.\.\.|[()]", " ", title or "").split() if w.lower() not in noise][:8]
    q = quote_plus(" ".join(words))
    return STORE_SEARCH.get(store, "https://www.google.com/search?q={q}").format(q=q)


# A real product page has this in its path (Tira: /product/<name>-<id>, Nykaa: /<name>/p/<id>)
PRODUCT_PATTERNS = {
    "nykaa": "/p/", "tira": "/product/", "amazon": "/dp/",
    "ajio": "/p/", "bigbasket": "/pd/", "blinkit": "/prn/",
}


def link_kind(store: str, url: str | None) -> str:
    """'product' if the link looks like the product's own page, else 'search'."""
    pattern = PRODUCT_PATTERNS.get(store)
    path = urlparse(url or "").path
    return "product" if pattern and pattern in path and not is_google_url(url) else "search"


def resolve_link(store: str, title: str | None) -> tuple[str | None, str]:
    """(url, reason). The store's own PRODUCT page for this listing, or (None, why not).

    Runs a Google search limited to the store's site and keeps the first result that looks
    like a product page (not a category or search page). Tries the full title, then a
    shorter one, because very long titles sometimes match nothing.
    """
    domain = STORE_DOMAINS.get(store)
    pattern = PRODUCT_PATTERNS.get(store, "")
    if not (domain and title):
        return None, "no store domain or title"
    if not os.environ.get("SERPAPI_KEY"):
        return None, "no SERPAPI_KEY"
    clean = re.sub(r"[…]|\.\.\.|[()|,]", " ", title)
    words = clean.split()
    queries = [" ".join(words[:14])]
    if len(words) > 6:
        queries.append(" ".join(words[:6]))
    reason = f"no {domain} product page in the search results"
    for q in queries:
        try:
            resp = requests.get(
                SERP_URL,
                params={"engine": "google", "q": f"{q} site:{domain}", "gl": "in", "hl": "en",
                        "api_key": os.environ["SERPAPI_KEY"]},
                timeout=30,
            )
            resp.raise_for_status()
            body = resp.json()
        except Exception as e:
            return None, _describe_error(e)
        for res in body.get("organic_results", []):
            link = res.get("link", "")
            if domain in link and (not pattern or pattern in urlparse(link).path):
                return link, "ok"
        reason = body.get("error") or reason
    return None, reason


def direct_link(store: str, title: str | None, url: str | None) -> str | None:
    """A link that opens the store, not Google: the real page if found, else the store's search."""
    global LAST_LINK_ERROR
    if not is_google_url(url):
        return url
    if os.environ.get("RESOLVE_PRODUCT_PAGES", "1") == "0":  # set to 0 in .env to save searches
        return store_search_url(store, title)
    found, reason = resolve_link(store, title)
    if found:
        return found
    LAST_LINK_ERROR = f"{STORE_LABELS.get(store, store)}: {reason}"
    return store_search_url(store, title)


def get_offers(query: str, force_refresh: bool = False, product_type: str = "") -> dict:
    """Return {product_id, name, is_mock, offers: {store: listing}} for a query."""
    global LAST_ERROR, LAST_LINK_ERROR
    name = re.sub(r"\s+", " ", query.strip().lower())
    conn = get_conn()
    conn.execute("INSERT OR IGNORE INTO products (name) VALUES (?)", (name,))
    product_id = conn.execute("SELECT id FROM products WHERE name = ?", (name,)).fetchone()["id"]

    rows = conn.execute("SELECT * FROM offers WHERE product_id = ?", (product_id,)).fetchall()
    # Cached demo data is never "fresh" once a key exists, so fixing the key takes effect at once
    stale_demo = bool(rows) and rows[0]["is_mock"] and bool(os.environ.get("SERPAPI_KEY"))
    ttl = ERROR_CACHE_SECONDS if rows and rows[0]["store"] == "_error" else CACHE_SECONDS
    fresh = bool(rows) and not stale_demo and (time.time() - min(r["fetched_at"] for r in rows) < ttl)

    if not fresh or force_refresh:
        is_mock, error, listings = 0, None, []
        if not os.environ.get("SERPAPI_KEY"):
            LAST_ERROR = "SERPAPI_KEY was not found. Check your .env file and restart Streamlit."
            listings, is_mock = _fetch_demo(name), 1  # demo prices only when no key is configured
        else:
            try:
                listings = _fetch_live(name)  # may be empty: Google has nothing for this query
                LAST_ERROR = None
            except Exception as e:
                error = LAST_ERROR = _describe_error(e)

        if error:  # a real failure: say so (briefly cached so reruns do not repeat the call)
            conn.execute("DELETE FROM offers WHERE product_id = ?", (product_id,))
            conn.execute(
                "INSERT INTO offers (product_id, store, title, price, url, is_mock, fetched_at) "
                "VALUES (?, '_error', ?, 0, NULL, 0, ?)", (product_id, error, int(time.time())))
            conn.commit()
            rows = conn.execute("SELECT * FROM offers WHERE product_id = ?", (product_id,)).fetchall()
            conn.close()
            return _result(product_id, name, rows, None, True)

        if not is_mock:
            unknown = sorted({l["source"] for l in listings if not l["store"] and l.get("source")})
            _t(f'Google Shopping for "{name}": {len(listings)} results, '
               f'{sum(1 for l in listings if l["store"])} from supported stores'
               + (f'; other stores seen: {", ".join(unknown[:8])}' if unknown else ""))
        verdict = None if (is_mock or not listings) else matcher.judge(name, listings, product_type)
        if verdict is not None:
            anchor, same, exact_ok = verdict
        else:  # model unavailable (or demo data): fall back to the word-based matcher
            anchor, _score_, same, exact_ok = _group(listings, name)
            if not is_mock:
                _t("AI judge unavailable, used the word-based matcher")
        best = _best_per_store(same)
        match_title = anchor["title"] if anchor else None
        exact_flag = 1 if (anchor is None or exact_ok) else 0
        if not is_mock:
            _t(f'Matched: {match_title!r} (exact={bool(exact_flag)})' if anchor else "Matched: nothing fits")
        if anchor is not None and not is_mock:
            # Ask Google for the exact product's stores: same product everywhere, direct links
            seed = anchor if anchor.get("token") else next((l for l in same if l.get("token")), None)
            if seed:
                exact, note = _fetch_exact_stores(seed["token"])
                _t(f"Stores selling that exact product: {', '.join(sorted({i['store'] for i in exact})) or 'none'} ({note})")
                if exact:
                    for it in exact:
                        best[it["store"]] = {**it, "thumbnail": it.get("thumbnail") or seed.get("thumbnail")}
                else:
                    LAST_LINK_ERROR = f"exact-product lookup: {note}"
            else:
                _t("Matched listing has no store-comparison page; using the listings found directly")
        if not is_mock:  # open the store's own page for the cheapest few
            for store, item in sorted(best.items(), key=lambda kv: kv[1]["price"])[:TOP_N]:
                item["url"] = direct_link(store, item["title"], item["url"])
            _t("Offers: " + (", ".join(f"{k} ₹{v['price']:,.0f}" for k, v in sorted(best.items(), key=lambda kv: kv[1]['price'])) or "none"))

        now = int(time.time())
        conn.execute("DELETE FROM offers WHERE product_id = ?", (product_id,))
        if not best:  # remember "nothing matched" so we do not search again on every rerun
            conn.execute(
                "INSERT INTO offers (product_id, store, title, price, url, is_mock, fetched_at) "
                "VALUES (?, '_none', NULL, 0, NULL, ?, ?)", (product_id, is_mock, now))
        for store, item in best.items():
            conn.execute(
                "INSERT INTO offers (product_id, store, title, price, url, delivery, thumbnail, is_mock, fetched_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (product_id, store, item["title"], item["price"], item["url"],
                 item["delivery"], item.get("thumbnail"), is_mock, now),
            )
        conn.execute("UPDATE products SET match_title = ?, exact = ? WHERE id = ?",
                     (match_title, exact_flag, product_id))
        conn.commit()
        rows = conn.execute("SELECT * FROM offers WHERE product_id = ?", (product_id,)).fetchall()

    prod = conn.execute("SELECT match_title, exact FROM products WHERE id = ?", (product_id,)).fetchone()
    conn.close()
    return _result(product_id, name, rows, prod["match_title"], bool(prod["exact"]))


def _result(product_id: int, name: str, rows, match_title: str | None, exact: bool) -> dict:
    error_row = next((r for r in rows if r["store"] == "_error"), None)
    return {
        "product_id": product_id,
        "name": name,
        "match_title": match_title,
        "exact": exact,
        "error": error_row["title"] if error_row else None,
        "is_mock": bool(rows and rows[0]["is_mock"]),
        "offers": {r["store"]: dict(r) for r in rows if r["store"] not in ("_none", "_error")},
    }


# ---------------------------------------------------------------------------
# Forgiving lookup: fixes spelling and retries with a simpler query
# ---------------------------------------------------------------------------
ALT_PROMPT = (
    "A shopper searched Google Shopping India for a beauty or personal-care product. The search may contain "
    "spelling mistakes, a missing brand, or a shade name.\n"
    "SEARCH: \"<<Q>>\"\n<<HINT>>"
    "Return a JSON array of up to 2 better search queries, most specific first: "
    "(1) the corrected, complete product name with brand, product line and shade; "
    "(2) brand + product line + product type (no shade or size). "
    "Use real product names you are confident about. Return ONLY the JSON array."
)


def _alternatives(query: str, closest: str | None = None) -> list[str]:
    """Other queries worth trying (at most 2; each may cost a search)."""
    alts: list[str] = []
    hint = f'The closest listing found so far was "{closest}", which was not an exact match.\n' if closest else ""
    try:
        out = llm_client.chat(ALT_PROMPT.replace("<<Q>>", query).replace("<<HINT>>", hint))
        data = json.loads(re.search(r"\[.*\]", out, flags=re.S).group())
        alts = [re.sub(r"\s+", " ", str(q)).strip() for q in data if str(q).strip()]
    except Exception:
        words = query.split()
        if len(words) > 4:
            alts = [" ".join(words[:4])]
    seen, result = {query.strip().lower()}, []
    for q in alts:
        if q.lower() not in seen:
            seen.add(q.lower())
            result.append(q)
    return result[:2]


def get_offers_with_fallback(query: str, product_type: str = "") -> dict:
    """get_offers, retrying with corrected / simpler queries when nothing, or only a rough match, is found.

    At most two retries; each can cost one search. The first exact match wins; otherwise the
    best of what was found is kept. How it went is saved in TRACE for the 'Search details' panel.
    """
    _CURRENT.clear()
    data = get_offers(query, product_type=product_type)
    best = data
    if not (data.get("error") or data["is_mock"] or (data["offers"] and data["exact"])):
        for alt in _alternatives(query, data.get("match_title")):
            _t(f'Retrying with: "{alt}"')
            retry = get_offers(alt, product_type=product_type)
            if retry.get("error"):
                break
            if retry["offers"] and (retry["exact"] or not best["offers"]):
                best = retry
                if retry["exact"]:
                    break
    if _CURRENT:
        TRACE[best["name"]] = list(_CURRENT)
    return best