import json
import os
import tempfile

from dotenv import load_dotenv

load_dotenv()

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components

import cart
import looks
import price_sources
import skin_quiz
from price_sources import STORE_LABELS, get_offers, usable_url
from setup_db import create_database
from shopping_agent import REFUSAL, agent, is_beauty_shopping

st.set_page_config(page_title="Skipia.ai · Beauty Assistant", page_icon="💄", layout="wide")
create_database()
skin_quiz.ensure_profile_table()

# Subtle frontend polish: cleaner card radius, refined spacing, and compact pills
st.markdown(
    """
    <style>
    .block-container { padding-top: 1.6rem; padding-bottom: 2.5rem; max-width: 1120px; }
    [data-testid="stVerticalBlockBorderWrapper"] {
        border-radius: 14px !important;
        transition: transform 0.15s ease, box-shadow 0.15s ease;
    }
    [data-testid="stVerticalBlockBorderWrapper"]:hover {
        box-shadow: 0 6px 20px rgba(0, 0, 0, 0.12);
    }
    .profile-pill {
        background: rgba(255, 105, 180, 0.12);
        border: 1px solid rgba(255, 105, 180, 0.35);
        border-radius: 10px;
        padding: 10px 12px;
        font-size: 0.86rem;
        line-height: 1.45;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

WELCOME_MSG = (
    "Hey there! ✨ I'm **Skipia**, your all-in-one skincare, haircare & makeup shopping bestie! 💖\n\n"
    "Here's what we can do together:\n"
    "- **Compare live prices** across Nykaa, Tira, Amazon, Myntra, Purplle & official brand sites\n"
    "- **Check real, unsponsored reviews** from Reddit & YouTube before you buy\n"
    "- **Build & customize a budget skincare routine** (take the **Skin Quiz** in the sidebar to start!)\n"
    "- **Shop by photo** or- **Import creator routines/looks from YouTube**\n"
    "\nWhat are we hunting for today? 🧴💄"
)

if "messages" not in st.session_state:
    st.session_state.messages = [{"role": "assistant", "content": WELCOME_MSG}]


def scroll_to_bottom() -> None:
    """Scroll the chat to the newest message."""
    components.html(
        """<script>
        const doc = window.parent.document;
        const el = doc.querySelector('[data-testid="stMain"]') || doc.querySelector('section.main');
        if (el) { setTimeout(() => el.scrollTo({top: el.scrollHeight, behavior: 'smooth'}), 150); }
        </script>""",
        height=0,
    )


def run_agent(skip_gate: bool = False) -> tuple[str, list[dict]]:
    """Gate first, then agent. Returns (reply text, list of {name, store} card specs)."""
    if not skip_gate and not is_beauty_shopping(st.session_state.messages):
        return REFUSAL, []

    recent = [{"role": m["role"], "content": m["content"]} for m in st.session_state.messages[-8:]]
    result = agent.invoke({"messages": recent}, {"recursion_limit": 12})
    msgs = result["messages"]

    last_human = max((i for i, m in enumerate(msgs) if getattr(m, "type", "") == "human"), default=-1)
    products: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for m in msgs[last_human + 1:]:
        if getattr(m, "type", "") == "tool" and getattr(m, "name", "") in ("compare_prices", "compare_routine_prices"):
            try:
                payload = json.loads(m.content)
                if isinstance(payload, list): 
                    # It's the compare_routine_prices tool
                    for item in payload:
                        name = item.get("product")
                        if name and (name, "") not in seen:
                            seen.add((name, ""))
                            products.append({"name": name, "store": ""})
                else: 
                    # It's the standard compare_prices tool
                    name = payload.get("product")
                    req_store = payload.get("requested_store") or ""
                    if name and (name, req_store) not in seen:
                        seen.add((name, req_store))
                        products.append({"name": name, "store": req_store})
            except Exception:
                continue
                
    return msgs[-1].content.replace("`", ""), products

# ---------------------------------------------------------------------------
# Product cards (Google Shopping style)
# ---------------------------------------------------------------------------
def render_cards(item: dict | str, key_prefix: str) -> None:
    product = item["name"] if isinstance(item, dict) else item
    req_store = item.get("store", "") if isinstance(item, dict) else ""

    target = req_store or (cart.get_setting("single_store") if cart.get_setting("cart_mode") == "single" else "")
    data = get_offers(product, target_store=target)
    offers = cart.visible_offers(data, requested_store=req_store)

    st.markdown(f"**{product.title()}**")
    if not offers and price_sources.TRACE.get(data["name"]):
        with st.expander("Search details"):
            st.code("\n".join(price_sources.TRACE[data["name"]]), language=None)
    if data.get("error"):
        st.error(f"Couldn't fetch prices right now: {data['error']}")
        return
    if not offers:
        if data["offers"] and (req_store or cart.get_setting("cart_mode") == "single"):
            store_key = (req_store or cart.get_setting("single_store")).lower()
            store_lbl = STORE_LABELS.get(store_key, req_store or "the selected app")
            st.info(f"Not available on {store_lbl}. Switch the cart mode to 'Cheapest across apps' to see other stores.")
        else:
            st.info("No trusted store lists this product right now.")
        return
    trace = price_sources.TRACE.get(data["name"])
    if trace:
        with st.expander("Search details"):
            st.code("\n".join(trace), language=None)
    if not data["exact"] and data["match_title"]:
        st.caption(f"Closest match found: {data['match_title']}")
    if data["is_mock"]:
        st.warning("Showing demo prices, not real ones. Reason: "
                   f"{price_sources.LAST_ERROR or 'unknown (restart the app and ask again)'}")
    for start in range(0, len(offers), 3):
        cols = st.columns(3)
        for j, o in enumerate(offers[start:start + 3]):
            label = STORE_LABELS.get(o["store"], o["store"])
            with cols[j]:
                with st.container(border=True):
                    if o.get("thumbnail"):
                        st.image(o["thumbnail"], use_container_width=True)
                    else:
                        st.markdown("<div style='font-size:64px;text-align:center'>🧴</div>",
                                    unsafe_allow_html=True)
                    st.markdown(f"**{label}**")
                    st.markdown(f"### ₹{o['price']:,.0f}")
                    st.caption((o["title"] or "")[:90])
                    link = usable_url(o["store"], o["title"], o.get("url"))
                    st.link_button(f"View on {label}", link, use_container_width=True)
                     # New inputs for Qty, Size, and Shade
                    q_col, s_col = st.columns([1, 2])
                    qty = q_col.number_input("Qty", min_value=1, max_value=10, value=1, key=f"qty_{key_prefix}_{o['store']}", label_visibility="collapsed")
                    size = s_col.text_input("Size", placeholder="Size/Vol (e.g. 50ml)", key=f"size_{key_prefix}_{o['store']}", label_visibility="collapsed")
                    
                    shade = st.text_input("Shade", placeholder="Shade/Variant (optional)", key=f"shade_{key_prefix}_{o['store']}", label_visibility="collapsed")

                    if st.button("Add to cart", key=f"btn_{key_prefix}_{o['store']}", use_container_width=True):
                        if cart.add_offer(data["product_id"], o["store"], qty, shade, size):
                            st.toast(f"Added to your {label} cart")


# ---------------------------------------------------------------------------
# Sidebar: navigation, saved skin badge, cart toggle, shop by image
# ---------------------------------------------------------------------------
if "goto" in st.session_state:
    st.session_state["screen"] = st.session_state.pop("goto")

with st.sidebar:
    st.markdown("## 💄 Skipia.ai")
    screen = st.radio("Navigate", ["Chat", "Skin Quiz", "Routines & Glam", "Cart"], horizontal=True, key="screen")

    saved_profile = skin_quiz.get_skin_profile()
    if saved_profile:
        st.markdown(
            f"<div class='profile-pill'>"
            f"<b>🧬 Your Skin Profile</b><br>"
            f"Type: <b>{saved_profile['skin_type']}</b> · Goal: {saved_profile['concern']}<br>"
            f"Routine Budget: <b>₹{saved_profile['budget']:,}</b>"
            f"</div>",
            unsafe_allow_html=True,
        )

    st.divider()
    st.header("Cart mode")
    mode_label = st.radio(
        "Where should items go?",
        ["Cheapest across apps", "One app only"],
        index=0 if cart.get_setting("cart_mode") == "multi" else 1,
    )
    cart.set_setting("cart_mode", "multi" if mode_label == "Cheapest across apps" else "single")

    if mode_label == "One app only":
        trusted_keys = ["amazon", "tira", "nykaa", "ajio", "purplle", "blinkit", "myntra", "bigbasket"]
        current = cart.get_setting("single_store")
        chosen = st.selectbox(
            "Which app?", trusted_keys,
            index=trusted_keys.index(current) if current in trusted_keys else 0,
            format_func=lambda s: STORE_LABELS[s],
        )
        cart.set_setting("single_store", chosen)

    st.divider()
    st.header("Shop by image")
    st.caption("Upload a product photo and I'll compare prices.")
    uploaded = st.file_uploader("Upload image", type=["jpg", "jpeg", "png", "webp"])
    if uploaded:
        st.image(uploaded, use_container_width=True)
    if uploaded and st.button("Find this product", use_container_width=True):
        suffix = os.path.splitext(uploaded.name)[1] or ".jpg"
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            tmp.write(uploaded.getvalue())
        st.session_state.messages.append({
            "role": "user",
            "content": f"I uploaded a product image. Please identify it and compare prices. Image path: {tmp.name}",
        })
        st.session_state.pending_image = True
        st.rerun()


# ---------------------------------------------------------------------------
# Skin Quiz & Budget OTC Routine Builder screen
# ---------------------------------------------------------------------------
def show_skin_quiz() -> None:
    st.title("🧬 Skin Type Quiz & Budget Routine Builder")
    st.caption(
        "Answer 5 quick questions to identify your skin type and build an over-the-counter (OTC) "
        "skincare routine backed by Reddit reviews within your budget."
    )

    existing = skin_quiz.get_skin_profile()

    with st.container(border=True):
        q1 = st.radio(
            "1. How does your bare skin feel 2 hours after washing (without moisturizer)?",
            [
                "Tight, dry, or slightly rough",
                "Shiny all over (forehead, cheeks, and chin)",
                "Shiny only in the T-zone (forehead/nose), normal or dry on cheeks",
                "Comfortable, balanced, neither tight nor greasy",
            ],
        )
        q2 = st.radio(
            "2. By midday, how does your face typically look?",
            [
                "Whole face looks greasy or dewy with visible pores",
                "Forehead & nose get shiny, cheeks stay matte",
                "Dull, dry, or occasionally flaky patches",
                "Even and comfortable throughout the day",
            ],
        )
        q3 = st.radio(
            "3. How often do you experience clogged pores or occasional breakouts?",
            [
                "Frequently (especially around T-zone or jawline)",
                "Occasionally (around weather changes or stress)",
                "Rarely or almost never",
            ],
        )
        q4 = st.radio(
            "4. How does your skin react to new skincare products or fragrance?",
            [
                "Easily turns red, stings, or feels irritated",
                "Sometimes reacts to strong actives",
                "Rarely reacts; tolerates most OTC products well",
            ],
        )

        c1, c2 = st.columns(2)
        with c1:
            concern = st.selectbox(
                "5. Primary cosmetic skincare goal",
                [
                    "Oil control & clogged pores",
                    "Deep hydration & barrier support",
                    "Brightening & uneven skin tone",
                    "Simple daily maintenance & sun protection",
                ],
            )
        with c2:
            budget = st.slider(
                "6. Total routine budget (₹)",
                min_value=800,
                max_value=5000,
                value=existing["budget"] if existing else 1500,
                step=100,
            )

        if st.button("Save Skin Profile", type="primary", use_container_width=True):
            profile = skin_quiz.evaluate_skin_quiz(q1, q2, q3, q4, concern, budget)
            st.success(f"Saved! Your evaluated skin type is **{profile['skin_type']}** (Budget: **₹{profile['budget']:,}**).")
            st.rerun()

    profile = skin_quiz.get_skin_profile()
    if profile:
        st.divider()
        with st.container(border=True):
            st.subheader(f"Your Saved Result: {profile['skin_type']} Skin")
            m1, m2, m3 = st.columns(3)
            m1.metric("Skin Type", profile["skin_type"])
            m2.metric("Primary Goal", profile["concern"].split("&")[0].strip())
            m3.metric("Max Budget", f"₹{profile['budget']:,}")
            st.caption(f"Sensitivity preference: {profile['sensitivity']} · OTC cosmetic products only.")

            b1, b2 = st.columns([3, 1])
            with b1:
                if st.button("✨ Build My Budget Routine (Reddit Backed)", type="primary", use_container_width=True):
                    prompt = (
                        f"Build a 3-step over-the-counter (OTC) daily skincare routine "
                        f"(cleanser, moisturizer, sunscreen) for my saved skin profile "
                        f"({profile['skin_type']} skin, goal: {profile['concern']}, sensitivity: {profile['sensitivity']}) "
                        f"under ₹{profile['budget']}. Check Reddit reviews once and compare real prices across trusted stores."
                    )
                    st.session_state.messages.append({"role": "user", "content": prompt})
                    st.session_state.pending_prompt = True
                    st.session_state.goto = "Chat"
                    st.rerun()
            with b2:
                if st.button("🗑️ Delete Skin Type", use_container_width=True):
                    skin_quiz.delete_skin_profile()
                    st.rerun()


# ---------------------------------------------------------------------------
# Cart screen
# ---------------------------------------------------------------------------
def show_cart() -> None:
    st.title("🛍️ Your carts")
    data = cart.get_cart()
    if not data["stores"]:
        st.info("Your cart is empty. Ask Skipia to add something!")
        return

    for store, group in data["stores"].items():
        with st.container(border=True):
            st.subheader(f"{group['label']}  ·  ₹{group['total']:,.0f}")
            
            for item in group["items"]:
                col1, col2 = st.columns([10, 1])
                line = f"{item['qty']} × {item['name'].title()} — ₹{item['price']:,.0f}"
                    
                variant_str = " / ".join(filter(None, [item.get('size'), item.get('shade')]))
                if variant_str:
                    line += f" · *{variant_str}*"
                        
                if item["url"]:
                    line += f"  ([view product]({item['url']}))"
                col1.markdown(line)
                    
                if col2.button("🗑️", key=f"del_{store}_{item['name']}_{item.get('size','')}_{item.get('shade','')}", help="Remove from cart"):
                    cart.remove_item(item["name"], store, item.get("shade", ""), item.get("size", ""))
                    st.rerun()
            
            # THIS IS THE MISSING PART
            if group.get("bulk_link"):
                st.link_button(f"Open {group['label']} cart with all items", group["bulk_link"])
            else:
                st.caption(f"{group['label']} has no one-click cart link; open each product above and tap Add to Bag.")


# ---------------------------------------------------------------------------
# Looks screen
# ---------------------------------------------------------------------------
def show_looks() -> None:
    st.title("🎨 Routines & Glam")
    st.caption("Drop a YouTube link or caption and Skipia will extract the products used. Save it, then add the whole routine or look to your cart.")

    with st.container(border=True):
        url = st.text_input("YouTube link (video or Short)", placeholder="https://www.youtube.com/watch?v=...")
        pasted = st.text_area(
            "Or paste the caption / product list (use this for Instagram)",
            height=100, placeholder="Copy the caption from the post and paste it here",
        )
        if st.button("Import look", type="primary"):
            if not url.strip() and not pasted.strip():
                st.warning("Paste a YouTube link or a caption first.")
            else:
                with st.spinner("Reading the video and finding products…"):
                    res = looks.import_look(url, pasted)
                if not res["ok"]:
                    st.error(res["error"])
                elif res["count"] == 0:
                    st.warning("Saved the look, but couldn't find any products in the description or "
                               "transcript. Open 'Edit' on it below to add them by hand.")
                else:
                    st.success(f"Saved a look with {res['count']} products (found in: {res['source']}).")

    for lk in looks.list_looks():
        lid = lk["id"]
        items = looks.get_items(lid)
        with st.container(border=True):
            c1, c2 = st.columns([1, 2])
            with c1:
                if lk["thumbnail_path"]:
                    st.image(lk["thumbnail_path"], use_container_width=True)
                else:
                    st.markdown("<div style='font-size:64px;text-align:center'>🎨</div>", unsafe_allow_html=True)
            with c2:
                st.subheader(lk["name"])
                meta = f"{len(items)} products"
                if lk["source_url"]:
                    meta += f"  ·  [source]({lk['source_url']})"
                st.caption(meta)
                for i, q in enumerate(items):
                    name_col, btn_col = st.columns([3, 1])
                    name_col.markdown(f"- {q}")
                    if btn_col.button("💰 Compare", key=f"cmp_{lid}_{i}", use_container_width=True,
                                      help=f"Compare prices for {q} in the chat"):
                        st.session_state.messages.append({"role": "user", "content": f"Compare prices for {q}"})
                        st.session_state.pending_prompt = True
                        st.session_state.goto = "Chat"
                        st.rerun()

                b1, b2 = st.columns(2)
                if b1.button("Add all to cart", key=f"addall_{lid}", use_container_width=True, disabled=not items):
                    bar = st.progress(0.0, text="Starting…")
                    results = looks.add_look_to_cart(
                        lid, progress=lambda i, n, q: bar.progress(i / n, text=f"Finding {q} ({i + 1}/{n})"))
                    bar.empty()
                    for r in results:
                        if r["ok"]:
                            st.markdown(f"✅ {r['query']} → **{r['store']}** ₹{r['price']:,.0f}")
                        else:
                            st.markdown(f"❌ {r['query']}: {r['error']}")
                    st.caption("Open the Cart screen to see everything.")
                if b2.button("Delete look", key=f"del_{lid}", use_container_width=True):
                    looks.delete_look(lid)
                    st.rerun()

                with st.expander("Edit name, screenshot and products"):
                    new_name = st.text_input("Look name", lk["name"], key=f"name_{lid}")
                    img = st.file_uploader("Replace screenshot", type=["jpg", "jpeg", "png", "webp"], key=f"img_{lid}")
                    edited = st.data_editor(pd.DataFrame({"Product": items}), num_rows="dynamic",
                                            key=f"ed_{lid}", use_container_width=True)
                    if st.button("Save changes", key=f"save_{lid}"):
                        looks.update_look(lid, new_name, edited["Product"].dropna().tolist(),
                                          (img.name, img.getvalue()) if img else None)
                        st.rerun()


# ---------------------------------------------------------------------------
# Chat screen
# ---------------------------------------------------------------------------
def show_chat() -> None:
    st.title("💄 Skipia.ai")
    st.caption("Your AI beauty & skincare shopping assistant · Compare prices across trusted apps, build budget routines, and fill your cart.")

    for idx, msg in enumerate(st.session_state.messages):
        with st.chat_message(msg["role"]):
            if msg["role"] == "user" and msg["content"].startswith("I uploaded a product image"):
                st.markdown(f"Searching by image: **{os.path.basename(msg['content'].split('Image path:')[-1].strip())}**")
            else:
                st.markdown(msg["content"].replace("$", r"\$"))
            for item in msg.get("products", []):
                prod_key = item["name"] if isinstance(item, dict) else item
                render_cards(item, f"m{idx}_{prod_key}")

    if st.session_state.get("pending_image") or st.session_state.get("pending_prompt"):
        with st.chat_message("assistant"):
            with st.spinner("Researching reviews & comparing live prices…"):
                response, products = run_agent(skip_gate=True)
        st.session_state.messages.append({"role": "assistant", "content": response, "products": products})
        st.session_state.scroll = True
        st.session_state.pop("pending_image", None)
        st.session_state.pop("pending_prompt", None)
        st.rerun()

    if prompt := st.chat_input("Ask Skipia to compare prices, check reviews, or build your routine..."):
        st.session_state.messages.append({"role": "user", "content": prompt})
        with st.chat_message("user"):
            st.markdown(prompt)
        scroll_to_bottom()
        with st.chat_message("assistant"):
            with st.spinner("Thinking…"):
                response, products = run_agent()
        st.session_state.messages.append({"role": "assistant", "content": response, "products": products})
        st.session_state.scroll = True
        st.rerun()

    if st.session_state.pop("scroll", False):
        scroll_to_bottom()


if screen == "Cart":
    show_cart()
elif screen == "Skin Quiz":
    show_skin_quiz()
elif screen == "Routines & Glam":
    show_looks()
else:
    show_chat()