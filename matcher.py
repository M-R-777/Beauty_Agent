"""Language-model judge: which Google Shopping listings are the product the shopper means?

Word matching cannot tell a hair serum from a shampoo of the same range, or a single
product from a combo pack. A language model reading the titles can.
"""
import json
import re

import llm_client

MAX_LISTINGS = 30

PROMPT = """You match online shopping listings to a beauty / personal-care product search.

SEARCH: "<<QUERY>>"
<<TYPE>>The search may contain spelling mistakes and may leave out the brand.

LISTINGS (index: title):
<<LINES>>

Rules:
1. BRAND: if the search names a brand, the listing must be that brand. Ignore spelling, punctuation and spacing ('elf' = 'e.l.f.', 'loreal' = "L'Oreal Paris"). If the search names no brand, any brand is fine.
2. PRODUCT LINE: if the search contains a specific product or line name (a proper name such as 'Extraordinary Oil', 'Fit Me', 'Glow Reviver', 'True Match'), the listing must be that same line. A different line from the same brand is NOT a match. Generic descriptions (skin tint, matte, hydrating, long wear) are not line names: for those, pick the closest product of the right brand and type.
3. TYPE AND FORM must match: a hair oil or serum is NOT a shampoo or conditioner; a compact powder is NOT a foundation; a liquid lip oil is NOT a lip oil stick; a lipstick is NOT a lip balm, unless the search asks for that.
4. A single product is NOT a combo, set, kit or multi-pack, unless the search asks for one.
5. If the search gives a shade, number or size, prefer the listing with it. If you can only find the right product in another shade or size, pick it and set exact to false.
6. Tolerate spelling mistakes everywhere.
7. "same" = every listing that is the SAME product as your pick (same brand, line, type, shade and size), from any store, including your pick.
8. If nothing fits rules 1 to 3, set best to null.

Return ONLY JSON, nothing else: {"best": <index or null>, "same": [<indexes>], "exact": <true if the pick is what the shopper asked for, false if it is only the closest alternative>}"""


def judge(query: str, listings: list[dict], product_type: str = ""):
    """Returns (anchor, same_listings, exact) or None if the model could not be used.

    anchor is None when nothing is a reasonable match.
    """
    items = listings[:MAX_LISTINGS]
    if not items:
        return None
    lines = "\n".join(f"{i}: {(l.get('title') or '')[:110]}" for i, l in enumerate(items))
    type_line = f'Product type the shopper means: "{product_type}".\n' if product_type else ""
    prompt = (PROMPT.replace("<<QUERY>>", query).replace("<<TYPE>>", type_line).replace("<<LINES>>", lines))
    try:
        out = llm_client.chat(prompt)
        data = json.loads(re.search(r"\{.*\}", out, flags=re.S).group())
        best = data.get("best")
        if best is None:
            return None, [], True  # the model says nothing here matches
        best = int(best)
        if not 0 <= best < len(items):
            return None
        same = {int(i) for i in (data.get("same") or []) if isinstance(i, (int, float)) and 0 <= int(i) < len(items)}
        same.add(best)
        return items[best], [items[i] for i in sorted(same)], bool(data.get("exact"))
    except Exception:
        return None