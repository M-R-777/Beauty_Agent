"""Review sources for the agent.

Reddit : Google search restricted to reddit.com, through SerpApi (SERPAPI_KEY).
         Returns thread titles + snippets, not full threads.
YouTube: YouTube Data API (YOUTUBE_API_KEY) to find videos and filter out
         sponsored ones, then transcripts via the unofficial
         youtube-transcript-api package (falls back to the description).
"""
import os
import re

import requests

TRANSCRIPT_CHARS = 1200  # keep small: Groq free tier allows only 8000 tokens/minute

# Signals of sponsorship / affiliate content. A filter, not a guarantee.
SPONSOR_WORDS = [
    "#ad", "#sponsored", "sponsored", "paid partnership", "paid promotion",
    "gifted", "pr package", "pr sample", "affiliate", "use code", "discount code",
    "coupon code", "in collaboration with", "collab with",
]
AFFILIATE_DOMAINS = [
    "amzn.to", "amzn.in", "fkrt.it", "fkrt.cc", "tidd.ly", "shopmy.us",
    "go.skimresources",
]


# ---------------------------------------------------------------------------
# Reddit (via Google results)
# ---------------------------------------------------------------------------
def _collect_reddit(node, found: list[dict]) -> None:
    """Find Reddit threads anywhere in a SerpApi response (they can sit in organic results or in
    a 'discussions and forums' block, and the layout changes)."""
    if isinstance(node, dict):
        link = node.get("link") or node.get("url")
        if isinstance(link, str) and "reddit.com" in link and ("/comments/" in link or "/r/" in link):
            found.append({"title": node.get("title"),
                          "snippet": (node.get("snippet") or node.get("description") or "")[:300],
                          "url": link})
        for value in node.values():
            _collect_reddit(value, found)
    elif isinstance(node, list):
        for value in node:
            _collect_reddit(value, found)


def _reddit_search(query: str, key: str) -> tuple[list[dict], int]:
    resp = requests.get(
        "https://serpapi.com/search.json",
        params={"engine": "google", "q": query, "gl": "in", "hl": "en", "api_key": key},
        timeout=30,
    )
    resp.raise_for_status()
    body = resp.json()
    found: list[dict] = []
    _collect_reddit(body, found)
    return found, len(body.get("organic_results") or [])


def reddit_reviews(product: str, limit: int = 6) -> dict:
    """Reddit threads about a product, found through Google. Up to 4 searches, stopping early once
    there are enough threads: the product as typed, then spelling-corrected and simpler names."""
    key = os.environ.get("SERPAPI_KEY")
    if not key:
        return {"available": False, "reason": "Reddit reviews need SERPAPI_KEY in .env."}
    threads: list[dict] = []
    seen: set[str] = set()
    searched: list[dict] = []

    def run(query: str) -> str | None:
        try:
            found, n = _reddit_search(query, key)
        except Exception as e:
            code = getattr(getattr(e, "response", None), "status_code", None)
            return f"HTTP {code}" if code else type(e).__name__
        new = [t for t in found if t["url"] not in seen]
        for t in new:
            seen.add(t["url"])
            threads.append(t)
        searched.append({"query": query, "google_results": n, "reddit_threads": len(new)})
        return None

    for query in (f"{product} review site:reddit.com", f"{product} reddit"):
        err = run(query)
        if err:
            return {"available": False, "reason": f"Reddit search failed ({err})."}
        if len(threads) >= 3:
            break
    if not threads:  # nothing under the typed name: try corrected / simpler names
        from price_sources import _alternatives  # lazy: avoids loading the pricing code unless needed
        for alt in _alternatives(product):
            if run(f"{alt} reddit review") is None and threads:
                break
    return {"available": True, "searched": searched,
            "note": "Snippets from Reddit threads found via Google, not full threads. "
                    "If threads is empty, say how many Google results came back and that none were Reddit threads.",
            "threads": threads[:limit]}


# ---------------------------------------------------------------------------
# YouTube
# ---------------------------------------------------------------------------
def sponsor_reason(snippet: dict, paid_flag: bool | None) -> str | None:
    """Why a video looks sponsored, or None if it looks clean."""
    if paid_flag:
        return "creator marked paid promotion"
    text = f"{snippet.get('title', '')} {snippet.get('description', '')}".lower()
    for w in SPONSOR_WORDS:
        if w in text:
            return f"mentions '{w}'"
    for d in AFFILIATE_DOMAINS:
        if d in text:
            return f"affiliate-style link ({d})"
    return None


def _yt_get(endpoint: str, params: dict) -> dict:
    params = {**params, "key": os.environ["YOUTUBE_API_KEY"]}
    r = requests.get(f"https://www.googleapis.com/youtube/v3/{endpoint}", params=params, timeout=30)
    r.raise_for_status()
    return r.json()


def get_transcript(video_id: str, max_chars: int = TRANSCRIPT_CHARS) -> str | None:
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
    except ImportError:
        return None
    try:
        try:  # newer versions
            fetched = YouTubeTranscriptApi().fetch(video_id, languages=["en", "hi"])
            text = " ".join(s.text for s in fetched)
        except AttributeError:  # older versions
            parts = YouTubeTranscriptApi.get_transcript(video_id, languages=["en", "hi"])
            text = " ".join(p["text"] for p in parts)
        return re.sub(r"\s+", " ", text)[:max_chars]
    except Exception:
        return None


def youtube_reviews(product: str, max_videos: int = 2) -> dict:
    if not os.environ.get("YOUTUBE_API_KEY"):
        return {"available": False, "reason": "YouTube reviews need YOUTUBE_API_KEY in .env."}
    try:
        found = _yt_get("search", {"part": "snippet", "q": f"{product} review", "type": "video",
                                   "maxResults": 12, "regionCode": "IN", "relevanceLanguage": "en"})
        ids = [i["id"]["videoId"] for i in found.get("items", [])]
        if not ids:
            return {"available": True, "videos": [], "skipped_sponsored": 0}
        try:
            details = _yt_get("videos", {"part": "snippet,paidProductPlacementDetail", "id": ",".join(ids)})
        except requests.HTTPError:  # part not accepted: retry without the paid flag
            details = _yt_get("videos", {"part": "snippet", "id": ",".join(ids)})
    except Exception as e:
        return {"available": False, "reason": f"YouTube request failed: {e}"}

    videos, skipped = [], 0
    for item in details.get("items", []):
        paid = item.get("paidProductPlacementDetail", {}).get("hasPaidProductPlacement")
        snip = item["snippet"]
        if sponsor_reason(snip, paid):
            skipped += 1
            continue
        if len(videos) >= max_videos:
            continue
        vid = item["id"]
        transcript = get_transcript(vid)
        videos.append({
            "title": snip.get("title"),
            "channel": snip.get("channelTitle"),
            "url": f"https://www.youtube.com/watch?v={vid}",
            "content": transcript or snip.get("description", "")[:TRANSCRIPT_CHARS],
            "content_type": "transcript" if transcript else "description only",
        })
    return {"available": True, "skipped_sponsored": skipped, "videos": videos,
            "note": "Sponsorship filtering is best-effort."}