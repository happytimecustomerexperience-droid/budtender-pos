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

logger = logging.getLogger(__name__)


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
