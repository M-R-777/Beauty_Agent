"""FastAPI wrapper exposing Skipia.ai's Python backend to the React frontend."""
import base64
import json
import os
import re
import tempfile
import traceback

from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI, File, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import requests

import cart
import looks
import skin_quiz
from price_sources import STORE_LABELS, get_offers, usable_url
from setup_db import create_database
from shopping_agent import GEMINI_MODEL, REFUSAL, agent, is_beauty_shopping

create_database()
skin_quiz.ensure_profile_table()
os.makedirs(looks.RESOURCES, exist_ok=True)

app = FastAPI(title="Skipia.ai API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
app.mount("/resources", StaticFiles(directory=looks.RESOURCES), name="resources")


class ChatRequest(BaseModel):
    messages: list[dict]
    skip_gate: bool = False
    cart_mode: str | None = None
    single_store: str | None = None


class OfferCartRequest(BaseModel):
    product_id: int
    store: str
    qty: int = 1


class SettingsRequest(BaseModel):
    cart_mode: str
    single_store: str


class QuizRequest(BaseModel):
    q1: str
    q2: str
    q3: str
    q4: str
    concern: str
    budget: int


class LookImportRequest(BaseModel):
    url: str = ""
    pasted: str = ""


class LookUpdateRequest(BaseModel):
    name: str
    queries: list[str]


def _serialize_offers(product: str, req_store: str = "") -> dict:
    single = cart.get_setting("cart_mode") == "single"
    selected_store = cart.get_setting("single_store") if single else ""
    target = req_store or selected_store
    data = get_offers(product, target_store=target)
    offers = cart.visible_offers(data, requested_store=req_store)
    formatted = [
        {
            "store": o["store"],
            "label": STORE_LABELS.get(o["store"], o["store"]),
            "price": o["price"],
            "title": o["title"],
            "thumbnail": o.get("thumbnail"),
            "url": usable_url(o["store"], o["title"], o.get("url")),
        }
        for o in offers
    ]
    available_other = []
    if not formatted and data.get("offers") and target:
        available_other = [STORE_LABELS.get(s, s) for s in data["offers"].keys()]

    return {
        "product_id": data["product_id"],
        "name": data["name"],
        "match_title": data.get("match_title"),
        "exact": data.get("exact", True),
        "is_mock": data.get("is_mock", False),
        "error": data.get("error"),
        "requested_store": req_store,
        "selected_store_label": STORE_LABELS.get(target.lower(), target) if target else "",
        "available_other": available_other,
        "offers": formatted,
    }


@app.get("/api/product")
def get_product_offers(name: str, store: str = ""):
    """Live re-fetch/filter endpoint when user toggles 'One app only' or changes store."""
    return _serialize_offers(name, store)


@app.post("/api/chat")
def chat_endpoint(req: ChatRequest):
    try:
        # Always sync sidebar cart_mode & single_store before invoking the agent
        if req.cart_mode in ("multi", "single"):
            cart.set_setting("cart_mode", req.cart_mode)
        if req.single_store:
            cart.set_setting("single_store", req.single_store)

        if not req.skip_gate and not is_beauty_shopping(req.messages):
            return {"reply": REFUSAL, "cards": []}

        recent = [{"role": m["role"], "content": m["content"]} for m in req.messages[-8:]]
        result = agent.invoke({"messages": recent}, {"recursion_limit": 12})
        msgs = result["messages"]

        last_human = max((i for i, m in enumerate(msgs) if getattr(m, "type", "") == "human"), default=-1)
        cards = []
        seen = set()
        for m in msgs[last_human + 1 :]:
            if getattr(m, "type", "") == "tool" and getattr(m, "name", "") in ("compare_prices", "compare_routine_prices"):
                try:
                    payload = json.loads(m.content)
                    if isinstance(payload, list):
                        # It's the routine tool returning multiple products
                        for item in payload:
                            name = item.get("product")
                            if name and (name, "") not in seen:
                                seen.add((name, ""))
                                cards.append(_serialize_offers(name, ""))
                    else:
                        # It's the standard single product tool
                        name = payload.get("product")
                        req_store = payload.get("requested_store") or ""
                        if name and (name, req_store) not in seen:
                            seen.add((name, req_store))
                            cards.append(_serialize_offers(name, req_store))
                except Exception:
                    continue

        reply = (msgs[-1].content or "").replace("`", "")
        return {"reply": reply, "cards": cards}
    except Exception as e:
        traceback.print_exc()
        return JSONResponse(
            status_code=500,
            content={"reply": f"Backend error: {type(e).__name__}: {e}", "cards": []},
        )


def _identify_image_like_lens(file_bytes: bytes, filename: str) -> str | None:
    """Identify the exact beauty product from an image (Simplified Gemini acting like Google Lens)."""
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return None
    ext = os.path.splitext(filename or "upload.jpg")[1].lower()
    mime = "image/jpeg" if ext in (".jpg", ".jpeg", "") else f"image/{ext.lstrip('.')}"
    b64 = base64.b64encode(file_bytes).decode()
    
    # Tell Gemini to return plain text, exactly like Google Lens does
    prompt = (
        "Act like Google Lens. Identify the exact beauty/skincare/haircare/makeup product shown in this image. "
        "Read the exact brand and product name on the packaging. "
        "Return ONLY a plain text string of the brand and product name, e.g. 'L'Oreal Paris Extraordinary Oil Serum'. "
        "Do not return JSON. Do not return any other text. If not a beauty product, return 'unsupported'."
    )
    
    try:
        resp = requests.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent",
            headers={"x-goog-api-key": api_key, "Content-Type": "application/json"},
            json={"contents": [{"parts": [{"inline_data": {"mime_type": mime, "data": b64}}, {"text": prompt}]}]},
            timeout=30,
        )
        resp.raise_for_status()
        text = resp.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
        
        if text.lower() == "unsupported" or not text:
            return None
            
        return text # Passes the string directly to price_sources.py!
    except Exception:
        pass
    return None


@app.post("/api/upload-image")
async def upload_image(file: UploadFile = File(...)):
    raw = await file.read()
    suffix = os.path.splitext(file.filename or "upload.jpg")[1] or ".jpg"
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp.write(raw)
        tmp_path = tmp.name

    detected_query = _identify_image_like_lens(raw, file.filename or "upload.jpg")
    return {
        "image_path": tmp_path,
        "filename": file.filename,
        "detected_query": detected_query,
    }


@app.get("/api/settings")
def get_settings():
    return {
        "cart_mode": cart.get_setting("cart_mode"),
        "single_store": cart.get_setting("single_store"),
        "stores": STORE_LABELS,
    }


@app.post("/api/settings")
def update_settings(req: SettingsRequest):
    cart.set_setting("cart_mode", req.cart_mode)
    cart.set_setting("single_store", req.single_store)
    return {"ok": True, "cart_mode": req.cart_mode, "single_store": req.single_store}


@app.get("/api/cart")
def get_cart():
    return cart.get_cart()


@app.post("/api/cart/offer")
def add_offer_to_cart(req: OfferCartRequest):
    ok = cart.add_offer(req.product_id, req.store, req.qty)
    return {"ok": ok, "cart": cart.get_cart()}


@app.delete("/api/cart")
def clear_cart():
    cart.clear_cart()
    return {"ok": True, "cart": cart.get_cart()}


@app.get("/api/profile")
def get_profile():
    return {"profile": skin_quiz.get_skin_profile()}


@app.post("/api/profile")
def save_profile(req: QuizRequest):
    profile = skin_quiz.evaluate_skin_quiz(req.q1, req.q2, req.q3, req.q4, req.concern, req.budget)
    return {"profile": profile}


@app.delete("/api/profile")
def delete_profile():
    skin_quiz.delete_skin_profile()
    return {"ok": True}


@app.get("/api/looks")
def get_looks():
    out = []
    for lk in looks.list_looks():
        thumb = lk["thumbnail_path"]
        if thumb and os.path.isfile(thumb):
            thumb = f"http://localhost:8000/resources/{os.path.basename(thumb)}"
        out.append({**lk, "thumbnail_url": thumb, "items": looks.get_items(lk["id"])})
    return {"looks": out}


@app.post("/api/looks")
def create_look(req: LookImportRequest):
    return looks.import_look(req.url, req.pasted)


@app.put("/api/looks/{look_id}")
def edit_look(look_id: int, req: LookUpdateRequest):
    looks.update_look(look_id, req.name, req.queries)
    return {"ok": True}


@app.delete("/api/looks/{look_id}")
def remove_look(look_id: int):
    looks.delete_look(look_id)
    return {"ok": True}


@app.post("/api/looks/{look_id}/cart")
def add_look_cart(look_id: int):
    return {"results": looks.add_look_to_cart(look_id), "cart": cart.get_cart()}

class RemoveItemRequest(BaseModel):
    product: str
    store: str
    shade: str = ""
    size: str = ""

@app.post("/api/cart/remove")
def remove_cart_item(req: RemoveItemRequest):
    ok = cart.remove_item(req.product, req.store, req.shade, req.size)
    return {"ok": ok, "cart": cart.get_cart()}