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
import skin_quiz
from price_sources import STORE_LABELS, get_offers_with_fallback

load_dotenv()

TEXT_MODEL = os.getenv("GROQ_TEXT_MODEL", "openai/gpt-oss-120b")
VISION_MODEL = os.getenv("GROQ_VISION_MODEL", "qwen/qwen3.8-27b")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

llm = ChatGroq(model=TEXT_MODEL, temperature=0.3)
vision_llm = ChatGroq(model=VISION_MODEL, temperature=0)

SCOPE = ("over-the-counter (OTC) cosmetic skincare, haircare, makeup, fragrance, body care, bath products, "
         "nail care, grooming (trimmers, razors, epilators, hair dryers, straighteners) and feminine hygiene")
REFUSAL = ("Aww, I'd love to help, but I'm strictly your beauty & personal-care shopping bestie! ✨ "
           "Ask me about skincare routines, makeup, haircare, reviews, or finding the best prices across Nykaa, Tira, and Amazon.")
ALLOWED_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}


# ---------------------------------------------------------------------------
# 1. Domain Guardrail Gate
# ---------------------------------------------------------------------------
GATE_PROMPT = (
    "You are a topic classifier for an Indian OTC beauty & personal-care shopping assistant (Nykaa/Tira/Amazon).\n"
    f"IN SCOPE: {SCOPE}, skin-type routines, swapping/adding/removing routine steps, beauty tools, greetings, and cart actions.\n"
    "OUT OF SCOPE: prescription medicines, dermatological ointments/steroids, oral pills/supplements, or non-beauty topics.\n"
    "Judge the LAST user message using prior context for short follow-ups ('hi', 'add a serum', 'swap the sunscreen', 'remove that').\n"
    "Answer with one word only: ALLOW or BLOCK.\n\n"
)


def is_beauty_shopping(messages: list[dict]) -> bool:
    """Verify the user's message is within the OTC beauty/personal-care shopping domain."""
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
def get_skin_profile() -> str:
    """Read the user's saved skin type, sensitivity level, primary skincare goal, and routine budget (in ₹)."""
    profile = skin_quiz.get_skin_profile()
    if not profile:
        return json.dumps({"has_profile": False, "message": "User has not completed the Skin Quiz tab yet."})
    return json.dumps({"has_profile": True, **profile})


@tool
def compare_prices(product: str, product_type: str = "", store: str = "") -> str:
    """Compare prices of a beauty product across trusted Indian stores (or check one specific store if named)."""
    query = product.strip()
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
    "You are a beauty product packaging & visual recognition expert. "
    "Examine the bottle/tube shape, cap design, logo, and all visible text carefully (e.g., silver metallic cap with '3D HYDRA' is KIKO Milano 3D Hydra Lipgloss). "
    "Return ONLY JSON: "
    '{"brand": str|null, "brand_readable": bool, "product_name": str|null, '
    '"product_type": str, "variant": str|null, "search_query": "<clean brand + product name WITHOUT shade numbers or ml size>"}. '
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
        get_skin_profile, compare_prices, add_to_cart, remove_from_cart,
        view_cart, get_reddit_reviews, get_youtube_reviews, describe_product_image,
    ],
    model=llm,
    system_prompt=(
        f"You are Skipia (Skipia.ai), a warm, upbeat, and helpful beauty & skincare shopping bestie for India ({SCOPE}, prices in ₹).\n\n"
        "TONE & PERSONALITY:\n"
        "- Sound friendly, natural, and encouraging—like a knowledgeable friend who loves skincare and saving money (use light, tasteful emojis like ✨, 🧴, 💖).\n"
        "- If the user simply greets you ('hi', 'hello', 'hey'), welcome them warmly as their all-in-one skincare, makeup & haircare shopping assistant and mention they can compare prices, check Reddit/YouTube reviews, or take the Skin Quiz.\n"
        "- Keep replies concise and easy to scan—never ramble.\n\n"
        f'SCOPE & SAFETY: Only recommend everyday cosmetic OTC products (cleansers, toners/serums, moisturizers, sunscreens, hair/makeup items). '
        f'NEVER recommend prescription medications, medicated dermatological ointments/steroids, or oral supplements/tablets. '
        f'For out-of-scope requests reply only with: "{REFUSAL}"\n\n'
        "SKIN ROUTINES & BUDGET PLANNING:\n"
        "- Call get_skin_profile first, then call get_reddit_reviews ONCE for '<skin_type> skincare routine IndianSkincareAddicts'.\n"
        "- Do NOT call get_youtube_reviews during routine building.\n"
        "- Choose 3 budget-conscious, well-known Indian OTC products upfront (1. Cleanser, 2. Moisturizer, 3. Sunscreen) "
        "whose combined price comfortably fits under the user's budget, and call compare_prices ONCE per product (3 calls max, no swaps/retries).\n"
        "- Present each step warmly, why Reddit loves it, its lowest price, and the total routine cost vs. budget.\n\n"
        "ROUTINE CUSTOMIZATION IN CHAT (SWAPS, ADDITIONS & REMOVALS):\n"
        "- When the user asks to swap a routine product (e.g., 'that sunscreen clogs my pores, suggest something in the same budget'), "
        "add a step (e.g., 'what if I want to add a serum?'), or remove a step from the routine discussed in recent messages:\n"
        "  1. Keep all unchanged steps and their prices from the conversation—do NOT re-search products that were already priced.\n"
        "  2. Respect the user's constraint (e.g., non-comedogenic/gel texture, same price range, or remaining budget from get_skin_profile).\n"
        "  3. Call compare_prices ONLY ONCE for the new/replacement OTC product.\n"
        "  4. Show the updated routine list, the new product's price, and the recalculated total vs. their budget (and offer to add it to cart).\n\n"
        "PRICES: Call compare_prices. If the user asks about a specific app/store (like Nykaa or Tira), pass that store name. "
        "Product cards appear under your reply, so for single-product lookups write 1–2 friendly sentences highlighting the best deal and savings. "
        "If exact_match is false, mention closest_match. NEVER invent or guess approximate prices or Markdown tables if offers is empty.\n\n"
        "REVIEWS: ONLY call get_reddit_reviews or get_youtube_reviews when the user explicitly asks for reviews, feedback, pros/cons, or whether a product is good. NEVER call review tools during price checks or image lookups. When asked, summarize pros, cons, and consensus with clickable Markdown links like [Title](https://...). Mention skipped sponsored videos.\n\n"
        "with clickable Markdown links like [Title](https://...). Mention skipped sponsored videos.\n\n"
        "IMAGES: Call describe_product_image first, then IMMEDIATELY call compare_prices with its search_query (brand + product name only, no shade code).\n\n"
        "CART: Call add_to_cart when requested. If the user specifies an app, pass store='nykaa'/'tira'/'amazon'; otherwise leave "
        "store empty so it defaults to Amazon. Confirm warmly which store it went to and the price. You can NEVER place an order or pay.\n\n"
        "Use plain text and Markdown links only: no tables, HTML tags, or code blocks."
    ),
)