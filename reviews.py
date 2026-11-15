"""Review sources for the agent.

Reddit : Google search restricted to reddit.com + Google Forums engine through SerpApi (SERPAPI_KEY).
         Captures both full threads and individual comment mentions, just like Google Search.
YouTube: YouTube Data API (YOUTUBE_API_KEY) to find videos and filter out
         sponsored ones, then transcripts via youtube-transcript-api (falls back to description).
"""
import os
import re

import requests

TRANSCRIPT_CHARS = 1200  # keep small: Groq free tier allows only 8000 tokens/minute

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
# Reddit (via Google Search + Google Forums)
# ---------------------------------------------------------------------------
def _is_reddit_link(link: str) -> bool:
    low = link.lower()
    if not ("reddit.com" in low or "redd.it" in low):
        return False
    # Exclude bare user profile pages so we keep posts, subreddits, share links, and comments
    return "/user/" not in low and "/u/" not in low


def _collect_reddit(node, found: list[dict], parent_title: str = "") -> None:
    """Recursively extract every Reddit post, comment snippet, and discussions_and_forums entry."""
    if isinstance(node, dict):
        title = node.get("title") or parent_title
        link = node.get("link") or node.get("url")
        if isinstance(link, str) and _is_reddit_link(link):
            snippet = (
                node.get("snippet")
                or node.get("description")
                or node.get("text")
                or ""
            )
            # Google's discussions_and_forums attaches individual Reddit comments under "answers"
            answers = node.get("answers")
            if isinstance(answers, list):
                comment_texts = [
                    (a.get("snippet") or a.get("text") or "").strip()
                    for a in answers
                    if isinstance(a, dict)
                ]
                joined = " | ".join(c for c in comment_texts if c)
                if joined:
                    snippet = f"{snippet} — Comments: {joined}".strip(" —")
            found.append({
                "title": title or "Reddit discussion",
                "snippet": snippet[:450],
                "url": link,
            })
        for value in node.values():
            _collect_reddit(value, found, title)
    elif isinstance(node, list):
        for value in node:
            _collect_reddit(value, found, parent_title)


def _reddit_search(query: str, key: str, engine: str = "google") -> tuple[list[dict], int]:
    params = {"engine": engine, "q": query, "gl": "in", "hl": "en", "api_key": key}
    resp = requests.get("https://serpapi.com/search.json", params=params, timeout=30)
    resp.raise_for_status()
    body = resp.json()
    found: list[dict] = []
    _collect_reddit(body, found)
    total = len(body.get("organic_results") or []) + len(body.get("discussions_and_forums") or [])
    return found, total


def reddit_reviews(product: str, limit: int = 8) -> dict:
    """Find any Reddit threads or comments mentioning a product, acting just like Google Search."""
    key = os.environ.get("SERPAPI_KEY")
    if not key:
        return {"available": False, "reason": "Reddit reviews need SERPAPI_KEY in .env."}
    threads: list[dict] = []
    seen: set[str] = set()
    searched: list[dict] = []

    def run(query: str, engine: str = "google") -> str | None:
        try:
            found, n = _reddit_search(query, key, engine=engine)
        except Exception as e:
            code = getattr(getattr(e, "response", None), "status_code", None)
            return f"HTTP {code}" if code else type(e).__name__
        new = [t for t in found if t["url"] not in seen]
        for t in new:
            seen.add(t["url"])
            threads.append(t)
        searched.append({"query": query, "engine": engine, "results": n, "reddit_mentions": len(new)})
        return None

    # Do NOT force the word "review" — search broadly so any thread or comment mentioning the product matches
    for query in (f"site:reddit.com {product}", f"{product} reddit"):
        err = run(query, engine="google")
        if err:
            return {"available": False, "reason": f"Reddit search failed ({err})."}
        if len(threads) >= 4:
            break

    # Expand abbreviations/names (e.g. "minimalist vit c" -> "Minimalist Vitamin C serum") & check Google Forums
    if len(threads) < 3:
        from price_sources import _alternatives
        for alt in _alternatives(product):
            run(f"site:reddit.com {alt}", engine="google")
            if len(threads) >= 4:
                break

    if len(threads) < 3:
        run(product, engine="google_forums")

    return {
        "available": True,
        "searched": searched,
        "note": (
            "Snippets from Reddit posts and comments found via Google Search. "
            "Summarize all user opinions, pros, cons, and experiences mentioned in these snippets with links."
        ),
        "threads": threads[:limit],
    }


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
        try:
            fetched = YouTubeTranscriptApi().fetch(video_id, languages=["en", "hi"])
            text = " ".join(s.text for s in fetched)
        except AttributeError:
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
        except requests.HTTPError:
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