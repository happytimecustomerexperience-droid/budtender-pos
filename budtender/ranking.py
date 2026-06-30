"""Profile-aware product ranking — ported from happytime-budtender/budtender/ranking.py.

Operates on plain enriched product dicts (live product_SearchV2 rows ⋈ happytime
budtender_product enrichment) + a profile dict, so it has no Django-model dependency
and is trivially testable. Same formula: margin-first when anonymous, taste-first when
a customer profile is present. Used to sort each menu category "For You".

A product dict (normalized in catalog.py) uses keys:
  brand, category, subcategory, strain, strain_type, terpene, thc, price, price_was,
  margin_pct, price_z, bucket, velocity, qty, effects(list)
A profile dict uses:
  brand_affinity, category_affinity, strain_type_affinity, subcategory_affinity,
  terpene_affinity, bucket_mix, price_tier, novelty_score, purchase_history
"""

from __future__ import annotations

# Anonymous: margin-first. Known customer: taste leads, margin still matters.
W_ANON = {"margin": 0.55, "affinity": 0.0, "category": 0.05, "bucket": 0.12, "quality": 0.0, "budget": 0.10}
W_KNOWN = {"margin": 0.22, "affinity": 0.40, "category": 0.04, "bucket": 0.12, "quality": 0.14, "budget": 0.04}

BUCKET_NUDGE = {"profit": 1.0, "core": 0.4, "traffic": 0.0}
_TIER_CENTER = {"value": -0.6, "mid": 0.0, "top": 0.6}

# Live in-session taste -> the affinity dicts the ranker already reads. This is what makes
# EVERY customer's feed personalized: a new/guest/DB-down shopper has no persisted profile,
# but the moment they view or add anything this visit, the feed adapts.
_SESSION_AFF = {"category": "category_affinity", "brand": "brand_affinity",
                "strain_type": "strain_type_affinity", "flavor": "flavor_affinity"}
SESSION_WEIGHT = 0.6  # strong but not total — persisted taste still leads when present


def blend_session_taste(profile, taste):
    """Fold this visit's `taste` ({field: {name: count}}) into `profile`'s affinities.
    Returns `profile` unchanged when there's no taste; builds a profile from taste alone
    when there's no persisted profile (so ranking switches to the taste-first weights)."""
    if not taste or not any(taste.get(f) for f in _SESSION_AFF):
        return profile
    eff = dict(profile or {})
    for field, akey in _SESSION_AFF.items():
        counts = taste.get(field) or {}
        if not counts:
            continue
        mx = max(counts.values()) or 1
        merged = dict(eff.get(akey) or {})
        for name, c in counts.items():
            merged[name] = min(1.0, _f(merged.get(name)) + SESSION_WEIGHT * (c / mx))
        eff[akey] = merged
    return eff


def _f(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _aff(profile, key, val):
    if not val:
        return 0.0
    d = (profile or {}).get(key) or {}
    return _f(d.get(val), 0.0)


def _flavor_aff(profile, p):
    """Best match of the product's flavors against the customer's flavor_affinity."""
    fa = (profile or {}).get("flavor_affinity") or {}
    if not fa:
        return 0.0
    return max((_f(fa.get(fl)) for fl in (p.get("flavors") or []) if fl), default=0.0)


def thc_band_fit(p, profile):
    """1.0 when the product THC sits in the customer's usual band [thc_min, thc_max],
    fading over ~10pp outside it. 0 when the band or the product THC is unknown."""
    if not profile:
        return 0.0
    lo, hi, thc = profile.get("thc_min"), profile.get("thc_max"), p.get("thc")
    if lo is None or hi is None or thc in (None, ""):
        return 0.0
    lo, hi, thc = _f(lo), _f(hi), _f(thc)
    if lo > hi:
        lo, hi = hi, lo
    if lo <= thc <= hi:
        return 1.0
    dist = (lo - thc) if thc < lo else (thc - hi)
    return max(0.0, 1.0 - dist / 10.0)


def affinity_score(p, profile):
    if not profile:
        return 0.0
    s = 0.0
    s += 1.6 * _aff(profile, "brand_affinity", p.get("brand"))
    s += 1.0 * _aff(profile, "strain_type_affinity", p.get("strain_type"))
    # Match category under the raw Dutchie name OR the canonical cat_key — happytime's
    # category vocabulary may differ from the raw ProductCategory; trying both avoids a
    # silent 0 without re-keying or losing granularity when they already agree.
    s += 0.6 * max(_aff(profile, "category_affinity", p.get("category")),
                   _aff(profile, "category_affinity", p.get("cat_key")))
    s += 0.6 * _aff(profile, "subcategory_affinity", p.get("subcategory"))
    s += 0.4 * _aff(profile, "terpene_affinity", p.get("terpene"))
    s += 0.4 * _flavor_aff(profile, p)
    return min(s, 1.0)


def quality_fit(p, profile):
    tier = (profile or {}).get("price_tier")
    if not tier:
        return 0.0
    center = _TIER_CENTER.get(tier, 0.0)
    return 1.0 - min(abs(_f(p.get("price_z")) - center) / 2.0, 1.0)


def novelty_bias(p, profile):
    brand = p.get("brand")
    if not profile or not brand:
        return 0.0
    known = _aff(profile, "brand_affinity", brand) > 0
    nov = _f(profile.get("novelty_score"))
    return 0.3 * (1.0 - nov) if known else 0.3 * nov


def _recent(profile, top=3):
    hist = (profile or {}).get("purchase_history") or []
    items = [h for h in hist if isinstance(h, dict) and h.get("last_bought_at")]
    items.sort(key=lambda h: str(h.get("last_bought_at")), reverse=True)
    r = items[:top]
    return ({h.get("brand") for h in r if h.get("brand")},
            {h.get("category") for h in r if h.get("category")})


def score_product(p, profile, w, m_lo, m_hi, mid, recent_brands, recent_cats, price_sensitive):
    span = (m_hi - m_lo) or 1.0
    margin_norm = (_f(p.get("margin_pct")) - m_lo) / span
    price = _f(p.get("price"))
    budget_fit = 1 - min(abs(price - mid) / (mid or 1), 1)
    nudge = BUCKET_NUDGE.get(p.get("bucket"), 0.4)
    if p.get("bucket") == "traffic" and price_sensitive:
        nudge = 0.6
    bmix = (profile or {}).get("bucket_mix")
    if bmix:
        nudge = 0.6 * nudge + 0.4 * _f(bmix.get(p.get("bucket")))
    recency = (0.10 if p.get("brand") in recent_brands else 0.0) + \
              (0.05 if p.get("category") in recent_cats else 0.0)
    return (
        w["margin"] * margin_norm
        + w["affinity"] * affinity_score(p, profile)
        + w["bucket"] * nudge
        + w["quality"] * quality_fit(p, profile)
        + w["budget"] * budget_fit
        + novelty_bias(p, profile)
        + 0.12 * thc_band_fit(p, profile)   # in their usual potency band
        + recency
    )


def rank(products, profile=None):
    """Return products sorted best-first for this customer, each annotated with
    `score` and `why`. Margin-first when profile is None."""
    products = list(products)
    if not products:
        return []
    w = W_KNOWN if profile else W_ANON
    margins = [_f(p.get("margin_pct")) for p in products]
    m_lo, m_hi = min(margins), max(margins)
    prices = [_f(p.get("price")) for p in products if _f(p.get("price")) > 0]
    mid = (min(prices) + min(max(prices), 200)) / 2 if prices else 0.0
    recent_brands, recent_cats = _recent(profile)
    price_sensitive = bool(profile and profile.get("price_tier") == "value")
    out = []
    for p in products:
        p = dict(p)
        p["score"] = score_product(p, profile, w, m_lo, m_hi, mid,
                                    recent_brands, recent_cats, price_sensitive)
        p["why"] = why(p, profile) if profile else ""
        out.append(p)
    out.sort(key=lambda x: x["score"], reverse=True)
    return out


def why(p, profile):
    """Short persuasive reason from real signals (no receipts, no fake claims)."""
    bits = []
    brand = p.get("brand")
    st = (p.get("strain_type") or "")
    if profile and brand and _aff(profile, "brand_affinity", brand) >= 0.25:
        bits.append(f"your go-to {brand}")
    elif profile and st and _aff(profile, "strain_type_affinity", st) >= 0.4:
        bits.append(f"right in your {st.lower()} lane")
    elif profile and p.get("subcategory") and _aff(profile, "subcategory_affinity", p["subcategory"]) >= 0.4:
        bits.append(f"your usual {p['subcategory']}")
    elif profile and profile.get("price_tier") and quality_fit(p, profile) >= 0.7:
        bits.append("exactly your usual quality")
    pw, pr = _f(p.get("price_was")), _f(p.get("price"))
    if pw - pr >= 1:
        bits.append(f"on sale — save ${pw - pr:.0f}")
    if _f(p.get("thc")) >= 25:
        bits.append(f"hits hard at {_f(p.get('thc')):.0f}% THC")
    if 0 < _f(p.get("qty")) <= 5:
        bits.append("almost gone")
    if len(bits) < 2 and p.get("terpene"):
        bits.append(f"{p['terpene'].lower()}-forward")
    elif len(bits) < 2 and p.get("strain"):
        bits.append(p["strain"])
    picked = [b for b in bits if b][:2]
    if not picked:
        return f"a standout {brand} pick" if brand else "a standout pick"
    s = " · ".join(picked)
    return s[0].upper() + s[1:]
