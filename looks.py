"""Looks: turn a makeup video into a saved loadout of products.

Sources
  YouTube link (video or Short): title, channel, thumbnail and description come from the
      YouTube Data API (YOUTUBE_API_KEY). Products are read from the description first;
      if that gives fewer than 2, the spoken transcript is used too.
  Pasted text: a caption or product list pasted by the user. This is how Instagram works,
      because Instagram does not allow reading posts from a program.
"""
import json
import os
import re
import time
from urllib.parse import urlparse

import requests

import cart
from reviews import get_transcript
from setup_db import get_conn

RESOURCES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "resources")
YT_ID = re.compile(r"(?:youtu\.be/|youtube\.com/(?:watch\?(?:[^ ]*&)?v=|shorts/|embed/|live/))([A-Za-z0-9_-]{11})")
DESCRIPTION_CHARS = 3500   # keep prompts small: Groq free tier is 8000 tokens/minute
TRANSCRIPT_CHARS = 6000

PROMPT = (
    "You extract beauty products from a makeup / skincare / haircare video.\n"
    "From the TEXT below, list every product the creator used or recommends. Ignore music, "
    "camera gear, social media links, discount codes, sponsor slogans and non-beauty items.\n"
    "Return ONLY a JSON array. Each item: {\"query\": \"<brand + product name + shade if given>\"}.\n"
    "Use only what the text says; never invent products or shades. Merge duplicates. "
    "If the text names no products, return [].\n\nTEXT:\n"
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _safe_error(e: Exception) -> str:
    """Readable reason that never contains an API key (requests errors include the URL)."""
    if isinstance(e, requests.HTTPError) and e.response is not None:
        return f"HTTP {e.response.status_code} from the server"
    if isinstance(e, requests.ConnectionError):
        return "could not reach the server (check your internet connection)"
    return type(e).__name__


def extract_video_id(url: str) -> str | None:
    m = YT_ID.search(url or "")
    return m.group(1) if m else None


def _llm_call(prompt: str) -> str:
    import llm_client
    return llm_client.chat(prompt)


def _llm_products(text: str) -> list[str]:
    if not text.strip():
        return []
    out = re.sub(r"<think>.*?</think>", "", _llm_call(PROMPT + text), flags=re.S)
    m = re.search(r"\[.*\]", out, flags=re.S)
    try:
        data = json.loads(m.group()) if m else []
    except Exception:
        data = []
    queries, seen = [], set()
    for d in data:
        q = (d.get("query") if isinstance(d, dict) else str(d)) or ""
        q = re.sub(r"\s+", " ", q).strip()
        if q and q.lower() not in seen:
            seen.add(q.lower())
            queries.append(q)
    return queries


def fetch_youtube_meta(video_id: str) -> dict:
    key = os.environ.get("YOUTUBE_API_KEY")
    if key:
        r = requests.get("https://www.googleapis.com/youtube/v3/videos",
                         params={"part": "snippet", "id": video_id, "key": key}, timeout=30)
        r.raise_for_status()
        items = r.json().get("items", [])
        if items:
            sn = items[0]["snippet"]
            th = sn.get("thumbnails", {})
            thumb = (th.get("maxres") or th.get("high") or th.get("medium") or {}).get("url")
            return {"title": sn.get("title"), "channel": sn.get("channelTitle"),
                    "description": sn.get("description", ""), "thumbnail": thumb}
    # No key (or video not found): oEmbed gives title, channel and thumbnail but no description
    r = requests.get("https://www.youtube.com/oembed",
                     params={"url": f"https://www.youtube.com/watch?v={video_id}", "format": "json"}, timeout=30)
    r.raise_for_status()
    j = r.json()
    return {"title": j.get("title"), "channel": j.get("author_name"),
            "description": "", "thumbnail": j.get("thumbnail_url")}


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------
def import_look(url: str = "", pasted: str = "") -> dict:
    """Create a look from a YouTube link and/or pasted text. Returns {ok, look_id, count, source} or {ok: False, error}."""
    url, pasted = (url or "").strip(), (pasted or "").strip()
    host = urlparse(url).netloc.lower() if url else ""
    if "instagram.com" in host and not pasted:
        return {"ok": False, "error": "Instagram doesn't allow reading posts automatically. "
                                      "Copy the post's caption and paste it in the box below, then import again."}

    meta = {"title": None, "channel": None, "description": "", "thumbnail": None}
    video_id = extract_video_id(url)
    if url and not video_id and "instagram.com" not in host:
        return {"ok": False, "error": "I can read YouTube links (videos and Shorts). "
                                      "For anything else, paste the caption or product list below."}
    try:
        if video_id:
            meta = fetch_youtube_meta(video_id)
    except Exception as e:
        return {"ok": False, "error": f"Couldn't open that YouTube video: {_safe_error(e)}."}

    source = "description"
    try:
        text = (meta["description"][:DESCRIPTION_CHARS] + "\n" + pasted).strip()
        if pasted and not meta["description"]:
            source = "pasted text"
        queries = _llm_products(text)
        if len(queries) < 2 and video_id:  # description had no (or almost no) product list
            transcript = get_transcript(video_id, max_chars=TRANSCRIPT_CHARS)
            if transcript:
                more = _llm_products(f"Video title: {meta['title']}\n(spoken transcript)\n{transcript}")
                have = {q.lower() for q in queries}
                queries += [q for q in more if q.lower() not in have]
                source = "transcript" if not have else "description + transcript"
        if not queries:
            source = "none"
    except Exception as e:
        return {"ok": False, "error": f"Couldn't extract products: {_safe_error(e)}. "
                                      "If it says HTTP 429, wait a minute (Groq free-tier limit)."}

    creator = meta["channel"]
    title = (meta["title"] or "My look").strip()
    name = f"{creator}: {title}" if creator else title
    look_id = create_look(name[:80], creator, url or None, meta["thumbnail"], queries)
    return {"ok": True, "look_id": look_id, "count": len(queries), "source": source}


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------
def create_look(name, creator, source_url, thumbnail, queries) -> int:
    conn = get_conn()
    cur = conn.execute("INSERT INTO looks (name, creator, source_url, thumbnail_path) VALUES (?, ?, ?, ?)",
                       (name, creator, source_url, thumbnail))
    look_id = cur.lastrowid
    conn.executemany("INSERT INTO look_items (look_id, query) VALUES (?, ?)", [(look_id, q) for q in queries])
    conn.commit()
    conn.close()
    return look_id


def list_looks() -> list[dict]:
    conn = get_conn()
    rows = conn.execute("SELECT * FROM looks ORDER BY id DESC").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_items(look_id: int) -> list[str]:
    conn = get_conn()
    rows = conn.execute("SELECT query FROM look_items WHERE look_id = ? ORDER BY id", (look_id,)).fetchall()
    conn.close()
    return [r["query"] for r in rows]


def update_look(look_id: int, name: str, queries: list[str], image: tuple[str, bytes] | None = None) -> None:
    conn = get_conn()
    conn.execute("UPDATE looks SET name = ? WHERE id = ?", (name.strip() or "Untitled look", look_id))
    if image:
        filename, data = image
        os.makedirs(RESOURCES, exist_ok=True)
        ext = os.path.splitext(filename)[1].lower() or ".png"
        path = os.path.join(RESOURCES, f"look_{look_id}_{int(time.time())}{ext}")
        with open(path, "wb") as f:
            f.write(data)
        conn.execute("UPDATE looks SET thumbnail_path = ? WHERE id = ?", (path, look_id))
    conn.execute("DELETE FROM look_items WHERE look_id = ?", (look_id,))
    clean = [re.sub(r"\s+", " ", q).strip() for q in queries if q and q.strip()]
    conn.executemany("INSERT INTO look_items (look_id, query) VALUES (?, ?)", [(look_id, q) for q in clean])
    conn.commit()
    conn.close()


def delete_look(look_id: int) -> None:
    conn = get_conn()
    conn.execute("DELETE FROM look_items WHERE look_id = ?", (look_id,))
    conn.execute("DELETE FROM looks WHERE id = ?", (look_id,))
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Cart
# ---------------------------------------------------------------------------
def add_look_to_cart(look_id: int, progress=None) -> list[dict]:
    """Add every product to the cart using the dashboard toggle. Returns one result per product."""
    items = get_items(look_id)
    results = []
    for i, query in enumerate(items):
        if progress:
            progress(i, len(items), query)
        results.append({"query": query, **cart.add_item(query)})
    return results