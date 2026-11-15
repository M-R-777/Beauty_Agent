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
from price_sources import STORE_LABELS, get_offers, usable_url
from setup_db import create_database
from shopping_agent import REFUSAL, agent, is_beauty_shopping

st.set_page_config(page_title="Beauty Shopping Assistant", page_icon="💄", layout="wide")
create_database()  # safe to call every run; creates missing tables only

if "messages" not in st.session_state:
    st.session_state.messages = []


def scroll_to_bottom() -> None:
    """Scroll the chat to the newest message (Streamlit has no built-in way to do this)."""
    components.html(
        """<script>
        const doc = window.parent.document;
        const el = doc.querySelector('[data-testid="stMain"]') || doc.querySelector('section.main');
        if (el) { setTimeout(() => el.scrollTo({top: el.scrollHeight, behavior: 'smooth'}), 150); }
        </script>""",
        height=0,
    )


def run_agent(skip_gate: bool = False) -> tuple[str, list[str]]:
    """Gate first, then agent. Returns (reply text, products to show as cards)."""
    if not skip_gate and not is_beauty_shopping(st.session_state.messages):
        return REFUSAL, []

    # Only send recent history: long chats exceed Groq's free-tier token limit
    recent = [{"role": m["role"], "content": m["content"]} for m in st.session_state.messages[-8:]]
    result = agent.invoke({"messages": recent})
    msgs = result["messages"]

    # Find which products the agent compared this turn, so we can show cards for them
    last_human = max((i for i, m in enumerate(msgs) if getattr(m, "type", "") == "human"), default=-1)
    products: list[str] = []
    for m in msgs[last_human + 1:]:
        if getattr(m, "type", "") == "tool" and getattr(m, "name", "") == "compare_prices":
            try:
                name = json.loads(m.content).get("product")
            except Exception:
                continue
            if name and name not in products:
                products.append(name)
    return msgs[-1].content.replace("`", ""), products


# ---------------------------------------------------------------------------
# Product cards (Google Shopping style)
# ---------------------------------------------------------------------------
def render_cards(product: str, key_prefix: str) -> None:
    data = get_offers(product)  # served from cache, no new API call
    offers = cart.visible_offers(data)  # follows the sidebar toggle: top 3 cheapest, or the one chosen app
    st.markdown(f"**{product.title()}**")
    if not offers and price_sources.TRACE.get(data["name"]):
        with st.expander("Search details"):
            st.code("\n".join(price_sources.TRACE[data["name"]]), language=None)
    if data.get("error"):
        st.error(f"Couldn't fetch prices right now: {data['error']}")
        return
    if not offers:
        if data["offers"] and cart.get_setting("cart_mode") == "single":
            store = STORE_LABELS.get(cart.get_setting("single_store"), "the selected app")
            st.info(f"Not available on {store}. Switch the cart mode to 'Cheapest across apps' to see other stores.")
        else:
            st.info("No store lists this product right now.")
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
                    if st.button("Add to cart", key=f"{key_prefix}_{o['store']}", use_container_width=True):
                        if cart.add_offer(data["product_id"], o["store"]):
                            st.toast(f"Added to your {label} cart")


# ---------------------------------------------------------------------------
# Sidebar: screen picker, cart toggle, shop by image
# ---------------------------------------------------------------------------
if "goto" in st.session_state:  # lets a button on one screen jump to another
    st.session_state["screen"] = st.session_state.pop("goto")

with st.sidebar:
    screen = st.radio("Screen", ["Chat", "Looks", "Cart"], horizontal=True, key="screen")

    st.header("Cart mode")
    mode_label = st.radio(
        "Where should items go?",
        ["Cheapest across apps", "One app only"],
        index=0 if cart.get_setting("cart_mode") == "multi" else 1,
    )
    cart.set_setting("cart_mode", "multi" if mode_label == "Cheapest across apps" else "single")

    if mode_label == "One app only":
        stores = list(STORE_LABELS)
        current = cart.get_setting("single_store")
        chosen = st.selectbox(
            "Which app?", stores,
            index=stores.index(current) if current in stores else 0,
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
# Cart screen
# ---------------------------------------------------------------------------
def show_cart() -> None:
    st.title("🛍️ Your carts")
    data = cart.get_cart()
    if not data["stores"]:
        st.info("Your cart is empty. Ask the assistant to add something.")
        return

    for store, group in data["stores"].items():
        with st.container(border=True):
            st.subheader(f"{group['label']}  ·  ₹{group['total']:,.0f}")
            for item in group["items"]:
                line = f"{item['qty']} × {item['name'].title()} — ₹{item['price']:,.0f}"
                if item["url"]:
                    line += f"  ([view product]({item['url']}))"
                st.markdown(line)
            if group.get("bulk_link"):
                st.link_button(f"Open {group['label']} cart with all items", group["bulk_link"])
            else:
                st.caption(f"{group['label']} has no one-click cart link; open each product above and tap Add to Bag.")
    st.metric("Grand total", f"₹{data['grand_total']:,.0f}")
    st.caption("Prices come from Google Shopping and can differ slightly on the store.")
    if st.button("Clear cart"):
        cart.clear_cart()
        st.rerun()


# ---------------------------------------------------------------------------
# Looks screen
# ---------------------------------------------------------------------------
def show_looks() -> None:
    st.title("🎨 Looks")
    st.caption("Drop a makeup video link and I'll pull out the products used. Save it, then add the whole look to your cart.")

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
                    st.warning("Saved the look, but I couldn't find any products in the description or "
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
    st.title("💄 Beauty Shopping Assistant")
    st.caption("Skincare, haircare and makeup: compare prices across apps and build your cart.")

    for idx, msg in enumerate(st.session_state.messages):
        with st.chat_message(msg["role"]):
            if msg["role"] == "user" and msg["content"].startswith("I uploaded a product image"):
                st.markdown(f"Searching by image: **{os.path.basename(msg['content'].split('Image path:')[-1].strip())}**")
            else:
                st.markdown(msg["content"].replace("$", r"\$"))
            for product in msg.get("products", []):
                render_cards(product, f"m{idx}_{product}")

    if st.session_state.get("pending_image") or st.session_state.get("pending_prompt"):
        with st.chat_message("assistant"):
            with st.spinner("Comparing prices…"):
                response, products = run_agent(skip_gate=True)  # these prompts come from the app itself
        st.session_state.messages.append({"role": "assistant", "content": response, "products": products})
        st.session_state.scroll = True
        st.session_state.pop("pending_image", None)
        st.session_state.pop("pending_prompt", None)
        st.rerun()

    if prompt := st.chat_input("e.g. Compare Maybelline Fit Me foundation across apps"):
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
elif screen == "Looks":
    show_looks()
else:
    show_chat()