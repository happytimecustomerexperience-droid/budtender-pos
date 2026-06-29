"""Customer-360 — reads the happytime-budtender CustomerProfile READ-ONLY.

# ponytail: the sister app `happytime-budtender` already builds rich per-customer
# taste profiles (keyed by phone) in its Postgres table `budtender_customerprofile`.
# We connect read-only via CUSTOMER_DB_DSN and read that row by phone. No writes,
# no changes to that app. Absent/unreachable DSN -> "history unavailable" (degrade).

Profile fields used: total_orders, last_purchase_at, price_tier, novelty_score,
brand_affinity/category_affinity (JSON {name: weight}), purchase_history
(JSON [{sku, brand, category, strain_type, qty, times_bought, last_bought_at}]).
The join key is PHONE (E.164) — we normalize the Dutchie phone to candidates.
"""

import logging
import os
import re

from django.core.cache import cache

logger = logging.getLogger(__name__)

_MISS = object()
_PROFILE_TTL = 300  # the menu re-renders per filter change; cache the taste profile


def _phone_candidates(phone: str) -> list[str]:
    """Normalize a Dutchie phone to the formats happytime might store."""
    digits = re.sub(r"\D", "", phone or "")
    if not digits:
        return []
    out = {phone.strip()}
    if len(digits) == 10:
        out.update({f"+1{digits}", f"1{digits}", digits})
    elif len(digits) == 11 and digits.startswith("1"):
        out.update({f"+{digits}", digits, digits[1:]})
    else:
        out.update({digits, f"+{digits}"})
    return [p for p in out if p]


def _top(affinity: dict, n: int = 5) -> list[str]:
    if not isinstance(affinity, dict):
        return []
    return [k for k, _ in sorted(affinity.items(), key=lambda kv: kv[1] or 0, reverse=True)[:n]]


def load_customer_history(acct_id=None, phone=None, name=None):
    """Rich profile from happytime-budtender by phone, or None if unavailable.

    Shape:
        {source:"happytime", orders, last_purchase, price_tier, novelty,
         top_categories:[str], top_brands:[str], recent:[{product,brand,times}],
         matched_by:"phone"}
    """
    dsn = os.environ.get("CUSTOMER_DB_DSN", "").strip()
    cands = _phone_candidates(phone or "")
    if not dsn or not cands:
        return None
    try:
        import psycopg
    except Exception:
        logger.debug("psycopg not installed")
        return None
    try:
        with psycopg.connect(dsn, connect_timeout=4, autocommit=True) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT total_orders, last_purchase_at, price_tier, novelty_score,
                           brand_affinity, category_affinity, purchase_history
                    FROM budtender_customerprofile
                    WHERE phone = ANY(%s)
                    LIMIT 1
                    """,
                    (cands,),
                )
                row = cur.fetchone()
        if not row:
            return None
        total_orders, last_purchase, price_tier, novelty, brand_aff, cat_aff, hist = row
        recent = []
        if isinstance(hist, list):
            hist_sorted = sorted(
                hist, key=lambda h: (h or {}).get("last_bought_at") or "", reverse=True
            )
            for h in hist_sorted[:10]:
                if isinstance(h, dict):
                    recent.append({
                        "product": h.get("sku") or h.get("product") or h.get("brand") or "—",
                        "brand": h.get("brand") or "",
                        "times": h.get("times_bought") or h.get("qty") or "",
                    })
        return {
            "source": "happytime",
            "orders": int(total_orders or 0),
            "last_purchase": str(last_purchase)[:10] if last_purchase else None,
            "price_tier": price_tier or "",
            "novelty": round(float(novelty), 2) if novelty is not None else None,
            "top_categories": _top(cat_aff),
            "top_brands": _top(brand_aff),
            "recent": recent,
            "matched_by": "phone",
        }
    except Exception:
        logger.debug("load_customer_history (happytime) failed", exc_info=True)
        return None


# store key -> happytime-budtender location_slug
_HHT_LOC = {"yakima": "yakima", "pullman": "pullman", "mtvernon": "mount-vernon"}


def _connect():
    dsn = os.environ.get("CUSTOMER_DB_DSN", "").strip()
    if not dsn:
        return None
    try:
        import psycopg
        return psycopg.connect(dsn, connect_timeout=4, autocommit=True)
    except Exception:
        logger.debug("CUSTOMER_DB connect failed", exc_info=True)
        return None


def load_profile_full(phone):
    """Raw affinity profile (for ranking + suggestions), or None. Keys match
    ranking.py / suggest.py expectations."""
    cands = _phone_candidates(phone or "")
    if not cands:
        return None
    conn = _connect()
    if not conn:
        return None
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT total_orders, last_purchase_at, price_tier, novelty_score,
                       brand_affinity, category_affinity, strain_type_affinity,
                       subcategory_affinity, terpene_affinity, bucket_mix, purchase_history
                FROM budtender_customerprofile WHERE phone = ANY(%s) LIMIT 1
                """,
                (cands,),
            )
            row = cur.fetchone()
        if not row:
            return None
        return {
            "orders": int(row[0] or 0),
            "last_purchase": str(row[1])[:10] if row[1] else None,
            "price_tier": row[2] or "",
            "novelty_score": row[3],
            "brand_affinity": row[4] or {},
            "category_affinity": row[5] or {},
            "strain_type_affinity": row[6] or {},
            "subcategory_affinity": row[7] or {},
            "terpene_affinity": row[8] or {},
            "bucket_mix": row[9] or {},
            "purchase_history": row[10] or [],
        }
    except Exception:
        logger.debug("load_profile_full failed", exc_info=True)
        return None


def load_profile_full_cached(phone, ttl=_PROFILE_TTL):
    """Cached `load_profile_full`. The personalized menu re-renders on EVERY filter change,
    so without this each keystroke makes a happytime Postgres round-trip. Negative results
    (None) are cached too, so a slow/down DB isn't hammered. Cache failures fall through to
    a live read — never breaks the page."""
    if not phone:
        return None
    key = f"prof:{re.sub(r'[^0-9+]', '', phone)}"
    try:
        hit = cache.get(key, _MISS)
        if hit is not _MISS:
            return hit
        # Stampede guard (mirrors catalog.get_inventory): only ONE worker per phone makes
        # the DB round-trip; concurrent losers degrade to None instead of each stacking a
        # 4s connect — a slow/down happytime DB can't exhaust gunicorn workers.
        if not cache.add(f"{key}:lock", "1", 10):
            return None
    except Exception:
        return load_profile_full(phone)
    try:
        val = load_profile_full(phone)
        cache.set(key, val, ttl)
        return val
    finally:
        try:
            cache.delete(f"{key}:lock")
        except Exception:
            pass


def load_product_enrichment(store_key):
    """{str(product_id) | sku: {strain_type, terpene, effects, bucket, velocity,
    margin_pct, price_z, subcategory, image, price_was}} from happytime's
    budtender_product for this store. {} when unavailable."""
    conn = _connect()
    if not conn:
        return {}
    slug = _HHT_LOC.get(store_key, store_key)
    out = {}
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT product_id, sku, strain_type, dominant_terpene, effects, bucket,
                       velocity, margin_pct, price_z, subcategory, image_url, price_was,
                       flavors, potency_mg, thc_percent
                FROM budtender_product WHERE location_slug = %s
                """,
                (slug,),
            )
            for r in cur.fetchall():
                rec = {
                    "strain_type": r[2] or "", "terpene": r[3] or "",
                    "effects": r[4] or [], "bucket": r[5] or "",
                    "velocity": r[6], "margin_pct": r[7], "price_z": r[8],
                    "subcategory": r[9] or "", "image_url": r[10] or "",
                    "price_was": r[11], "flavors": r[12] or [],
                    "potency_mg": r[13], "thc_percent": r[14],
                }
                if r[0] is not None:
                    out[str(r[0])] = rec
                if r[1]:
                    out.setdefault(str(r[1]), rec)
        return out
    except Exception:
        logger.debug("load_product_enrichment failed", exc_info=True)
        return {}
