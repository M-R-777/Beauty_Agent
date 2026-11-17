"""Fetch unfiltered user reviews from Reddit (via Google Search) and YouTube."""
import os
import re

import requests

TRANSCRIPT_CHARS = 1200

SPONSOR_WORDS = [
    "#ad", "#sponsored", "sponsored", "paid partnership", "paid promotion",
    "gifted", "pr package", "pr sample", "affiliate", "use code", "discount code",
    "coupon code", "in collaboration with", "collab with",
]
AFFILIATE_DOMAINS = ["amzn.to", "amzn.in", "fkrt.it", "fkrt.cc", "tidd.ly", "shopmy.us", "go.skimresources"]


# ---------------------------------------------------------------------------
# Reddit (posts & individual comment mentions via SerpApi)
# ---------------------------------------------------------------------------
def _is_reddit_link(link: str) -> bool:
    low = link.lower()
    return ("reddit.com" in low or "redd.it" in low) and "/user/" not in low and "/u/" not in low


def _collect_reddit(node, found: list[dict], parent_title: str = "") -> None:
    """Recursively extract Reddit posts, comment snippets, and Google Forums answers."""
    if isinstance(node, dict):
        title = node.get("title") or parent_title
        link = node.get("link") or node.get("url")
        if isinstance(link, str) and _is_reddit_link(link):
            snippet = node.get("snippet") or node.get("description") or node.get("text") or ""
            answers = node.get("answers")
            if isinstance(answers, list):
                comments = [
                    (a.get("snippet") or a.get("text") or "").strip()
                    for a in answers if isinstance(a, dict)
                ]
                joined = " | ".join(c for c in comments if c)
                if joined:
                    snippet = f"{snippet} — Comments: {joined}".strip(" —")
            found.append({"title": title or "Reddit discussion", "snippet": snippet[:450], "url": link})
        for v in node.values():
            _collect_reddit(v, found, title)
    elif isinstance(node, list):
        for v in node:
            _collect_reddit(v, found, parent_title)


def _reddit_search(query: str, key: str, engine: str = "google") -> list[dict]:
    resp = requests.get(
        "https://serpapi.com/search.json",
        params={"engine": engine, "q": query, "gl": "in", "hl": "en", "api_key": key},
        timeout=30,
    )
    resp.raise_for_status()
    found: list[dict] = []
    _collect_reddit(resp.json(), found)
    return found


def reddit_reviews(product: str, limit: int = 8) -> dict:
    """Search Reddit threads and comments mentioning a product (max 2 API calls)."""
    key = os.environ.get("SERPAPI_KEY")
    if not key:
        return {"available": False, "reason": "Reddit reviews need SERPAPI_KEY in .env."}

    threads: list[dict] = []
    seen: set[str] = set()

    for q in (f"site:reddit.com {product}", f"{product} reddit India"):
        try:
            items = _reddit_search(q, key, engine="google")
        except Exception as e:
            if not threads:
                return {"available": False, "reason": f"Reddit search failed ({type(e).__name__})."}
            break
        for t in items:
            if t["url"] not in seen:
                seen.add(t["url"])
                threads.append(t)
        if len(threads) >= 3:
            break

    return {"available": True, "threads": threads[:limit]}

# ---------------------------------------------------------------------------
# YouTube (sponsored videos filtered out)
# ---------------------------------------------------------------------------
def sponsor_reason(snippet: dict, paid_flag: bool | None) -> str | None:
    if paid_flag:
        return "paid promotion"
    text = f"{snippet.get('title', '')} {snippet.get('description', '')}".lower()
    for w in SPONSOR_WORDS:
        if w in text:
            return f"mentions '{w}'"
    for d in AFFILIATE_DOMAINS:
        if d in text:
            return f"affiliate link ({d})"
    return None


def _yt_get(endpoint: str, params: dict) -> dict:
    r = requests.get(
        f"https://www.googleapis.com/youtube/v3/{endpoint}",
        params={**params, "key": os.environ["YOUTUBE_API_KEY"]},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


def get_transcript(video_id: str, max_chars: int = TRANSCRIPT_CHARS) -> str | None:
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
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
    """Fetch non-sponsored YouTube review transcripts/descriptions."""
    if not os.environ.get("YOUTUBE_API_KEY"):
        return {"available": False, "reason": "YouTube reviews need YOUTUBE_API_KEY in .env."}
    try:
        found = _yt_get("search", {
            "part": "snippet", "q": f"{product} review", "type": "video",
            "maxResults": 12, "regionCode": "IN", "relevanceLanguage": "en",
        })
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
    return {"available": True, "skipped_sponsored": skipped, "videos": videos}