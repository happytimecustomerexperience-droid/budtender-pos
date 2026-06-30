"""Cart-aware cross-sell — ONE-to-few complementary, cheaper, in-stock add-ons for the
anchor in the cart. Slim port of happytime-budtender/budtender/pairing.py adapted to our
catalog dicts: complement ladder + the customer's OWN co-purchase (replenishment recency)
+ taste affinity + price-fit + margin. No Numbers-Guard issue — every term is real data.

We deliberately skip happytime's nightly Redis co-purchase matrices (its Redis isn't shared
with this app); the ladder + history + affinity carry the recommendation honestly.
"""

from __future__ import annotations

import datetime

from . import ranking

# Add-on is a LIGHTER, cheaper category than the anchor (grab-and-go impulse). Keyed by OUR
# cat_keys; pre-rolls lead (highest attachment). Heavier/equal categories intentionally absent.
LADDER = {
    "flower": ["pre-rolls", "edibles", "tinctures"],
    "concentrate": ["pre-rolls", "edibles", "tinctures"],
    "vapes": ["pre-rolls", "edibles", "tinctures"],
    "pre-rolls": ["edibles", "tinctures"],
    "edibles": ["tinctures"],
    "tinctures": ["edibles"],
    "topicals": ["edibles", "tinctures"],
}
DEFAULT_COMPLEMENT = ["pre-rolls", "edibles"]

MAX_PAIR_PRICE_RATIO = 0.50    # hard gate: pair price <= 50% of the anchor
IDEAL_PAIR_PRICE_RATIO = 0.25  # impulse-price sweet spot the price-fit term peaks at
MIN_STOCK = 5                  # never pair anything with < 5 on the floor (owner policy)
RECENT_DAYS = 30
W_BASKET, W_CUSTOMER, W_LADDER, W_MARGIN, W_PRICEFIT = 0.40, 0.25, 0.15, 0.15, 0.25


def _f(v, d=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


def _history_index(profile):
    out = {}
    for h in (profile or {}).get("purchase_history") or []:
        if isinstance(h, dict):
            key = h.get("product_id") or h.get("sku")
            if key:
                out[str(key)] = h
    return out


def _copurchase_signal(pid, hist):
    """(score, reason_code) from the customer's own history — repeat buy or a lapsed favorite
    (the replenishment recency lever); a very-recent buy returns 0 (don't re-push it)."""
    h = hist.get(str(pid))
    if not h:
        return 0.0, ""
    if int(_f(h.get("times_bought"))) >= 2:
        return 1.0, "bought_2plus"
    last = h.get("last_bought_at")
    if last:
        try:
            d = datetime.date.fromisoformat(str(last)[:10])
            if (datetime.date.today() - d).days > RECENT_DAYS:
                return 0.7, "bought_before"
        except (ValueError, TypeError):
            pass
        return 0.0, ""
    return 0.4, "bought_before"


def _reason(code, anchor, cand):
    acat = (anchor.get("cat_label") or "this").lower()
    pcat = (cand.get("cat_label") or "add-on").lower()
    if code == "bought_2plus":
        return f"You grab this a lot — restock it alongside your {acat}."
    if code == "bought_before":
        return f"You've loved this before — pairs great with your {acat}."
    if code == "your_brand":
        return f"It's {cand.get('brand')} — right in your wheelhouse."
    if code == "your_lane":
        return f"Matches your taste and rounds out the {acat}."
    return f"An easy, cheaper {pcat} to round out your {acat}."


def pair_for(items, anchor, profile, n=3):
    """Up to `n` complementary add-ons for `anchor` (a catalog product dict), best-first.
    Each carries `why` + `pair_strength` ∈ [0,1]. [] when nothing lighter+cheaper is in stock."""
    if not anchor or not items:
        return []
    complements = LADDER.get(anchor.get("cat_key"), DEFAULT_COMPLEMENT)
    apr = _f(anchor.get("price")) or 1.0
    cands = [p for p in items
             if p.get("cat_key") in complements
             and _f(p.get("qty")) >= MIN_STOCK
             and str(p.get("product_id")) != str(anchor.get("product_id"))
             and 0 < _f(p.get("price")) <= MAX_PAIR_PRICE_RATIO * apr]
    if not cands:
        return []
    hist = _history_index(profile)
    margins = [_f(p.get("margin_pct")) for p in cands]
    m_lo, m_hi = min(margins), max(margins)
    span = (m_hi - m_lo) or 1.0

    scored = []
    for p in cands:
        margin_norm = (_f(p.get("margin_pct")) - m_lo) / span
        lr = 1 - complements.index(p["cat_key"]) / max(len(complements), 1)
        if p.get("subcategory") and anchor.get("subcategory") and p["subcategory"] == anchor["subcategory"]:
            lr *= 0.5  # prefer a different size/format, not a near-dupe
        price_fit = max(0.0, 1 - abs(_f(p.get("price")) / apr - IDEAL_PAIR_PRICE_RATIO) / IDEAL_PAIR_PRICE_RATIO)
        basket, co_reason = _copurchase_signal(p.get("product_id"), hist)
        cust = (0.6 * ranking.affinity_score(p, profile) + 0.4 * ranking.quality_fit(p, profile)) if profile else 0.0
        score = (W_BASKET * basket + W_CUSTOMER * cust + W_LADDER * lr
                 + W_MARGIN * margin_norm + W_PRICEFIT * price_fit)
        if co_reason:
            reason = co_reason
        elif profile and p.get("brand") and _f((profile.get("brand_affinity") or {}).get(p["brand"])) >= 0.25:
            reason = "your_brand"
        elif profile and ranking.affinity_score(p, profile) >= 0.3:
            reason = "your_lane"
        else:
            reason = "pairs_well"
        q = dict(p)
        q["why"] = _reason(reason, anchor, p)
        q["pair_strength"] = round(min(1.0, 0.45 * basket + 0.35 * cust + 0.20 * price_fit
                                       + (0.15 if co_reason else 0.0)), 3)
        scored.append((score, q))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [q for _, q in scored[:n]]
