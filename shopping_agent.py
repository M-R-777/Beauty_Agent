"""Agentic AI orchestration layer using LangChain's ReAct tool-calling agent."""
import base64
import json
import os
import re
import tempfile

import requests
from dotenv import load_dotenv
from langchain.agents import create_agent
from langchain.tools import tool
from langchain_core.messages import HumanMessage
from langchain_groq import ChatGroq

import cart
import reviews
from price_sources import STORE_LABELS, get_offers_with_fallback

load_dotenv()

TEXT_MODEL = os.getenv("GROQ_TEXT_MODEL", "openai/gpt-oss-120b")
VISION_MODEL = os.getenv("GROQ_VISION_MODEL", "qwen/qwen3.8-27b")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

llm = ChatGroq(model=TEXT_MODEL, temperature=0)
vision_llm = ChatGroq(model=VISION_MODEL, temperature=0)

SCOPE = ("skincare, haircare, makeup, fragrance, body care, bath products, nail care, grooming "
         "(trimmers, razors, epilators, hair dryers, straighteners) and feminine hygiene")
REFUSAL = ("I can only help with shopping for beauty and personal-care products: skincare, haircare, "
           "makeup, fragrance, body care and grooming.")
ALLOWED_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}


# ---------------------------------------------------------------------------
# 1. Domain Guardrail Gate
# ---------------------------------------------------------------------------
GATE_PROMPT = (
    "You are a topic classifier for an Indian beauty & personal-care shopping assistant (Nykaa/Tira/Amazon).\n"
    f"IN SCOPE: {SCOPE}, beauty tools, and cart actions.\n"
    "Judge the LAST user message using prior context for short follow-ups ('add it', 'yes', 'check tira').\n"
    "Answer with one word only: ALLOW or BLOCK.\n\n"
)


def is_beauty_shopping(messages: list[dict]) -> bool:
    """Verify the user's message is within the beauty/personal-care shopping domain."""
    convo = "\n".join(f"{m['role']}: {m['content'][:300]}" for m in messages[-4:])
    try:
        out = llm.invoke(GATE_PROMPT + f"Conversation:\n{convo}").content
    except Exception:
        return True
    out = re.sub(r"<think>.*?</think>", "", out, flags=re.S).strip().upper()
    return out.startswith("ALLOW")


# ---------------------------------------------------------------------------
# 2. Agent Tools
# ---------------------------------------------------------------------------
@tool
def compare_prices(product: str, product_type: str = "", store: str = "") -> str:
    """Compare prices of a beauty product across trusted Indian stores (or check one specific store if named)."""
    query = product.strip()
    # Avoid appending generic category words like 'Skincare' that hurt exact search queries
    if product_type and product_type.lower() not in ("skincare", "makeup", "haircare", "beauty", "cosmetics"):
        if product_type.lower() not in query.lower():
            query = f"{query} {product_type.strip()}"

    single = cart.get_setting("cart_mode") == "single"
    selected_store = store.strip() or (cart.get_setting("single_store") if single else "")

    data = get_offers_with_fallback(query, product_type, target_store=selected_store)
    if data.get("error"):
        return json.dumps({"product": data["name"], "offers": [], "price_service_error": data["error"]})

    shown = cart.visible_offers(data, requested_store=store)
    result = {
        "product": data["name"],
        "requested_store": store or None,
        "mode": "single_app" if (single or store) else "all_apps",
        "is_demo_data": data["is_mock"],
        "exact_match": data["exact"],
        "closest_match": None if data["exact"] else data["match_title"],
        "offers": [{"store": STORE_LABELS.get(o["store"], o["store"]), "price": o["price"], "title": o["title"]} for o in shown],
    }
    if single or store:
        target_key = selected_store.lower()
        result["selected_store"] = STORE_LABELS.get(target_key, selected_store)
        if not shown and data["offers"]:
            result["available_on_other_stores"] = [STORE_LABELS.get(s, s) for s in data["offers"]]
    elif len(shown) > 1:
        same = shown[0]["price"] == shown[-1]["price"]
        result["same_price"] = same
        result["cheapest"] = None if same else STORE_LABELS.get(shown[0]["store"], shown[0]["store"])
    return json.dumps(result)


@tool
def add_to_cart(product: str, quantity: int = 1, store: str = "") -> str:
    """Add a product to a specific store's cart (e.g. 'amazon', 'nykaa', 'tira'); defaults to Amazon if unspecified."""
    return json.dumps(cart.add_item(product, quantity, store))


@tool
def remove_from_cart(product: str) -> str:
    """Remove a product from the cart by name."""
    return json.dumps({"removed": cart.remove_item(product)})


@tool
def view_cart() -> str:
    """View all items currently in the cart grouped by store with totals."""
    return json.dumps(cart.get_cart())


@tool
def get_reddit_reviews(product: str) -> str:
    """Fetch Reddit threads and comment mentions about a beauty or personal-care product."""
    return json.dumps(reviews.reddit_reviews(product))


@tool
def get_youtube_reviews(product: str) -> str:
    """Fetch non-sponsored YouTube review transcripts and summaries for a product."""
    return json.dumps(reviews.youtube_reviews(product))


VISION_PROMPT = (
    "Read all text on this beauty product packaging and return ONLY JSON: "
    '{"brand": str|null, "brand_readable": bool, "product_name": str|null, '
    '"product_type": str, "variant": str|null, "search_query": str}. '
    'If not a beauty/personal-care product, return {"product_type": "unsupported"}.'
)


@tool
def describe_product_image(image_path: str) -> str:
    """Identify a beauty product from an uploaded photo and return structured attributes."""
    real_path = os.path.realpath(image_path)
    temp_dir = os.path.realpath(tempfile.gettempdir())
    ext = os.path.splitext(real_path)[1].lower()
    if not real_path.startswith(temp_dir + os.sep) or ext not in ALLOWED_IMAGE_EXTS or not os.path.isfile(real_path):
        return json.dumps({"error": "Invalid image path."})

    with open(real_path, "rb") as f:
        image_data = base64.b64encode(f.read()).decode()
    mime = "image/jpeg" if ext in (".jpg", ".jpeg") else f"image/{ext.lstrip('.')}"

    if os.environ.get("GEMINI_API_KEY"):
        try:
            resp = requests.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent",
                headers={"x-goog-api-key": os.environ["GEMINI_API_KEY"], "Content-Type": "application/json"},
                json={"contents": [{"parts": [{"inline_data": {"mime_type": mime, "data": image_data}}, {"text": VISION_PROMPT}]}]},
                timeout=60,
            )
            resp.raise_for_status()
            return resp.json()["candidates"][0]["content"]["parts"][0]["text"]
        except Exception:
            pass

    try:
        msg = HumanMessage(content=[
            {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{image_data}"}},
            {"type": "text", "text": VISION_PROMPT},
        ])
        return vision_llm.invoke([msg]).content
    except Exception as e:
        return json.dumps({"error": f"Vision model unavailable: {type(e).__name__}"})


# ---------------------------------------------------------------------------
# 3. ReAct Agent Initialization
# ---------------------------------------------------------------------------
agent = create_agent(
    tools=[
        compare_prices, add_to_cart, remove_from_cart, view_cart,
        get_reddit_reviews, get_youtube_reviews, describe_product_image,
    ],
    model=llm,
    system_prompt=(
        f"You are a shopping assistant for beauty and personal-care products ({SCOPE}) in India (prices in ₹).\n\n"
        f'SCOPE: Only assist with beauty/personal-care shopping. For anything else reply only with: "{REFUSAL}"\n\n'
        "PRICES: Call compare_prices. If the user asks about a specific app/store (like Nykaa or Tira), pass "
        "that store name in the store argument. Product cards appear under your reply, so do NOT list stores or "
        "prices—write at most 2 short sentences stating the cheapest store and savings (or the selected store's price). "
        "If exact_match is false, mention closest_match. If offers is empty, say no trusted stores list it right now.\n\n"
        "REVIEWS: Call get_reddit_reviews and/or get_youtube_reviews. Summarize pros, cons, and user consensus "
        "with clickable Markdown links like [Title](https://...). Mention skipped sponsored videos.\n\n"
        "IMAGES: Call describe_product_image, then compare_prices using its search_query.\n\n"
        "CART: Call add_to_cart when requested. If the user specifies an app (e.g. 'add to Nykaa' or 'add to Amazon'), "
        "pass store='nykaa' / store='amazon'; if they don't specify an app, leave store empty so it defaults to Amazon. "
        "Report which store it went to and the price. You can NEVER place an order or pay. Never add to cart unless asked.\n\n"
        "Use plain text and Markdown links only: no tables, HTML tags, or code blocks."
    ),
)