import { useEffect, useRef, useState } from "react";
import {
  Camera, ChevronDown, ExternalLink, MessageCircle, Palette, Send, ShoppingBag, Sparkles, Trash2, UploadCloud,
} from "lucide-react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

const API = "http://localhost:8000";

const WELCOME_MSG =
  "Hey there! ✨ I'm **Skipia**, your AI beauty & personal-care shopping assistant. 💖\n\n" +
  "Here's what I can do for you:\n" +
  "- **Compare live prices** across Nykaa, Tira, Amazon, Myntra, Purplle & official brand sites\n" +
  "- **Check unsponsored reviews** from Reddit & YouTube before you buy\n" +
  "- **Build & customize a budget skincare routine** (take the **Skin Quiz** in the sidebar to start!)\n" +
  "- **Shop by photo** or **import creator routines/looks from YouTube**\n\n" +
  "What are we hunting for today? 🧴💄";

function AeroSelect({ value, options, onChange }) {
  const [open, setOpen] = useState(false);
  const wrapRef = useRef(null);

  useEffect(() => {
    const handleOutside = (e) => {
      if (wrapRef.current && !wrapRef.current.contains(e.target)) setOpen(false);
    };
    document.addEventListener("mousedown", handleOutside);
    return () => document.removeEventListener("mousedown", handleOutside);
  }, []);

  const normalized = options.map((opt) => typeof opt === "string" ? { value: opt, label: opt } : opt);
  const current = normalized.find((o) => o.value === value) || normalized[0];

  return (
    <div className="aero-dropdown" ref={wrapRef}>
      <button type="button" className="aero-dropdown-trigger" onClick={() => setOpen((prev) => !prev)}>
        <span>{current?.label}</span>
        <ChevronDown size={16} style={{ transform: open ? "rotate(180deg)" : "rotate(0deg)" }} />
      </button>
      {open && (
        <div className="aero-dropdown-menu">
          {normalized.map((opt) => (
            <button key={opt.value} type="button" className={`aero-dropdown-option ${opt.value === value ? "active" : ""}`}
              onClick={() => { onChange(opt.value); setOpen(false); }}>
              {opt.label}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

export default function App() {
  const [tab, setTab] = useState("chat");
  const [messages, setMessages] = useState([{ role: "assistant", content: WELCOME_MSG, cards: [] }]);
  const [input, setInput] = useState("");
  const [loading, setLoading] = useState(false);
  const [toast, setToast] = useState("");

  const [settings, setSettings] = useState({ cart_mode: "multi", single_store: "nykaa", stores: {} });
  const [profile, setProfile] = useState(null);
  const [cartData, setCartData] = useState({ stores: {}, grand_total: 0 });
  const [looksList, setLooksList] = useState([]);

  const [imageFile, setImageFile] = useState(null);
  const [imagePreview, setImagePreview] = useState(null);
  const fileInputRef = useRef(null);

  const [q1, setQ1] = useState("Tight, dry, or slightly rough");
  const [q2, setQ2] = useState("Whole face looks greasy or dewy with visible pores");
  const [q3, setQ3] = useState("Frequently (especially around T-zone or jawline)");
  const [q4, setQ4] = useState("Easily turns red, stings, or feels irritated");
  const [concern, setConcern] = useState("Oil control & clogged pores");
  const [budget, setBudget] = useState(1500);

  const [lookUrl, setLookUrl] = useState("");
  const [lookPasted, setLookPasted] = useState("");
  const [importingLook, setImportingLook] = useState(false);

  const chatEndRef = useRef(null);

  const showToast = (msg) => { setToast(msg); setTimeout(() => setToast(""), 2800); };

  const refreshAll = async () => {
    try {
      const [sRes, pRes, cRes, lRes] = await Promise.all([
        fetch(`${API}/api/settings`).then((r) => r.json()),
        fetch(`${API}/api/profile`).then((r) => r.json()),
        fetch(`${API}/api/cart`).then((r) => r.json()),
        fetch(`${API}/api/looks`).then((r) => r.json()),
      ]);
      setSettings(sRes); setProfile(pRes.profile);
      if (pRes.profile?.budget) setBudget(pRes.profile.budget);
      setCartData(cRes); setLooksList(lRes.looks || []);
    } catch {}
  };

  useEffect(() => { refreshAll(); }, []);
  useEffect(() => { chatEndRef.current?.scrollIntoView({ behavior: "smooth" }); }, [messages, loading]);

  const sendMessage = async (text, skipGate = false, displayLabel = null) => {
    if (!text.trim() || loading) return;
    const userMsg = { role: "user", content: text, display: displayLabel || text };
    const nextHistory = [...messages, userMsg];
    setMessages(nextHistory); setInput(""); setTab("chat"); setLoading(true);

    try {
      const res = await fetch(`${API}/api/chat`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          messages: nextHistory.map((m) => ({ role: m.role, content: m.content })),
          skip_gate: skipGate, cart_mode: settings.cart_mode, single_store: settings.single_store,
        }),
      }).then((r) => r.json());

      setMessages((prev) => [...prev, { role: "assistant", content: res.reply, cards: res.cards || [] }]);
      const cRes = await fetch(`${API}/api/cart`).then((r) => r.json());
      setCartData(cRes);
    } catch {
      setMessages((prev) => [...prev, { role: "assistant", content: "Oops! Couldn't reach Skipia's backend. Start uvicorn.", cards: [] }]);
    } finally { setLoading(false); }
  };

  const handleFileSelect = (file) => { if (!file) return; setImageFile(file); setImagePreview(URL.createObjectURL(file)); };
  
  const handleImageSearch = async () => {
    if (!imageFile || loading) return;
    setLoading(true); setTab("chat");
    const fileToUpload = imageFile; setImageFile(null); setImagePreview(null);

    try {
      const form = new FormData(); form.append("file", fileToUpload);
      const data = await fetch(`${API}/api/upload-image`, { method: "POST", body: form }).then((r) => r.json());
      setLoading(false);
      if (data.detected_query) {
        await sendMessage(`Compare prices for ${data.detected_query}`, true, `📷 Visual search: ${data.detected_query}`);
      } else {
        await sendMessage(`I uploaded a product image. Please identify it and compare prices. Image path: ${data.image_path}`, true, `📷 Visual search: ${data.filename}`);
      }
    } catch { setLoading(false); showToast("Could not upload image."); }
  };

  const handleAddOffer = async (productId, store, label) => {
    const res = await fetch(`${API}/api/cart/offer`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ product_id: productId, store, qty: 1 }),
    }).then((r) => r.json());
    if (res.ok) { setCartData(res.cart); showToast(`Added to your ${label} cart 🫧`); }
  };

  const handleRemoveItem = async (productName, store, shade, size) => {
    const res = await fetch(`${API}/api/cart/remove`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ product: productName, store, shade: shade || "", size: size || "" }),
    }).then((r) => r.json());
    if (res.ok) { setCartData(res.cart); showToast("Item removed from cart"); }
  };

  const updateCartMode = async (cart_mode, single_store) => {
    setSettings((s) => ({ ...s, cart_mode, single_store }));
    await fetch(`${API}/api/settings`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ cart_mode, single_store }) });
    const hasCards = messages.some((m) => (m.cards || []).length > 0);
    if (!hasCards) return;
    const updated = await Promise.all(messages.map(async (m) => {
      if (!m.cards || m.cards.length === 0) return m;
      const refreshedCards = await Promise.all(m.cards.map((c) => fetch(`${API}/api/product?name=${encodeURIComponent(c.name)}&store=${encodeURIComponent(c.requested_store || "")}`).then((r) => r.json())));
      return { ...m, cards: refreshedCards };
    }));
    setMessages(updated);
  };

  const cartCount = Object.values(cartData.stores || {}).reduce((acc, g) => acc + g.items.reduce((s, it) => s + it.qty, 0), 0);
  const storeOptions = ["amazon", "tira", "nykaa", "ajio", "purplle", "blinkit", "myntra", "bigbasket"].map((k) => ({ value: k, label: settings.stores[k] || k }));

  return (
    <>
      <div className="aero-bg-orbs"><div className="aero-orb orb-1" /><div className="aero-orb orb-2" /><div className="aero-orb orb-3" /><div className="aero-orb orb-4" /></div>
      {toast && <div className="aero-glass" style={{ position: "fixed", top: 18, right: 22, zIndex: 99999, padding: "10px 18px", fontWeight: 700, color: "#065f46" }}>{toast}</div>}

      <div className="app-shell">
        <aside className="aero-glass" style={{ padding: 18, display: "flex", flexDirection: "column", gap: 16 }}>
          <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
            <div style={{ width: 44, height: 44, borderRadius: 15, background: "linear-gradient(180deg, #a5f3fc 0%, #38bdf8 48%, #0284c7 100%)", border: "2px solid #ffffff", display: "grid", placeItems: "center", fontSize: 22 }}>🫧</div>
            <div><h2 className="aero-title" style={{ fontSize: "1.5rem" }}>Skipia.ai</h2><div style={{ fontSize: "0.78rem", color: "var(--text-muted)", fontWeight: 700 }}>AI Beauty Assistant</div></div>
          </div>

          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 8 }}>
            <button className={`gel-btn ${tab === "chat" ? "gel-btn-lime" : "gel-btn-white"}`} onClick={() => setTab("chat")}><MessageCircle size={15} /> Chat</button>
            <button className={`gel-btn ${tab === "quiz" ? "gel-btn-lime" : "gel-btn-white"}`} onClick={() => setTab("quiz")}><Sparkles size={15} /> Skin Quiz</button>
            <button className={`gel-btn ${tab === "looks" ? "gel-btn-lime" : "gel-btn-white"}`} onClick={() => setTab("looks")}><Palette size={15} /> Routines & Glam</button>
            <button className={`gel-btn ${tab === "cart" ? "gel-btn-lime" : "gel-btn-white"}`} onClick={() => setTab("cart")}><ShoppingBag size={15} /> Cart ({cartCount})</button>
          </div>

          {profile && (
            <div style={{ padding: 12, borderRadius: 16, background: "rgba(236,253,245,0.95)", border: "1.5px solid #fff", fontSize: "0.84rem" }}>
              <div style={{ fontWeight: 800, marginBottom: 3 }}>🌿 Your Skin Profile</div>
              <div>Type: <strong>{profile.skin_type}</strong></div>
              <div>Goal: {profile.concern.split("&")[0].trim()}</div>
              <div>Budget: <strong>₹{profile.budget.toLocaleString()}</strong></div>
            </div>
          )}

          <div>
            <div style={{ fontWeight: 800, fontSize: "0.88rem", marginBottom: 6 }}>🛒 Shopping Mode</div>
            <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
              <label className={`aero-radio-pill ${settings.cart_mode === "multi" ? "selected" : ""}`} onClick={() => updateCartMode("multi", settings.single_store)}>
                <input type="radio" checked={settings.cart_mode === "multi"} readOnly /> Cheapest across apps
              </label>
              <label className={`aero-radio-pill ${settings.cart_mode === "single" ? "selected" : ""}`} onClick={() => updateCartMode("single", settings.single_store)}>
                <input type="radio" checked={settings.cart_mode === "single"} readOnly /> One app only
              </label>
              {settings.cart_mode === "single" && <AeroSelect value={settings.single_store} options={storeOptions} onChange={(val) => updateCartMode("single", val)} />}
            </div>
          </div>

          <div>
            <div style={{ fontWeight: 800, fontSize: "0.88rem", marginBottom: 6 }}><Camera size={14} style={{ verticalAlign: "middle" }} /> Shop by Image</div>
            <input ref={fileInputRef} type="file" accept="image/*" style={{ display: "none" }} onChange={(e) => handleFileSelect(e.target.files?.[0])} />
            {!imagePreview ? (
              <div className="aero-dropzone" onClick={() => fileInputRef.current?.click()} onDragOver={(e) => e.preventDefault()} onDrop={(e) => { e.preventDefault(); handleFileSelect(e.dataTransfer.files?.[0]); }}>
                <UploadCloud size={18} /><div style={{ fontWeight: 800, fontSize: "0.85rem" }}>Drop product photo</div>
              </div>
            ) : (
              <div style={{ padding: 10, borderRadius: 16, background: "rgba(255,255,255,0.85)" }}>
                <img src={imagePreview} alt="Preview" style={{ width: "100%", borderRadius: 11, maxHeight: 130, objectFit: "cover" }} />
                <div style={{ display: "flex", gap: 6, marginTop: 8 }}>
                  <button className="gel-btn gel-btn-lime" style={{ flex: 1, padding: "7px" }} onClick={handleImageSearch}>Find ✨</button>
                  <button className="gel-btn gel-btn-white" style={{ padding: "7px" }} onClick={() => { setImageFile(null); setImagePreview(null); }}>Clear</button>
                </div>
              </div>
            )}
          </div>
        </aside>

        <main style={{ display: "flex", flexDirection: "column", gap: 16 }}>
          <header className="aero-glass" style={{ padding: "16px 24px" }}>
            <h1 className="aero-title" style={{ fontSize: "1.9rem" }}>
              {tab === "chat" && "💄 Skipia.ai"}
              {tab === "quiz" && "🧬 Skin Type Quiz & Budget Routine Builder"}
              {tab === "looks" && "🎨 Routines & Glam"}
              {tab === "cart" && "🛍️ Your Carts"}
            </h1>
            <div style={{ color: "var(--text-muted)", fontWeight: 600, fontSize: "0.92rem" }}>
              {tab === "chat" && "Your AI beauty & skincare shopping assistant · Compare prices across trusted apps, build budget routines, and fill your cart."}
              {tab === "quiz" && "Answer 5 quick questions to identify your skin type and build an over-the-counter (OTC) skincare routine backed by Reddit reviews within your budget."}
              {tab === "looks" && "Drop a YouTube link or caption and Skipia will extract the products used. Save it, then add the whole routine or look to your cart."}
              {tab === "cart" && "View your grouped carts, see item totals, and open store bulk carts."}
            </div>
          </header>

          {tab === "chat" && (
            <section className="aero-glass" style={{ padding: 20, display: "flex", flexDirection: "column", gap: 14 }}>
              <div className="chat-scroll">
                {messages.map((m, idx) => (
                  <div key={idx} className={m.role === "user" ? "bubble-user" : "bubble-assistant"}>
                    {m.role === "user" ? <div>{m.display || m.content}</div> : (
                      <div className="aero-markdown">
                        <ReactMarkdown remarkPlugins={[remarkGfm]} components={{ a: ({ node, ...props }) => <a {...props} target="_blank" rel="noreferrer" /> }}>{m.content}</ReactMarkdown>
                        {(m.cards || []).map((card, cIdx) => (
                          <div key={cIdx} style={{ marginTop: 14 }}>
                            <div style={{ fontWeight: 800, color: "#0369a1", fontSize: "0.95rem" }}>💧 {card.name.toUpperCase()}</div>
                            {card.offers?.length > 0 ? (
                              <div className="offer-grid">
                                {card.offers.map((o) => (
                                  <div key={o.store} className="offer-card">
                                    {o.thumbnail ? <img src={o.thumbnail} alt={o.title} className="offer-thumb" /> : <div style={{ height: 125, background: "#e0f2fe", borderRadius: 12, display: "flex", alignItems: "center", justifyContent: "center", fontWeight: "bold" }}>✨ {o.label}</div>}
                                    <div style={{ fontWeight: 800, color: "#0284c7" }}>{o.label}</div>
                                    <div style={{ fontSize: "1.35rem", fontWeight: 800 }}>₹{Number(o.price).toLocaleString()}</div>
                                    <div style={{ fontSize: "0.8rem", color: "var(--text-muted)", marginBottom: 10 }}>{(o.title || "").slice(0, 75)}</div>
                                    <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
                                      <a href={o.url} target="_blank" rel="noreferrer" className="gel-btn">View on {o.label} <ExternalLink size={13} /></a>
                                      <button className="gel-btn gel-btn-lime" onClick={() => handleAddOffer(card.product_id, o.store, o.label)}>Add to Cart</button>
                                    </div>
                                  </div>
                                ))}
                              </div>
                            ) : <div style={{ fontSize: "0.85rem", color: "var(--text-muted)", marginTop: 4 }}>{card.error || "No offers found."}</div>}
                          </div>
                        ))}
                      </div>
                    )}
                  </div>
                ))}
                {loading && <div className="bubble-assistant" style={{ fontWeight: 700, color: "#0284c7" }}>🫧 Skipia is checking live prices...</div>}
                <div ref={chatEndRef} />
              </div>
              <form onSubmit={(e) => { e.preventDefault(); sendMessage(input); }} style={{ display: "flex", gap: 10 }}>
                <input className="aero-input" placeholder="Ask Skipia to compare prices..." value={input} onChange={(e) => setInput(e.target.value)} />
                <button type="submit" className="gel-btn gel-btn-lime" disabled={loading}><Send size={16} /> Send</button>
              </form>
            </section>
          )}

          {tab === "quiz" && (
            <section className="aero-glass" style={{ padding: 22, display: "flex", flexDirection: "column", gap: 14 }}>
              <div><label style={{ fontWeight: 800, display: "block", marginBottom: 6 }}>1. How does your bare skin feel 2 hours after washing?</label><AeroSelect value={q1} onChange={setQ1} options={["Tight, dry, or slightly rough", "Shiny all over", "Shiny only in the T-zone", "Comfortable, balanced"]} /></div>
              <div><label style={{ fontWeight: 800, display: "block", marginBottom: 6 }}>2. By midday, how does your face typically look?</label><AeroSelect value={q2} onChange={setQ2} options={["Whole face looks greasy", "Forehead & nose shiny", "Dull or flaky patches", "Even and comfortable"]} /></div>
              <div><label style={{ fontWeight: 800, display: "block", marginBottom: 6 }}>3. How often do you experience clogged pores or breakouts?</label><AeroSelect value={q3} onChange={setQ3} options={["Frequently", "Occasionally", "Rarely or almost never"]} /></div>
              <div><label style={{ fontWeight: 800, display: "block", marginBottom: 6 }}>4. How does your skin react to new skincare products or fragrance?</label><AeroSelect value={q4} onChange={setQ4} options={["Easily irritated", "Sometimes reacts", "Rarely reacts"]} /></div>
              <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12 }}>
                <div><label style={{ fontWeight: 800, display: "block", marginBottom: 6 }}>5. Primary cosmetic skincare goal</label><AeroSelect value={concern} onChange={setConcern} options={["Oil control & clogged pores", "Deep hydration & barrier support", "Brightening & uneven skin tone", "Simple daily maintenance & sun protection"]} /></div>
                <div><label style={{ fontWeight: 800, display: "block", marginBottom: 6 }}>6. Total routine budget: ₹{budget}</label><input type="range" min={800} max={5000} step={100} value={budget} onChange={(e) => setBudget(Number(e.target.value))} style={{ width: "100%", accentColor: "#0284c7", marginTop: 10 }} /></div>
              </div>
              <button className="gel-btn gel-btn-lime" onClick={async () => {
                const res = await fetch(`${API}/api/profile`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ q1, q2, q3, q4, concern, budget }) }).then((r) => r.json());
                setProfile(res.profile); showToast(`Saved ${res.profile.skin_type} profile! 🌿`);
              }}>Save Skin Profile ✨</button>

              {profile && (
                <div style={{ marginTop: 8, padding: 20, borderRadius: 18, background: "rgba(255,255,255,0.95)", display: "flex", flexDirection: "column", gap: 12 }}>
                  <h3 style={{ margin: 0, color: "#0369a1" }}>Your Saved Result: {profile.skin_type} Skin</h3>
                  <div style={{ display: "flex", gap: 24, flexWrap: "wrap", fontSize: "0.95rem" }}>
                    <div><strong>Skin Type:</strong> {profile.skin_type}</div>
                    <div><strong>Primary Goal:</strong> {profile.concern}</div>
                    <div><strong>Max Budget:</strong> ₹{profile.budget.toLocaleString()}</div>
                  </div>
                  <div style={{ fontSize: "0.85rem", color: "var(--text-muted)" }}>
                    Sensitivity preference: {profile.sensitivity} · OTC cosmetic products only.
                  </div>
                  <div style={{ display: "flex", gap: 8, marginTop: 8 }}>
                    <button className="gel-btn gel-btn-lime" onClick={() => sendMessage(`Build a 3-step over-the-counter (OTC) daily skincare routine (cleanser, moisturizer, sunscreen) for my saved skin profile (${profile.skin_type} skin, goal: ${profile.concern}, sensitivity: ${profile.sensitivity}) under ₹${profile.budget}. Check Reddit reviews once and compare real prices across trusted stores.`, true, `✨ Build my ₹${profile.budget.toLocaleString()} ${profile.skin_type} skincare routine`)}>✨ Build My Budget Routine</button>
                    <button className="gel-btn gel-btn-danger" onClick={async () => { await fetch(`${API}/api/profile`, { method: "DELETE" }); setProfile(null); showToast("Deleted profile"); }}><Trash2 size={15} /> Delete</button>
                  </div>
                </div>
              )}
            </section>
          )}

          {tab === "looks" && (
            <section style={{ display: "flex", flexDirection: "column", gap: 14 }}>
              <div className="aero-glass" style={{ padding: 20, display: "flex", flexDirection: "column", gap: 10 }}>
                <input className="aero-input" placeholder="YouTube link (video or Short)" value={lookUrl} onChange={(e) => setLookUrl(e.target.value)} />
                <textarea className="aero-textarea" rows={3} placeholder="Or paste the caption / product list" value={lookPasted} onChange={(e) => setLookPasted(e.target.value)} />
                <button className="gel-btn gel-btn-lime" disabled={importingLook} onClick={async () => {
                  setImportingLook(true);
                  const res = await fetch(`${API}/api/looks`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ url: lookUrl, pasted: lookPasted }) }).then((r) => r.json());
                  setImportingLook(false); if (!res.ok) showToast(res.error); else { setLookUrl(""); setLookPasted(""); showToast(`Imported ${res.count} products! 🎨`); refreshAll(); }
                }}>{importingLook ? "Extracting…" : "Import Look ✨"}</button>
              </div>
              {looksList.map((lk) => (
                <div key={lk.id} className="aero-glass" style={{ padding: 18, display: "grid", gridTemplateColumns: "160px 1fr", gap: 16 }}>
                  {lk.thumbnail_url ? <img src={lk.thumbnail_url} style={{ width: "100%", borderRadius: 14 }} /> : <div style={{ fontSize: 56, display: "grid", placeItems: "center" }}>🎨</div>}
                  <div>
                    <h3>{lk.name}</h3>
                    {lk.items.map((item, idx) => (
                      <div key={idx} style={{ display: "flex", justifyContent: "space-between", marginBottom: 4 }}>
                        <span>• {item}</span>
                        <button className="gel-btn gel-btn-white" style={{ padding: "4px 12px", fontSize: "0.8rem" }} onClick={() => sendMessage(`Compare prices for ${item}`, true)}>💰 Compare</button>
                      </div>
                    ))}
                    <button className="gel-btn gel-btn-danger" style={{ marginTop: 10 }} onClick={async () => { await fetch(`${API}/api/looks/${lk.id}`, { method: "DELETE" }); refreshAll(); }}>Delete Look</button>
                  </div>
                </div>
              ))}
            </section>
          )}

          {tab === "cart" && (
            <section style={{ display: "flex", flexDirection: "column", gap: 14 }}>
              {Object.keys(cartData.stores || {}).length === 0 ? <div className="aero-glass" style={{ padding: 24, fontWeight: 700 }}>Your cart is empty! Ask Skipia to add something 🫧</div> : (
                <>
                  {Object.entries(cartData.stores).map(([storeKey, grp]) => (
                    <div key={storeKey} className="aero-glass" style={{ padding: 20 }}>
                      <h3 style={{ marginTop: 0 }}>💧 {grp.label} · ₹{Number(grp.total).toLocaleString()}</h3>
                      
                      {grp.items.map((it, idx) => (
                        <div key={idx} style={{ display: "flex", justifyContent: "space-between", alignItems: "center", padding: "6px 0", borderBottom: "1px solid rgba(186, 230, 253, 0.4)" }}>
                          <div style={{ fontSize: "0.95rem" }}>
                            <strong>{it.qty} × {it.name}</strong> — ₹{Number(it.price).toLocaleString()}
                            {it.url && <a href={it.url} target="_blank" rel="noreferrer" style={{ marginLeft: 8, color: "#0284c7", fontWeight: "bold", textDecoration: "none" }}>(view product)</a>}
                          </div>
                          <button 
                            className="gel-btn gel-btn-danger" 
                            style={{ padding: "6px", borderRadius: "10px" }}
                            title="Remove from cart"
                            onClick={() => handleRemoveItem(it.name, storeKey, it.shade, it.size)}
                          >
                            <Trash2 size={16} />
                          </button>
                        </div>
                      ))}

                      <div style={{ marginTop: 14 }}>
                        {grp.bulk_link ? (
                          <a href={grp.bulk_link} target="_blank" rel="noreferrer" className="gel-btn gel-btn-lime">
                            Open {grp.label} Cart <ExternalLink size={14} />
                          </a>
                        ) : (
                          <div style={{ fontSize: "0.85rem", color: "var(--text-muted)", fontStyle: "italic" }}>
                            {grp.label} has no one-click cart link; open each product above and tap Add to Bag.
                          </div>
                        )}
                      </div>
                    </div>
                  ))}
                  <div className="aero-glass" style={{ padding: 18, display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                    <div style={{ fontSize: "1.3rem", fontWeight: 800 }}>Grand Total: ₹{Number(cartData.grand_total).toLocaleString()}</div>
                    <button className="gel-btn gel-btn-danger" onClick={async () => { const res = await fetch(`${API}/api/cart`, { method: "DELETE" }).then((r) => r.json()); setCartData(res.cart); showToast("Cleared"); }}>Clear Cart</button>
                  </div>
                </>
              )}
            </section>
          )}
        </main>
      </div>
    </>
  );
}